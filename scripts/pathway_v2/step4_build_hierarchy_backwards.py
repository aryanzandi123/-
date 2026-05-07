#!/usr/bin/env python3
"""
Step 4: Build Hierarchy Backwards (Parallel Version)
=====================================================
Goal: For every NEW finalized pathway, build the hierarchy chain backwards until a Root or Existing Pathway is reached.

OPTIMIZED: Two-phase parallel climbing
- Phase A: All leaf pathways ask for parent SIMULTANEOUSLY
- Phase B: Dedupe, then process next level in parallel
- Repeat until all chains reach roots

Expected speedup: ~25 min -> ~2-3 minutes
"""

import sys
import time
import logging
from pathlib import Path
from typing import List, Dict, Set, Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from scripts.pathway_v2.llm_utils import _call_gemini_json_cached, _call_gemini_json_batch
from scripts.pathway_v2.async_utils import run_parallel, MAX_CONCURRENT_FLASH
from scripts.pathway_v2.cache import get_pathway_cache, save_cache
from scripts.pathway_v2.step1_init_roots import ROOT_PATHWAYS
from scripts.pathway_v2.step6_utils import get_smart_rescue_parent, verify_chain_reaches_root, STRICT_ROOTS, would_create_cycle, build_parent_graph

ROOT_NAMES = {r['name'] for r in ROOT_PATHWAYS}

PARENT_PROMPT = """You are a biological taxonomy expert building a detailed pathway hierarchy.
Task: Identify the IMMEDIATE biological parent pathway for: "{child_name}".

## CONTEXT
We are building a DEEP hierarchy tree with multiple levels between specific pathways and roots.
The ROOTS are: {roots_list}

## CRITICAL RULES
1. **DO NOT** jump directly to a Root unless "{child_name}" is truly a top-level biological category.
2. Most pathways should have 3-6 levels between them and a Root.
3. The parent must be ONE LEVEL more general than the child - not multiple levels.

## EXAMPLES OF CORRECT HIERARCHIES
- "p53 Signaling" -> "Apoptosis Signaling" (NOT directly to "Cell Death" or "Cellular Signaling")
- "Aggrephagy" -> "Selective Macroautophagy" -> "Macroautophagy" -> "Autophagy" -> "Protein Quality Control"
- "mTOR Signaling" -> "Nutrient Sensing" -> "Growth Factor Signaling" -> "Cellular Signaling"
- "HDAC6-mediated Deacetylation" -> "Histone Deacetylation" -> "Epigenetic Regulation" -> "Gene Expression" -> "Cellular Signaling"

## EXAMPLES OF WRONG (TOO SHALLOW) HIERARCHIES
- "p53 Signaling" -> "Cellular Signaling" (WRONG - skips intermediate levels)
- "Aggrephagy" -> "Protein Quality Control" (WRONG - skips 4 intermediate levels)
- "HDAC6 Activity" -> "Metabolism" (WRONG - too vague, skips specificity)

## INSTRUCTIONS
1. Name the IMMEDIATE parent (exactly one level broader).
2. The parent must PERFECTLY ENCAPSULATE the child but be only SLIGHTLY more general.
3. Think: "What TYPE of thing is {child_name}?" The answer is the parent.
4. Only return a Root if {child_name} is genuinely a level-1 category (like "Apoptosis" under "Cell Death").

## RESPONSE FORMAT (Strict JSON)
{{
  "child": "{child_name}",
  "parent": "Name of Immediate Parent Pathway",
  "reasoning": "Explain why this is the direct parent, not a more distant ancestor"
}}
Respond with ONLY the JSON.
"""


def _get_parent_for_pathway(pathway_name: str) -> Dict[str, Any]:
    """
    Get parent for a single pathway. Called in parallel.
    """
    roots_str = ", ".join(ROOT_NAMES)

    try:
        resp = _call_gemini_json_cached(
            PARENT_PROMPT.format(child_name=pathway_name, roots_list=roots_str),
            cache_key=pathway_name,
            cache_type="parent",
            
        )

        parent_name = resp.get('parent')

        return {
            'child': pathway_name,
            'parent': parent_name,
            'reasoning': resp.get('reasoning', ''),
            'error': None,
            'cached': resp.get('_cached', False)
        }
    except Exception as e:
        logger.error(f"Error getting parent for {pathway_name}: {e}")
        return {
            'child': pathway_name,
            'parent': None,
            'reasoning': '',
            'error': str(e),
            'cached': False
        }


