#!/usr/bin/env python3
"""
Step 7 Auto-Repair Functions
============================
Functions to automatically fix issues found during verification.
"""

import logging
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from collections import deque

from scripts.pathway_v2.step7_checks import Issue, Severity
from scripts.pathway_v2.step6_utils import STRICT_ROOTS
from scripts.pathway_v2.step6_utils import get_smart_rescue_parent

logger = logging.getLogger(__name__)


# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

@dataclass
class RepairResult:
    """Result of a repair operation."""
    issue: Issue
    success: bool
    action_taken: str
    error: Optional[str] = None


@dataclass
class RepairSummary:
    """Summary of all repairs attempted."""
    total_issues: int
    attempted: int
    succeeded: int
    failed: int
    skipped: int
    results: List[RepairResult]

    def add_result(self, result: RepairResult):
        self.results.append(result)
        if result.success:
            self.succeeded += 1
        else:
            self.failed += 1


# ==============================================================================
# REPAIR FUNCTIONS
# ==============================================================================

def repair_missing_root(db, Pathway, name: str) -> RepairResult:
    """Create a missing root pathway."""
    issue = Issue(
        check_name="all_roots_exist",
        severity=Severity.CRITICAL,
        message=f"Creating missing root: {name}",
        entity_type="pathway",
        auto_fixable=True
    )

    try:
        existing = Pathway.query.filter_by(name=name).first()
        if existing:
            # Root exists but maybe wrong level
            existing.hierarchy_level = 0
            existing.is_leaf = False
            db.session.commit()
            return RepairResult(
                issue=issue,
                success=True,
                action_taken=f"Set existing pathway '{name}' to level 0"
            )

        # Create new root
        root = Pathway(
            name=name,
            hierarchy_level=0,
            is_leaf=False,
            ai_generated=False
        )
        db.session.add(root)
        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Created root pathway '{name}'"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(
            issue=issue,
            success=False,
            action_taken="Failed to create root",
            error=str(e)
        )


