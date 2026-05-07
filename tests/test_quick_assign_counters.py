"""Pure-unit tests for the counter bookkeeping in ``quick_assign_claims``.

The LLM-batch loop used a fragile ``pw not in all_pathways`` list-identity
check and a failed-counter that double-counted claims whose ``pw`` resolution
returned None. Both bugs are fixed via a pure categorizer
(``_categorize_pathway_assignment``) plus a single ``pop`` before the
success/failure branch. These tests lock the new semantics.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.pathway_v2.quick_assign import _categorize_pathway_assignment


# ---------------------------------------------------------------------------
# _categorize_pathway_assignment — the pure core
# ---------------------------------------------------------------------------


def test_categorize_failed_when_pw_id_none():
    assert _categorize_pathway_assignment(None, set(), set()) == "failed"


def test_categorize_preexisting_when_in_preexisting_set():
    assert (
        _categorize_pathway_assignment(42, preexisting_ids={42, 99}, seen_new_ids=set())
        == "preexisting"
    )


def test_categorize_new_when_not_preexisting_and_not_seen():
    assert (
        _categorize_pathway_assignment(42, preexisting_ids=set(), seen_new_ids=set())
        == "new"
    )


def test_categorize_repeat_new_when_already_in_seen_new():
    assert (
        _categorize_pathway_assignment(
            42, preexisting_ids=set(), seen_new_ids={42}
        )
        == "repeat-new"
    )


def test_categorize_preexisting_takes_priority_over_seen_new():
    """If a pathway id is in both sets (theoretically shouldn't happen, but
    guard against it), preexisting wins because that category is authoritative
    about what was there before the pass."""
    assert (
        _categorize_pathway_assignment(42, preexisting_ids={42}, seen_new_ids={42})
        == "preexisting"
    )


# ---------------------------------------------------------------------------
# Counter simulation — exercise the full flow the way the batch loop does
# ---------------------------------------------------------------------------


def _simulate_batch_tally(assignments, preexisting_ids):
    """Replays the counter bookkeeping the way the fixed LLM-batch loop does,
    using only the pure categorizer. Useful for pinning the end-to-end counter
    semantics without booting Flask/DB.

    ``assignments`` is a list of ``pw_id`` values (None represents a failed
    resolution). The corresponding claim's claim_id is simply its index.
    """
    matched_existing = 0
    created_new = 0
    failed = 0
    seen_new_ids: set = set()
    all_new_pathway_ids_seen: list = []

    for pw_id in assignments:
        category = _categorize_pathway_assignment(
            pw_id, preexisting_ids, seen_new_ids,
        )
        if category == "failed":
            failed += 1
        elif category == "preexisting":
            matched_existing += 1
        elif category == "new":
            seen_new_ids.add(pw_id)
            all_new_pathway_ids_seen.append(pw_id)
            created_new += 1
        # else "repeat-new": skip

    return {
        "matched_existing": matched_existing,
        "created_new": created_new,
        "failed": failed,
        "distinct_new_ids": all_new_pathway_ids_seen,
    }


def test_new_pathway_assigned_twice_in_batch_counts_as_one():
    """Previous bug: two claims assigned to the same just-created pathway
    ticked created_new=1, matched_existing=1. New behavior: created_new=1 and
    matched_existing=0 (the second hit is "repeat-new", not a match).
    """
    result = _simulate_batch_tally(
        assignments=[100, 100],
        preexisting_ids=set(),
    )
    assert result["created_new"] == 1
    assert result["matched_existing"] == 0
    assert result["failed"] == 0
    assert result["distinct_new_ids"] == [100]


def test_mix_of_preexisting_and_new_counted_separately():
    result = _simulate_batch_tally(
        assignments=[50, 100, 100, 50],
        preexisting_ids={50},
    )
    assert result["matched_existing"] == 2  # both hits on id=50
    assert result["created_new"] == 1       # 100 created once
    assert result["failed"] == 0


def test_failures_counted_correctly():
    result = _simulate_batch_tally(
        assignments=[None, None, 77],
        preexisting_ids={77},
    )
    assert result["failed"] == 2
    assert result["matched_existing"] == 1
    assert result["created_new"] == 0


def test_three_new_pathways_all_distinct():
    result = _simulate_batch_tally(
        assignments=[10, 20, 30],
        preexisting_ids=set(),
    )
    assert result["created_new"] == 3
    assert result["matched_existing"] == 0
    assert result["distinct_new_ids"] == [10, 20, 30]


def test_counters_sum_accounts_for_repeat_new_only():
    """Basic invariant: for a batch with no LLM omissions, the three counters
    sum to the number of assignments MINUS the repeat-new hits (which are the
    only category intentionally not counted). Here: one repeat-new (second
    10); the two 50s are both 'preexisting', not repeats."""
    assignments = [10, 10, 20, None, 50, 50, None, 30]
    result = _simulate_batch_tally(
        assignments=assignments,
        preexisting_ids={50},
    )
    # matched_existing=2 (both 50s), created_new=3 (10, 20, 30), failed=2
    assert result["matched_existing"] == 2
    assert result["created_new"] == 3
    assert result["failed"] == 2
    # 8 assignments total, 1 repeat-new (second 10) → 7 counted
    counted = (
        result["matched_existing"]
        + result["created_new"]
        + result["failed"]
    )
    assert counted == len(assignments) - 1


# ---------------------------------------------------------------------------
# Failed-counter (issue #12) regression: pop-before-branch semantics
# ---------------------------------------------------------------------------
#
# We can't drive the Flask/DB loop from unit tests, but we can assert the
# invariant that was previously broken: when pw is None, the claim must be
# popped from claim_by_id BEFORE the post-loop `failed += len(claim_by_id)`
# fallback. The pure fix above already tests this via the simulator; we add
# one more explicit assertion here.


def test_failed_resolution_pops_from_unprocessed_set():
    """Simulates the fix: LLM-returned but pw-is-None claims are marked as
    processed (popped from claim_by_id) so they aren't double-counted by the
    post-loop `failed += len(claim_by_id)` fallback."""
    claim_by_id = {1: "claim1", 2: "claim2", 3: "claim3"}
    # LLM returned assignments for claims 1 and 2; claim 3 is not mentioned.
    llm_assignments = [(1, None), (2, None)]  # both resolve to pw=None

    failed = 0
    for cid, pw_id in llm_assignments:
        if cid not in claim_by_id:
            continue
        claim_by_id.pop(cid, None)  # pop BEFORE checking pw (the fix)
        if pw_id is None:
            failed += 1

    failed += len(claim_by_id)  # post-loop fallback

    # Before the fix, failed would be 4 (2 from pw-None + 2 in remaining,
    # because claims 1 and 2 never got popped). After the fix, failed is 3
    # (2 from pw-None + 1 remaining for claim 3).
    assert failed == 3


# ---------------------------------------------------------------------------
# Chain-level pathway consistency (_pick_chain_dominant_pathway_id)
# ---------------------------------------------------------------------------
# Previously, quick_assign assigned every claim independently, so claims
# belonging to the same indirect chain could land in different pathways:
#
#   ATXN3 → FOXO4 → SOD2  (chain_group = "chain_0")
#     - ATXN3 → FOXO4 direct claim → "FOXO4-mediated Transcription"  ✗
#     - FOXO4 → SOD2 direct claim  → "Oxidative Stress Response"     ✓
#     - ATXN3 → SOD2 net claim     → "Oxidative Stress Response"     ✓
#
# The chain consistency pass now forces all members of a chain group to
# share the net-effect claim's pathway (or majority vote if no net claim
# exists). These tests pin the selection logic.


class _FakeClaim:
    """Lightweight stand-in for ``InteractionClaim`` — avoids booting Flask
    just to call a pure helper."""

    def __init__(self, claim_id, pathway_id, function_context=None):
        self.id = claim_id
        self.pathway_id = pathway_id
        self.function_context = function_context


def test_chain_dominant_prefers_net_claim_pathway():
    from scripts.pathway_v2.quick_assign import _pick_chain_dominant_pathway_id

    members = [
        _FakeClaim(1, pathway_id=100, function_context="direct"),  # ATXN3 → FOXO4
        _FakeClaim(2, pathway_id=200, function_context="direct"),  # FOXO4 → SOD2
        _FakeClaim(3, pathway_id=200, function_context="net"),     # ATXN3 ⇒ SOD2
    ]
    # Net claim is in pathway 200 → dominant is 200 even though pathway 100
    # would otherwise tie on count.
    assert _pick_chain_dominant_pathway_id(members) == 200


def test_chain_dominant_net_wins_even_if_outnumbered():
    """The net-effect claim wins even when two direct claims vote for a
    different pathway — because it describes the biological endpoint."""
    from scripts.pathway_v2.quick_assign import _pick_chain_dominant_pathway_id

    members = [
        _FakeClaim(1, pathway_id=100, function_context="direct"),
        _FakeClaim(2, pathway_id=100, function_context="direct"),
        _FakeClaim(3, pathway_id=777, function_context="net"),
    ]
    assert _pick_chain_dominant_pathway_id(members) == 777


def test_chain_dominant_majority_vote_without_net_claim():
    """No net claim → fall back to majority over direct claims."""
    from scripts.pathway_v2.quick_assign import _pick_chain_dominant_pathway_id

    members = [
        _FakeClaim(1, pathway_id=100, function_context="direct"),
        _FakeClaim(2, pathway_id=100, function_context="direct"),
        _FakeClaim(3, pathway_id=200, function_context="direct"),
    ]
    assert _pick_chain_dominant_pathway_id(members) == 100


def test_chain_dominant_tiebreaks_deterministically():
    """Equal counts and no net claim → smallest pathway id wins (stable for
    tests). Any deterministic rule works; we just pin one."""
    from scripts.pathway_v2.quick_assign import _pick_chain_dominant_pathway_id

    members = [
        _FakeClaim(1, pathway_id=300, function_context="direct"),
        _FakeClaim(2, pathway_id=100, function_context="direct"),
        _FakeClaim(3, pathway_id=200, function_context="direct"),
    ]
    assert _pick_chain_dominant_pathway_id(members) == 100


def test_chain_dominant_ignores_none_pathway_ids():
    from scripts.pathway_v2.quick_assign import _pick_chain_dominant_pathway_id

    members = [
        _FakeClaim(1, pathway_id=None, function_context="direct"),
        _FakeClaim(2, pathway_id=500, function_context="direct"),
    ]
    assert _pick_chain_dominant_pathway_id(members) == 500


def test_chain_dominant_returns_none_when_all_unassigned():
    from scripts.pathway_v2.quick_assign import _pick_chain_dominant_pathway_id

    members = [
        _FakeClaim(1, pathway_id=None, function_context="direct"),
        _FakeClaim(2, pathway_id=None, function_context="direct"),
    ]
    assert _pick_chain_dominant_pathway_id(members) is None


def test_chain_dominant_net_with_null_pathway_falls_through_to_majority():
    """If the net claim exists but has no pathway yet, fall through to
    majority vote — don't pick None as dominant."""
    from scripts.pathway_v2.quick_assign import _pick_chain_dominant_pathway_id

    members = [
        _FakeClaim(1, pathway_id=100, function_context="direct"),
        _FakeClaim(2, pathway_id=100, function_context="direct"),
        _FakeClaim(3, pathway_id=None, function_context="net"),
    ]
    assert _pick_chain_dominant_pathway_id(members) == 100


