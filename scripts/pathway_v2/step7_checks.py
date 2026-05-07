#!/usr/bin/env python3
"""
Step 7 Verification Checks
==========================
Individual verification checks for the pathway pipeline.
Each check returns a CheckResult with details.
"""

import logging
from typing import List, Dict, Set, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

logger = logging.getLogger(__name__)


# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

class Severity(Enum):
    """Severity levels for verification issues."""
    LOW = "low"          # Can auto-fix, non-blocking
    MEDIUM = "medium"    # Can auto-fix, should review
    HIGH = "high"        # Cannot auto-fix, blocking
    CRITICAL = "critical"  # Abort immediately


@dataclass
class Issue:
    """A single verification issue."""
    check_name: str
    severity: Severity
    message: str
    entity_type: str  # 'pathway', 'interaction', 'link'
    entity_id: Optional[int] = None
    auto_fixable: bool = False
    fix_action: Optional[str] = None  # Description of fix


@dataclass
class CheckResult:
    """Result of a single verification check."""
    check_name: str
    passed: bool
    issues: List[Issue] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def add_issue(self, severity: Severity, message: str,
                  entity_type: str = "unknown", entity_id: int = None,
                  auto_fixable: bool = False, fix_action: str = None):
        self.issues.append(Issue(
            check_name=self.check_name,
            severity=severity,
            message=message,
            entity_type=entity_type,
            entity_id=entity_id,
            auto_fixable=auto_fixable,
            fix_action=fix_action
        ))
        if severity in (Severity.HIGH, Severity.CRITICAL):
            self.passed = False


# ==============================================================================
# CONSTANTS
# ==============================================================================

# Import canonical roots from the single source of truth (step6_utils)
from scripts.pathway_v2.step6_utils import STRICT_ROOTS


# ==============================================================================
# INTERACTION CHECKS
# ==============================================================================

def check_interactions_have_pathway(db, Interaction, PathwayInteraction,
                                     scope_interaction_ids=None) -> CheckResult:
    """Verify interactions have at least one PathwayInteraction record.

    When scope_interaction_ids is provided, only checks those specific
    interactions (used by quick-assign mode to skip re-validating the
    entire database).
    """
    result = CheckResult(check_name="interactions_have_pathway", passed=True)

    from sqlalchemy import text
    if scope_interaction_ids:
        id_csv = ",".join(str(int(i)) for i in scope_interaction_ids)
        orphans = db.session.execute(text(f"""
            SELECT i.id, pa.symbol as protein_a, pb.symbol as protein_b
            FROM interactions i
            JOIN proteins pa ON i.protein_a_id = pa.id
            JOIN proteins pb ON i.protein_b_id = pb.id
            LEFT JOIN pathway_interactions pi ON i.id = pi.interaction_id
            WHERE pi.id IS NULL AND i.id IN ({id_csv})
        """)).fetchall()
        result.stats['total_interactions'] = len(scope_interaction_ids)
    else:
        orphans = db.session.execute(text("""
            SELECT i.id, pa.symbol as protein_a, pb.symbol as protein_b
            FROM interactions i
            JOIN proteins pa ON i.protein_a_id = pa.id
            JOIN proteins pb ON i.protein_b_id = pb.id
            LEFT JOIN pathway_interactions pi ON i.id = pi.interaction_id
            WHERE pi.id IS NULL
        """)).fetchall()
        result.stats['total_interactions'] = Interaction.query.count()

    result.stats['orphaned_count'] = len(orphans)

    if orphans:
        result.passed = False
        for row in orphans[:20]:  # Limit to first 20
            result.add_issue(
                severity=Severity.MEDIUM,
                message=f"Interaction {row[0]} ({row[1]}<->{row[2]}) has no pathway",
                entity_type="interaction",
                entity_id=row[0],
                auto_fixable=True,
                fix_action="Assign pathway from step3_finalized_pathway or fallback"
            )

        if len(orphans) > 20:
            result.add_issue(
                severity=Severity.HIGH,
                message=f"...and {len(orphans) - 20} more orphaned interactions",
                entity_type="interaction"
            )

    return result