def repair_root_level(db, Pathway, pathway_id: int) -> RepairResult:
    """Fix a root pathway that has wrong hierarchy level."""
    issue = Issue(
        check_name="all_roots_exist",
        severity=Severity.MEDIUM,
        message=f"Fixing root level for pathway {pathway_id}",
        entity_type="pathway",
        entity_id=pathway_id,
        auto_fixable=True
    )

    try:
        pw = db.session.get(Pathway, pathway_id)
        if not pw:
            return RepairResult(issue=issue, success=False,
                                action_taken="Pathway not found", error="Not found")

        old_level = pw.hierarchy_level
        pw.hierarchy_level = 0
        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Changed '{pw.name}' level from {old_level} to 0"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_usage_count(db, Pathway, PathwayInteraction, pathway_id: int) -> RepairResult:
    """Recalculate usage_count for a pathway."""
    issue = Issue(
        check_name="usage_count_accuracy",
        severity=Severity.LOW,
        message=f"Recalculating usage_count for pathway {pathway_id}",
        entity_type="pathway",
        entity_id=pathway_id,
        auto_fixable=True
    )

    try:
        pw = db.session.get(Pathway, pathway_id)
        if not pw:
            return RepairResult(issue=issue, success=False,
                                action_taken="Pathway not found", error="Not found")

        actual_count = PathwayInteraction.query.filter_by(pathway_id=pathway_id).count()
        old_count = pw.usage_count
        pw.usage_count = actual_count
        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Updated '{pw.name}' usage_count: {old_count} -> {actual_count}"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_hierarchy_level(db, Pathway, PathwayParent, pathway_id: int) -> RepairResult:
    """Recalculate hierarchy_level based on parent."""
    issue = Issue(
        check_name="levels_correct",
        severity=Severity.LOW,
        message=f"Recalculating level for pathway {pathway_id}",
        entity_type="pathway",
        entity_id=pathway_id,
        auto_fixable=True
    )

    try:
        pw = db.session.get(Pathway, pathway_id)
        if not pw:
            return RepairResult(issue=issue, success=False,
                                action_taken="Pathway not found", error="Not found")

        parent_link = PathwayParent.query.filter_by(child_pathway_id=pathway_id).first()
        if not parent_link:
            # No parent - should be root or orphan
            if pw.name in STRICT_ROOTS:
                pw.hierarchy_level = 0
            else:
                pw.hierarchy_level = -1  # Orphan
            db.session.commit()
            return RepairResult(
                issue=issue,
                success=True,
                action_taken=f"Set '{pw.name}' level to {pw.hierarchy_level} (no parent)"
            )

        parent = db.session.get(Pathway, parent_link.parent_pathway_id)
        if not parent:
            return RepairResult(issue=issue, success=False,
                                action_taken="Parent not found", error="Parent missing")

        old_level = pw.hierarchy_level
        pw.hierarchy_level = parent.hierarchy_level + 1
        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Updated '{pw.name}' level: {old_level} -> {pw.hierarchy_level}"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_is_leaf(db, Pathway, PathwayParent, pathway_id: int) -> RepairResult:
    """Recalculate is_leaf based on whether pathway has children."""
    issue = Issue(
        check_name="is_leaf_accurate",
        severity=Severity.LOW,
        message=f"Recalculating is_leaf for pathway {pathway_id}",
        entity_type="pathway",
        entity_id=pathway_id,
        auto_fixable=True
    )

    try:
        pw = db.session.get(Pathway, pathway_id)
        if not pw:
            return RepairResult(issue=issue, success=False,
                                action_taken="Pathway not found", error="Not found")

        has_children = PathwayParent.query.filter_by(parent_pathway_id=pathway_id).count() > 0
        old_value = pw.is_leaf
        pw.is_leaf = not has_children
        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Updated '{pw.name}' is_leaf: {old_value} -> {pw.is_leaf}"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_ancestor_ids(db, Pathway, PathwayParent, pathway_id: int) -> RepairResult:
    """Rebuild ancestor_ids JSONB from parent chain."""
    issue = Issue(
        check_name="ancestor_ids_accurate",
        severity=Severity.LOW,
        message=f"Rebuilding ancestor_ids for pathway {pathway_id}",
        entity_type="pathway",
        entity_id=pathway_id,
        auto_fixable=True
    )

    try:
        pw = db.session.get(Pathway, pathway_id)
        if not pw:
            return RepairResult(issue=issue, success=False,
                                action_taken="Pathway not found", error="Not found")

        # Build parent map
        parent_map = {
            link.child_pathway_id: link.parent_pathway_id
            for link in PathwayParent.query.all()
        }

        # Traverse upward
        ancestors = []
        current = pathway_id
        visited = set()

        while current in parent_map and current not in visited:
            visited.add(current)
            parent = parent_map[current]
            ancestors.append(parent)
            current = parent

        old_ancestors = pw.ancestor_ids
        pw.ancestor_ids = ancestors
        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Updated '{pw.name}' ancestors: {old_ancestors} -> {ancestors}"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_orphan_interaction(db, Interaction, Pathway, PathwayInteraction,
                               interaction_id: int) -> RepairResult:
    """Assign pathway to an orphaned interaction."""
    issue = Issue(
        check_name="interactions_have_pathway",
        severity=Severity.MEDIUM,
        message=f"Assigning pathway to interaction {interaction_id}",
        entity_type="interaction",
        entity_id=interaction_id,
        auto_fixable=True
    )

    try:
        interaction = db.session.get(Interaction, interaction_id)
        if not interaction:
            return RepairResult(issue=issue, success=False,
                                action_taken="Interaction not found", error="Not found")

        # Try to find pathway from interaction data
        assigned_pathway = None
        source = None

        if interaction.data:
            # Priority 1: step3_finalized_pathway
            if 'step3_finalized_pathway' in interaction.data:
                pw_name = interaction.data['step3_finalized_pathway']
                assigned_pathway = Pathway.query.filter_by(name=pw_name).first()
                if assigned_pathway:
                    source = "step3_finalized_pathway"

            # Priority 2: step2_proposal
            if not assigned_pathway and 'step2_proposal' in interaction.data:
                pw_name = interaction.data['step2_proposal']
                assigned_pathway = Pathway.query.filter_by(name=pw_name).first()
                if assigned_pathway:
                    source = "step2_proposal"

        # Priority 3: Fallback
        if not assigned_pathway:
            assigned_pathway = Pathway.query.filter_by(name="Protein Quality Control").first()
            source = "fallback"

        if not assigned_pathway:
            return RepairResult(issue=issue, success=False,
                                action_taken="No fallback pathway found", error="Missing fallback")

        # Create PathwayInteraction
        pi = PathwayInteraction(
            pathway_id=assigned_pathway.id,
            interaction_id=interaction_id,
            assignment_method=f'step7_repair_{source}'
        )
        db.session.add(pi)

        # Update interaction data
        if not interaction.data:
            interaction.data = {}
        interaction.data['_step7_repaired'] = True
        interaction.data['_step7_pathway'] = assigned_pathway.name

        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Assigned interaction {interaction_id} to '{assigned_pathway.name}' via {source}"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_dangling_pathway_link(db, PathwayInteraction, link_id: int) -> RepairResult:
    """Delete a PathwayInteraction pointing to missing pathway."""
    issue = Issue(
        check_name="pathway_references_valid",
        severity=Severity.MEDIUM,
        message=f"Deleting dangling link {link_id}",
        entity_type="pathway_interaction",
        entity_id=link_id,
        auto_fixable=True
    )

    try:
        pi = db.session.get(PathwayInteraction, link_id)
        if not pi:
            return RepairResult(issue=issue, success=False,
                                action_taken="Link not found", error="Not found")

        interaction_id = pi.interaction_id
        pathway_id = pi.pathway_id
        db.session.delete(pi)
        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Deleted link (interaction={interaction_id}, pathway={pathway_id})"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_stale_pathway_interaction(db, PathwayInteraction, link_id: int) -> RepairResult:
    """Delete a PathwayInteraction that has no matching claim."""
    issue = Issue(
        check_name="pathway_claim_consistency",
        severity=Severity.MEDIUM,
        message=f"Deleting stale PathwayInteraction {link_id}",
        entity_type="pathway_interaction",
        entity_id=link_id,
        auto_fixable=True,
    )

    try:
        pi = db.session.get(PathwayInteraction, link_id)
        if not pi:
            return RepairResult(issue=issue, success=False,
                                action_taken="Record not found", error="Not found")

        interaction_id = pi.interaction_id
        pathway_id = pi.pathway_id
        db.session.delete(pi)
        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=(
                f"Deleted stale PathwayInteraction "
                f"(interaction={interaction_id}, pathway={pathway_id})"
            ),
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_function_context_drift(db, Interaction, interaction_id: int) -> RepairResult:
    """Recompute Interaction.function_context from its claims.

    Mirrors the rollup invariant used on first write in
    utils/db_sync.py:_save_interaction — single non-null child context wins,
    two or more distinct non-null contexts collapse to 'mixed'. NULL claim
    contexts are ignored in the rollup so one stray NULL doesn't forever
    stamp the parent as 'mixed'.
    """
    from models import db, InteractionClaim

    issue = Issue(
        check_name="function_context_drift",
        severity=Severity.MEDIUM,
        message=f"Recomputing parent function_context for interaction {interaction_id}",
        entity_type="interaction",
        entity_id=interaction_id,
        auto_fixable=True,
    )
    try:
        interaction = db.session.get(Interaction, interaction_id)
        if interaction is None:
            return RepairResult(issue=issue, success=False,
                                action_taken="Not found",
                                error="No such interaction")
        contexts = {
            (c.function_context or "").strip().lower()
            for c in InteractionClaim.query.filter_by(
                interaction_id=interaction_id
            ).all()
        }
        contexts.discard("")
        old_ctx = interaction.function_context
        if not contexts:
            new_ctx = old_ctx  # no children — leave as-is
        elif len(contexts) == 1:
            new_ctx = next(iter(contexts))
        else:
            new_ctx = "mixed"

        if new_ctx != old_ctx:
            interaction.function_context = new_ctx
            db.session.commit()
        return RepairResult(
            issue=issue,
            success=True,
            action_taken=(
                f"Set interaction {interaction_id} function_context "
                f"'{old_ctx}' → '{new_ctx}'"
            ),
        )
    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_broken_parent_link(db, PathwayParent, Pathway, link_id: int) -> RepairResult:
    """Fix or delete a PathwayParent with missing parent."""
    issue = Issue(
        check_name="parent_exists",
        severity=Severity.HIGH,
        message=f"Fixing broken parent link {link_id}",
        entity_type="pathway_parent",
        entity_id=link_id,
        auto_fixable=True
    )

    try:
        link = db.session.get(PathwayParent, link_id)
        if not link:
            return RepairResult(issue=issue, success=False,
                                action_taken="Link not found", error="Not found")

        child = db.session.get(Pathway, link.child_pathway_id)
        if not child:
            db.session.delete(link)
            db.session.commit()
            return RepairResult(
                issue=issue,
                success=True,
                action_taken="Deleted link (child also missing)"
            )

        # Try to assign to fallback root
        fallback = Pathway.query.filter_by(name="Protein Quality Control").first()
        if fallback:
            link.parent_pathway_id = fallback.id
            db.session.commit()
            return RepairResult(
                issue=issue,
                success=True,
                action_taken=f"Reassigned '{child.name}' to Protein Quality Control"
            )

        # No fallback - delete link
        db.session.delete(link)
        db.session.commit()
        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Deleted broken link for '{child.name}'"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