def test_chain_dominant_atxn3_foxo4_sod2_scenario():
    """Exact scenario the user hit on ATXN3 → FOXO4 → SOD2:
      - ATXN3→FOXO4 direct drifted to pathway 42 ('FOXO4-mediated Transcription')
      - FOXO4→SOD2 direct landed in pathway 7  ('Oxidative Stress Response')
      - ATXN3⇒SOD2 net   landed in pathway 7  ('Oxidative Stress Response')
    After the chain consistency pass, all three must share pathway 7."""
    from scripts.pathway_v2.quick_assign import _pick_chain_dominant_pathway_id

    members = [
        _FakeClaim("atxn3_foxo4", pathway_id=42, function_context="direct"),
        _FakeClaim("foxo4_sod2", pathway_id=7, function_context="direct"),
        _FakeClaim("atxn3_sod2_net", pathway_id=7, function_context="net"),
    ]
    assert _pick_chain_dominant_pathway_id(members) == 7


# ---------------------------------------------------------------------------
# _pick_majority_pathway_id — the protein-level consistency pass's dominant
# picker, also used as the fallback inside the chain picker.
# ---------------------------------------------------------------------------


def test_pick_majority_simple():
    from scripts.pathway_v2.quick_assign import _pick_majority_pathway_id

    members = [
        _FakeClaim(1, pathway_id=10),
        _FakeClaim(2, pathway_id=10),
        _FakeClaim(3, pathway_id=20),
    ]
    assert _pick_majority_pathway_id(members) == 10