def check_pathway_references_valid(db, PathwayInteraction, Pathway) -> CheckResult:
    """
    Verify all PathwayInteraction.pathway_id references exist.
    """
    result = CheckResult(check_name="pathway_references_valid", passed=True)

    from sqlalchemy import text
    dangling = db.session.execute(text("""
        SELECT pi.id, pi.pathway_id, pi.interaction_id
        FROM pathway_interactions pi
        LEFT JOIN pathways p ON pi.pathway_id = p.id
        WHERE p.id IS NULL
    """)).fetchall()

    result.stats['total_links'] = PathwayInteraction.query.count()
    result.stats['dangling_count'] = len(dangling)

    if dangling:
        result.passed = False
        for row in dangling[:10]:
            result.add_issue(
                severity=Severity.MEDIUM,
                message=f"PathwayInteraction {row[0]} references missing pathway {row[1]}",
                entity_type="pathway_interaction",
                entity_id=row[0],
                auto_fixable=True,
                fix_action="Delete dangling record"
            )

    return result


def check_pathway_claim_consistency(db, PathwayInteraction, scope_interaction_ids=None) -> CheckResult:
    """Verify every PathwayInteraction record is backed by at least one matching claim.

    A stale PathwayInteraction (no claim with that interaction_id + pathway_id)
    causes interactors to appear in a pathway card but show zero SCI claims
    in the modal.
    """
    from models import InteractionClaim

    result = CheckResult(check_name="pathway_claim_consistency", passed=True)

    from sqlalchemy import text

    if scope_interaction_ids:
        id_csv = ",".join(str(int(i)) for i in scope_interaction_ids)
        stale = db.session.execute(text(f"""
            SELECT pi.id, pi.interaction_id, pi.pathway_id, p.name
            FROM pathway_interactions pi
            JOIN pathways p ON pi.pathway_id = p.id
            LEFT JOIN interaction_claims ic
                ON ic.interaction_id = pi.interaction_id
                AND ic.pathway_id = pi.pathway_id
            WHERE ic.id IS NULL
                AND pi.interaction_id IN ({id_csv})
        """)).fetchall()
    else:
        stale = db.session.execute(text("""
            SELECT pi.id, pi.interaction_id, pi.pathway_id, p.name
            FROM pathway_interactions pi
            JOIN pathways p ON pi.pathway_id = p.id
            LEFT JOIN interaction_claims ic
                ON ic.interaction_id = pi.interaction_id
                AND ic.pathway_id = pi.pathway_id
            WHERE ic.id IS NULL
        """)).fetchall()

    result.stats['stale_pathway_interactions'] = len(stale)

    if stale:
        result.passed = False
        for row in stale[:20]:
            result.add_issue(
                severity=Severity.MEDIUM,
                message=(
                    f"PathwayInteraction {row[0]} (interaction={row[1]}, "
                    f"pathway='{row[3]}') has no matching claim"
                ),
                entity_type="pathway_interaction",
                entity_id=row[0],
                auto_fixable=True,
                fix_action="Delete stale PathwayInteraction record",
            )

        if len(stale) > 20:
            result.add_issue(
                severity=Severity.MEDIUM,
                message=f"...and {len(stale) - 20} more stale PathwayInteraction records",
                entity_type="pathway_interaction",
            )

    return result


def check_interaction_data_consistency(Interaction) -> CheckResult:
    """
    Verify interaction.data has expected fields from pipeline.
    """
    result = CheckResult(check_name="interaction_data_consistency", passed=True)

    interactions = Interaction.query.all()
    missing_step2 = 0
    missing_step3 = 0
    step3_without_step2 = 0

    for i in interactions:
        if not i.data:
            continue

        has_step2 = 'step2_proposal' in i.data
        has_step3 = 'step3_finalized_pathway' in i.data

        if not has_step2:
            missing_step2 += 1
        if not has_step3:
            missing_step3 += 1
        if has_step3 and not has_step2:
            step3_without_step2 += 1

    result.stats['total'] = len(interactions)
    result.stats['missing_step2'] = missing_step2
    result.stats['missing_step3'] = missing_step3
    result.stats['step3_without_step2'] = step3_without_step2

    # Warnings for missing data
    if missing_step3 > 0:
        result.add_issue(
            severity=Severity.LOW,
            message=f"{missing_step3} interactions missing step3_finalized_pathway",
            entity_type="interaction",
            auto_fixable=False
        )

    if step3_without_step2 > 0:
        result.add_issue(
            severity=Severity.LOW,
            message=f"{step3_without_step2} interactions have step3 but not step2",
            entity_type="interaction"
        )

    return result