def repair_orphan_pathway(db, Pathway, PathwayParent, pathway_id: int) -> RepairResult:
    """Attach an orphaned pathway to the semantically correct root using smart rescue."""
    issue = Issue(
        check_name="no_orphan_pathways",
        severity=Severity.MEDIUM,
        message=f"Rescuing orphaned pathway {pathway_id}",
        entity_type="pathway",
        entity_id=pathway_id,
        auto_fixable=True
    )

    try:
        pw = db.session.get(Pathway, pathway_id)
        if not pw:
            return RepairResult(issue=issue, success=False,
                                action_taken="Pathway not found", error="Not found")

        # Don't touch roots
        if pw.name in STRICT_ROOTS:
            pw.hierarchy_level = 0
            db.session.commit()
            return RepairResult(
                issue=issue,
                success=True,
                action_taken=f"Fixed root '{pw.name}' level to 0"
            )

        # Use SMART rescue to find the correct root based on semantic meaning
        smart_parent = get_smart_rescue_parent(pw.name, Pathway)
        if not smart_parent:
            # Fallback to Protein Quality Control if smart rescue fails
            smart_parent = Pathway.query.filter_by(name="Protein Quality Control").first()
            if not smart_parent:
                return RepairResult(issue=issue, success=False,
                                    action_taken="No fallback root", error="Missing fallback")

        # Check if already has parent link
        existing_link = PathwayParent.query.filter_by(child_pathway_id=pathway_id).first()
        if existing_link:
            existing_link.parent_pathway_id = smart_parent.id
        else:
            new_link = PathwayParent(
                child_pathway_id=pathway_id,
                parent_pathway_id=smart_parent.id,
                relationship_type='is_a'
            )
            db.session.add(new_link)

        pw.hierarchy_level = 1
        db.session.commit()

        return RepairResult(
            issue=issue,
            success=True,
            action_taken=f"Smart rescue: '{pw.name}' -> '{smart_parent.name}'"
        )

    except Exception as e:
        db.session.rollback()
        return RepairResult(issue=issue, success=False,
                            action_taken="Failed", error=str(e))


