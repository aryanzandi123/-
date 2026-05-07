#!/usr/bin/env python3
"""
Step 5: Discover Siblings (Parallel Version)
=============================================
Goal: Populate the tree with related pathways (siblings) to build a complete biological taxonomy.

OPTIMIZED: All parent pathways processed in parallel (they are independent).
Expected speedup: ~15 min -> ~30-45 seconds
"""

import sys
import time
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from scripts.pathway_v2.llm_utils import _call_gemini_json_cached, _call_gemini_json_batch
from scripts.pathway_v2.async_utils import run_parallel, MAX_CONCURRENT_FLASH
from scripts.pathway_v2.cache import get_pathway_cache, save_cache
from scripts.pathway_v2.step6_utils import would_create_cycle, build_parent_graph

SIBLING_PROMPT = """You are a biological taxonomy expert.
Task: Identify the SIBLING pathways of "{child_name}" that also fall under the parent category "{parent_name}".

## CONTEXT
Parent: {parent_name}
Child: {child_name}

## INSTRUCTIONS
1. List significant biological pathways that are "siblings" (other types/subprocesses of the parent).
2. Use standard terminology.
3. Limit to top 5-7 most relevant siblings.

## RESPONSE FORMAT (Strict JSON)
{{
  "siblings": [
     {{ "name": "Sibling Name", "description": "Brief desc" }}
  ]
}}
Respond with ONLY the JSON.
"""