# ==============================================================================
# CHAIN INTEGRITY CHECKS (H3)
# ==============================================================================
#
# These three checks are the safety net for the refactor-6 chain schema.
# They catch failures in the write path that wouldn't be detected by any
# of the pre-existing pathway / hierarchy checks:
#
#  1. claim_chain_id_backfill — ``_tag_claims_with_chain`` is supposed to
#     wire every claim to its parent interaction's ``chain_id``. If a
#     writer path skipped the tagger (crash mid-sync, retry landed in a
#     different code path, legacy row backfilled without going through
#     sync_chain_relationships), claims end up NULL-``chain_id`` even
#     though their parent row has a valid ``chain_id``. The chain-view
#     reader still works because it prefers the parent ``chain_id``,
#     but any query that joins via ``claim.chain_id`` (e.g. the
#     ``/api/chain/<id>`` participants list) will miss those claims.
#     Fixable by a simple UPDATE.
#
#  2. chain_pathway_consistency — every claim under one ``IndirectChain``
#     describes one biological story, so they should all share one
#     pathway_id. If dedup or quick_assign split them across pathways,
#     the chain card in the frontend will render a fragmented story.
#     Report-only (not auto-fixable) because picking the "right" pathway
#     is a biological decision.
#
#  3. function_context_drift — ``Interaction.function_context`` is the
#     roll-up of all its claims' ``function_context`` values. If claims
#     were edited (dedup, pathway-assign, manual cleanup) without
#     updating the parent row, the parent's label stops matching its
#     children's labels. Frontend filtering can then produce wrong
#     results. Report-only because fixing requires deciding which label
#     is authoritative.
# ==============================================================================


def check_claim_chain_id_backfill(db, scope_interaction_ids=None) -> CheckResult:
    """Find claims that need ``chain_id`` backfilled from their parent row.

    A claim is considered out-of-sync when:
      • its parent ``Interaction.chain_id`` is NOT NULL, AND
      • the claim's own ``chain_id`` IS NULL.

    This happens when ``DatabaseSyncLayer._tag_claims_with_chain`` didn't
    run for that interaction, or ran before the claims were created.
    The fix is a bulk UPDATE; every affected claim inherits its parent's
    ``chain_id``.
    """
    result = CheckResult(check_name="claim_chain_id_backfill", passed=True)

    from sqlalchemy import text

    scope_filter = ""
    if scope_interaction_ids:
        id_csv = ",".join(str(int(i)) for i in scope_interaction_ids)
        scope_filter = f"AND ic.interaction_id IN ({id_csv})"

    rows = db.session.execute(text(f"""
        SELECT ic.id, ic.interaction_id, i.chain_id AS parent_chain_id
        FROM interaction_claims ic
        JOIN interactions i ON ic.interaction_id = i.id
        WHERE ic.chain_id IS NULL
          AND i.chain_id IS NOT NULL
          {scope_filter}
    """)).fetchall()

    result.stats['orphaned_claim_count'] = len(rows)

    if rows:
        # Cap how many individual issues we emit so the repair loop
        # doesn't get swamped — the batch repair handles the whole set
        # in one UPDATE anyway. The summary counter on the first issue
        # carries the full scope.
        for row in rows[:20]:
            result.add_issue(
                severity=Severity.LOW,
                message=(
                    f"Claim {row[0]} (interaction={row[1]}) has NULL chain_id "
                    f"but parent interaction chain_id={row[2]}"
                ),
                entity_type="interaction_claim",
                entity_id=row[0],
                auto_fixable=True,
                fix_action="Backfill claim.chain_id from parent interaction.chain_id",
            )
        if len(rows) > 20:
            result.add_issue(
                severity=Severity.LOW,
                message=f"...and {len(rows) - 20} more claims needing chain_id backfill",
                entity_type="interaction_claim",
                auto_fixable=True,
                fix_action="Batch backfill (handled by backfill_all_claim_chain_ids)",
            )

    return result


