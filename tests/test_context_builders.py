"""Unit tests for pipeline/context_builders.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import pytest

from pipeline.context_builders import (
    build_known_interactions_context,
    build_prompt,
    dumps_compact,
)
from pipeline.types import StepConfig


def _make_step(name: str = "step1", **overrides) -> StepConfig:
    """Helper to build a minimal StepConfig for testing."""
    defaults = dict(
        name=name,
        model="gemini-3-pro",
        prompt_template="Analyze {user_query}",
        expected_columns=["ctx_json", "step_json"],
    )
    defaults.update(overrides)
    return StepConfig(**defaults)


# ── dumps_compact ────────────────────────────────────────────────────────


def test_dumps_compact_no_spaces():
    data = {"a": 1, "b": [2, 3]}
    result = dumps_compact(data)
    assert " " not in result


def test_dumps_compact_valid_json():
    data = {"key": "value", "list": [1, 2, 3]}
    result = dumps_compact(data)
    parsed = json.loads(result)
    assert parsed == data


# ── build_known_interactions_context ─────────────────────────────────────


def test_build_known_interactions_context_empty():
    assert build_known_interactions_context([]) == ""


def test_build_known_interactions_context_formats():
    interactions = [
        {"primary": "MDM2", "confidence": 0.95},
        {"primary": "BRCA1", "confidence": 0.80},
    ]
    result = build_known_interactions_context(interactions)
    assert "MDM2" in result
    assert "BRCA1" in result
    assert "ALREADY IN DATABASE" in result


def test_build_known_interactions_context_emits_all_symbols():
    # No 50-cap — every symbol must appear in the exclusion list.
    interactions = [{"primary": f"PROT{i}", "confidence": 0.5} for i in range(60)]
    result = build_known_interactions_context(interactions)
    assert "PROT0" in result
    assert "PROT59" in result
    assert "60 total" in result


# ── build_prompt ─────────────────────────────────────────────────────────


def test_build_prompt_first_step():
    step = _make_step(prompt_template="Find interactors for {user_query}")
    result = build_prompt(step, prior_payload=None, user_query="TP53", is_first_step=True)
    assert "TP53" in result
    assert "initialize ctx_json" in result


def test_build_prompt_with_prior():
    step = _make_step(prompt_template="Continue analysis of {user_query}")
    prior = {"ctx_json": {"main": "TP53", "interactors": []}}
    result = build_prompt(step, prior_payload=prior, user_query="TP53", is_first_step=False)
    assert "CONTEXT (from previous steps)" in result
    # The ctx_json should be serialized compactly in the prompt
    assert '"main":"TP53"' in result or '"main": "TP53"' in result


# ── _compact_ctx_for_step: deepcopy removal invariants ─────────────────────


def _make_heavy_ctx(n_interactors: int = 30, n_functions: int = 5):
    """Build a ctx_json representative of the post-function-mapping state."""
    interactors = []
    for i in range(n_interactors):
        interactors.append({
            "primary": f"PROT{i}",
            "interaction_type": "direct",
            "upstream_interactor": None,
            "mediator_chain": None,
            "depth": 0,
            "functions": [
                {
                    "function": f"function_{i}_{j}",
                    "arrow": "activates",
                    "interaction_effect": "activates",
                    "cellular_process": "nucleus",
                    "biological_consequence": ["some long text"],
                    "evidence": [
                        {"pmid": f"{10000000 + i * 100 + j}", "quote": "evidence text " * 10}
                    ],
                    "pmids": [f"{10000000 + i * 100 + j}"],
                    "confidence": 0.9,
                }
                for j in range(n_functions)
            ],
        })
    return {
        "main": "ATXN3",
        "interactors": interactors,
        "interactor_history": [f"PROT{i}" for i in range(n_interactors)],
        "function_history": {
            f"PROT{i}": [f"func_{i}_{k}" for k in range(12)]  # >8 to trigger cap
            for i in range(n_interactors)
        },
    }


def test_compact_does_not_mutate_original_for_function_step():
    """The shallow-copy refactor must NOT mutate the caller's ctx_json. If it
    did, the runner's differential merge would silently lose fields between
    steps.
    """
    from pipeline.context_builders import _compact_ctx_for_step

    ctx = _make_heavy_ctx(n_interactors=10, n_functions=3)
    original_interactor_count = len(ctx["interactors"])
    # Capture identities of specific objects we expect to remain untouched.
    original_interactor_0 = ctx["interactors"][0]
    original_functions_0 = ctx["interactors"][0]["functions"]
    original_evidence_0 = ctx["interactors"][0]["functions"][0]["evidence"]
    original_history_prot0 = ctx["function_history"]["PROT0"]

    compact = _compact_ctx_for_step(ctx, "step2a_function_mapping")

    # Original ctx is unchanged structurally.
    assert len(ctx["interactors"]) == original_interactor_count
    assert ctx["interactors"][0] is original_interactor_0
    assert ctx["interactors"][0]["functions"] is original_functions_0
    assert ctx["interactors"][0]["functions"][0]["evidence"] is original_evidence_0
    # function_history sub-lists are not truncated in the original.
    assert ctx["function_history"]["PROT0"] is original_history_prot0
    assert len(ctx["function_history"]["PROT0"]) == 12

    # Compact is a different dict object.
    assert compact is not ctx
    # Compact's function_history is capped to 8 names per protein.
    assert all(
        len(names) <= 8
        for names in compact["function_history"].values()
    )
    # Compact's interactors are slim dicts, not the originals.
    assert compact["interactors"][0] is not original_interactor_0


def test_compact_does_not_mutate_original_for_discovery_step():
    from pipeline.context_builders import _compact_ctx_for_step

    ctx = _make_heavy_ctx(n_interactors=5, n_functions=2)
    original_interactor_0 = ctx["interactors"][0]

    compact = _compact_ctx_for_step(ctx, "step1a_discover_interactors")

    assert ctx["interactors"][0] is original_interactor_0
    # Discovery compaction keeps only primary + interaction_type.
    assert set(compact["interactors"][0].keys()) == {"primary", "interaction_type"}


def test_compact_slims_chain_step_without_losing_chain_fields():
    """Chain steps slim prose fields while preserving cascade metadata."""
    from pipeline.context_builders import _compact_ctx_for_step

    ctx = _make_heavy_ctx(n_interactors=3, n_functions=2)
    ctx["interactors"][0]["interaction_type"] = "indirect"
    ctx["interactors"][0]["chain_context"] = {"full_chain": ["ATXN3", "PNKP", "LIG3"]}
    ctx["interactors"][0]["chain_link_functions"] = {
        "PNKP->LIG3": [{"function": "ligation relay"}]
    }
    original_interactor_0 = ctx["interactors"][0]
    original_evidence = ctx["interactors"][0]["functions"][0]["evidence"]

    compact = _compact_ctx_for_step(ctx, "step2ab_chain_determination")

    assert compact is not ctx
    assert ctx["interactors"][0] is original_interactor_0
    assert ctx["interactors"][0]["functions"][0]["evidence"] is original_evidence
    slim = compact["interactors"][0]
    assert slim is not original_interactor_0
    assert slim["chain_context"]["full_chain"] == ["ATXN3", "PNKP", "LIG3"]
    assert slim["chain_link_functions"]["PNKP->LIG3"][0]["function"] == "ligation relay"
    assert "evidence" not in slim["functions"][0]


def test_compact_returns_original_for_unrecognized_step():
    from pipeline.context_builders import _compact_ctx_for_step

    ctx = _make_heavy_ctx(n_interactors=3, n_functions=2)
    assert _compact_ctx_for_step(ctx, "some_custom_step") is ctx


def test_build_prompt_large_ctx_does_not_mutate_prior_payload():
    """The >12000 guard in build_prompt should not touch prior_payload even
    when it needs to shrink the interactors list to a placeholder.
    """
    step = _make_step(name="step2a_function_mapping", prompt_template="Do work")
    # Heavy ctx ensures we trip the >12000 guard after compaction.
    ctx = _make_heavy_ctx(n_interactors=40, n_functions=5)
    prior = {"ctx_json": ctx}
    original_interactors = ctx["interactors"]
    original_interactor_0 = ctx["interactors"][0]
    original_history_len = len(ctx["interactor_history"])

    build_prompt(step, prior_payload=prior, user_query="ATXN3", is_first_step=False)

    # prior's ctx_json MUST be unchanged.
    assert prior["ctx_json"] is ctx
    assert ctx["interactors"] is original_interactors
    assert ctx["interactors"][0] is original_interactor_0
    assert len(ctx["interactor_history"]) == original_history_len


def test_build_prompt_heavy_ctx_avoids_deepcopy_cost():
    """Regression benchmark: the pre-refactor deepcopy scaled O(N * funcs).
    With the shallow-copy refactor, a ctx with 30 interactors × 5 functions
    should build a prompt in well under 100 ms. This isn't a hard threshold
    for production; it catches gross re-introduction of deepcopy on the hot
    path.
    """
    import time as time_module

    step = _make_step(name="step2a_function_mapping", prompt_template="Do work")
    ctx = _make_heavy_ctx(n_interactors=30, n_functions=5)
    prior = {"ctx_json": ctx}

    # Warm up (imports, JIT if any).
    build_prompt(step, prior_payload=prior, user_query="ATXN3", is_first_step=False)

    start = time_module.perf_counter()
    for _ in range(5):
        build_prompt(step, prior_payload=prior, user_query="ATXN3", is_first_step=False)
    elapsed = time_module.perf_counter() - start

    # 5 iterations should easily fit in 500 ms on any reasonable machine,
    # giving generous headroom without being flaky.
    assert elapsed < 0.5, f"build_prompt too slow: {elapsed:.3f}s for 5 runs"


# ── build_known_interactions_context memoization (step 10 / #8) ─────────


def test_known_interactions_context_memoized_on_fingerprint():
    """Two calls with the same (primary, confidence) list should hit the LRU
    cache and return identical strings."""
    from pipeline.context_builders import (
        _build_known_interactions_context_cached,
    )
    # Clear the cache so test state is deterministic.
    _build_known_interactions_context_cached.cache_clear()

    interactions = [
        {"primary": "MDM2", "confidence": 0.95, "direction": "main_to_primary"},
        {"primary": "BRCA1", "confidence": 0.80, "direction": "primary_to_main"},
    ]
    from pipeline.context_builders import build_known_interactions_context

    a = build_known_interactions_context(interactions)
    b = build_known_interactions_context(interactions)
    assert a == b

    info = _build_known_interactions_context_cached.cache_info()
    assert info.hits >= 1  # second call hit the cache
    assert info.misses >= 1  # first call was a miss


def test_known_interactions_context_cache_ignores_non_projected_fields():
    """Memoization key uses only (primary, confidence), so adding unrelated
    fields to the input shouldn't produce a cache miss."""
    from pipeline.context_builders import (
        _build_known_interactions_context_cached,
        build_known_interactions_context,
    )
    _build_known_interactions_context_cached.cache_clear()

    base = [{"primary": "MDM2", "confidence": 0.95}]
    enriched = [{"primary": "MDM2", "confidence": 0.95, "extra": "metadata"}]

    first = build_known_interactions_context(base)
    second = build_known_interactions_context(enriched)
    assert first == second

    info = _build_known_interactions_context_cached.cache_info()
    assert info.hits >= 1