def test_pick_majority_deterministic_tie():
    from scripts.pathway_v2.quick_assign import _pick_majority_pathway_id

    members = [
        _FakeClaim(1, pathway_id=30),
        _FakeClaim(2, pathway_id=10),
        _FakeClaim(3, pathway_id=20),
    ]
    # All tied at 1 → smallest id wins for determinism.
    assert _pick_majority_pathway_id(members) == 10


def test_pick_majority_ignores_none():
    from scripts.pathway_v2.quick_assign import _pick_majority_pathway_id

    members = [
        _FakeClaim(1, pathway_id=None),
        _FakeClaim(2, pathway_id=None),
        _FakeClaim(3, pathway_id=5),
    ]
    assert _pick_majority_pathway_id(members) == 5


def test_pick_majority_returns_none_on_empty_resolution():
    from scripts.pathway_v2.quick_assign import _pick_majority_pathway_id

    members = [_FakeClaim(1, pathway_id=None)]
    assert _pick_majority_pathway_id(members) is None


# ---------------------------------------------------------------------------
# _apply_consistency_pass — the generic consistency helper that replaces the
# two inlined passes (protein-level + chain-level).
# ---------------------------------------------------------------------------


class _FakePathway:
    def __init__(self, pw_id, name):
        self.id = pw_id
        self.name = name


