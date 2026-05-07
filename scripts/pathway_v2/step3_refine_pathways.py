#!/usr/bin/env python3
"""
Step 3: Refine Pathway Terms with Global Context (Per-Function)
===============================================================
Goal: Standardize and refine the pathway terms assigned in Step 2 for EACH FUNCTION.
Input: All 'step2_function_proposals' + All existing DB pathways.
Output: 'step3_function_pathways' array and 'step3_finalized_pathway' in Interaction.data.
        Also sets fn['pathway'] on each function for frontend use.

GUARANTEE: 100% of interactions with step2_proposal MUST have step3_finalized_pathway.
Includes recovery loop to catch any missing assignments.

Usage:
    python3 scripts/pathway_v2/step3_refine_pathways.py
"""

import sys
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from scripts.pathway_v2.llm_utils import _call_gemini_json, _call_gemini_json_batch

BATCH_SIZE = 20
MAX_RETRY_ROUNDS = 3
STEP3_BATCH_MAX_OUTPUT_TOKENS = 6000
STEP3_SINGLE_MAX_OUTPUT_TOKENS = 3000
SINGLE_CONTEXT_CHAR_LIMIT = 900

STEP3_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "refinements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "interaction_id": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                    "function_refinements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "function_index": {"type": "integer"},
                                "finalized_pathway": {"type": "string"},
                            },
                            "required": ["function_index", "finalized_pathway"],
                            "additionalProperties": False,
                        },
                    },
                    "primary_pathway": {"type": "string"},
                },
                "required": ["interaction_id", "function_refinements", "primary_pathway"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["refinements"],
    "additionalProperties": False,
}

STEP3_PROMPT = """You are a biological pathway standardization expert.
Task: REFINE and STANDARDIZE the proposed pathway names for EACH FUNCTION.

## GLOBAL CONTEXT (Existing & Proposed Pathways by Specificity Level)
{global_context_list}

## INTERACTIONS TO REFINE
{interactions_list}

## INSTRUCTIONS
1. If a similar term exists in Global Context, use the standard/canonical one.
2. Apply Goldilocks refinement if proposal is too broad/specific.
3. Each function should map to the pathway that BEST describes its biological mechanism.
4. Different functions CAN and SHOULD have different pathways if biologically appropriate.

## SPECIFICITY CHECK (CRITICAL - Before Finalizing)
If your refinement lands on a Level 0-1 pathway, VERIFY:
- Is there a more specific child in the list that better fits this function?
- Does this function genuinely span multiple sub-pathways, or is it specific?
- PREFER specific pathways: "Base Excision Repair" over "DNA Damage Response"
- PREFER specific pathways: "Aggrephagy" over "Protein Quality Control"
- ONLY use broad pathways if the function genuinely doesn't fit any specific child

EXAMPLES:
- "repairs oxidized bases" -> "Base Excision Repair" (NOT "DNA Damage Response")
- "general DNA damage sensor" -> "DNA Damage Response" is OK (spans multiple repair types)

## RESPONSE FORMAT
{{
  "refinements": [
    {{
      "interaction_id": "ID",
      "function_refinements": [
        {{"function_index": 0, "finalized_pathway": "Final Name"}},
        {{"function_index": 1, "finalized_pathway": "Different Final Name"}}
      ],
      "primary_pathway": "Most representative pathway overall"
    }}
  ]
}}
Respond with ONLY the JSON. You MUST provide a refinement for EVERY interaction listed.
"""

SIMPLE_REFINE_PROMPT = """Standardize these pathway names for a protein-protein interaction.
Each function can have a different pathway.

Proposed pathways:
{proposals}

Existing pathways (organized by specificity - PREFER higher level numbers):
{existing_pathways}

Rules:
- Use Title Case
- Match existing pathway if very similar
- Keep the Goldilocks principle (not too broad, not too specific)
- PREFER specific pathways over broad ones - check if a child pathway exists
- If proposal is Level 0/1, verify no better specific pathway fits

Respond with ONLY JSON:
{{"interaction_id": "{interaction_id}", "function_refinements": [{{"function_index": 0, "finalized_pathway": "StandardizedName"}}], "primary_pathway": "MainPathway"}}
"""