# ==============================================================================
# Chain pathway unification — B8 from the whole-codebase audit
# ==============================================================================

def repair_chain_pathway_fragmentation(db, chain_id: int, issue: Issue) -> RepairResult:
    """Unify pathway assignment across all claims in one IndirectChain.

    The check ``chain_pathway_consistency`` flags chains whose member claims
    span multiple pathways. The fix is a majority vote: pick the pathway
    with the most claims in this chain, reassign every claim to it, and
    resync the chain's denormalized ``pathway_name`` + any stale
    PathwayInteraction rows.

    Implementation delegates to ``quick_assign._unify_chain_claims_one``
    (extracted from the loop-body in _unify_all_chain_claims) so step7
    and the live pipeline agree byte-for-byte on how a chain gets
    unified.
    """
    try:
        # Defense-in-depth: the check is already env-gated and returns
        # zero issues when CHAIN_PATHWAY_UNIFY is off, so this repair
        # shouldn't fire at all in that case. But a direct caller (CLI,
        # test, future code) could still land here — bail explicitly
        # instead of silently overriding the user's diversity choice.
        import os
        if os.getenv("CHAIN_PATHWAY_UNIFY", "false").lower() != "true":
            return RepairResult(
                issue=issue,
                success=True,
                action_taken=(
                    f"Skipped chain {chain_id} unification — "
                    "CHAIN_PATHWAY_UNIFY disabled (per-hop diversity preserved)"
                ),
            )

        # Lazy import: quick_assign pulls in LLM dependencies we don't
        # want to load unless this repair actually fires.
        from scripts.pathway_v2.quick_assign import unify_one_chain_pathway

        unified = unify_one_chain_pathway(db, chain_id)
        return RepairResult(
            issue=issue,
            success=True,
            action_taken=(
                f"Unified {unified} claim(s) in IndirectChain {chain_id} "
                "onto the dominant pathway"
            ),
        )
    except Exception as e:
        db.session.rollback()
        logger.exception("repair_chain_pathway_fragmentation failed")
        return RepairResult(
            issue=issue,
            success=False,
            action_taken="Failed to unify chain pathway",
            error=str(e),
        )


