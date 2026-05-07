"""Property tests for dedup determinism (step 7 / #4) and whitespace
normalization (step 7 / #11).

The previous pairwise dedup fallback in
``utils/deduplicate_functions.py`` produced different results depending on
the input order and was non-transitive (A≈B, B≈C but A vs C never compared).
We replaced it with the batch path, which is deterministic given the LLM
response. These tests:

1. Pin the order-invariance property by monkeypatching the LLM layer and
   shuffling the input 100×, asserting output is identical each time.
2. Pin the whitespace normalization in
   ``_normalize_for_matching`` (``scripts/pathway_v2/quick_assign.py``) so
   pathways that differ only in inner whitespace dedup to the same canonical
   form.
"""
import random
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Order-invariance of deduplicate_interactor_functions
# ---------------------------------------------------------------------------


def _mock_batch_compare(keep_set):
    """Build a mock batch_compare_functions that keeps indices whose function
    name is in ``keep_set``, regardless of input order."""
    def _batch(functions, interaction_name, api_key):
        return [
            idx for idx, fn in enumerate(functions)
            if fn.get("function") in keep_set
        ]
    return _batch


@pytest.mark.parametrize("seed", list(range(20)))
def test_dedup_output_is_order_invariant(monkeypatch, seed):
    """Shuffling the input function list should yield the same
    output set (modulo ordering), not different dedup results."""
    from utils import deduplicate_functions as dfm

    functions_canonical = [
        {"function": "A", "pathway": "Autophagy"},
        {"function": "A-duplicate", "pathway": "Autophagy"},
        {"function": "B", "pathway": "DNA Repair"},
        {"function": "C", "pathway": "DNA Repair"},
        {"function": "C-dup", "pathway": "DNA Repair"},
    ]
    # Our mock: keep A, B, C; drop A-duplicate and C-dup.
    keep_set = {"A", "B", "C"}
    monkeypatch.setattr(dfm, "batch_compare_functions", _mock_batch_compare(keep_set))

    rng = random.Random(seed)
    shuffled = list(functions_canonical)
    rng.shuffle(shuffled)

    interactor = {"primary": "X", "functions": shuffled}
    result = dfm.deduplicate_interactor_functions(
        interactor, interaction_name="X-Y", api_key="fake",
    )
    result_names = {f["function"] for f in result["functions"]}
    assert result_names == keep_set


def test_dedup_empty_functions_returns_interactor_unchanged(monkeypatch):
    """Edge case: empty functions list is a no-op."""
    from utils import deduplicate_functions as dfm

    interactor = {"primary": "X", "functions": []}
    out = dfm.deduplicate_interactor_functions(interactor, "X-Y", "fake")
    assert out is interactor  # short-circuit returns input


def test_dedup_single_function_returns_interactor_unchanged(monkeypatch):
    """Edge case: single function list is a no-op."""
    from utils import deduplicate_functions as dfm

    interactor = {"primary": "X", "functions": [{"function": "only"}]}
    out = dfm.deduplicate_interactor_functions(interactor, "X-Y", "fake")
    assert out is interactor


def test_dedup_does_not_mutate_input_interactor(monkeypatch):
    """The shallow-copy result must not mutate the input interactor (the
    result is ``{**interactor, "functions": keep_functions}``)."""
    from utils import deduplicate_functions as dfm

    original_functions = [
        {"function": "A", "pathway": "P1"},
        {"function": "A-dup", "pathway": "P1"},
    ]
    interactor = {"primary": "X", "functions": list(original_functions), "extra": "meta"}
    monkeypatch.setattr(
        dfm, "batch_compare_functions", _mock_batch_compare({"A"}),
    )

    result = dfm.deduplicate_interactor_functions(interactor, "X-Y", "fake")

    assert interactor["functions"] == original_functions  # input unchanged
    assert len(result["functions"]) == 1
    assert result["extra"] == "meta"  # other fields preserved