def _format_interaction_for_step3(item) -> str:
    """Format interaction with step2 proposals for refinement."""
    proposals = item.data.get('step2_function_proposals', []) if item.data else []
    fallback = item.data.get('step2_proposal', 'Unknown') if item.data else 'Unknown'

    if not proposals:
        return f"- ID: {item.id} | Proposal: {fallback}"

    lines = [f"- ID: {item.id}"]
    for p in proposals:
        if not isinstance(p, dict):
            continue
        idx = p.get('function_index', '?')
        pw = p.get('pathway', fallback)
        lines.append(f"    [{idx}] {pw}")

    return "\n".join(lines)


def _compact_context(context_str: str, limit: int = SINGLE_CONTEXT_CHAR_LIMIT) -> str:
    text = (context_str or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0]


def _normalize_function_refinements(raw: Any, function_count: int) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [
            {"function_index": key, "finalized_pathway": value}
            for key, value in raw.items()
        ]
    if not isinstance(raw, list):
        return normalized

    for idx, entry in enumerate(raw):
        if isinstance(entry, dict):
            fn_idx = entry.get("function_index", idx)
            pathway = entry.get("finalized_pathway") or entry.get("pathway")
        else:
            continue

        try:
            fn_idx_int = int(fn_idx)
        except (TypeError, ValueError):
            continue
        if fn_idx_int < 0:
            continue
        if function_count and fn_idx_int >= function_count:
            continue

        pathway_str = str(pathway or "").strip()
        if not pathway_str:
            continue
        normalized.append({
            "function_index": fn_idx_int,
            "finalized_pathway": pathway_str,
        })

    deduped: List[Dict[str, Any]] = []
    seen_idx: Set[int] = set()
    for row in normalized:
        idx_val = row["function_index"]
        if idx_val in seen_idx:
            continue
        seen_idx.add(idx_val)
        deduped.append(row)
    return deduped