# ==============================================================================
# BATCH REPAIRS
# ==============================================================================

def recalculate_all_levels(db, Pathway, PathwayParent) -> int:
    """Recalculate hierarchy_level for all pathways via BFS."""
    logger.info("Recalculating all hierarchy levels...")

    # Reset all to -1
    for pw in Pathway.query.all():
        pw.hierarchy_level = -1

    # Build child graph
    child_graph = {}
    for link in PathwayParent.query.all():
        if link.parent_pathway_id not in child_graph:
            child_graph[link.parent_pathway_id] = []
        child_graph[link.parent_pathway_id].append(link.child_pathway_id)

    # Initialize roots
    queue = deque()
    for pw in Pathway.query.all():
        if pw.name in STRICT_ROOTS:
            pw.hierarchy_level = 0
            queue.append(pw.id)

    # BFS
    updated = 0
    while queue:
        current_id = queue.popleft()
        current = db.session.get(Pathway, current_id)
        if not current:
            continue

        for child_id in child_graph.get(current_id, []):
            child = db.session.get(Pathway, child_id)
            if child and child.hierarchy_level == -1:
                child.hierarchy_level = current.hierarchy_level + 1
                queue.append(child_id)
                updated += 1

    db.session.commit()
    logger.info(f"Recalculated levels for {updated} pathways")
    return updated


def recalculate_all_usage_counts(db, Pathway, PathwayInteraction) -> int:
    """Recalculate usage_count for all pathways."""
    from sqlalchemy import func

    logger.info("Recalculating all usage counts...")

    # Get actual counts
    actual_counts = dict(
        db.session.query(
            PathwayInteraction.pathway_id,
            func.count(PathwayInteraction.id)
        ).group_by(PathwayInteraction.pathway_id).all()
    )

    updated = 0
    for pw in Pathway.query.all():
        actual = actual_counts.get(pw.id, 0)
        if pw.usage_count != actual:
            pw.usage_count = actual
            updated += 1

    db.session.commit()
    logger.info(f"Updated usage_count for {updated} pathways")
    return updated