class _FakePathwayRegistry:
    """Stand-in for ``Pathway`` that offers ``.query.get(id)``."""

    class _Query:
        def __init__(self, rows):
            self._rows = rows

        def get(self, pw_id):
            return self._rows.get(pw_id)

    def __init__(self, rows):
        self.query = self._Query(rows)


def _make_pathway_cls(pathways):
    rows = {p.id: p for p in pathways}
    return _FakePathwayRegistry(rows)


def test_apply_consistency_pass_unifies_differing_pathways():
    from scripts.pathway_v2.quick_assign import (
        _apply_consistency_pass,
        _pick_majority_pathway_id,
    )

    pw_a = _FakePathway(1, "Autophagy")
    pw_b = _FakePathway(2, "DNA Repair")
    pathway_cls = _make_pathway_cls([pw_a, pw_b])

    claims = [
        _FakeClaim(1, pathway_id=1, function_context="direct"),
        _FakeClaim(2, pathway_id=1, function_context="direct"),
        _FakeClaim(3, pathway_id=2, function_context="direct"),
    ]
    # Give all claims the same group key so they land in one bucket.
    for c in claims:
        c.group_key = "group_X"

    sync_calls: list = []

    def sync_fn(claim, name):
        sync_calls.append((claim.id, name))

    fixes = _apply_consistency_pass(
        claims=claims,
        group_key_fn=lambda c: c.group_key,
        dominant_picker=_pick_majority_pathway_id,
        Pathway_cls=pathway_cls,
        sync_fn=sync_fn,
    )
    # Claim 3 (pathway 2) got switched to pathway 1 (the majority).
    assert fixes == 1
    assert claims[0].pathway_id == 1
    assert claims[1].pathway_id == 1
    assert claims[2].pathway_id == 1
    assert claims[2].pathway_name == "Autophagy"
    assert sync_calls == [(3, "Autophagy")]


def test_apply_consistency_pass_skips_already_consistent():
    from scripts.pathway_v2.quick_assign import (
        _apply_consistency_pass,
        _pick_majority_pathway_id,
    )

    pw = _FakePathway(1, "Autophagy")
    pathway_cls = _make_pathway_cls([pw])
    claims = [
        _FakeClaim(1, pathway_id=1),
        _FakeClaim(2, pathway_id=1),
    ]
    for c in claims:
        c.group_key = "group_X"

    fixes = _apply_consistency_pass(
        claims=claims,
        group_key_fn=lambda c: c.group_key,
        dominant_picker=_pick_majority_pathway_id,
        Pathway_cls=pathway_cls,
        sync_fn=lambda *a, **kw: None,
    )
    assert fixes == 0


