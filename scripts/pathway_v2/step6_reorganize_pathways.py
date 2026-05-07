#!/usr/bin/env python3
"""
Step 6: Reorganize and Clean Pathways (V2 Complete Rewrite)
============================================================
Bulletproof pathway reorganization with validation at every step.

Phases:
1. Deduplication - Merge synonyms with adaptive batching (JSON-safe)
2. Tree Enforcement - Single parent per pathway (cycle-safe)
3. Hierarchy Repair - Fix shallow/broken chains
4. Interaction Sync - Ensure every interaction has valid pathway
5. Pruning - Remove truly orphaned pathways
6. Pre-Flight Validation - Sanity checks before declaring success

Usage:
    python3 scripts/pathway_v2/step6_reorganize_pathways.py [--dry-run] [--phase N]
"""

import sys
import logging
import time
import argparse
from pathlib import Path
from difflib import SequenceMatcher
from typing import List, Dict, Tuple, Optional, Set
from collections import defaultdict

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from scripts.pathway_v2.llm_utils import _call_gemini_json
from scripts.pathway_v2.step6_utils import (
    STRICT_ROOTS,
    LEGITIMATE_LEVEL1,
    ChangeType,
    Change,
    PhaseResult,
    MigrationPlan,
    build_parent_graph,
    build_child_graph,
    detect_cycle_from_node,
    would_create_cycle,
    find_all_cycles,
    detect_multi_parent_nodes,
    calculate_hierarchy_levels,
    find_orphan_pathways,
    validate_pathway_name_exists,
    validate_no_orphan_interactions,
    validate_no_dangling_pathway_links,
    AdaptiveBatcher,
    build_merge_migration_plan,
    execute_migration_plan,
    save_checkpoint,
    get_smart_rescue_parent,
    verify_chain_reaches_root,
    find_broken_chains,
)


# ==============================================================================
# PROMPTS
# ==============================================================================

MERGE_PROMPT = """You are a biological data curator.
Task: Decide if the following pairs of pathway names represent the SAME biological concept and should be merged.

## CANDIDATE PAIRS
{pairs_list}

## INSTRUCTIONS
1. For each pair, determine if they are synonyms or just minor variations.
2. If they are the SAME concept, choose the "canonical_name" (cleaner, shorter, standard version).
3. If they are DISTINCT concepts, set action to "KEEP_DISTINCT".

## RESPONSE FORMAT (Strict JSON)
{{
  "merges": [
    {{
      "name_a": "Name 1",
      "name_b": "Name 2",
      "action": "MERGE",
      "canonical_name": "The Better Name"
    }},
    {{
      "name_a": "Name 3",
      "name_b": "Name 4",
      "action": "KEEP_DISTINCT",
      "canonical_name": null
    }}
  ]
}}
Respond with ONLY the JSON."""


BEST_PARENT_PROMPT = """You are a biological taxonomy expert.
Task: Select the SINGLE BEST biological parent for "{child_name}" to enforce a strict tree.

## CURRENT PARENTS (Multiple - Must Pick ONE)
{parents_list}

## INSTRUCTIONS
1. Select the ONE parent that is the most direct and appropriate super-category.
2. Consider biological accuracy and pathway organization.
3. The goal is a strict tree (not DAG).

## RESPONSE FORMAT (Strict JSON)
{{
  "child": "{child_name}",
  "selected_parent": "Name of Best Parent",
  "reasoning": "Brief explanation"
}}
Respond with ONLY the JSON."""


FIND_PARENT_PROMPT = """You are a biological taxonomy expert.
Task: Find the appropriate parent pathway for "{child_name}".

## AVAILABLE ROOT CATEGORIES
{roots_list}

## EXISTING PATHWAYS (potential parents)
{existing_pathways}

## INSTRUCTIONS
1. Identify the IMMEDIATE biological parent for "{child_name}".
2. The parent should be ONE level more general.
3. If no good intermediate exists, pick the most relevant root.

## RESPONSE FORMAT (Strict JSON)
{{
  "child": "{child_name}",
  "parent": "Name of Parent Pathway",
  "reasoning": "Brief explanation"
}}
Respond with ONLY the JSON."""