def check_chain_pathway_consistency(db, scope_interaction_ids=None) -> CheckResult:
    """Find IndirectChains whose member claims span multiple pathways.

    Every claim under one chain represents one biological story, so
    they should all share a single ``pathway_id``. If dedup or
    quick_assign split them across pathways, the chain card in the
    frontend renders a fragmented story. Not auto-fixable — picking
    the "right" pathway is a biology call.
    """
    # B8 — auto-fixable. The fix path lives in
    # scripts/pathway_v2/quick_assign.py ``_unify_all_chain_claims`` which
    # resolves fragmented chains by majority-vote pathway. We wire a
    # step7_repair branch to it below.
    result = CheckResult(check_name="chain_pathway_consistency", passed=True)

    # Respect the user's env-level choice. When CHAIN_PATHWAY_UNIFY is off
    # (the default — per-hop pathway diversity is a FEATURE of cross-
    # pathway cascades like query → kinase → autophagy → proteostasis),
    # flagging diverse chains as fragmented is a false positive. The
    # auto-fix (repair_chain_pathway_fragmentation) is also gated off
    # in step7_repairs.py so the two stay in lockstep.
    import os
    if os.getenv("CHAIN_PATHWAY_UNIFY", "false").lower() != "true":
        result.stats["skipped_reason"] = (
            "CHAIN_PATHWAY_UNIFY disabled — per-hop pathway diversity preserved"
        )
        return result

    from sqlalchemy import text

    scope_filter = ""
    if scope_interaction_ids:
        id_csv = ",".join(str(int(i)) for i in scope_interaction_ids)
        scope_filter = (
            f"WHERE ic.chain_id IN ("
            f"  SELECT chain_id FROM interaction_claims "
            f"  WHERE interaction_id IN ({id_csv}) AND chain_id IS NOT NULL"
            f")"
        )

    # For each chain_id, count the distinct non-NULL pathway_ids the
    # claims map to. A chain with >1 distinct non-NULL pathways is
    # fragmented. Pathway-name lookups happen in a follow-up query so
    # this query stays portable across PostgreSQL and SQLite (no
    # GROUP_CONCAT / STRING_AGG dialect split).
    rows = db.session.execute(text(f"""
        SELECT ic.chain_id, COUNT(DISTINCT ic.pathway_id) AS distinct_pathways
        FROM interaction_claims ic
        {scope_filter}
        GROUP BY ic.chain_id
        HAVING ic.chain_id IS NOT NULL
           AND COUNT(DISTINCT ic.pathway_id) > 1
    """)).fetchall()

    result.stats['fragmented_chain_count'] = len(rows)

    if rows:
        result.passed = False
        # Per-chain follow-up: small extra queries to fetch pathway
        # names for the first 20 fragmented chains so the issue
        # message is informative. Bounded by the same 20-issue cap.
        for row in rows[:20]:
            chain_id = row[0]
            distinct_pathways = row[1]
            pw_rows = db.session.execute(text("""
                SELECT DISTINCT p.name
                FROM interaction_claims ic
                JOIN pathways p ON ic.pathway_id = p.id
                WHERE ic.chain_id = :chain_id
            """), {"chain_id": chain_id}).fetchall()
            pathway_names = ", ".join(sorted(r[0] for r in pw_rows if r[0])) or "<none>"
            result.add_issue(
                severity=Severity.MEDIUM,
                message=(
                    f"IndirectChain {chain_id} has claims split across "
                    f"{distinct_pathways} pathways: {pathway_names}"
                ),
                entity_type="indirect_chain",
                entity_id=chain_id,
                auto_fixable=True,
                fix_action=(
                    "Majority-vote unification via "
                    "quick_assign._unify_all_chain_claims"
                ),
            )
        if len(rows) > 20:
            result.add_issue(
                severity=Severity.MEDIUM,
                message=f"...and {len(rows) - 20} more fragmented chains",
                entity_type="indirect_chain",
            )

    return result