def _get_parents_via_batch_api(pathway_names: List[str]) -> List[Dict[str, Any]]:
    """Get parents for multiple pathways via a single Batch API job."""
    import time as _t
    cache = get_pathway_cache()
    roots_str = ", ".join(ROOT_NAMES)

    prompts = []
    uncached_names = []
    cached_results = []

    for name in pathway_names:
        hit = cache.get_parent(name)
        if hit:
            cached_results.append({
                'child': name, 'parent': hit, 'reasoning': '',
                'error': None, 'cached': True,
            })
        else:
            uncached_names.append(name)
            prompts.append(PARENT_PROMPT.format(child_name=name, roots_list=roots_str))

    if not prompts:
        return cached_results

    logger.info(f"  Batch API: {len(prompts)} parent lookups ({len(cached_results)} cache hits)")
    raw = _call_gemini_json_batch(
        prompts,
        display_name=f"step4-parents-{int(_t.time())}",
    )

    batch_results = []
    for idx, parsed in enumerate(raw):
        name = uncached_names[idx]
        parent = parsed.get('parent') if isinstance(parsed, dict) else None
        if parent:
            cache.set_parent(name, parent)
        batch_results.append({
            'child': name,
            'parent': parent,
            'reasoning': parsed.get('reasoning', '') if isinstance(parsed, dict) else '',
            'error': None if parent else 'empty response',
            'cached': False,
        })

    save_cache()
    return cached_results + batch_results


def _run_recovery_for_missing_steps(interaction_ids: List[int] = None) -> None:
    """Ensure all interactions have step2 and step3 data."""
    try:
        from app import app, db
        from models import Interaction
    except ImportError:
        return

    with app.app_context():
        # Check for missing step2
        query2 = Interaction.query.filter(~Interaction.data.has_key('step2_proposal'))
        if interaction_ids:
            query2 = query2.filter(Interaction.id.in_(interaction_ids))
        missing2 = query2.all()

        if missing2:
            logger.warning(f"Recovery: {len(missing2)} missing step2_proposal")
            from scripts.pathway_v2.step2_assign_initial_terms import assign_initial_terms_for_interactions
            assign_initial_terms_for_interactions(missing2)

        # Check for missing step3
        query3 = Interaction.query.filter(
            Interaction.data.has_key('step2_proposal'),
            ~Interaction.data.has_key('step3_finalized_pathway')
        )
        if interaction_ids:
            query3 = query3.filter(Interaction.id.in_(interaction_ids))
        missing3 = query3.all()

        if missing3:
            logger.warning(f"Recovery: {len(missing3)} missing step3_finalized_pathway")
            from scripts.pathway_v2.step3_refine_pathways import refine_pathways_for_interactions
            refine_pathways_for_interactions(missing3)