def recalculate_all_is_leaf(db, Pathway, PathwayParent) -> int:
    """Recalculate is_leaf for all pathways."""
    logger.info("Recalculating all is_leaf flags...")

    # Get pathways that have children
    parents_with_children = set(
        row[0] for row in
        PathwayParent.query.with_entities(PathwayParent.parent_pathway_id).distinct().all()
    )

    updated = 0
    for pw in Pathway.query.all():
        should_be_leaf = pw.id not in parents_with_children
        if pw.is_leaf != should_be_leaf:
            pw.is_leaf = should_be_leaf
            updated += 1

    db.session.commit()
    logger.info(f"Updated is_leaf for {updated} pathways")
    return updated


def recalculate_all_ancestor_ids(db, Pathway, PathwayParent) -> int:
    """Recalculate ancestor_ids for all pathways."""
    logger.info("Recalculating all ancestor_ids...")

    # Build parent map
    parent_map = {
        link.child_pathway_id: link.parent_pathway_id
        for link in PathwayParent.query.all()
    }

    updated = 0
    for pw in Pathway.query.all():
        # Traverse upward
        ancestors = []
        current = pw.id
        visited = set()

        while current in parent_map and current not in visited:
            visited.add(current)
            parent = parent_map[current]
            ancestors.append(parent)
            current = parent

        # Type-safe comparison: ancestor_ids might be int/None/corrupted from JSONB
        stored = pw.ancestor_ids if isinstance(pw.ancestor_ids, list) else []
        if stored != ancestors:
            pw.ancestor_ids = ancestors
            updated += 1

    db.session.commit()
    logger.info(f"Updated ancestor_ids for {updated} pathways")
    return updated


# ==============================================================================
# H3: CHAIN INTEGRITY REPAIRS
# ==============================================================================

def backfill_all_claim_chain_ids(db) -> int:
    """Bulk-set ``claim.chain_id = claim.interaction.chain_id`` for any
    claim where the parent has a chain_id but the claim is NULL.

    This is the H3 auto-repair for ``check_claim_chain_id_backfill``. The
    fix is intentionally a single SQL UPDATE; it's the same operation
    that ``DatabaseSyncLayer._tag_claims_with_chain`` performs per-row,
    but executed in one statement so it can repair an entire database
    backlog without iterating Python objects.
    """
    from sqlalchemy import text

    logger.info("Backfilling missing claim.chain_id from parent interaction...")

    # Use the ORM session's connection so we run inside the active
    # transaction; the caller (run_auto_repairs) is responsible for the
    # commit.
    result = db.session.execute(text("""
        UPDATE interaction_claims AS ic
        SET chain_id = i.chain_id
        FROM interactions AS i
        WHERE ic.interaction_id = i.id
          AND ic.chain_id IS NULL
          AND i.chain_id IS NOT NULL
    """))
    db.session.commit()

    updated = result.rowcount or 0
    logger.info(f"Backfilled chain_id on {updated} claims")
    return updated


# ==============================================================================
# MASTER REPAIR RUNNER
# ==============================================================================