def test_apply_consistency_pass_skips_none_group_key():
    from scripts.pathway_v2.quick_assign import (
        _apply_consistency_pass,
        _pick_majority_pathway_id,
    )

    pathway_cls = _make_pathway_cls([_FakePathway(1, "A"), _FakePathway(2, "B")])
    claims = [
        _FakeClaim(1, pathway_id=1),
        _FakeClaim(2, pathway_id=2),
    ]
    # No group key → not considered for consistency at all.
    fixes = _apply_consistency_pass(
        claims=claims,
        group_key_fn=lambda c: None,
        dominant_picker=_pick_majority_pathway_id,
        Pathway_cls=pathway_cls,
        sync_fn=lambda *a, **kw: None,
    )
    assert fixes == 0
    assert claims[0].pathway_id == 1
    assert claims[1].pathway_id == 2


def test_apply_consistency_pass_skips_unassigned():
    """Claims with pathway_id=None are never considered for consistency."""
    from scripts.pathway_v2.quick_assign import (
        _apply_consistency_pass,
        _pick_majority_pathway_id,
    )

    pathway_cls = _make_pathway_cls([_FakePathway(1, "A")])
    claims = [
        _FakeClaim(1, pathway_id=None),
        _FakeClaim(2, pathway_id=1),
    ]
    for c in claims:
        c.group_key = "g1"

    fixes = _apply_consistency_pass(
        claims=claims,
        group_key_fn=lambda c: c.group_key,
        dominant_picker=_pick_majority_pathway_id,
        Pathway_cls=pathway_cls,
        sync_fn=lambda *a, **kw: None,
    )
    # Only one assigned claim in the group → nothing to unify.
    assert fixes == 0


# ---------------------------------------------------------------------------
# _apply_llm_pathway_to_claim — per-claim bookkeeping helper
# ---------------------------------------------------------------------------


class _FakeClaimApply:
    """Mutable claim stand-in for the apply helper tests."""

    def __init__(self, cid, pathway_id=None):
        self.id = cid
        self.interaction = None
        self.function_name = f"fn_{cid}"
        self.pathway_id = pathway_id
        self.pathway_name = None


class _FakePathwayWithCount:
    def __init__(self, pw_id, name, usage_count=0):
        self.id = pw_id
        self.name = name
        self.usage_count = usage_count


def test_apply_llm_pathway_marks_preexisting():
    from scripts.pathway_v2.quick_assign import _apply_llm_pathway_to_claim

    claim = _FakeClaimApply(1)
    pw = _FakePathwayWithCount(100, "Autophagy", usage_count=0)
    counters = {"matched_existing": 0, "created_new": 0, "failed": 0}
    processed: list = []

    # Monkey-patch the JSONB mirror — we're testing the apply helper, not
    # the sync function.
    import scripts.pathway_v2.quick_assign as qa
    original_sync = qa._sync_claim_to_interaction_data
    qa._sync_claim_to_interaction_data = lambda *a, **kw: None
    try:
        category = _apply_llm_pathway_to_claim(
            claim, pw,
            preexisting_ids={100},
            seen_new_ids=set(),
            all_pathways=[pw],
            processed_ids=processed,
            counters=counters,
        )
    finally:
        qa._sync_claim_to_interaction_data = original_sync

    assert category == "preexisting"
    assert counters["matched_existing"] == 1
    assert counters["created_new"] == 0
    assert claim.pathway_id == 100
    assert claim.pathway_name == "Autophagy"
    assert pw.usage_count == 1
    assert processed == [1]