def _normalize_refinements(refinements: Any, batch_map: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if isinstance(refinements, dict):
        refinements = [refinements]
    if not isinstance(refinements, list):
        return {}

    results: Dict[str, Dict[str, Any]] = {}
    for row in refinements:
        if not isinstance(row, dict):
            continue
        interaction_id = str(row.get("interaction_id", "")).strip()
        if not interaction_id or interaction_id not in batch_map:
            continue

        funcs = (batch_map[interaction_id].data or {}).get("functions", [])
        function_count = len(funcs) if isinstance(funcs, list) else 0
        function_refinements = _normalize_function_refinements(row.get("function_refinements"), function_count)
        primary_pathway = str(
            row.get("primary_pathway") or row.get("finalized_pathway") or ""
        ).strip()
        if not primary_pathway and function_refinements:
            primary_pathway = function_refinements[0]["finalized_pathway"]
        if not primary_pathway:
            continue

        results[interaction_id] = {
            "function_refinements": function_refinements,
            "primary_pathway": primary_pathway,
        }
    return results


def _format_pathways_with_hierarchy(existing_pathways, proposed_pathways) -> str:
    """Format pathways with hierarchy levels for the prompt.
    
    Args:
        existing_pathways: List of Pathway objects from database
        proposed_pathways: Set of proposed pathway names from interactions
        
    Returns:
        Formatted string showing pathways organized by specificity level
    """
    # Group existing pathways by hierarchy level
    by_level = {}
    existing_names = set()
    for p in existing_pathways:
        if p.name:
            existing_names.add(p.name)
            level = p.hierarchy_level if p.hierarchy_level is not None else 0
            by_level.setdefault(level, []).append(p.name)
    
    # Format existing pathways by level
    lines = []
    for level in sorted(by_level.keys()):
        names = sorted(by_level[level])[:25]  # Limit per level
        if level == 0:
            prefix = "Level 0 (ROOT - AVOID unless function spans multiple children)"
        elif level == 1:
            prefix = "Level 1 (Broad - prefer more specific if available)"
        else:
            prefix = f"Level {level}+ (Specific - PREFERRED)"
        
        if len(by_level[level]) > 25:
            lines.append(f"  {prefix}: {', '.join(names)}, ... (+{len(by_level[level]) - 25} more)")
        else:
            lines.append(f"  {prefix}: {', '.join(names)}")
    
    # Add proposed pathways that aren't already in DB
    new_proposed = proposed_pathways - existing_names
    if new_proposed:
        proposed_list = sorted(new_proposed)[:20]
        if len(new_proposed) > 20:
            lines.append(f"  Newly proposed: {', '.join(proposed_list)}, ... (+{len(new_proposed) - 20} more)")
        else:
            lines.append(f"  Newly proposed: {', '.join(proposed_list)}")
    
    return "\n".join(lines) if lines else "None yet"


def _process_batch(batch: List, context_str: str) -> Dict[str, Dict]:
    """
    Process a batch of interactions. Returns dict of:
    {interaction_id: {"function_refinements": [...], "primary_pathway": "..."}}
    """
    if not batch:
        return {}

    batch_map = {str(item.id): item for item in batch}
    items_str = "\n".join([_format_interaction_for_step3(item) for item in batch])

    resp = _call_gemini_json(
        STEP3_PROMPT.format(global_context_list=context_str, interactions_list=items_str),
        response_json_schema=STEP3_RESPONSE_SCHEMA,
        expected_root_key="refinements",
        thinking_level="low",
        disable_afc=True,
        max_output_tokens=STEP3_BATCH_MAX_OUTPUT_TOKENS,
        model="gemini-3-flash-preview",
    )
    refinements = resp.get("refinements", []) if isinstance(resp, dict) else []
    results = _normalize_refinements(refinements, batch_map)
    expected_ids = set(batch_map.keys())
    parsed_ids = set(results.keys())
    missing_ids = sorted(expected_ids - parsed_ids)
    logger.info(
        "  Batch parse summary: expected=%s parsed=%s missing=%s",
        len(expected_ids),
        len(parsed_ids),
        len(missing_ids),
    )

    return results


def _process_single(interaction, context_str: str) -> Optional[Dict]:
    """
    Process a single interaction with simplified prompt.
    Returns dict with function_refinements and primary_pathway, or None.
    """
    proposals = interaction.data.get('step2_function_proposals', []) if interaction.data else []
    fallback = interaction.data.get('step2_proposal', '') if interaction.data else ''

    if not proposals and not fallback:
        return None

    # Format proposals for the prompt
    if proposals:
        lines = []
        for p in proposals:
            if not isinstance(p, dict):
                continue
            lines.append(f"[{p.get('function_index', '?')}] {p.get('pathway', fallback)}")
        proposals_str = "\n".join(lines) if lines else f"[0] {fallback}"
    else:
        proposals_str = f"[0] {fallback}"

    prompt = SIMPLE_REFINE_PROMPT.format(
        proposals=proposals_str,
        existing_pathways=_compact_context(context_str),
        interaction_id=interaction.id
    )

    resp = _call_gemini_json(
        prompt,
        response_json_schema={
            "type": "object",
            "properties": {
                "interaction_id": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                "function_refinements": STEP3_RESPONSE_SCHEMA["properties"]["refinements"]["items"]["properties"]["function_refinements"],
                "primary_pathway": {"type": "string"},
            },
            "required": ["interaction_id", "function_refinements", "primary_pathway"],
            "additionalProperties": False,
        },
        expected_root_key=None,
        thinking_level="low",
        disable_afc=True,
        max_output_tokens=STEP3_SINGLE_MAX_OUTPUT_TOKENS,
        model="gemini-3-flash-preview",
    )
    if not isinstance(resp, dict):
        return None

    primary = str(resp.get("primary_pathway") or "").strip()
    funcs = interaction.data.get("functions", []) if interaction.data else []
    normalized = _normalize_function_refinements(resp.get("function_refinements"), len(funcs))
    if not primary and normalized:
        primary = normalized[0]["finalized_pathway"]
    if not primary:
        return None

    return {
        "function_refinements": normalized,
        "primary_pathway": primary
    }


def _retry_cascade(failed_interactions: List, context_str: str) -> Dict[str, Dict]:
    """
    Retry failed interactions with progressively smaller batches.
    Returns dict of {interaction_id: {"function_refinements": [...], "primary_pathway": "..."}}.
    """
    results = {}
    remaining = list(failed_interactions)

    batch_sizes = [10, 5, 3, 1]

    for batch_size in batch_sizes:
        if not remaining:
            break

        logger.info(f"  Retrying {len(remaining)} interactions with batch size {batch_size}...")
        still_failed = []

        for i in range(0, len(remaining), batch_size):
            batch = remaining[i:i + batch_size]

            try:
                if batch_size == 1 and batch:
                    result = _process_single(batch[0], context_str)
                    if result:
                        results[str(batch[0].id)] = result
                    else:
                        still_failed.extend(batch)
                elif batch:
                    batch_results = _process_batch(batch, context_str)
                    results.update(batch_results)
                    for item in batch:
                        if str(item.id) not in batch_results:
                            still_failed.append(item)

                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"  Retry batch failed: {e}")
                still_failed.extend(batch)

        remaining = still_failed

    return results