def check_function_context_drift(db, scope_interaction_ids=None) -> CheckResult:
    """Find interactions whose function_context disagrees with their claims.

    The parent ``Interaction.function_context`` column is a roll-up of
    the contexts its child ``InteractionClaim.function_context`` values
    take on. A drift means someone edited claims without refreshing the
    parent label — frontend filters that read the parent row can then
    serve wrong results. Report-only: the caller decides which value
    is authoritative.
    """
    result = CheckResult(check_name="function_context_drift", passed=True)

    from sqlalchemy import text

    scope_filter = ""
    if scope_interaction_ids:
        id_csv = ",".join(str(int(i)) for i in scope_interaction_ids)
        scope_filter = f"AND i.id IN ({id_csv})"

    # An interaction is "drifted" when at least one of its claims has a
    # function_context value that doesn't match the parent — except for
    # the 'mixed' label on the parent, which is supposed to cover any
    # combination of children. Portable across PostgreSQL and SQLite:
    # COUNT(DISTINCT) and MIN are standard; the per-issue claim-context
    # list is fetched in a small follow-up query rather than via
    # GROUP_CONCAT/STRING_AGG to keep the SQL dialect-neutral.
    rows = db.session.execute(text(f"""
        SELECT i.id, i.function_context AS parent_ctx
        FROM interactions i
        JOIN interaction_claims ic ON ic.interaction_id = i.id
        WHERE i.function_context IS NOT NULL
          AND i.function_context != 'mixed'
          {scope_filter}
        GROUP BY i.id, i.function_context
        HAVING COUNT(DISTINCT COALESCE(ic.function_context, '<null>')) > 1
            OR MIN(COALESCE(ic.function_context, '<null>')) != i.function_context
    """)).fetchall()

    result.stats['drifted_interaction_count'] = len(rows)

    if rows:
        result.passed = False
        for row in rows[:20]:
            interaction_id = row[0]
            parent_ctx = row[1]
            ctx_rows = db.session.execute(text("""
                SELECT DISTINCT COALESCE(function_context, '<null>')
                FROM interaction_claims
                WHERE interaction_id = :iid
            """), {"iid": interaction_id}).fetchall()
            claim_ctxs = ", ".join(sorted(r[0] for r in ctx_rows))
            result.add_issue(
                severity=Severity.MEDIUM,
                message=(
                    f"Interaction {interaction_id} function_context='{parent_ctx}' "
                    f"but claims have [{claim_ctxs}]"
                ),
                entity_type="interaction",
                entity_id=interaction_id,
                auto_fixable=True,
                fix_action="Recompute parent function_context from child claim rollup",
            )
        if len(rows) > 20:
            result.add_issue(
                severity=Severity.MEDIUM,
                message=f"...and {len(rows) - 20} more drifted interactions",
                entity_type="interaction",
            )

    return result


# ==============================================================================
# PATHWAY CHECKS
# ==============================================================================

def check_all_roots_exist(Pathway) -> CheckResult:
    """
    Verify all 10 required root pathways exist at level 0.
    """
    result = CheckResult(check_name="all_roots_exist", passed=True)

    roots = Pathway.query.filter(Pathway.name.in_(STRICT_ROOTS)).all()
    found_names = {r.name for r in roots}
    missing = STRICT_ROOTS - found_names

    result.stats['expected_roots'] = len(STRICT_ROOTS)
    result.stats['found_roots'] = len(found_names)
    result.stats['missing'] = list(missing)

    if missing:
        result.passed = False
        for name in missing:
            result.add_issue(
                severity=Severity.CRITICAL,
                message=f"Missing root pathway: {name}",
                entity_type="pathway",
                auto_fixable=True,
                fix_action=f"Create root pathway '{name}' at level 0"
            )

    # Check levels
    wrong_level = [r for r in roots if r.hierarchy_level != 0]
    if wrong_level:
        for r in wrong_level:
            result.add_issue(
                severity=Severity.MEDIUM,
                message=f"Root '{r.name}' has level {r.hierarchy_level}, should be 0",
                entity_type="pathway",
                entity_id=r.id,
                auto_fixable=True,
                fix_action="Set hierarchy_level to 0"
            )

    return result


def check_no_duplicate_names(Pathway) -> CheckResult:
    """
    Verify no two pathways have the same name.
    """
    result = CheckResult(check_name="no_duplicate_names", passed=True)

    from sqlalchemy import func
    duplicates = Pathway.query.with_entities(
        Pathway.name, func.count(Pathway.id).label('count')
    ).group_by(Pathway.name).having(func.count(Pathway.id) > 1).all()

    result.stats['duplicate_count'] = len(duplicates)

    if duplicates:
        result.passed = False
        for name, count in duplicates:
            result.add_issue(
                severity=Severity.HIGH,
                message=f"Duplicate pathway name: '{name}' ({count} occurrences)",
                entity_type="pathway",
                auto_fixable=False  # Needs manual decision on which to keep
            )

    return result


def check_no_empty_names(Pathway) -> CheckResult:
    """
    Verify no pathways have empty or null names.
    """
    result = CheckResult(check_name="no_empty_names", passed=True)

    empty = Pathway.query.filter(
        (Pathway.name.is_(None)) | (Pathway.name == '')
    ).all()

    result.stats['empty_count'] = len(empty)

    if empty:
        result.passed = False
        for pw in empty:
            result.add_issue(
                severity=Severity.HIGH,
                message=f"Pathway {pw.id} has empty name",
                entity_type="pathway",
                entity_id=pw.id,
                auto_fixable=False
            )

    return result