def run_auto_repairs(
    issues: List[Issue],
    db,
    Pathway,
    PathwayParent,
    PathwayInteraction,
    Interaction
) -> RepairSummary:
    """
    Run auto-repairs for all fixable issues.

    Returns RepairSummary with details of all repairs attempted.
    """
    summary = RepairSummary(
        total_issues=len(issues),
        attempted=0,
        succeeded=0,
        failed=0,
        skipped=0,
        results=[]
    )

    # First, run batch recalculations for LOW severity issues.
    # ONLY recalculate hierarchy if the LOW issues are actually about
    # hierarchy (levels, usage counts, is_leaf, ancestor_ids) — NOT if
    # they're just chain_id backfills which don't affect the pathway tree.
    low_issues = [i for i in issues if i.severity == Severity.LOW]
    hierarchy_issues = [
        i for i in low_issues
        if i.check_name not in ("claim_chain_id_backfill",)
    ]
    if hierarchy_issues:
        logger.info("Running batch recalculations for LOW severity hierarchy issues...")
        recalculate_all_levels(db, Pathway, PathwayParent)
        recalculate_all_usage_counts(db, Pathway, PathwayInteraction)
        recalculate_all_is_leaf(db, Pathway, PathwayParent)
        recalculate_all_ancestor_ids(db, Pathway, PathwayParent)
    elif low_issues:
        logger.info("LOW severity issues are chain-only — skipping hierarchy recalculation")

    # H3: chain_id backfill is also a batch operation. Run it whenever
    # the check produced any issues, regardless of how the LOW filter
    # bucketed them — the SQL is a single UPDATE so the cost is fixed.
    chain_id_issues = [i for i in issues if i.check_name == "claim_chain_id_backfill"]
    if chain_id_issues:
        logger.info("Backfilling missing claim.chain_id values...")
        backfilled = backfill_all_claim_chain_ids(db)
        # Mark every chain_id_backfill issue as repaired so the
        # per-issue loop below skips them (they're handled in bulk).
        backfill_issue = Issue(
            check_name="claim_chain_id_backfill",
            severity=Severity.LOW,
            message=f"Backfilled chain_id on {backfilled} claims",
            entity_type="interaction_claim",
            auto_fixable=True,
        )
        summary.add_result(RepairResult(
            issue=backfill_issue,
            success=True,
            action_taken=f"Bulk UPDATE — backfilled {backfilled} claim.chain_id rows",
        ))
        summary.attempted += 1

    # Process individual repairs for MEDIUM+ issues
    for issue in issues:
        if not issue.auto_fixable:
            summary.skipped += 1
            continue

        if issue.severity == Severity.LOW:
            # Already handled by batch recalculations
            summary.skipped += 1
            continue

        summary.attempted += 1

        try:
            result = None

            # Route to appropriate repair function
            if issue.check_name == "all_roots_exist" and "Missing root" in issue.message:
                name = issue.message.split(": ")[1] if ": " in issue.message else None
                if name:
                    result = repair_missing_root(db, Pathway, name)

            elif issue.check_name == "all_roots_exist":
                if issue.entity_id:
                    result = repair_root_level(db, Pathway, issue.entity_id)

            elif issue.check_name == "interactions_have_pathway":
                if issue.entity_id:
                    result = repair_orphan_interaction(
                        db, Interaction, Pathway, PathwayInteraction, issue.entity_id
                    )

            elif issue.check_name == "pathway_references_valid":
                if issue.entity_id:
                    result = repair_dangling_pathway_link(db, PathwayInteraction, issue.entity_id)

            elif issue.check_name == "pathway_claim_consistency":
                if issue.entity_id:
                    result = repair_stale_pathway_interaction(db, PathwayInteraction, issue.entity_id)

            elif issue.check_name == "parent_exists":
                if issue.entity_id:
                    result = repair_broken_parent_link(db, PathwayParent, Pathway, issue.entity_id)

            elif issue.check_name == "no_orphan_pathways":
                if issue.entity_id:
                    result = repair_orphan_pathway(db, Pathway, PathwayParent, issue.entity_id)

            elif issue.check_name == "chain_pathway_consistency":
                if issue.entity_id:
                    result = repair_chain_pathway_fragmentation(db, issue.entity_id, issue)

            elif issue.check_name == "function_context_drift":
                if issue.entity_id:
                    result = repair_function_context_drift(db, Interaction, issue.entity_id)

            if result:
                summary.add_result(result)
            else:
                summary.skipped += 1
                summary.attempted -= 1

        except Exception as e:
            logger.error(f"Error repairing issue: {e}")
            summary.add_result(RepairResult(
                issue=issue,
                success=False,
                action_taken="Exception during repair",
                error=str(e)
            ))

    return summary