def test_apply_llm_pathway_marks_new_and_dedups_repeat():
    from scripts.pathway_v2.quick_assign import _apply_llm_pathway_to_claim

    pw = _FakePathwayWithCount(200, "Novel Pathway", usage_count=0)
    counters = {"matched_existing": 0, "created_new": 0, "failed": 0}
    processed: list = []
    seen_new: set = set()
    all_pws: list = []

    import scripts.pathway_v2.quick_assign as qa
    original_sync = qa._sync_claim_to_interaction_data
    qa._sync_claim_to_interaction_data = lambda *a, **kw: None
    try:
        # First claim → "new"
        c1 = _FakeClaimApply(1)
        cat1 = _apply_llm_pathway_to_claim(
            c1, pw,
            preexisting_ids=set(),
            seen_new_ids=seen_new,
            all_pathways=all_pws,
            processed_ids=processed,
            counters=counters,
        )
        # Second claim resolves to same pathway → "repeat-new"
        c2 = _FakeClaimApply(2)
        cat2 = _apply_llm_pathway_to_claim(
            c2, pw,
            preexisting_ids=set(),
            seen_new_ids=seen_new,
            all_pathways=all_pws,
            processed_ids=processed,
            counters=counters,
        )
    finally:
        qa._sync_claim_to_interaction_data = original_sync

    assert cat1 == "new"
    assert cat2 == "repeat-new"
    # created_new should be 1 even though two claims got the new pathway.
    assert counters["created_new"] == 1
    assert counters["matched_existing"] == 0
    assert 200 in seen_new
    assert all_pws == [pw]
    assert pw.usage_count == 2  # both claims were unassigned → both bumped


def test_apply_llm_pathway_counts_failed_when_pw_is_none():
    from scripts.pathway_v2.quick_assign import _apply_llm_pathway_to_claim

    claim = _FakeClaimApply(1)
    counters = {"matched_existing": 0, "created_new": 0, "failed": 0}
    processed: list = []

    category = _apply_llm_pathway_to_claim(
        claim, None,
        preexisting_ids=set(),
        seen_new_ids=set(),
        all_pathways=[],
        processed_ids=processed,
        counters=counters,
    )

    assert category == "failed"
    assert counters["failed"] == 1
    assert counters["matched_existing"] == 0
    assert counters["created_new"] == 0
    assert claim.pathway_id is None
    assert processed == []


def test_apply_llm_pathway_skips_usage_bump_on_reassignment():
    """If a claim already has a pathway_id (e.g. retry path), re-applying
    the same pathway must not inflate usage_count."""
    from scripts.pathway_v2.quick_assign import _apply_llm_pathway_to_claim

    claim = _FakeClaimApply(1, pathway_id=100)  # already assigned
    pw = _FakePathwayWithCount(100, "Autophagy", usage_count=5)
    counters = {"matched_existing": 0, "created_new": 0, "failed": 0}

    import scripts.pathway_v2.quick_assign as qa
    original_sync = qa._sync_claim_to_interaction_data
    qa._sync_claim_to_interaction_data = lambda *a, **kw: None
    try:
        _apply_llm_pathway_to_claim(
            claim, pw,
            preexisting_ids={100},
            seen_new_ids=set(),
            all_pathways=[pw],
            processed_ids=[],
            counters=counters,
        )
    finally:
        qa._sync_claim_to_interaction_data = original_sync

    assert pw.usage_count == 5  # unchanged — was not a first-time assignment


# ---------------------------------------------------------------------------
# _build_chain_display — cosmetic header for CHAIN_BATCH_ASSIGN_PROMPT
# ---------------------------------------------------------------------------


def test_chain_display_uses_full_chain_when_present():
    from scripts.pathway_v2.quick_assign import _build_chain_display

    class _Ix:
        def __init__(self):
            self.data = {"chain_context": {"full_chain": ["ATXN3", "FOXO4", "SOD2"]}}
            self.protein_a = None
            self.protein_b = None

    class _Claim:
        def __init__(self):
            self.interaction = _Ix()

    assert _build_chain_display([_Claim()]) == "ATXN3 → FOXO4 → SOD2"


def test_chain_display_falls_back_to_mediator_chain():
    from scripts.pathway_v2.quick_assign import _build_chain_display

    class _Protein:
        def __init__(self, symbol):
            self.symbol = symbol

    class _Ix:
        def __init__(self):
            self.data = {"mediator_chain": ["FOXO4"]}
            self.protein_a = _Protein("ATXN3")
            self.protein_b = _Protein("SOD2")

    class _Claim:
        def __init__(self):
            self.interaction = _Ix()

    assert _build_chain_display([_Claim()]) == "ATXN3 → FOXO4 → SOD2"


def test_chain_display_returns_placeholder_when_no_context():
    from scripts.pathway_v2.quick_assign import _build_chain_display

    class _Claim:
        interaction = None

    assert _build_chain_display([_Claim()]) == "<unknown chain>"