def check_usage_count_accuracy(db, Pathway, PathwayInteraction) -> CheckResult:
    """
    Verify pathway.usage_count matches actual PathwayInteraction count.
    """
    result = CheckResult(check_name="usage_count_accuracy", passed=True)

    from sqlalchemy import func

    # Get actual counts
    actual_counts = dict(
        db.session.query(
            PathwayInteraction.pathway_id,
            func.count(PathwayInteraction.id)
        ).group_by(PathwayInteraction.pathway_id).all()
    )

    mismatches = []
    for pw in Pathway.query.all():
        actual = actual_counts.get(pw.id, 0)
        if pw.usage_count != actual:
            mismatches.append((pw.id, pw.name, pw.usage_count, actual))

    result.stats['total_pathways'] = Pathway.query.count()
    result.stats['mismatches'] = len(mismatches)

    if mismatches:
        for pw_id, name, old, actual in mismatches[:10]:
            result.add_issue(
                severity=Severity.LOW,
                message=f"Pathway '{name}' usage_count={old}, actual={actual}",
                entity_type="pathway",
                entity_id=pw_id,
                auto_fixable=True,
                fix_action=f"Update usage_count to {actual}"
            )

    return result


# ==============================================================================
# HIERARCHY CHECKS
# ==============================================================================

def check_no_cycles(PathwayParent) -> CheckResult:
    """
    Verify the hierarchy graph has no cycles.
    """
    result = CheckResult(check_name="no_cycles", passed=True)

    # Build graph
    graph = defaultdict(list)
    for link in PathwayParent.query.all():
        graph[link.child_pathway_id].append(link.parent_pathway_id)

    # DFS cycle detection
    def find_cycle(start):
        visited = set()
        rec_stack = set()
        path = []

        def dfs(node):
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for parent in graph.get(node, []):
                if parent in rec_stack:
                    cycle_start = path.index(parent)
                    return path[cycle_start:] + [parent]
                if parent not in visited:
                    result = dfs(parent)
                    if result:
                        return result

            path.pop()
            rec_stack.remove(node)
            return None

        return dfs(start)

    cycles = []
    visited_global = set()
    for node in graph.keys():
        if node not in visited_global:
            cycle = find_cycle(node)
            if cycle:
                cycles.append(cycle)
            visited_global.add(node)

    result.stats['cycle_count'] = len(cycles)

    if cycles:
        result.passed = False
        for cycle in cycles[:5]:
            result.add_issue(
                severity=Severity.CRITICAL,
                message=f"Cycle detected: {' -> '.join(map(str, cycle))}",
                entity_type="pathway",
                auto_fixable=False
            )

    return result


def check_single_parent(PathwayParent, Pathway) -> CheckResult:
    """
    Verify each non-root pathway has exactly one parent (tree structure).
    """
    result = CheckResult(check_name="single_parent", passed=True)

    from sqlalchemy import func

    # Count parents per child
    parent_counts = dict(
        PathwayParent.query.with_entities(
            PathwayParent.child_pathway_id,
            func.count(PathwayParent.parent_pathway_id)
        ).group_by(PathwayParent.child_pathway_id).all()
    )

    multi_parent = {
        child_id: count
        for child_id, count in parent_counts.items()
        if count > 1
    }

    result.stats['multi_parent_count'] = len(multi_parent)

    if multi_parent:
        # Get pathway names for better messages
        for child_id, count in list(multi_parent.items())[:10]:
            pw = Pathway.query.get(child_id)
            name = pw.name if pw else f"ID:{child_id}"
            result.add_issue(
                severity=Severity.MEDIUM,
                message=f"Pathway '{name}' has {count} parents (should be 1)",
                entity_type="pathway",
                entity_id=child_id,
                auto_fixable=True,
                fix_action="Use LLM to select best parent"
            )

    return result


def check_no_orphan_pathways(Pathway) -> CheckResult:
    """
    Verify no pathways are unreachable (hierarchy_level == -1).
    """
    result = CheckResult(check_name="no_orphan_pathways", passed=True)

    orphans = Pathway.query.filter_by(hierarchy_level=-1).all()

    result.stats['orphan_count'] = len(orphans)

    if orphans:
        result.passed = False
        for pw in orphans[:10]:
            result.add_issue(
                severity=Severity.MEDIUM,
                message=f"Pathway '{pw.name}' is orphaned (level=-1)",
                entity_type="pathway",
                entity_id=pw.id,
                auto_fixable=True,
                fix_action="Attach to nearest root"
            )

        if len(orphans) > 10:
            result.add_issue(
                severity=Severity.HIGH,
                message=f"...and {len(orphans) - 10} more orphaned pathways",
                entity_type="pathway"
            )

    return result