def build_hierarchy(interaction_ids: List[int] = None, mode: str = "standard") -> None:
    """
    Build pathway hierarchy using two-phase parallel climbing.

    Args:
        interaction_ids: Optional list of interaction IDs to process.
                        If None, processes all interactions.
        mode: "standard" (async parallel LLM) or "batch" (Batch API, 50% savings).
    """
    try:
        from app import app, db
        from models import Interaction, Pathway, PathwayParent, PathwayInteraction
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    # First run recovery for any missing steps
    _run_recovery_for_missing_steps(interaction_ids)

    with app.app_context():
        # Get interactions with finalized pathways
        query = Interaction.query.filter(
            Interaction.data.has_key('step3_finalized_pathway') |
            Interaction.data.has_key('step3_function_pathways')
        )
        if interaction_ids:
            query = query.filter(Interaction.id.in_(interaction_ids))
            logger.info(f"Filtering to {len(interaction_ids)} interactions from query filter")

        interactions = query.all()

        # Build term -> interactions map
        term_to_interactions: Dict[str, List] = {}
        interaction_to_terms: Dict[int, Set[str]] = {}

        for i in interactions:
            interaction_terms = set()

            # Method 1: Function-level pathways (preferred)
            func_pathways = i.data.get('step3_function_pathways', []) if i.data else []
            for fp in func_pathways:
                pw = fp.get('finalized_pathway')
                if pw:
                    interaction_terms.add(pw)

            # Method 2: Function's own pathway field
            for func in (i.data.get('functions', []) if i.data else []):
                pw = func.get('pathway')
                if isinstance(pw, str) and pw:
                    interaction_terms.add(pw)
                elif isinstance(pw, dict) and pw.get('name'):
                    interaction_terms.add(pw['name'])

            # Method 3: Fallback to interaction-level
            if not interaction_terms and i.data:
                primary = i.data.get('step3_finalized_pathway')
                if primary:
                    interaction_terms.add(primary)

            # Add interaction to each of its pathways
            for term in interaction_terms:
                if term not in term_to_interactions:
                    term_to_interactions[term] = []
                term_to_interactions[term].append(i)

            interaction_to_terms[i.id] = interaction_terms

        unique_terms = list(term_to_interactions.keys())
        logger.info(f"Processing hierarchy for {len(unique_terms)} unique terms from {len(interactions)} interactions.")

        # ------------------------------------------------------------------
        # Step 1: Create leaf pathways and PathwayInteraction links
        # ------------------------------------------------------------------
        total_pathway_interactions_created = 0

        for leaf_name in unique_terms:
            # Create Leaf Pathway if missing
            curr_pw = Pathway.query.filter_by(name=leaf_name).first()
            if not curr_pw:
                curr_pw = Pathway(
                    name=leaf_name,
                    hierarchy_level=10,  # Placeholder level
                    is_leaf=True,
                    ai_generated=True
                )
                db.session.add(curr_pw)
                db.session.flush()  # Get ID without full commit

            # Assign Interactions to this pathway
            interactions_for_term = term_to_interactions[leaf_name]
            for ix in interactions_for_term:
                existing_link = PathwayInteraction.query.filter_by(
                    pathway_id=curr_pw.id,
                    interaction_id=ix.id
                ).first()
                if not existing_link:
                    pi = PathwayInteraction(
                        pathway_id=curr_pw.id,
                        interaction_id=ix.id,
                        assignment_method='AI_V2_Step4'
                    )
                    db.session.add(pi)
                    total_pathway_interactions_created += 1

        # Commit all leaf pathways and their interaction links in one batch
        db.session.commit()

        logger.info(f"Created {total_pathway_interactions_created} PathwayInteraction links")

        # ------------------------------------------------------------------
        # Step 2: Two-phase parallel hierarchy climbing
        # ------------------------------------------------------------------

        # Find pathways that need parents (not roots, don't have parent yet)
        def get_pathways_needing_parents() -> List[str]:
            """Get pathway names that need parent assignment."""
            needs_parent = []
            for term in unique_terms:
                if term in ROOT_NAMES:
                    continue
                pw = Pathway.query.filter_by(name=term).first()
                if not pw:
                    continue
                existing_parent = PathwayParent.query.filter_by(child_pathway_id=pw.id).first()
                if not existing_parent:
                    needs_parent.append(term)
            return needs_parent

        pending = get_pathways_needing_parents()
        all_processed = set()
        level = 1
        total_cache_hits = 0

        # Build parent graph once; update incrementally inside the loop
        parent_graph = build_parent_graph(PathwayParent)
        all_touched_pw_ids = set()

        while pending:
            logger.info(f"\n--- Hierarchy Level {level}: {len(pending)} pathways ---")

            # Process all pending pathways — batch API or async parallel
            if mode == "batch":
                results = _get_parents_via_batch_api(pending)
            else:
                results = run_parallel(
                    pending,
                    _get_parent_for_pathway,
                    max_concurrent=MAX_CONCURRENT_FLASH,
                    desc=f"Level {level} parent lookup"
                )

            # Process results
            next_level_candidates = set()

            for result in results:
                if isinstance(result, Exception):
                    continue

                child_name = result['child']
                parent_name = result.get('parent')

                if result.get('cached'):
                    total_cache_hits += 1

                if not parent_name or parent_name == child_name:
                    logger.warning(f"  Invalid parent '{parent_name}' for '{child_name}'")
                    continue

                logger.info(f"  '{child_name}' -> '{parent_name}'")

                # Get or create child pathway
                child_pw = Pathway.query.filter_by(name=child_name).first()
                if not child_pw:
                    continue

                # Check if already has parent
                existing_parent = PathwayParent.query.filter_by(child_pathway_id=child_pw.id).first()
                if existing_parent:
                    continue

                # Get or create parent pathway
                parent_pw = Pathway.query.filter_by(name=parent_name).first()
                if not parent_pw:
                    child_level = child_pw.hierarchy_level if child_pw.hierarchy_level is not None else 10
                    parent_pw = Pathway(
                        name=parent_name,
                        hierarchy_level=0 if parent_name in ROOT_NAMES else max(0, child_level - 1),
                        is_leaf=False,
                        ai_generated=True
                    )
                    db.session.add(parent_pw)
                    db.session.flush()  # Get ID without full commit

                # Prevent cycles - check with in-memory graph (no DB round-trip)
                if would_create_cycle(child_pw.id, parent_pw.id, parent_graph):
                    logger.warning(f"  Skipping '{child_name}' -> '{parent_name}': would create cycle")
                    continue

                # Create parent-child link
                link = PathwayParent(
                    child_pathway_id=child_pw.id,
                    parent_pathway_id=parent_pw.id,
                    relationship_type='is_a'
                )
                db.session.add(link)

                # Update in-memory graph incrementally
                parent_graph.setdefault(child_pw.id, []).append(parent_pw.id)
                all_touched_pw_ids.add(child_pw.id)
                all_touched_pw_ids.add(parent_pw.id)

                all_processed.add(child_name)

                # Add parent to next level if not a root and not already processed
                if parent_name not in ROOT_NAMES and parent_name not in all_processed:
                    # Check if parent needs a parent
                    parent_has_parent = PathwayParent.query.filter_by(child_pathway_id=parent_pw.id).first()
                    if not parent_has_parent:
                        next_level_candidates.add(parent_name)

            # Commit all additions for this level in one batch
            db.session.commit()

            # Rate limiting between batch submissions (matches step2/step3 pattern)
            time.sleep(1)

            # Prepare next level
            pending = list(next_level_candidates)
            level += 1

            # Safety limit
            if level > 10:
                logger.warning("Reached maximum hierarchy depth, stopping")
                break

        # ------------------------------------------------------------------
        # Step 3: Chain verification and smart rescue for broken chains
        # ------------------------------------------------------------------

        logger.info("\n--- Verifying hierarchy chains reach roots ---")
        broken_count = 0
        fixed_count = 0

        # Only verify pathways touched in this run (leaf terms + newly created parents)
        verify_names = set(unique_terms) | all_processed
        if all_touched_pw_ids:
            touched_pws = Pathway.query.filter(Pathway.id.in_(all_touched_pw_ids)).all()
            verify_names.update(pw.name for pw in touched_pws)
        pathways_to_verify = Pathway.query.filter(Pathway.name.in_(verify_names)).all() if verify_names else []

        for pw in pathways_to_verify:
            if pw.name in ROOT_NAMES or pw.name in STRICT_ROOTS:
                continue

            if not verify_chain_reaches_root(pw.id, PathwayParent, Pathway):
                broken_count += 1
                # Use smart rescue to attach to correct root
                smart_parent = get_smart_rescue_parent(pw.name, Pathway)
                if smart_parent:
                    # Check if already has parent link
                    existing = PathwayParent.query.filter_by(child_pathway_id=pw.id).first()
                    if existing:
                        existing.parent_pathway_id = smart_parent.id
                    else:
                        link = PathwayParent(
                            child_pathway_id=pw.id,
                            parent_pathway_id=smart_parent.id,
                            relationship_type='is_a'
                        )
                        db.session.add(link)
                    logger.info(f"  Fixed broken chain: '{pw.name}' -> '{smart_parent.name}'")
                    fixed_count += 1

        if broken_count > 0:
            db.session.commit()
            logger.info(f"  Found {broken_count} broken chains, fixed {fixed_count}")
        else:
            logger.info("  All chains reach roots!")

        # ------------------------------------------------------------------
        # Step 4: Final verification and fallback for interactions
        # ------------------------------------------------------------------

        unlinked = []
        for i in interactions:
            has_any_link = PathwayInteraction.query.filter_by(interaction_id=i.id).first()
            if not has_any_link:
                unlinked.append(i)

        if unlinked:
            logger.warning(f"Found {len(unlinked)} interactions without PathwayInteraction records. Creating smart fallback links...")
            for ix in unlinked:
                # Try to get pathway from interaction data for smart rescue
                pw_name = None
                if ix.data:
                    pw_name = ix.data.get('step3_finalized_pathway') or ix.data.get('step2_proposal')

                if pw_name:
                    smart_parent = get_smart_rescue_parent(pw_name, Pathway)
                else:
                    smart_parent = Pathway.query.filter_by(name='Protein Quality Control').first()

                if smart_parent:
                    pi = PathwayInteraction(
                        pathway_id=smart_parent.id,
                        interaction_id=ix.id,
                        assignment_method='SmartFallback_Step4'
                    )
                    db.session.add(pi)

            db.session.commit()
            logger.info(f"  Created {len(unlinked)} smart fallback PathwayInteraction links.")

        # Save cache
        save_cache()

        # Final report
        logger.info(f"\n{'='*60}")
        logger.info(f"Step 4 Complete (Parallel):")
        logger.info(f"  Unique pathway terms: {len(unique_terms)}")
        logger.info(f"  PathwayInteraction records created: {total_pathway_interactions_created}")
        logger.info(f"  Hierarchy levels processed: {level - 1}")
        logger.info(f"  Cache hits: {total_cache_hits}")
        logger.info(f"  Interactions processed: {len(interactions)}")
        logger.info(f"  Broken chains fixed: {fixed_count}")
        logger.info(f"{'='*60}\n")


if __name__ == "__main__":
    build_hierarchy()