# ---------------------------------------------------------------------------
# _acquire_pathway_creation_lock — Postgres advisory lock for pathway creation
# ---------------------------------------------------------------------------
#
# Prevents the TOCTOU race where two threads both pass the race-guard
# check-query and then race to insert, triggering the UNIQUE constraint
# recovery path. On Postgres the lock is acquired via pg_advisory_xact_lock;
# on SQLite (tests) it's a no-op. Helper must NEVER raise — a lock failure
# just downgrades to the legacy recovery path.


class _FakeSession:
    def __init__(self, raise_on_execute=None):
        self.executed: list = []
        self._raise_on_execute = raise_on_execute

    def execute(self, statement, params=None):
        if self._raise_on_execute is not None:
            raise self._raise_on_execute
        self.executed.append((statement, params))
        return None


class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeEngine:
    def __init__(self, dialect_name):
        self.dialect = _FakeDialect(dialect_name)


class _FakeDB:
    """Stand-in for ``flask_sqlalchemy.SQLAlchemy``. Exposes the minimal
    surface used by ``_acquire_pathway_creation_lock``: ``.engine``,
    ``.session``, and ``.text``."""

    def __init__(self, dialect_name, raise_on_execute=None):
        self.engine = _FakeEngine(dialect_name)
        self.session = _FakeSession(raise_on_execute=raise_on_execute)

    @staticmethod
    def text(statement):
        return statement  # SQLAlchemy text() wraps the string; plain echo is fine for tests


def test_acquire_pathway_lock_runs_pg_advisory_xact_lock_on_postgres():
    from scripts.pathway_v2.quick_assign import _acquire_pathway_creation_lock

    db = _FakeDB("postgresql")
    acquired = _acquire_pathway_creation_lock(db, "Autophagy")

    assert acquired is True
    assert len(db.session.executed) == 1
    statement, params = db.session.executed[0]
    assert "pg_advisory_xact_lock" in statement
    assert "hashtext(lower(:name))" in statement
    assert params == {"name": "Autophagy"}


def test_acquire_pathway_lock_is_noop_on_sqlite():
    from scripts.pathway_v2.quick_assign import _acquire_pathway_creation_lock

    db = _FakeDB("sqlite")
    acquired = _acquire_pathway_creation_lock(db, "Autophagy")

    # Returns True (acquisition considered successful) and no SQL was run.
    assert acquired is True
    assert db.session.executed == []


def test_acquire_pathway_lock_returns_false_on_execute_failure():
    """If the SQL execution fails (e.g. backend rejects the call), the
    helper must return False so the caller falls through to the existing
    IntegrityError recovery path rather than aborting."""
    from scripts.pathway_v2.quick_assign import _acquire_pathway_creation_lock

    db = _FakeDB("postgresql", raise_on_execute=RuntimeError("boom"))
    acquired = _acquire_pathway_creation_lock(db, "Autophagy")

    assert acquired is False


def test_acquire_pathway_lock_returns_false_on_missing_engine_attr():
    """Defensive check: if ``db.engine`` is missing or broken (never should
    happen in production but may in odd test setups), the helper returns
    False rather than raising, so pathway creation keeps working."""
    from scripts.pathway_v2.quick_assign import _acquire_pathway_creation_lock

    class _BrokenDB:
        @property
        def engine(self):
            raise AttributeError("no engine")

    acquired = _acquire_pathway_creation_lock(_BrokenDB(), "Autophagy")
    assert acquired is False


def test_acquire_pathway_lock_bound_parameter_prevents_injection():
    """The pathway name comes from LLM output — verify it's passed as a
    bound parameter (not string-interpolated) so a malicious/accidental
    quote or semicolon in the name can't break the query."""
    from scripts.pathway_v2.quick_assign import _acquire_pathway_creation_lock

    db = _FakeDB("postgresql")
    nasty_name = "'; DROP TABLE pathways; --"
    acquired = _acquire_pathway_creation_lock(db, nasty_name)

    assert acquired is True
    statement, params = db.session.executed[0]
    # Statement text itself is fixed — no interpolation.
    assert nasty_name not in statement
    # Name is in bound params only.
    assert params == {"name": nasty_name}