def check_parent_exists(db, PathwayParent, Pathway) -> CheckResult:
    """
    Verify every PathwayParent.parent_pathway_id references an existing pathway.
    """
    result = CheckResult(check_name="parent_exists", passed=True)

    from sqlalchemy import text
    broken = db.session.execute(text("""
        SELECT pp.id, pp.child_pathway_id, pp.parent_pathway_id
        FROM pathway_parents pp
        LEFT JOIN pathways p ON pp.parent_pathway_id = p.id
        WHERE p.id IS NULL
    """)).fetchall()

    result.stats['broken_links'] = len(broken)

    if broken:
        result.passed = False
        for row in broken:
            result.add_issue(
                severity=Severity.HIGH,
                message=f"PathwayParent {row[0]}: parent {row[2]} does not exist",
                entity_type="pathway_parent",
                entity_id=row[0],
                auto_fixable=True,
                fix_action="Delete broken link or reassign"
            )

    return result


def check_levels_correct(Pathway, PathwayParent) -> CheckResult:
    """
    Verify hierarchy_level = parent.hierarchy_level + 1 for all pathways.
    """
    result = CheckResult(check_name="levels_correct", passed=True)

    incorrect = []

    for link in PathwayParent.query.all():
        child = Pathway.query.get(link.child_pathway_id)
        parent = Pathway.query.get(link.parent_pathway_id)

        if not child or not parent:
            continue

        expected_level = parent.hierarchy_level + 1
        if child.hierarchy_level != expected_level:
            incorrect.append((child, parent, expected_level))

    result.stats['incorrect_levels'] = len(incorrect)

    if incorrect:
        for child, parent, expected in incorrect[:10]:
            result.add_issue(
                severity=Severity.LOW,
                message=f"Pathway '{child.name}' level={child.hierarchy_level}, "
                        f"should be {expected} (parent '{parent.name}' is level {parent.hierarchy_level})",
                entity_type="pathway",
                entity_id=child.id,
                auto_fixable=True,
                fix_action=f"Set hierarchy_level to {expected}"
            )

    return result


def check_ancestor_ids_accurate(Pathway, PathwayParent) -> CheckResult:
    """
    Verify ancestor_ids JSONB matches actual path to root.
    """
    result = CheckResult(check_name="ancestor_ids_accurate", passed=True)

    # Build parent lookup
    parent_map = {}
    for link in PathwayParent.query.all():
        parent_map[link.child_pathway_id] = link.parent_pathway_id

    def get_actual_ancestors(pw_id):
        ancestors = []
        current = pw_id
        visited = set()
        while current in parent_map and current not in visited:
            visited.add(current)
            parent = parent_map[current]
            ancestors.append(parent)
            current = parent
        return ancestors

    mismatches = []
    for pw in Pathway.query.all():
        actual = get_actual_ancestors(pw.id)
        # Type-safe: ancestor_ids might be int/None/corrupted from JSONB
        stored = pw.ancestor_ids if isinstance(pw.ancestor_ids, list) else []

        if set(actual) != set(stored):
            mismatches.append((pw, stored, actual))

    result.stats['mismatches'] = len(mismatches)

    if mismatches:
        for pw, stored, actual in mismatches[:5]:
            result.add_issue(
                severity=Severity.LOW,
                message=f"Pathway '{pw.name}' ancestor_ids mismatch: stored={stored}, actual={actual}",
                entity_type="pathway",
                entity_id=pw.id,
                auto_fixable=True,
                fix_action="Rebuild ancestor_ids from parent chain"
            )

    return result