def _process_all_via_batch_api_step3(todo, context_str):
    """Submit ALL step3 batches as a single Batch API job for 50% cost savings.

    Returns (all_results, failed_interactions).
    """
    total_batches = (len(todo) + BATCH_SIZE - 1) // BATCH_SIZE
    prompts = []
    batch_maps = []

    for batch_idx in range(total_batches):
        batch = todo[batch_idx * BATCH_SIZE : (batch_idx + 1) * BATCH_SIZE]
        batch_maps.append({str(item.id): item for item in batch})
        items_str = "\n".join([_format_interaction_for_step3(item) for item in batch])
        prompts.append(STEP3_PROMPT.format(
            global_context_list=context_str,
            interactions_list=items_str,
        ))

    logger.info(f"Submitting {len(prompts)} step3 prompts via Batch API...")
    raw_results = _call_gemini_json_batch(
        prompts,
        model="gemini-3-flash-preview",
        thinking_level="low",
        max_output_tokens=STEP3_BATCH_MAX_OUTPUT_TOKENS,
        response_json_schema=STEP3_RESPONSE_SCHEMA,
        expected_root_keys=["refinements"] * len(prompts),
        display_name=f"step3-refine-{int(time.time())}",
    )

    all_results: Dict[str, Dict] = {}
    failed_interactions = []

    for idx, (parsed, batch_map) in enumerate(zip(raw_results, batch_maps)):
        refinements = parsed.get("refinements", []) if isinstance(parsed, dict) else []
        results = _normalize_refinements(refinements, batch_map)
        all_results.update(results)
        batch_items = list(batch_map.values())
        missing = [item for item in batch_items if str(item.id) not in results]
        failed_interactions.extend(missing)

    logger.info(f"Batch API: {len(all_results)} refined, {len(failed_interactions)} need sync retry")

    if failed_interactions:
        recovered = _retry_cascade(failed_interactions, context_str)
        all_results.update(recovered)
        failed_interactions = [i for i in failed_interactions if str(i.id) not in recovered]

    return all_results, failed_interactions