def _discover_siblings_for_parent(parent_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Discover siblings for a single parent. Called in parallel.

    Args:
        parent_data: Dict with 'parent_id', 'parent_name', 'sample_child_name'

    Returns:
        Dict with 'parent_id', 'siblings', 'error'
    """
    parent_id = parent_data['parent_id']
    parent_name = parent_data['parent_name']
    sample_child = parent_data.get('sample_child_name', 'a child pathway')

    try:
        resp = _call_gemini_json_cached(
            SIBLING_PROMPT.format(child_name=sample_child, parent_name=parent_name),
            cache_key=parent_name,
            cache_type="siblings",
            
        )

        siblings = resp.get('siblings', [])
        return {
            'parent_id': parent_id,
            'parent_name': parent_name,
            'siblings': siblings,
            'error': None,
            'cached': resp.get('_cached', False)
        }
    except Exception as e:
        logger.error(f"Error discovering siblings for {parent_name}: {e}")
        return {
            'parent_id': parent_id,
            'parent_name': parent_name,
            'siblings': [],
            'error': str(e),
            'cached': False
        }


def _discover_siblings_via_batch_api(parent_data_list):
    """Discover siblings for all parents via a single Batch API job."""
    import time as _t
    cache = get_pathway_cache()

    prompts = []
    uncached = []
    cached_results = []

    for pd in parent_data_list:
        hit = cache.get_siblings(pd['parent_name'])
        if hit:
            cached_results.append({
                'parent_id': pd['parent_id'],
                'parent_name': pd['parent_name'],
                'siblings': hit,
                'error': None,
                'cached': True,
            })
        else:
            uncached.append(pd)
            prompts.append(SIBLING_PROMPT.format(
                child_name=pd.get('sample_child_name', 'a child pathway'),
                parent_name=pd['parent_name'],
            ))

    if not prompts:
        return cached_results

    logger.info(f"  Batch API: {len(prompts)} sibling lookups ({len(cached_results)} cache hits)")
    raw = _call_gemini_json_batch(
        prompts,
        display_name=f"step5-siblings-{int(_t.time())}",
    )

    batch_results = []
    for idx, parsed in enumerate(raw):
        pd = uncached[idx]
        siblings = parsed.get('siblings', []) if isinstance(parsed, dict) else []
        if siblings:
            cache.set_siblings(pd['parent_name'], siblings)
        batch_results.append({
            'parent_id': pd['parent_id'],
            'parent_name': pd['parent_name'],
            'siblings': siblings,
            'error': None if siblings else 'empty response',
            'cached': False,
        })

    save_cache()
    return cached_results + batch_results


def discover_siblings(mode: str = "standard"):
    """Discover sibling pathways for ALL parent pathways."""
    try:
        from app import app, db
        from models import Pathway, PathwayParent
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    with app.app_context():
        # Get ALL unique parent pathways that have at least one child
        parent_ids = db.session.query(PathwayParent.parent_pathway_id).distinct().all()
        parent_ids = [pid[0] for pid in parent_ids]

        logger.info(f"Found {len(parent_ids)} parent pathways to check for sibling completeness.")

        # Build list of parent data for parallel processing
        parent_data_list = []
        for parent_id in parent_ids:
            parent = db.session.get(Pathway, parent_id)
            if not parent:
                continue

            # Get existing children names for this parent
            existing_links = PathwayParent.query.filter_by(parent_pathway_id=parent_id).all()
            existing_child_names = {link.child.name for link in existing_links if link.child and link.child.name}

            if not existing_child_names:
                continue

            # Use first child as sample for context
            sample_child_name = list(existing_child_names)[0]

            parent_data_list.append({
                'parent_id': parent_id,
                'parent_name': parent.name,
                'sample_child_name': sample_child_name,
                'existing_children': existing_child_names
            })

        if not parent_data_list:
            logger.info("No parents to process.")
            return

        logger.info(f"Processing {len(parent_data_list)} parents...")

        # Run all sibling discovery — batch API or async parallel
        if mode == "batch":
            results = _discover_siblings_via_batch_api(parent_data_list)
        else:
            results = run_parallel(
                parent_data_list,
                _discover_siblings_for_parent,
                max_concurrent=MAX_CONCURRENT_FLASH,
                desc="Sibling discovery"
            )

        # Rate limiting between batch submission and result processing (matches step2/step3 pattern)
        time.sleep(1)

        # Process results and create pathways
        total_siblings_added = 0
        cache_hits = 0

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Failed for parent: {result}")
                continue

            parent_id = result['parent_id']
            parent_name = result['parent_name']
            siblings = result.get('siblings', [])

            if result.get('cached'):
                cache_hits += 1

            if result.get('error'):
                continue

            # Get existing children for this parent (need to re-query to get current state)
            existing_links = PathwayParent.query.filter_by(parent_pathway_id=parent_id).all()
            existing_child_names = {link.child.name for link in existing_links if link.child}

            count = 0
            for sib in siblings:
                name = sib.get('name')
                if not name or name in existing_child_names:
                    continue

                # Create pathway if doesn't exist
                existing_pw = Pathway.query.filter_by(name=name).first()
                if not existing_pw:
                    parent = db.session.get(Pathway, parent_id)
                    existing_pw = Pathway(
                        name=name,
                        description=sib.get('description'),
                        hierarchy_level=parent.hierarchy_level + 1 if parent else 1,
                        is_leaf=True,
                        ai_generated=True
                    )
                    db.session.add(existing_pw)
                    db.session.commit()

                # Create parent link if doesn't exist
                if not PathwayParent.query.filter_by(
                    child_pathway_id=existing_pw.id,
                    parent_pathway_id=parent_id
                ).first():
                    # Check for cycle before creating
                    parent_graph = build_parent_graph(PathwayParent)
                    if would_create_cycle(existing_pw.id, parent_id, parent_graph):
                        logger.warning(f"Skipping sibling link '{name}' -> parent_id={parent_id}: would create cycle")
                        continue
                    link = PathwayParent(
                        child_pathway_id=existing_pw.id,
                        parent_pathway_id=parent_id,
                        relationship_type='is_a'
                    )
                    db.session.add(link)
                    count += 1

            db.session.commit()

            if count > 0:
                logger.info(f"  Added {count} siblings under '{parent_name}'")
                total_siblings_added += count

        # Save cache at end
        save_cache()

        logger.info(f"\n{'='*60}")
        logger.info(f"Step 5 Complete (Parallel):")
        logger.info(f"  Parents processed: {len(parent_data_list)}")
        logger.info(f"  Cache hits: {cache_hits}")
        logger.info(f"  Total siblings added: {total_siblings_added}")
        logger.info(f"{'='*60}\n")


if __name__ == "__main__":
    discover_siblings()