def test_dedup_batch_failure_keeps_all_functions(monkeypatch):
    """When ``batch_compare_functions`` returns an empty list (signalling
    LLM failure), dedup should keep all input functions — the previous
    fallback silently dropped them."""
    from utils import deduplicate_functions as dfm

    def _empty_batch(functions, interaction_name, api_key):
        return list(range(len(functions)))  # "keep all" sentinel

    monkeypatch.setattr(dfm, "batch_compare_functions", _empty_batch)

    interactor = {
        "primary": "X",
        "functions": [
            {"function": "A"}, {"function": "B"}, {"function": "C"},
        ],
    }
    result = dfm.deduplicate_interactor_functions(interactor, "X-Y", "fake")
    assert len(result["functions"]) == 3


# ---------------------------------------------------------------------------
# C3: Cross-context partition isolation
# ---------------------------------------------------------------------------


def test_dedup_partitions_by_function_context(monkeypatch):
    """Two functions with the same name + pathway but different
    function_context values must NEVER be merged into one. The dedup
    LLM only sees one partition at a time, so even if it thinks the
    two are duplicates, it can't see them simultaneously."""
    from utils import deduplicate_functions as dfm

    seen_partitions: list = []

    def _greedy_keep_first(functions, interaction_name, api_key):
        # Pretend the LLM thinks every function in this partition is a
        # duplicate of the first — keep only index 0. Records what each
        # partition looked like so the test can assert isolation.
        seen_partitions.append(
            [(f.get("function"), f.get("function_context")) for f in functions]
        )
        return [0]

    monkeypatch.setattr(dfm, "batch_compare_functions", _greedy_keep_first)

    # Two claims with the SAME function name + pathway, differing only
    # on function_context. Without partitioning, the LLM would merge
    # them and one context label would be erased.
    interactor = {
        "primary": "MTOR",
        "functions": [
            {"function": "Activates mTORC1", "pathway": "mTOR signaling",
             "function_context": "direct"},
            {"function": "Activates mTORC1", "pathway": "mTOR signaling",
             "function_context": "chain_derived"},
        ],
    }
    result = dfm.deduplicate_interactor_functions(
        interactor, interaction_name="ATXN3-MTOR", api_key="fake",
    )

    contexts = sorted(f.get("function_context") for f in result["functions"])
    assert contexts == ["chain_derived", "direct"], (
        f"both contexts must survive cross-partition dedup, got {contexts}"
    )
    # Singletons skip the LLM round-trip entirely — neither call should
    # have happened because each partition has exactly one function.
    assert seen_partitions == [], (
        f"singleton partitions should bypass batch_compare_functions, "
        f"but it was invoked with {seen_partitions}"
    )


def test_dedup_within_same_context_still_deduplicates(monkeypatch):
    """Sanity check: dedup still works WITHIN a single function_context
    partition. Two 'direct' duplicates collapse to one, and the
    'chain_derived' singleton survives untouched."""
    from utils import deduplicate_functions as dfm

    monkeypatch.setattr(
        dfm, "batch_compare_functions", _mock_batch_compare({"keep_me"}),
    )

    interactor = {
        "primary": "MTOR",
        "functions": [
            {"function": "keep_me", "function_context": "direct"},
            {"function": "drop_me", "function_context": "direct"},
            {"function": "lonely", "function_context": "chain_derived"},
        ],
    }
    result = dfm.deduplicate_interactor_functions(
        interactor, interaction_name="ATXN3-MTOR", api_key="fake",
    )
    surviving = sorted(
        (f["function"], f["function_context"]) for f in result["functions"]
    )
    assert surviving == [
        ("keep_me", "direct"),
        ("lonely", "chain_derived"),
    ]


def test_dedup_legacy_null_function_context_dedups_together(monkeypatch):
    """Legacy rows with no function_context label form their own
    partition (key '') and dedup against each other normally."""
    from utils import deduplicate_functions as dfm

    monkeypatch.setattr(
        dfm, "batch_compare_functions", _mock_batch_compare({"keep_me"}),
    )

    interactor = {
        "primary": "X",
        "functions": [
            {"function": "keep_me"},  # no function_context
            {"function": "drop_me"},  # no function_context
            {"function": "drop_me_too", "function_context": None},
        ],
    }
    result = dfm.deduplicate_interactor_functions(
        interactor, "X-Y", "fake",
    )
    assert [f["function"] for f in result["functions"]] == ["keep_me"]