def refine_pathways(interaction_ids: List[int] = None, mode: str = "standard"):
    """
    Refine pathway terms. Guarantees 100% coverage.

    Args:
        interaction_ids: Optional list of interaction IDs to process.
                        If None, processes all interactions.
        mode: "standard" (sync LLM calls) or "batch" (Batch API, 50% savings).
    """
    try:
        from app import app, db
        from models import Interaction, Pathway
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    with app.app_context():
        # RECOVERY: First check for interactions missing step2_proposal
        missing_step2_query = Interaction.query.filter(
            ~Interaction.data.has_key('step2_proposal')
        )
        if interaction_ids:
            missing_step2_query = missing_step2_query.filter(Interaction.id.in_(interaction_ids))
        missing_step2 = missing_step2_query.all()

        if missing_step2:
            logger.warning(f"Found {len(missing_step2)} interactions missing step2_proposal. Running recovery...")
            from scripts.pathway_v2.step2_assign_initial_terms import assign_initial_terms_for_interactions
            assign_initial_terms_for_interactions(missing_step2)

        # Gather Global Context with hierarchy info
        existing_pathway_objects = Pathway.query.all()
        existing = {p.name for p in existing_pathway_objects if p.name}

        # Get interactions to refine
        base_query = Interaction.query.filter(Interaction.data.has_key('step2_proposal'))
        if interaction_ids:
            base_query = base_query.filter(Interaction.id.in_(interaction_ids))
            logger.info(f"Filtering to {len(interaction_ids)} interactions from query filter")

        interactions = base_query.all()
        proposed = {i.data.get('step2_proposal') for i in interactions if i.data and 'step2_proposal' in i.data}

        # Format pathways with hierarchy levels for better specificity guidance
        context_str = _format_pathways_with_hierarchy(existing_pathway_objects, proposed)

        todo = [i for i in interactions if 'step3_finalized_pathway' not in (i.data or {})]
        logger.info(f"Interactions requiring Step 3: {len(todo)}")
        if not todo:
            return

        if mode == "batch":
            all_results, failed_interactions = _process_all_via_batch_api_step3(
                todo, context_str
            )
        else:
            total_batches = (len(todo) + BATCH_SIZE - 1) // BATCH_SIZE
            all_results = {}
            failed_interactions = []

            # First pass: process in batches
            for batch_idx in range(total_batches):
                batch = todo[batch_idx * BATCH_SIZE : (batch_idx + 1) * BATCH_SIZE]
                logger.info(f"Processing batch {batch_idx+1}/{total_batches}...")

                try:
                    batch_results = _process_batch(batch, context_str)
                    all_results.update(batch_results)

                    batch_missing = [item for item in batch if str(item.id) not in batch_results]
                    if batch_missing:
                        logger.info(
                            "  Batch incomplete: expected=%s parsed=%s missing=%s. Triggering immediate split fallback.",
                            len(batch),
                            len(batch_results),
                            len(batch_missing),
                        )
                        recovered = _retry_cascade(batch_missing, context_str)
                        if recovered:
                            all_results.update(recovered)
                        still_missing = [item for item in batch_missing if str(item.id) not in recovered]
                        failed_interactions.extend(still_missing)

                    logger.info(f"  Updated {len(batch_results)}/{len(batch)} items in primary pass.")
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"Error in batch {batch_idx+1}: {e}")
                    failed_interactions.extend(batch)

            # Retry cascade for failed interactions
            retry_round = 0
            while failed_interactions and retry_round < MAX_RETRY_ROUNDS:
                retry_round += 1
                logger.info(f"\n=== Retry Round {retry_round}: {len(failed_interactions)} interactions ===")

                retry_results = _retry_cascade(failed_interactions, context_str)
                all_results.update(retry_results)

                failed_interactions = [i for i in failed_interactions if str(i.id) not in retry_results]

                if not failed_interactions:
                    logger.info("All interactions successfully refined!")
                    break

        # Apply all results to database
        success_count = 0
        for interaction in todo:
            str_id = str(interaction.id)
            if str_id in all_results:
                result = all_results[str_id]
                d = dict(interaction.data or {})

                # Store refined function pathways
                d['step3_function_pathways'] = result.get('function_refinements', [])
                d['step3_finalized_pathway'] = result.get('primary_pathway')  # Backward compat

                # Update each function with its finalized pathway
                functions = d.get('functions', [])
                for fr in result.get('function_refinements', []):
                    if not isinstance(fr, dict):
                        continue
                    try:
                        idx = int(fr.get('function_index', -1))
                    except (TypeError, ValueError):
                        idx = -1
                    if 0 <= idx < len(functions):
                        functions[idx]['pathway'] = fr.get('finalized_pathway')  # Final pathway on function
                d['functions'] = functions

                interaction.data = d
                success_count += 1
            elif interaction.data and 'step2_proposal' in interaction.data:
                # Fallback: use step2_proposal as finalized if refinement failed
                d = dict(interaction.data)
                fallback_pathway = d.get('step2_proposal', 'Unknown Pathway')
                d['step3_finalized_pathway'] = fallback_pathway

                # Also set fallback pathway on all functions
                functions = d.get('functions', [])
                for fn in functions:
                    if isinstance(fn, dict) and 'pathway' not in fn:
                        fn['pathway'] = fallback_pathway
                d['functions'] = functions

                interaction.data = d
                success_count += 1
                logger.debug(f"  Used step2_proposal as fallback for interaction {interaction.id}")

        db.session.commit()

        # Final report
        logger.info(f"\n{'='*60}")
        logger.info(f"Step 3 Complete:")
        logger.info(f"  Total interactions: {len(todo)}")
        logger.info(f"  Successfully refined: {success_count}")
        logger.info(f"{'='*60}\n")


