#!/usr/bin/env python3
"""
Unified Pathway Pipeline Entry Point
=====================================
Single function to run the complete V2 pathway pipeline (steps 1-7)
plus ontology enrichment.  Replaces the 7-import boilerplate pattern
in runner.py and routes/pipeline.py.
"""

import logging
import sys
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _apply_ontology_enrichment() -> int:
    """Enrich Pathway records that lack ontology_id using static mappings.

    Returns the number of pathways enriched.
    """
    from scripts.pathway_v2.ontology_mappings import enrich_pathway_with_ontology

    try:
        from app import app, db
        from models import Pathway
    except ImportError:
        logger.warning("Cannot import app/models for ontology enrichment")
        return 0

    with app.app_context():
        pathways = Pathway.query.filter(
            (Pathway.ontology_id.is_(None)) | (Pathway.ontology_id == "")
        ).all()

        enriched = 0
        for pw in pathways:
            match = enrich_pathway_with_ontology(pw.name)
            if match:
                pw.ontology_id = match["ontology_id"]
                pw.ontology_source = match["ontology_source"]
                if match.get("canonical_name"):
                    pw.canonical_term = match["canonical_name"]
                enriched += 1

        if enriched:
            db.session.commit()
            logger.info("Ontology enrichment: %d pathways updated", enriched)

    return enriched


def run_pathway_pipeline(
    *,
    mode: str = "standard",
    quick_assign: bool = False,
    interaction_ids: Optional[List[int]] = None,
    skip_siblings: bool = False,
    skip_reorganize: bool = False,
    skip_hierarchy: bool = False,
    auto_fix: bool = True,
) -> Dict:
    """Run the V2 pathway pipeline.

    Args:
        mode: "standard" (synchronous LLM calls) or "batch" (Batch API, 50% savings).
        quick_assign: If True, use DB-first matching instead of full pipeline.
            Runs: Init Roots → Quick Assign → Ontology Enrichment → Scoped Verify.
        interaction_ids: Scope to these interaction IDs (used with quick_assign).
        skip_siblings: Skip step 5 (sibling discovery).
        skip_reorganize: Skip step 6 (reorganize/dedup).
        skip_hierarchy: Skip steps 4-6 (hierarchy building entirely).
        auto_fix: Pass to step 7 verification.

    Returns dict with:
        passed: bool — verification passed
        status: str — human-readable summary
        steps_completed: list[str] — names of completed steps
        timing: dict[str, float] — seconds per step
        verification: dict — step 7 result
    """
    from scripts.pathway_v2.step1_init_roots import init_roots
    from scripts.pathway_v2.verify_pipeline import verify

    steps_completed: List[str] = []
    timing: Dict[str, float] = {}
    pipeline_start = time.time()

    def _run_step(name: str, fn, **kwargs):
        print(f">> {name}...", file=sys.stderr)
        t0 = time.time()
        result = fn(**kwargs)
        elapsed = time.time() - t0
        timing[name] = round(elapsed, 2)
        steps_completed.append(name)
        print(f"   done ({elapsed:.1f}s)", file=sys.stderr)
        return result

    # --- Quick Assign Mode (abbreviated pipeline) ---
    if quick_assign:
        from scripts.pathway_v2.quick_assign import quick_assign_pathways

        print(f"\n{'='*80}", file=sys.stderr)
        print(f"RUNNING QUICK PATHWAY ASSIGNMENT", file=sys.stderr)
        print(f"{'='*80}", file=sys.stderr)

        _run_step("Step 1: Init Roots", init_roots)

        quick_result = _run_step(
            "Quick Assign Pathways",
            quick_assign_pathways,
            interaction_ids=interaction_ids,
        )

        _run_step("Ontology Enrichment", _apply_ontology_enrichment)

        scope_ids = (quick_result or {}).get("processed_interaction_ids", interaction_ids)
        verification_result = _run_step(
            "Step 7: Verify Pipeline (scoped)",
            verify,
            auto_fix=auto_fix,
            scope_interaction_ids=scope_ids,
        )

        total_elapsed = time.time() - pipeline_start
        passed = bool(verification_result and verification_result.get("passed"))

        print(f"{'='*80}", file=sys.stderr)
        print(
            f"QUICK ASSIGN {'PASSED' if passed else 'FAILED'} "
            f"in {total_elapsed:.1f}s "
            f"({len(steps_completed)} steps)",
            file=sys.stderr,
        )
        print(f"{'='*80}\n", file=sys.stderr)

        return {
            "passed": passed,
            "status": "passed" if passed else "failed",
            "steps_completed": steps_completed,
            "timing": timing,
            "total_seconds": round(total_elapsed, 2),
            "verification": verification_result or {},
            "quick_assign": quick_result or {},
        }

    # --- Full Pipeline Mode ---
    from scripts.pathway_v2.step2_assign_initial_terms import assign_initial_terms
    from scripts.pathway_v2.step3_refine_pathways import refine_pathways
    from scripts.pathway_v2.step4_build_hierarchy_backwards import build_hierarchy
    from scripts.pathway_v2.step5_discover_siblings import discover_siblings
    from scripts.pathway_v2.step6_reorganize_pathways import reorganize_pathways

    print(f"\n{'='*80}", file=sys.stderr)
    print(f"RUNNING UNIFIED PATHWAY PIPELINE (mode={mode})", file=sys.stderr)
    print(f"{'='*80}", file=sys.stderr)

    # Step 1: Ensure root pathways exist
    _run_step("Step 1: Init Roots", init_roots)

    # Step 2: Assign initial pathway terms per function
    step2_kwargs = {}
    if mode == "batch":
        step2_kwargs["mode"] = "batch"
    _run_step("Step 2: Assign Initial Terms", assign_initial_terms, **step2_kwargs)

    # Step 3: Refine/standardize pathway names
    step3_kwargs = {}
    if mode == "batch":
        step3_kwargs["mode"] = "batch"
    _run_step("Step 3: Refine Pathways", refine_pathways, **step3_kwargs)

    # Ontology enrichment (new — applies V1 mappings to Pathway records)
    _run_step("Ontology Enrichment", _apply_ontology_enrichment)

    if not skip_hierarchy:
        # Step 4: Build hierarchy tree
        step4_kwargs = {}
        if mode == "batch":
            step4_kwargs["mode"] = "batch"
        _run_step("Step 4: Build Hierarchy", build_hierarchy, **step4_kwargs)

        # Step 5: Discover sibling pathways
        if not skip_siblings:
            step5_kwargs = {}
            if mode == "batch":
                step5_kwargs["mode"] = "batch"
            _run_step("Step 5: Discover Siblings", discover_siblings, **step5_kwargs)

        # Step 6: Reorganize (dedup, tree enforcement, hierarchy repair)
        if not skip_reorganize:
            _run_step("Step 6: Reorganize Pathways", reorganize_pathways)

    # Step 7: Verification
    verification_result = _run_step(
        "Step 7: Verify Pipeline", verify, auto_fix=auto_fix
    )

    total_elapsed = time.time() - pipeline_start
    passed = bool(verification_result and verification_result.get("passed"))

    print(f"{'='*80}", file=sys.stderr)
    print(
        f"PIPELINE {'PASSED' if passed else 'FAILED'} "
        f"in {total_elapsed:.1f}s "
        f"({len(steps_completed)} steps)",
        file=sys.stderr,
    )
    print(f"{'='*80}\n", file=sys.stderr)

    return {
        "passed": passed,
        "status": "passed" if passed else "failed",
        "steps_completed": steps_completed,
        "timing": timing,
        "total_seconds": round(total_elapsed, 2),
        "verification": verification_result or {},
    }