SHALLOW_HIERARCHY_PROMPT = """You are a biological taxonomy expert.
Task: The pathway "{child_name}" is directly under root "{root_name}".
This may be too shallow. Most specific pathways need 2-5 intermediate levels.

## QUESTION
Should "{child_name}" have an intermediate parent between it and "{root_name}"?

## EXAMPLES OF CORRECT DEPTH
- "p53 Signaling" -> "Tumor Suppressor Signaling" -> "Cell Death Signaling" -> "Cellular Signaling"
- "HDAC6 Activity" -> "Histone Deacetylation" -> "Protein Deacetylation" -> "Protein Quality Control"

## EXAMPLES OF CORRECTLY SHALLOW (Keep at Level 1)
- "Apoptosis" directly under "Cell Death" (major category)
- "MAPK Signaling" directly under "Cellular Signaling" (major cascade)

## INSTRUCTIONS
1. If "{child_name}" is a MAJOR biological category, return action "KEEP".
2. If intermediate parent needed, return action "INSERT_INTERMEDIATE" with new_parent.

## RESPONSE FORMAT (Strict JSON)
{{
  "child": "{child_name}",
  "current_parent": "{root_name}",
  "action": "KEEP",
  "new_parent": null,
  "reasoning": "Brief explanation"
}}
OR
{{
  "child": "{child_name}",
  "current_parent": "{root_name}",
  "action": "INSERT_INTERMEDIATE",
  "new_parent": "Name of Intermediate Parent",
  "reasoning": "Brief explanation"
}}
Respond with ONLY the JSON."""


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def get_similarity(a: str, b: str) -> float:
    """Calculate string similarity ratio."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_duplicate_candidates(pathways: List) -> List[Tuple[str, str]]:
    """
    Find pathway name pairs that might be duplicates.

    Returns list of (name_a, name_b) tuples.
    """
    names = sorted([p.name for p in pathways])
    candidates = []
    used = set()

    for i in range(len(names)):
        if names[i] in used:
            continue
        for j in range(i + 1, len(names)):
            if names[j] in used:
                continue

            n1, n2 = names[i], names[j]
            sim = get_similarity(n1, n2)

            # Check containment (one name inside other with small difference)
            is_contained = (
                (n1.lower() in n2.lower() and len(n2) < len(n1) + 15) or
                (n2.lower() in n1.lower() and len(n1) < len(n2) + 15)
            )

            if sim > 0.85 or is_contained:
                candidates.append((n1, n2))
                used.add(n2)  # Don't pair n2 with others

    return candidates


def recalculate_all_levels(db, Pathway, PathwayParent) -> int:
    """
    Recalculate hierarchy_level for all pathways via BFS from roots.

    Returns count of pathways with updated levels.
    """
    from collections import deque

    logger.info("Recalculating hierarchy levels...")

    # Reset all to -1
    pathways = Pathway.query.all()
    for pw in pathways:
        pw.hierarchy_level = -1

    # Build child graph
    child_graph = build_child_graph(PathwayParent)

    # Initialize roots
    queue = deque()
    for pw in pathways:
        if pw.name in STRICT_ROOTS:
            pw.hierarchy_level = 0
            queue.append(pw.id)

    # Pre-load all pathways into dict for in-memory BFS (avoids N+1 queries)
    pw_by_id = {pw.id: pw for pw in pathways}

    # BFS — pure in-memory, no DB queries
    updated = 0
    while queue:
        current_id = queue.popleft()
        current = pw_by_id.get(current_id)
        if not current:
            continue

        for child_id in child_graph.get(current_id, []):
            child = pw_by_id.get(child_id)
            if child and child.hierarchy_level == -1:
                child.hierarchy_level = current.hierarchy_level + 1
                queue.append(child_id)
                updated += 1

    db.session.commit()
    logger.info(f"Updated levels for {updated} pathways")
    return updated


# ==============================================================================
# PHASE 1: DEDUPLICATION
# ==============================================================================

def phase1_deduplication(db, Pathway, PathwayParent, PathwayInteraction, dry_run: bool) -> PhaseResult:
    """
    Phase 1: Merge duplicate/synonym pathways.
    OPTIMIZED: Parallel LLM calls for merge decisions.
    """
    from scripts.pathway_v2.async_utils import run_parallel, MAX_CONCURRENT_FLASH

    result = PhaseResult(phase_name="Deduplication", success=True)
    logger.info("=" * 60)
    logger.info("PHASE 1: DEDUPLICATION (Parallel)")
    logger.info("=" * 60)

    # Find candidates
    pathways = Pathway.query.all()
    candidates = find_duplicate_candidates(pathways)

    if not candidates:
        logger.info("No duplicate candidates found.")
        return result

    logger.info(f"Found {len(candidates)} candidate duplicate pairs")

    # Group into batches of 3 pairs each
    from scripts.pathway_v2.async_utils import chunk_list
    batches = chunk_list(candidates, 3)

    def process_batch(batch):
        """Process a batch of candidate pairs."""
        pairs_str = "\n".join([f"- Pair: '{p[0]}' vs '{p[1]}'" for p in batch])
        try:
            resp = _call_gemini_json(
                MERGE_PROMPT.format(pairs_list=pairs_str),
                                max_output_tokens=12096
            )
            return {'batch': batch, 'response': resp, 'error': None}
        except Exception as e:
            return {'batch': batch, 'response': None, 'error': str(e)}

    # Process all batches in parallel
    batch_results = run_parallel(
        batches,
        process_batch,
        max_concurrent=MAX_CONCURRENT_FLASH,
        desc="Deduplication batches"
    )

    # Process results
    for batch_result in batch_results:
        if isinstance(batch_result, Exception):
            result.add_error(f"Batch failed: {batch_result}")
            continue

        if batch_result.get('error'):
            result.add_warning(f"Batch error: {batch_result['error']}")
            continue

        resp = batch_result.get('response', {})
        merges = resp.get('merges', [])

        for m in merges:
            if m.get('action') != 'MERGE':
                continue

            canon = m.get('canonical_name')
            name_a = m.get('name_a')
            name_b = m.get('name_b')

            # Validate names exist and are not whitespace-only
            if not canon or not name_a or not name_b:
                continue
            canon = canon.strip() if isinstance(canon, str) else ''
            name_a = name_a.strip() if isinstance(name_a, str) else ''
            name_b = name_b.strip() if isinstance(name_b, str) else ''
            if not canon or not name_a or not name_b:
                continue

            to_keep = canon
            to_drop = name_a if name_a != canon else name_b

            if dry_run:
                logger.info(f"[DRY RUN] MERGE: '{to_drop}' -> '{to_keep}'")
                result.add_change(Change(
                    change_type=ChangeType.MERGE,
                    entity_type='pathway',
                    entity_id=0,
                    old_value=to_drop,
                    new_value=to_keep,
                    reason="Duplicate names"
                ))
                continue

            # Get pathway records
            keep_pw = Pathway.query.filter_by(name=to_keep).first()
            drop_pw = Pathway.query.filter_by(name=to_drop).first()

            if not keep_pw or not drop_pw:
                result.add_warning(f"Could not find pathways for merge: {to_keep}, {to_drop}")
                continue

            # Build and execute migration plan
            plan = build_merge_migration_plan(
                drop_pw.id, keep_pw.id,
                PathwayParent, PathwayInteraction
            )

            success, errors = execute_migration_plan(
                plan, db, Pathway, PathwayParent, PathwayInteraction
            )

            if success:
                result.add_change(Change(
                    change_type=ChangeType.MERGE,
                    entity_type='pathway',
                    entity_id=drop_pw.id,
                    old_value=to_drop,
                    new_value=to_keep,
                    reason="Merged duplicate"
                ))
                logger.info(f"Merged '{to_drop}' into '{to_keep}'")
            else:
                result.add_error(f"Failed to merge {to_drop}: {errors}")

    # Verify no dangling links
    if not dry_run:
        dangling = validate_no_dangling_pathway_links(db, PathwayInteraction, Pathway)
        if dangling:
            result.add_error(f"Found {len(dangling)} dangling PathwayInteraction records after merge")
            result.success = False

    return result


# ==============================================================================
# PHASE 2: TREE ENFORCEMENT
# ==============================================================================

def phase2_tree_enforcement(db, Pathway, PathwayParent, dry_run: bool) -> PhaseResult:
    """
    Phase 2: Ensure each pathway has exactly one parent (except roots).
    OPTIMIZED: Parallel LLM calls for multi-parent resolution.
    """
    from scripts.pathway_v2.async_utils import run_parallel, MAX_CONCURRENT_FLASH

    result = PhaseResult(phase_name="Tree Enforcement", success=True)
    logger.info("=" * 60)
    logger.info("PHASE 2: TREE ENFORCEMENT (Parallel)")
    logger.info("=" * 60)

    # Build graph and find multi-parent nodes
    parent_graph = build_parent_graph(PathwayParent)
    multi_parent = detect_multi_parent_nodes(parent_graph)

    if not multi_parent:
        logger.info("No multi-parent pathways found. Tree is valid.")
        return result

    logger.info(f"Found {len(multi_parent)} pathways with multiple parents")

    # Build list of items to process
    items_to_process = []
    for child_id, parent_ids in multi_parent.items():
        child = db.session.get(Pathway, child_id)
        if not child:
            continue

        parents = [db.session.get(Pathway, pid) for pid in parent_ids]
        parents = [p for p in parents if p]

        if len(parents) <= 1:
            continue

        items_to_process.append({
            'child_id': child_id,
            'child_name': child.name,
            'parent_ids': parent_ids,
            'parent_names': [p.name for p in parents]
        })

    def pick_best_parent(item):
        """Pick best parent for a child with multiple parents."""
        try:
            resp = _call_gemini_json(
                BEST_PARENT_PROMPT.format(
                    child_name=item['child_name'],
                    parents_list=", ".join(item['parent_names'])
                ),
                
            )
            return {
                'child_id': item['child_id'],
                'child_name': item['child_name'],
                'parent_ids': item['parent_ids'],
                'parent_names': item['parent_names'],
                'selected': resp.get('selected_parent'),
                'error': None
            }
        except Exception as e:
            return {
                'child_id': item['child_id'],
                'child_name': item['child_name'],
                'parent_ids': item['parent_ids'],
                'parent_names': item['parent_names'],
                'selected': None,
                'error': str(e)
            }

    # Process all multi-parent nodes in parallel
    results = run_parallel(
        items_to_process,
        pick_best_parent,
        max_concurrent=MAX_CONCURRENT_FLASH,
        desc="Multi-parent resolution"
    )

    # Apply results
    for res in results:
        if isinstance(res, Exception):
            result.add_error(f"Resolution failed: {res}")
            continue

        if res.get('error'):
            result.add_warning(f"Error for '{res['child_name']}': {res['error']}")
            # Fallback: pick first parent
            res['selected'] = res['parent_names'][0] if res['parent_names'] else None

        child_id = res['child_id']
        child_name = res['child_name']
        parent_names = res['parent_names']
        selected_name = res['selected']

        if not selected_name or selected_name not in parent_names:
            result.add_warning(f"LLM returned invalid parent '{selected_name}' for '{child_name}'")
            selected_name = parent_names[0] if parent_names else None

        if not selected_name:
            continue

        # Find selected parent
        selected_parent = Pathway.query.filter_by(name=selected_name).first()
        if not selected_parent:
            continue

        # Verify this won't create a cycle - rebuild graph from DB for accuracy
        fresh_graph = build_parent_graph(PathwayParent)
        fresh_graph[child_id] = [selected_parent.id]

        if would_create_cycle(child_id, selected_parent.id, fresh_graph):
            result.add_warning(f"Selecting '{selected_name}' for '{child_name}' would create cycle. Trying alternative...")
            # Try other parents
            for alt_name in parent_names:
                if alt_name != selected_name:
                    alt_parent = Pathway.query.filter_by(name=alt_name).first()
                    if alt_parent:
                        fresh_graph[child_id] = [alt_parent.id]
                        if not would_create_cycle(child_id, alt_parent.id, fresh_graph):
                            selected_parent = alt_parent
                            selected_name = alt_name
                            break
            else:
                result.add_error(f"No valid parent found for '{child_name}' - all create cycles")
                continue

        if dry_run:
            logger.info(f"[DRY RUN] SELECT PARENT: '{selected_name}' for '{child_name}'")
            result.add_change(Change(
                change_type=ChangeType.REPARENT,
                entity_type='pathway',
                entity_id=child_id,
                old_value=parent_names,
                new_value=selected_name,
                reason="Tree enforcement"
            ))
            continue

        # Delete other parent links
        for link in PathwayParent.query.filter_by(child_pathway_id=child_id).all():
            if link.parent_pathway_id != selected_parent.id:
                db.session.delete(link)

        db.session.commit()

        result.add_change(Change(
            change_type=ChangeType.REPARENT,
            entity_type='pathway',
            entity_id=child_id,
            old_value=parent_names,
            new_value=selected_name,
            reason="Tree enforcement"
        ))

        logger.info(f"Enforced parent '{selected_name}' for '{child_name}'")

    # Recalculate levels after tree changes
    if not dry_run and result.changes:
        recalculate_all_levels(db, Pathway, PathwayParent)

    # Verify no cycles remain
    parent_graph = build_parent_graph(PathwayParent)
    cycles = find_all_cycles(parent_graph)
    if cycles:
        result.add_error(f"CRITICAL: {len(cycles)} cycles detected after tree enforcement!")
        result.success = False

    # Verify no multi-parent nodes remain
    multi = detect_multi_parent_nodes(parent_graph)
    if multi:
        result.add_warning(f"{len(multi)} pathways still have multiple parents")

    return result


# ==============================================================================
# PHASE 3: HIERARCHY REPAIR
# ==============================================================================

def phase3_hierarchy_repair(db, Pathway, PathwayParent, dry_run: bool) -> PhaseResult:
    """
    Phase 3: Fix broken chains and shallow hierarchies.

    Pass A: Repair broken parent links
    Pass B: Deepen shallow hierarchies
    """
    result = PhaseResult(phase_name="Hierarchy Repair", success=True)
    logger.info("=" * 60)
    logger.info("PHASE 3: HIERARCHY REPAIR")
    logger.info("=" * 60)

    # PASS A: Find and fix broken parent links
    logger.info("--- Pass A: Broken Chain Repair ---")

    broken_links = []
    for link in PathwayParent.query.all():
        parent = db.session.get(Pathway, link.parent_pathway_id)
        if not parent:
            broken_links.append(link)

    if broken_links:
        logger.info(f"Found {len(broken_links)} broken parent links")

        for link in broken_links:
            child = db.session.get(Pathway, link.child_pathway_id)
            if not child:
                db.session.delete(link)
                continue

            # Ask LLM for new parent
            existing = [p.name for p in Pathway.query.filter(
                Pathway.hierarchy_level >= 0,
                Pathway.hierarchy_level <= 2
            ).limit(50).all()]

            try:
                resp = _call_gemini_json(
                    FIND_PARENT_PROMPT.format(
                        child_name=child.name,
                        roots_list=", ".join(STRICT_ROOTS),
                        existing_pathways=", ".join(existing[:30])
                    ),
                    
                )

                new_parent_name = resp.get('parent')

                if dry_run:
                    logger.info(f"[DRY RUN] FIX BROKEN: '{child.name}' -> '{new_parent_name}'")
                    db.session.delete(link)
                    continue

                # Find or create new parent
                new_parent = Pathway.query.filter_by(name=new_parent_name).first()
                if not new_parent and new_parent_name in STRICT_ROOTS:
                    new_parent = Pathway.query.filter_by(name=new_parent_name).first()

                if new_parent:
                    link.parent_pathway_id = new_parent.id
                    result.add_change(Change(
                        change_type=ChangeType.REPARENT,
                        entity_type='pathway',
                        entity_id=child.id,
                        old_value="BROKEN",
                        new_value=new_parent_name,
                        reason="Broken chain repair"
                    ))
                else:
                    # Delete broken link, child becomes orphan (will be rescued in Phase 4)
                    db.session.delete(link)
                    result.add_warning(f"Could not find parent for '{child.name}', marked as orphan")

            except Exception as e:
                result.add_error(f"Error repairing '{child.name}': {e}")
                db.session.delete(link)

        db.session.commit()
    else:
        logger.info("No broken parent links found")

    # PASS B: Deepen shallow hierarchies
    logger.info("--- Pass B: Shallow Hierarchy Deepening ---")

    # Recalculate levels first
    if not dry_run:
        recalculate_all_levels(db, Pathway, PathwayParent)

    level1_pathways = Pathway.query.filter_by(hierarchy_level=1).all()
    logger.info(f"Found {len(level1_pathways)} level-1 pathways to evaluate")

    deepened = 0
    for pw in level1_pathways:
        # Skip known legitimate level-1 pathways
        if pw.name in LEGITIMATE_LEVEL1:
            continue

        parent_link = PathwayParent.query.filter_by(child_pathway_id=pw.id).first()
        if not parent_link:
            continue

        root = db.session.get(Pathway, parent_link.parent_pathway_id)
        if not root or root.name not in STRICT_ROOTS:
            continue

        try:
            resp = _call_gemini_json(
                SHALLOW_HIERARCHY_PROMPT.format(
                    child_name=pw.name,
                    root_name=root.name
                ),
                
            )

            action = resp.get('action')
            new_parent_name = resp.get('new_parent')

            if action != 'INSERT_INTERMEDIATE' or not new_parent_name:
                continue

            # Validate new parent name
            if new_parent_name == pw.name or new_parent_name == root.name:
                result.add_warning(f"Invalid intermediate '{new_parent_name}' for '{pw.name}'")
                continue

            if dry_run:
                logger.info(f"[DRY RUN] DEEPEN: '{pw.name}' -> '{new_parent_name}' -> '{root.name}'")
                deepened += 1
                continue

            # Find or create intermediate
            intermediate = Pathway.query.filter_by(name=new_parent_name).first()
            if not intermediate:
                intermediate = Pathway(
                    name=new_parent_name,
                    hierarchy_level=1,
                    is_leaf=False,
                    ai_generated=True
                )
                db.session.add(intermediate)
                db.session.flush()

                # Link intermediate to root
                int_link = PathwayParent(
                    child_pathway_id=intermediate.id,
                    parent_pathway_id=root.id,
                    relationship_type='is_a'
                )
                db.session.add(int_link)

                result.add_change(Change(
                    change_type=ChangeType.CREATE,
                    entity_type='pathway',
                    entity_id=intermediate.id,
                    old_value=None,
                    new_value=new_parent_name,
                    reason="Intermediate for hierarchy deepening"
                ))

            # Reparent child to intermediate
            parent_link.parent_pathway_id = intermediate.id
            db.session.commit()

            result.add_change(Change(
                change_type=ChangeType.REPARENT,
                entity_type='pathway',
                entity_id=pw.id,
                old_value=root.name,
                new_value=new_parent_name,
                reason="Hierarchy deepening"
            ))

            logger.info(f"Deepened: '{pw.name}' -> '{new_parent_name}' -> '{root.name}'")
            deepened += 1

        except Exception as e:
            result.add_error(f"Error deepening '{pw.name}': {e}")

        time.sleep(0.5)  # Rate limiting

    logger.info(f"Deepened {deepened} hierarchies")

    # Final level recalculation
    if not dry_run and deepened > 0:
        recalculate_all_levels(db, Pathway, PathwayParent)

    return result


# ==============================================================================
# PHASE 4: INTERACTION SYNC
# ==============================================================================

def phase4_interaction_sync(db, Pathway, PathwayInteraction, Interaction, dry_run: bool) -> PhaseResult:
    """
    Phase 4: Ensure every interaction has a valid pathway assignment.

    Fixes orphaned interactions and dangling pathway links.
    """
    result = PhaseResult(phase_name="Interaction Sync", success=True)
    logger.info("=" * 60)
    logger.info("PHASE 4: INTERACTION SYNC")
    logger.info("=" * 60)

    valid_pathway_ids = {p.id for p in Pathway.query.all()}
    fallback_pathway = Pathway.query.filter_by(name="Protein Quality Control").first()

    if not fallback_pathway:
        result.add_error("CRITICAL: Fallback pathway 'Protein Quality Control' not found!")
        result.success = False
        return result

    # Step 1: Find dangling PathwayInteraction records
    logger.info("--- Checking for dangling pathway links ---")
    dangling_pis = []
    for pi in PathwayInteraction.query.all():
        if pi.pathway_id not in valid_pathway_ids:
            dangling_pis.append(pi)

    if dangling_pis:
        logger.info(f"Found {len(dangling_pis)} dangling PathwayInteraction records")

        for pi in dangling_pis:
            if dry_run:
                logger.info(f"[DRY RUN] DELETE dangling PI for interaction {pi.interaction_id}")
                continue

            db.session.delete(pi)
            result.add_change(Change(
                change_type=ChangeType.DELETE,
                entity_type='pathway_interaction',
                entity_id=pi.id,
                old_value=pi.pathway_id,
                new_value=None,
                reason="Dangling pathway reference"
            ))

        if not dry_run:
            db.session.commit()

    # Step 2: Find interactions without any PathwayInteraction
    logger.info("--- Checking for orphaned interactions ---")
    orphan_ids = validate_no_orphan_interactions(db, Interaction, PathwayInteraction)

    if orphan_ids:
        logger.info(f"Found {len(orphan_ids)} interactions without pathway assignment")

        for int_id in orphan_ids:
            interaction = db.session.get(Interaction, int_id)
            if not interaction:
                continue

            # Try to find pathway from interaction data
            assigned_pathway = None
            assignment_source = None

            # Ensure interaction.data is a dict before key access
            data = interaction.data if isinstance(interaction.data, dict) else {}

            # Priority 1: step3_finalized_pathway
            if 'step3_finalized_pathway' in data:
                pw_name = data['step3_finalized_pathway']
                if pw_name and isinstance(pw_name, str):
                    pw = Pathway.query.filter_by(name=pw_name).first()
                    if pw:
                        assigned_pathway = pw
                        assignment_source = "step3_finalized_pathway"

            # Priority 2: step2_proposal
            if not assigned_pathway and 'step2_proposal' in data:
                pw_name = data['step2_proposal']
                if pw_name and isinstance(pw_name, str):
                    pw = Pathway.query.filter_by(name=pw_name).first()
                    if pw:
                        assigned_pathway = pw
                        assignment_source = "step2_proposal"

            # Priority 3: Fallback
            if not assigned_pathway:
                assigned_pathway = fallback_pathway
                assignment_source = "fallback"
                result.add_warning(f"Interaction {int_id} assigned to fallback pathway")

            if dry_run:
                logger.info(f"[DRY RUN] ASSIGN interaction {int_id} -> '{assigned_pathway.name}' ({assignment_source})")
                continue

            # Create PathwayInteraction
            pi = PathwayInteraction(
                pathway_id=assigned_pathway.id,
                interaction_id=int_id,
                assignment_method=f'step6_{assignment_source}'
            )
            db.session.add(pi)

            # Update interaction data
            if not interaction.data:
                interaction.data = {}
            interaction.data['_step6_reassigned'] = True
            interaction.data['_step6_pathway'] = assigned_pathway.name
            interaction.data['_step6_source'] = assignment_source

            result.add_change(Change(
                change_type=ChangeType.REASSIGN_INTERACTION,
                entity_type='interaction',
                entity_id=int_id,
                old_value=None,
                new_value=assigned_pathway.name,
                reason=f"Orphan rescue via {assignment_source}"
            ))

            logger.info(f"Assigned interaction {int_id} -> '{assigned_pathway.name}'")

        if not dry_run:
            db.session.commit()
    else:
        logger.info("No orphaned interactions found")

    # Verify: all interactions should now have PathwayInteraction
    if not dry_run:
        remaining = validate_no_orphan_interactions(db, Interaction, PathwayInteraction)
        if remaining:
            result.add_error(f"CRITICAL: {len(remaining)} interactions still orphaned after sync!")
            result.success = False

    return result


# ==============================================================================
# PHASE 5: SAFE PRUNING
# ==============================================================================

def phase5_pruning(db, Pathway, PathwayParent, PathwayInteraction, dry_run: bool) -> PhaseResult:
    """
    Phase 5: Remove truly orphaned pathways.

    Only deletes pathways that are:
    - Unreachable from roots (hierarchy_level == -1)
    - Have no interactions
    - Have no children
    - Are not in STRICT_ROOTS
    """
    result = PhaseResult(phase_name="Pruning", success=True)
    logger.info("=" * 60)
    logger.info("PHASE 5: SAFE PRUNING")
    logger.info("=" * 60)

    # Recalculate levels to find unreachable pathways
    if not dry_run:
        recalculate_all_levels(db, Pathway, PathwayParent)

    # Find orphan candidates
    orphans = Pathway.query.filter_by(hierarchy_level=-1).all()
    logger.info(f"Found {len(orphans)} unreachable pathways")

    pruned = 0
    rescued = 0

    for pw in orphans:
        # Never prune roots
        if pw.name in STRICT_ROOTS:
            result.add_warning(f"Root '{pw.name}' is unreachable - needs manual fix")
            continue

        # Check if has interactions
        interaction_count = PathwayInteraction.query.filter_by(pathway_id=pw.id).count()

        # Check if has children
        child_count = PathwayParent.query.filter_by(parent_pathway_id=pw.id).count()

        if interaction_count > 0 or child_count > 0:
            # Pathway has data - rescue it instead of deleting
            # Use SMART rescue to pick the correct root based on semantic meaning
            smart_parent = get_smart_rescue_parent(pw.name, Pathway)
            parent_name = smart_parent.name if smart_parent else "Protein Quality Control"

            logger.info(f"Rescuing '{pw.name}' (has {interaction_count} interactions, {child_count} children)")

            if dry_run:
                logger.info(f"[DRY RUN] RESCUE: '{pw.name}' -> '{parent_name}'")
                rescued += 1
                continue

            if smart_parent:
                # Check if already has parent link (shouldn't, but safe check)
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

                result.add_change(Change(
                    change_type=ChangeType.REPARENT,
                    entity_type='pathway',
                    entity_id=pw.id,
                    old_value="ORPHAN",
                    new_value=parent_name,
                    reason=f"Smart rescue to {parent_name}"
                ))
                rescued += 1

            continue

        # Safe to prune - no interactions, no children
        if dry_run:
            logger.info(f"[DRY RUN] PRUNE: '{pw.name}'")
            pruned += 1
            continue

        # Delete any remaining parent links
        PathwayParent.query.filter_by(child_pathway_id=pw.id).delete()
        PathwayParent.query.filter_by(parent_pathway_id=pw.id).delete()

        db.session.delete(pw)

        result.add_change(Change(
            change_type=ChangeType.DELETE,
            entity_type='pathway',
            entity_id=pw.id,
            old_value=pw.name,
            new_value=None,
            reason="Orphan with no data"
        ))
        pruned += 1

    if not dry_run:
        db.session.commit()
        # Final level recalculation
        recalculate_all_levels(db, Pathway, PathwayParent)

    logger.info(f"Pruned {pruned} pathways, rescued {rescued} pathways")

    return result


# ==============================================================================
# PHASE 6: PRE-FLIGHT VALIDATION
# ==============================================================================

def phase6_preflight(db, Pathway, PathwayParent, PathwayInteraction, Interaction) -> PhaseResult:
    """
    Phase 6: Final validation before declaring Step 6 complete.

    Checks all invariants that must hold.
    """
    result = PhaseResult(phase_name="Pre-Flight Validation", success=True)
    logger.info("=" * 60)
    logger.info("PHASE 6: PRE-FLIGHT VALIDATION")
    logger.info("=" * 60)

    checks_passed = 0
    checks_failed = 0

    # Check 1: All roots exist at level 0
    roots = Pathway.query.filter(Pathway.name.in_(STRICT_ROOTS)).all()
    root_names = {r.name for r in roots}
    missing_roots = STRICT_ROOTS - root_names

    if missing_roots:
        result.add_error(f"CRITICAL: Missing roots: {missing_roots}")
        checks_failed += 1
    else:
        level0_roots = [r for r in roots if r.hierarchy_level == 0]
        if len(level0_roots) != len(STRICT_ROOTS):
            result.add_warning(f"Only {len(level0_roots)}/{len(STRICT_ROOTS)} roots at level 0")
        else:
            logger.info(f"[OK] All {len(STRICT_ROOTS)} roots exist at level 0")
            checks_passed += 1

    # Check 2: No pathways with hierarchy_level = -1
    orphan_count = Pathway.query.filter_by(hierarchy_level=-1).count()
    if orphan_count > 0:
        result.add_error(f"Found {orphan_count} unreachable pathways (level=-1)")
        checks_failed += 1
    else:
        logger.info("[OK] No unreachable pathways")
        checks_passed += 1

    # Check 3: No multi-parent pathways
    parent_graph = build_parent_graph(PathwayParent)
    multi_parent = detect_multi_parent_nodes(parent_graph)
    if multi_parent:
        result.add_warning(f"{len(multi_parent)} pathways still have multiple parents")
        # Not a hard failure, but logged
    else:
        logger.info("[OK] No multi-parent pathways (strict tree)")
        checks_passed += 1

    # Check 4: No cycles
    cycles = find_all_cycles(parent_graph)
    if cycles:
        result.add_error(f"CRITICAL: {len(cycles)} cycles detected in hierarchy!")
        result.success = False
        checks_failed += 1
    else:
        logger.info("[OK] No cycles in hierarchy")
        checks_passed += 1

    # Check 5: All interactions have PathwayInteraction
    orphan_interactions = validate_no_orphan_interactions(db, Interaction, PathwayInteraction)
    if orphan_interactions:
        result.add_error(f"{len(orphan_interactions)} interactions without pathway assignment")
        checks_failed += 1
    else:
        logger.info("[OK] All interactions have pathway assignment")
        checks_passed += 1

    # Check 6: No dangling PathwayInteraction records
    dangling = validate_no_dangling_pathway_links(db, PathwayInteraction, Pathway)
    if dangling:
        result.add_error(f"{len(dangling)} PathwayInteraction records point to missing pathways")
        checks_failed += 1
    else:
        logger.info("[OK] No dangling pathway references")
        checks_passed += 1

    # Summary
    logger.info("-" * 40)
    logger.info(f"Pre-flight: {checks_passed} passed, {checks_failed} failed")

    if checks_failed > 0:
        result.success = False

    return result


# ==============================================================================
# MAIN ORCHESTRATOR
# ==============================================================================

def reorganize_pathways(dry_run: bool = False, start_phase: int = 1) -> Dict[str, PhaseResult]:
    """
    Main entry point for Step 6 pathway reorganization.

    Args:
        dry_run: If True, simulate changes without committing
        start_phase: Start from this phase (1-6)

    Returns:
        Dict of phase name -> PhaseResult
    """
    try:
        from app import app, db
        from models import Pathway, PathwayParent, PathwayInteraction, Interaction
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return {}

    results = {}

    with app.app_context():
        logger.info("=" * 70)
        logger.info("STEP 6: PATHWAY REORGANIZATION")
        logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
        logger.info(f"Starting from Phase: {start_phase}")
        logger.info("=" * 70)

        # Phase 1: Deduplication
        if start_phase <= 1:
            result = phase1_deduplication(db, Pathway, PathwayParent, PathwayInteraction, dry_run)
            results['deduplication'] = result
            if not dry_run:
                save_checkpoint(db, Interaction, 1, 'complete' if result.success else 'failed', result.changes)

        # Phase 2: Tree Enforcement
        if start_phase <= 2:
            result = phase2_tree_enforcement(db, Pathway, PathwayParent, dry_run)
            results['tree_enforcement'] = result
            if not dry_run:
                save_checkpoint(db, Interaction, 2, 'complete' if result.success else 'failed', result.changes)

        # Phase 3: Hierarchy Repair
        if start_phase <= 3:
            result = phase3_hierarchy_repair(db, Pathway, PathwayParent, dry_run)
            results['hierarchy_repair'] = result
            if not dry_run:
                save_checkpoint(db, Interaction, 3, 'complete' if result.success else 'failed', result.changes)

        # Phase 4: Interaction Sync
        if start_phase <= 4:
            result = phase4_interaction_sync(db, Pathway, PathwayInteraction, Interaction, dry_run)
            results['interaction_sync'] = result
            if not dry_run:
                save_checkpoint(db, Interaction, 4, 'complete' if result.success else 'failed', result.changes)

        # Phase 5: Pruning
        if start_phase <= 5:
            result = phase5_pruning(db, Pathway, PathwayParent, PathwayInteraction, dry_run)
            results['pruning'] = result
            if not dry_run:
                save_checkpoint(db, Interaction, 5, 'complete' if result.success else 'failed', result.changes)

        # Phase 6: Pre-Flight Validation
        if start_phase <= 6:
            result = phase6_preflight(db, Pathway, PathwayParent, PathwayInteraction, Interaction)
            results['preflight'] = result
            if not dry_run:
                save_checkpoint(db, Interaction, 6, 'complete' if result.success else 'failed', result.changes)

        # Summary
        logger.info("=" * 70)
        logger.info("STEP 6 SUMMARY")
        logger.info("=" * 70)

        total_changes = 0
        total_errors = 0
        total_warnings = 0

        for name, res in results.items():
            status = "OK" if res.success else "FAILED"
            logger.info(f"  {name}: {status} ({len(res.changes)} changes, {len(res.errors)} errors)")
            total_changes += len(res.changes)
            total_errors += len(res.errors)
            total_warnings += len(res.warnings)

        logger.info("-" * 40)
        logger.info(f"Total: {total_changes} changes, {total_errors} errors, {total_warnings} warnings")

        overall_success = all(r.success for r in results.values())
        logger.info(f"Step 6 Status: {'SUCCESS' if overall_success else 'NEEDS ATTENTION'}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Step 6: Reorganize Pathways")
    parser.add_argument("--dry-run", action="store_true", help="Simulate changes without committing")
    parser.add_argument("--phase", type=int, default=1, choices=[1, 2, 3, 4, 5, 6],
                        help="Start from this phase (default: 1)")
    args = parser.parse_args()

    reorganize_pathways(dry_run=args.dry_run, start_phase=args.phase)