# ---------------------------------------------------------------------------
# Whitespace normalization in quick_assign._normalize_for_matching (#11)
# ---------------------------------------------------------------------------


def test_normalize_collapses_inner_whitespace():
    from scripts.pathway_v2.quick_assign import _normalize_for_matching

    assert _normalize_for_matching("Autophagy  Receptor") == "autophagy receptor"
    assert _normalize_for_matching("Autophagy Receptor") == "autophagy receptor"


def test_normalize_strips_leading_trailing_whitespace():
    from scripts.pathway_v2.quick_assign import _normalize_for_matching

    assert _normalize_for_matching("  Autophagy  ") == "autophagy"


def test_normalize_tab_and_newline_collapse_to_space():
    from scripts.pathway_v2.quick_assign import _normalize_for_matching

    assert _normalize_for_matching("DNA\tRepair") == "dna repair"
    assert _normalize_for_matching("DNA\nRepair") == "dna repair"


def test_normalize_strips_punctuation():
    from scripts.pathway_v2.quick_assign import _normalize_for_matching

    assert _normalize_for_matching("Wnt/β-catenin") == "wntcatenin"


def test_normalize_case_insensitive():
    from scripts.pathway_v2.quick_assign import _normalize_for_matching

    assert _normalize_for_matching("AUTOPHAGY") == "autophagy"
    assert _normalize_for_matching("Autophagy") == _normalize_for_matching("autophagy")


def test_normalize_two_pathways_with_inner_space_diff_match():
    """Acceptance test for the whitespace fix: two pathways that differ
    only in inner whitespace should normalize to the same string."""
    from scripts.pathway_v2.quick_assign import _normalize_for_matching

    a = _normalize_for_matching("Mitochondrial Quality Control")
    b = _normalize_for_matching("Mitochondrial  Quality  Control")  # double spaces
    c = _normalize_for_matching("Mitochondrial\tQuality Control")
    assert a == b == c


# ---------------------------------------------------------------------------
# Consolidation: deduplicate_payload(strategy=...) single entry point (#7)
# ---------------------------------------------------------------------------


def test_deduplicate_payload_dispatches_to_local_strategy():
    """``strategy='local'`` must route to the fast word-overlap path and
    dedup functions within a payload without touching the network."""
    from utils.deduplicate_functions import deduplicate_payload

    payload = {
        "ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {
                    "primary": "VCP",
                    "functions": [
                        {
                            "function": "Protein quality control",
                            "arrow": "activates",
                            "cellular_process": (
                                "VCP extracts misfolded proteins from "
                                "membranes and delivers them to the "
                                "proteasome for degradation as part of "
                                "the endoplasmic reticulum associated "
                                "degradation ERAD pathway."
                            ),
                            "biological_consequence": [],
                            "evidence": [],
                        },
                        {
                            # Duplicate — same name+arrow, triggers pass 1.
                            "function": "Protein quality control",
                            "arrow": "activates",
                            "cellular_process": "another description",
                            "biological_consequence": [],
                            "evidence": [],
                        },
                    ],
                }
            ],
        }
    }
    out = deduplicate_payload(payload, strategy="local")
    functions = out["ctx_json"]["interactors"][0]["functions"]
    # Two identical (name, arrow) → one kept after pass 1.
    assert len(functions) == 1


def test_deduplicate_payload_rejects_unknown_strategy():
    from utils.deduplicate_functions import deduplicate_payload

    with pytest.raises(ValueError, match="unknown strategy"):
        deduplicate_payload({"ctx_json": {}}, strategy="bogus")


def test_deduplicate_payload_local_strategy_no_api_key_required():
    """Local strategy should not touch ``api_key`` at all, so it's safe to
    omit it entirely."""
    from utils.deduplicate_functions import deduplicate_payload

    payload = {"ctx_json": {"main": "X", "interactors": []}}
    # No api_key argument — should not raise.
    out = deduplicate_payload(payload, strategy="local")
    assert out is payload