def test_known_interactions_context_cache_miss_on_different_symbols():
    """Different symbol lists must produce different cached output strings."""
    from pipeline.context_builders import (
        _build_known_interactions_context_cached,
        build_known_interactions_context,
    )
    _build_known_interactions_context_cached.cache_clear()

    a = build_known_interactions_context([{"primary": "MDM2"}])
    b = build_known_interactions_context([{"primary": "BRCA1"}])
    assert a != b


# ── make_batch_directive (step 10 / #8) ─────────────────────────────────


def test_make_batch_directive_default_label():
    from pipeline.prompts.shared_blocks import make_batch_directive

    template = make_batch_directive()
    # Placeholders should be preserved for downstream .format(...).
    assert "{count}" in template
    assert "{batch_names}" in template
    assert "interactors:" in template
    assert "DEPTH" in template
    assert "UNIQUENESS" in template


def test_make_batch_directive_variant_label():
    from pipeline.prompts.shared_blocks import make_batch_directive

    template = make_batch_directive("NEWLY DISCOVERED interactors")
    assert "NEWLY DISCOVERED interactors:" in template


def test_make_batch_directive_formats_correctly():
    from pipeline.prompts.shared_blocks import make_batch_directive

    template = make_batch_directive("interactors")
    filled = template.format(count=3, batch_names="VCP, MDM2, BRCA1")
    assert "3 interactors" in filled
    assert "VCP, MDM2, BRCA1" in filled
    # Placeholders must be fully substituted.
    assert "{count}" not in filled
    assert "{batch_names}" not in filled


def test_make_batch_directive_depth_uniqueness_constant_identical():
    """The depth/uniqueness block must be the same across all variants, so
    re-labeling can never drift one variant away from the others."""
    from pipeline.prompts.shared_blocks import (
        BATCH_DIRECTIVE_DEPTH_UNIQUENESS,
        make_batch_directive,
    )

    for label in ("interactors", "NEW", "CHAIN"):
        assert BATCH_DIRECTIVE_DEPTH_UNIQUENESS in make_batch_directive(label)