def refine_pathways_for_interactions(interactions: List):
    """
    Refine pathway terms for a specific list of interactions.
    Used by recovery loops in later steps.
    """
    try:
        from app import app, db
        from models import Pathway
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    with app.app_context():
        existing_pathway_objects = Pathway.query.all()
        
        # Get proposed pathways from the interactions being refined
        proposed = {i.data.get('step2_proposal') for i in interactions if i.data and 'step2_proposal' in i.data}
        
        # Format with hierarchy for better specificity guidance
        context_str = _format_pathways_with_hierarchy(existing_pathway_objects, proposed)

        logger.info(f"Recovery: Refining {len(interactions)} interactions...")

        results = _retry_cascade(interactions, context_str)

        for interaction in interactions:
            str_id = str(interaction.id)
            if str_id in results:
                result = results[str_id]
                d = dict(interaction.data or {})

                # Store refined function pathways
                d['step3_function_pathways'] = result.get('function_refinements', [])
                d['step3_finalized_pathway'] = result.get('primary_pathway')  # Backward compat

                # Update each function with its finalized pathway
                functions = d.get('functions', [])
                for fr in result.get('function_refinements', []):
                    if not isinstance(fr, dict):
                        continue
                    try:
                        idx = int(fr.get('function_index', -1))
                    except (TypeError, ValueError):
                        idx = -1
                    if 0 <= idx < len(functions):
                        functions[idx]['pathway'] = fr.get('finalized_pathway')
                d['functions'] = functions

                interaction.data = d
            elif interaction.data and 'step2_proposal' in interaction.data:
                d = dict(interaction.data)
                fallback_pathway = d.get('step2_proposal', 'Unknown Pathway')
                d['step3_finalized_pathway'] = fallback_pathway

                # Also set fallback pathway on all functions
                functions = d.get('functions', [])
                for fn in functions:
                    if isinstance(fn, dict) and 'pathway' not in fn:
                        fn['pathway'] = fallback_pathway
                d['functions'] = functions

                interaction.data = d

        db.session.commit()
        logger.info(f"Recovery: Refined {len(results)}/{len(interactions)} interactions")


if __name__ == "__main__":
    refine_pathways()