def check_is_leaf_accurate(db, Pathway, PathwayParent) -> CheckResult:
    """
    Verify is_leaf flag matches whether pathway has children.
    """
    result = CheckResult(check_name="is_leaf_accurate", passed=True)

    from sqlalchemy import func

    # Get pathways that have children
    parents_with_children = set(
        row[0] for row in
        PathwayParent.query.with_entities(PathwayParent.parent_pathway_id).distinct().all()
    )

    incorrect = []
    for pw in Pathway.query.all():
        has_children = pw.id in parents_with_children
        should_be_leaf = not has_children

        if pw.is_leaf != should_be_leaf:
            incorrect.append((pw, should_be_leaf))

    result.stats['incorrect_count'] = len(incorrect)

    if incorrect:
        for pw, should_be in incorrect[:10]:
            result.add_issue(
                severity=Severity.LOW,
                message=f"Pathway '{pw.name}' is_leaf={pw.is_leaf}, should be {should_be}",
                entity_type="pathway",
                entity_id=pw.id,
                auto_fixable=True,
                fix_action=f"Set is_leaf to {should_be}"
            )

    return result


# ==============================================================================
# MASTER CHECK RUNNER
# ==============================================================================

def run_all_checks(db, Pathway, PathwayParent, PathwayInteraction, Interaction,
                    scope_interaction_ids=None) -> Dict[str, CheckResult]:
    """Run verification checks and return results.

    When scope_interaction_ids is provided, runs a reduced check set:
    only scoped interaction checks + cheap pathway checks, skipping
    expensive hierarchy checks.
    """
    results = {}

    # Interaction checks (scoped when IDs provided)
    logger.info("Running interaction checks...")
    results['interactions_have_pathway'] = check_interactions_have_pathway(
        db, Interaction, PathwayInteraction,
        scope_interaction_ids=scope_interaction_ids,
    )
    results['pathway_references_valid'] = check_pathway_references_valid(db, PathwayInteraction, Pathway)
    results['pathway_claim_consistency'] = check_pathway_claim_consistency(
        db, PathwayInteraction,
        scope_interaction_ids=scope_interaction_ids,
    )

    # H3: chain integrity checks — run in both scoped and full modes
    # because chain consistency is always relevant (the whole point of
    # refactor #6 is that chain state lives in one place).
    logger.info("Running chain integrity checks...")
    results['claim_chain_id_backfill'] = check_claim_chain_id_backfill(
        db, scope_interaction_ids=scope_interaction_ids,
    )
    results['chain_pathway_consistency'] = check_chain_pathway_consistency(
        db, scope_interaction_ids=scope_interaction_ids,
    )
    results['function_context_drift'] = check_function_context_drift(
        db, scope_interaction_ids=scope_interaction_ids,
    )

    if scope_interaction_ids is None:
        # Full mode — run all checks
        results['interaction_data_consistency'] = check_interaction_data_consistency(Interaction)

        logger.info("Running pathway checks...")
        results['all_roots_exist'] = check_all_roots_exist(Pathway)
        results['no_duplicate_names'] = check_no_duplicate_names(Pathway)
        results['no_empty_names'] = check_no_empty_names(Pathway)
        results['usage_count_accuracy'] = check_usage_count_accuracy(db, Pathway, PathwayInteraction)

        logger.info("Running hierarchy checks...")
        results['no_cycles'] = check_no_cycles(PathwayParent)
        results['single_parent'] = check_single_parent(PathwayParent, Pathway)
        results['no_orphan_pathways'] = check_no_orphan_pathways(Pathway)
        results['parent_exists'] = check_parent_exists(db, PathwayParent, Pathway)
        results['levels_correct'] = check_levels_correct(Pathway, PathwayParent)
        results['ancestor_ids_accurate'] = check_ancestor_ids_accurate(Pathway, PathwayParent)
        results['is_leaf_accurate'] = check_is_leaf_accurate(db, Pathway, PathwayParent)
    else:
        # Scoped mode — only cheap pathway checks
        logger.info("Running scoped pathway checks (skipping hierarchy)...")
        results['no_duplicate_names'] = check_no_duplicate_names(Pathway)

    return results


def get_all_issues(results: Dict[str, CheckResult]) -> List[Issue]:
    """Extract all issues from check results."""
    all_issues = []
    for result in results.values():
        all_issues.extend(result.issues)
    return all_issues


def get_issues_by_severity(results: Dict[str, CheckResult]) -> Dict[Severity, List[Issue]]:
    """Group issues by severity level."""
    by_severity = defaultdict(list)
    for issue in get_all_issues(results):
        by_severity[issue.severity].append(issue)
    return dict(by_severity)


def get_auto_fixable_issues(results: Dict[str, CheckResult]) -> List[Issue]:
    """Get all issues that can be auto-fixed."""
    return [i for i in get_all_issues(results) if i.auto_fixable]
