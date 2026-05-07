#!/usr/bin/env python3
"""Tests for the PostProcessor pipeline."""

import itertools
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.post_processor import (
    PostProcessor,
    StageDescriptor,
    StageKind,
    _adapt_normalize_function_contexts,
)


# ---------------------------------------------------------------------------
# Unit tests — StageDescriptor / PostProcessor construction
# ---------------------------------------------------------------------------

def test_default_stages_count():
    """Default chain has 12 stages including the quality_validation gate.

    Stage list (post-2026-04-29 reorder + Phase A2 quality validator wire-up):
      1.  chain_group_tagging          (PURE)
      2.  chain_link_completeness      (PURE)
      3.  normalize_function_contexts  (PURE)
      4.  schema_pre_gate              (PURE)
      5.  arrow_validation             (LLM, critical)  ← runs before dedup
      6.  dedup_functions              (LLM)
      7.  evidence_validation          (LLM)
      8.  update_pmids                 (LLM, default_skip=True)
      9.  interaction_metadata         (LLM)
      10. clean_function_names         (PURE)
      11. quality_validation           (PURE) — depth gate
      12. finalize_metadata            (PURE)

    The active-stage count excludes default_skip=True (update_pmids),
    so 12 → 11 active in the default config.
    """
    stages = PostProcessor.default_stages()
    assert len(stages) == 12
    active = [s for s in stages if not s.default_skip]
    assert len(active) == 11
    # Order assertion: arrow_validation MUST precede dedup_functions
    # so dedup compares functions with corrected arrows.
    names = [s.name for s in stages]
    assert names.index("arrow_validation") < names.index("dedup_functions")


def test_requery_stages_count():
    """Requery chain has 4 stages (S1 removed bidirectional_split)."""
    stages = PostProcessor.requery_stages()
    assert len(stages) == 4


def test_requery_stages_order():
    """Requery stages must follow: evidence → metadata → pmids → dedup (S1: no split)."""
    names = [s.name for s in PostProcessor.requery_stages()]
    assert names == [
        "evidence_validation",
        "interaction_metadata",
        "update_pmids",
        "dedup_functions",
    ]


def test_normalize_function_contexts_stamps_direct_default():
    """Direct interactor's functions without context → stamped 'direct'."""
    payload = {
        "ctx_json": {
            "interactors": [
                {
                    "primary": "VCP",
                    "interaction_type": "direct",
                    "functions": [
                        {"function": "unfolds aggregates"},
                        {"function": "p97 ATPase activity"},
                    ],
                },
            ],
        },
    }
    out = _adapt_normalize_function_contexts(payload)
    interactor = out["ctx_json"]["interactors"][0]
    assert interactor["function_context"] == "direct"
    for fn in interactor["functions"]:
        assert fn["function_context"] == "direct"


def test_normalize_function_contexts_stamps_net_default_for_indirect():
    """Indirect interactor's functions without context → stamped 'net'."""
    payload = {
        "ctx_json": {
            "interactors": [
                {
                    "primary": "MTOR",
                    "interaction_type": "indirect",
                    "functions": [{"function": "regulates autophagy via RHEB"}],
                },
            ],
        },
    }
    out = _adapt_normalize_function_contexts(payload)
    interactor = out["ctx_json"]["interactors"][0]
    assert interactor["function_context"] == "net"
    assert interactor["functions"][0]["function_context"] == "net"


def test_normalize_function_contexts_preserves_explicit_chain_derived():
    """Explicit 'chain_derived' tag on a function is NOT overwritten."""
    payload = {
        "ctx_json": {
            "interactors": [
                {
                    "primary": "RHEB",
                    "interaction_type": "direct",
                    "functions": [
                        {
                            "function": "activates mTORC1",
                            "function_context": "chain_derived",
                        },
                        {"function": "binds TSC1/TSC2"},
                    ],
                },
            ],
        },
    }
    out = _adapt_normalize_function_contexts(payload)
    fns = out["ctx_json"]["interactors"][0]["functions"]
    assert fns[0]["function_context"] == "chain_derived"  # preserved
    assert fns[1]["function_context"] == "direct"  # stamped default


def test_normalize_function_contexts_idempotent():
    """Running the stage twice on the same payload produces no changes."""
    payload = {
        "ctx_json": {
            "interactors": [
                {
                    "primary": "TP53",
                    "interaction_type": "direct",
                    "functions": [{"function": "DNA damage response"}],
                },
            ],
        },
    }
    first = _adapt_normalize_function_contexts(payload)
    second = _adapt_normalize_function_contexts(first)
    assert second == first


def test_default_stages_arrow_before_metadata():
    """Arrow validation must run before interaction metadata for consistency."""
    names = [s.name for s in PostProcessor.default_stages()]
    arrow_idx = names.index("arrow_validation")
    metadata_idx = names.index("interaction_metadata")
    assert arrow_idx < metadata_idx, (
        f"arrow_validation (index {arrow_idx}) must come before "
        f"interaction_metadata (index {metadata_idx})"
    )


def test_skip_deduplicator_removes_dedup_stage():
    pp = PostProcessor(skip_flags={"skip_deduplicator": True})
    active_names = [s.name for s in pp.active_stages()]
    assert "dedup_functions" not in active_names


def test_skip_validation_removes_evidence_stage():
    pp = PostProcessor(skip_flags={"skip_validation": True})
    active_names = [s.name for s in pp.active_stages()]
    assert "evidence_validation" not in active_names


def test_count_steps_matches_active_stages():
    for flags in [
        {},
        {"skip_validation": True},
        {"skip_deduplicator": True},
        {"skip_validation": True, "skip_deduplicator": True},
    ]:
        pp = PostProcessor(skip_flags=flags)
        assert pp.count_steps() == len(pp.active_stages())


def test_active_stages_never_exceeds_total():
    """Property: active stages <= total stages for any combination of skip flags."""
    all_flags = [
        "skip_validation", "skip_deduplicator", "skip_fact_checking",
        "skip_schema_validation",
        "skip_interaction_metadata", "skip_pmid_update",
        "skip_arrow_validation", "skip_clean_names", "skip_finalize_metadata",
        "skip_normalize_function_contexts",
    ]
    total = len(PostProcessor.default_stages())
    for r in range(len(all_flags) + 1):
        for combo in itertools.combinations(all_flags, r):
            flags = {f: True for f in combo}
            pp = PostProcessor(skip_flags=flags)
            assert pp.count_steps() <= total


def test_skip_schema_validation_removes_stage():
    pp = PostProcessor(skip_flags={"skip_schema_validation": True})
    active_names = [s.name for s in pp.active_stages()]
    assert "schema_pre_gate" not in active_names


def test_skip_interaction_metadata_removes_stage():
    pp = PostProcessor(skip_flags={"skip_interaction_metadata": True})
    active_names = [s.name for s in pp.active_stages()]
    assert "interaction_metadata" not in active_names


def test_skip_pmid_update_removes_stage():
    pp = PostProcessor(skip_flags={"skip_pmid_update": True})
    active_names = [s.name for s in pp.active_stages()]
    assert "update_pmids" not in active_names


def test_citation_finder_stage_requires_api_key():
    """The update_pmids stage (citation finder) requires an API key."""
    stages = PostProcessor.default_stages()
    pmid_stage = next(s for s in stages if s.name == "update_pmids")
    assert pmid_stage.requires_api_key is True
    assert pmid_stage.kind == StageKind.LLM


def test_skip_arrow_validation_removes_stage():
    pp = PostProcessor(skip_flags={"skip_arrow_validation": True})
    active_names = [s.name for s in pp.active_stages()]
    assert "arrow_validation" not in active_names


def test_skip_clean_names_removes_stage():
    pp = PostProcessor(skip_flags={"skip_clean_names": True})
    active_names = [s.name for s in pp.active_stages()]
    assert "clean_function_names" not in active_names


def test_skip_finalize_metadata_removes_stage():
    pp = PostProcessor(skip_flags={"skip_finalize_metadata": True})
    active_names = [s.name for s in pp.active_stages()]
    assert "finalize_metadata" not in active_names


# ---------------------------------------------------------------------------
# Integration tests — run() with mock stages
# ---------------------------------------------------------------------------

def _make_mock_stage(name, kind=StageKind.PURE, **kw):
    """Create a StageDescriptor with a simple tracking function."""
    call_log = []

    def fn(payload, **kwargs):
        call_log.append(name)
        payload.setdefault("_call_log", []).append(name)
        return payload

    stage = StageDescriptor(name=name, label=f"Running {name}...", kind=kind, fn=fn, **kw)
    return stage, call_log


def test_run_chains_stages_in_order():
    stage_a, log_a = _make_mock_stage("a")
    stage_b, log_b = _make_mock_stage("b")
    stage_c, log_c = _make_mock_stage("c")

    pp = PostProcessor(stages=[stage_a, stage_b, stage_c])
    result, step = pp.run({"ctx_json": {}}, current_step=0, total_steps=3)

    assert result["_call_log"] == ["a", "b", "c"]
    assert step == 3
    assert len(log_a) == 1
    assert len(log_b) == 1
    assert len(log_c) == 1


def test_api_key_required_stages_skipped_without_key():
    called = []

    def fn(payload, **kwargs):
        called.append(True)
        return payload

    stage = StageDescriptor(
        name="needs_key", label="Test", kind=StageKind.LLM,
        fn=fn, requires_api_key=True,
    )
    pp = PostProcessor(stages=[stage])
    result, step = pp.run({"ctx_json": {}}, api_key=None)
    assert len(called) == 0
    assert step == 0  # stage was skipped, so step not incremented


def test_api_key_required_stages_run_with_key():
    called = []

    def fn(payload, **kwargs):
        called.append(True)
        return payload

    stage = StageDescriptor(
        name="needs_key", label="Test", kind=StageKind.LLM,
        fn=fn, requires_api_key=True,
    )
    pp = PostProcessor(stages=[stage])
    result, step = pp.run({"ctx_json": {}}, api_key="test-key")
    assert len(called) == 1
    assert step == 1


def test_update_status_increments_step_number():
    statuses = []

    def mock_status(text, current_step, total_steps):
        statuses.append((text, current_step, total_steps))

    def noop(payload, **kwargs):
        return payload

    stages = [
        StageDescriptor(name="a", label="Step A", kind=StageKind.PURE, fn=noop),
        StageDescriptor(name="b", label="Step B", kind=StageKind.PURE, fn=noop),
    ]
    pp = PostProcessor(stages=stages)
    pp.run({}, update_status=mock_status, current_step=5, total_steps=10)
    assert statuses == [("Step A", 6, 10), ("Step B", 7, 10)]


def test_consume_metrics_called_for_llm_stages():
    consumed = []

    def mock_consume(payload):
        consumed.append(payload.get("_request_metrics"))

    def mock_llm_fn(payload, **kwargs):
        payload["_request_metrics"] = {"evidence_calls_2_5pro": 3}
        return payload

    def mock_pure_fn(payload, **kwargs):
        return payload

    stages = [
        StageDescriptor(name="pure", label="Pure", kind=StageKind.PURE, fn=mock_pure_fn),
        StageDescriptor(
            name="llm", label="LLM", kind=StageKind.LLM,
            fn=mock_llm_fn, requires_api_key=True,
        ),
    ]
    pp = PostProcessor(stages=stages)
    pp.run({"ctx_json": {}}, api_key="test-key", consume_metrics=mock_consume)
    # consume_metrics should only be called for the LLM stage
    assert len(consumed) == 1
    assert consumed[0] == {"evidence_calls_2_5pro": 3}


def test_consume_metrics_not_called_for_pure_stages():
    consumed = []

    def mock_consume(payload):
        consumed.append(True)

    def noop(payload, **kwargs):
        return payload

    stages = [
        StageDescriptor(name="pure", label="Pure", kind=StageKind.PURE, fn=noop),
    ]
    pp = PostProcessor(stages=stages)
    pp.run({}, consume_metrics=mock_consume)
    assert len(consumed) == 0


def test_skip_flag_prevents_stage():
    called = []

    def fn(payload, **kwargs):
        called.append(True)
        return payload

    stage = StageDescriptor(
        name="skippable", label="Test", kind=StageKind.LLM,
        fn=fn, skip_flag="skip_me",
    )
    pp = PostProcessor(stages=[stage], skip_flags={"skip_me": True})
    pp.run({})
    assert len(called) == 0


def test_default_skip_prevents_stage():
    called = []

    def fn(payload, **kwargs):
        called.append(True)
        return payload

    stage = StageDescriptor(
        name="deprecated", label="Test", kind=StageKind.PURE,
        fn=fn, default_skip=True,
    )
    pp = PostProcessor(stages=[stage])
    pp.run({})
    assert len(called) == 0


def test_custom_stages_override_default():
    """PostProcessor accepts custom stages list, ignoring defaults."""

    def fn(payload, **kwargs):
        payload["custom"] = True
        return payload

    stages = [StageDescriptor(name="custom", label="Custom", kind=StageKind.PURE, fn=fn)]
    pp = PostProcessor(stages=stages)
    assert pp.count_steps() == 1
    result, _ = pp.run({})
    assert result["custom"] is True


# ---------------------------------------------------------------------------
# Adapter tests (using monkeypatch on actual utility modules)
# ---------------------------------------------------------------------------


def test_schema_pre_gate_adapter(monkeypatch):
    from utils.post_processor import _adapt_schema_pre_gate
    call_args = {}

    def mock_validate(json_data, fix_arrows=True, fix_chains=True, fix_directions=True, verbose=False):
        call_args.update(dict(fix_arrows=fix_arrows, fix_chains=fix_chains, fix_directions=fix_directions))
        return json_data

    monkeypatch.setattr("utils.schema_validator.validate_schema_consistency", mock_validate)
    _adapt_schema_pre_gate({"ctx_json": {}})
    assert call_args["fix_arrows"] is True
    assert call_args["fix_chains"] is True
    assert call_args["fix_directions"] is True


def test_finalize_metadata_adapter(monkeypatch):
    from utils.post_processor import _adapt_finalize_metadata
    call_args = {}

    def mock_finalize(json_data, add_arrow_notation=True, validate_snapshot=True, verbose=False):
        call_args.update(dict(add_arrow_notation=add_arrow_notation, validate_snapshot=validate_snapshot))
        return json_data

    monkeypatch.setattr("utils.schema_validator.finalize_interaction_metadata", mock_finalize)
    _adapt_finalize_metadata({"ctx_json": {}})
    assert call_args["add_arrow_notation"] is True
    assert call_args["validate_snapshot"] is True


# ---------------------------------------------------------------------------
# Deduplication JSON parsing tests
# ---------------------------------------------------------------------------

def test_dedup_compare_functions_parses_json(monkeypatch):
    """compare_functions must correctly parse JSON from structured output."""
    from utils.deduplicate_functions import compare_functions

    def mock_flash(prompt, api_key):
        return '{"duplicate": "YES", "better": "2", "reason": "Function 2 is more specific"}'

    monkeypatch.setattr("utils.deduplicate_functions.call_gemini_flash", mock_flash)

    func1 = {"function": "DNA Repair", "direction": "main_to_primary", "pathway": "DDR"}
    func2 = {"function": "DNA Damage Repair", "direction": "main_to_primary", "pathway": "DDR"}
    is_dup, better = compare_functions(func1, func2, "ATXN3-VCP", "fake-key")
    assert is_dup is True
    assert better == 2


def test_dedup_compare_functions_not_duplicate(monkeypatch):
    """compare_functions returns False for NO response."""
    from utils.deduplicate_functions import compare_functions

    def mock_flash(prompt, api_key):
        return '{"duplicate": "NO", "better": "EQUAL", "reason": "Different mechanisms"}'

    monkeypatch.setattr("utils.deduplicate_functions.call_gemini_flash", mock_flash)

    func1 = {"function": "Autophagy", "direction": "main_to_primary", "pathway": "Autophagy"}
    func2 = {"function": "ER-phagy", "direction": "main_to_primary", "pathway": "ER Stress"}
    is_dup, better = compare_functions(func1, func2, "ATXN3-VCP", "fake-key")
    assert is_dup is False
    assert better == 0


def test_dedup_batch_compare_functions(monkeypatch):
    """batch_compare_functions returns correct keep_indices from LLM JSON response."""
    from utils.deduplicate_functions import batch_compare_functions

    class _FakeResp:
        text = '{"keep_indices": [0, 2], "groups": [{"indices": [1, 2], "kept": 2, "reason": "More specific"}]}'

    class _FakeModels:
        def generate_content(self, model, contents, config):
            return _FakeResp()

    class _FakeClient:
        def __init__(self, **kwargs):
            self.models = _FakeModels()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr("utils.deduplicate_functions.genai.Client", _FakeClient)

    functions = [
        {"function": "A", "direction": "main_to_primary"},
        {"function": "A duplicate", "direction": "main_to_primary"},
        {"function": "B", "direction": "primary_to_main"},
    ]
    keep = batch_compare_functions(functions, "X-Y", "fake-key")
    assert keep == [0, 2]


def test_dedup_compare_functions_handles_malformed_json(monkeypatch):
    """compare_functions falls back to not-duplicate on parse error."""
    from utils.deduplicate_functions import compare_functions

    def mock_flash(prompt, api_key):
        return "INVALID JSON"

    monkeypatch.setattr("utils.deduplicate_functions.call_gemini_flash", mock_flash)

    func1 = {"function": "A", "direction": "main_to_primary"}
    func2 = {"function": "B", "direction": "main_to_primary"}
    is_dup, better = compare_functions(func1, func2, "X-Y", "fake-key")
    assert is_dup is False
    assert better == 0


# ---------------------------------------------------------------------------
# Arrow validator merge tests
# ---------------------------------------------------------------------------

def test_validate_arrows_for_payload_importable():
    """validate_arrows_for_payload should be importable from arrow_effect_validator."""
    from utils.arrow_effect_validator import validate_arrows_for_payload
    assert callable(validate_arrows_for_payload)


def test_extract_direct_mediator_links_importable():
    """Merged function should be importable."""
    from utils.arrow_effect_validator import extract_direct_mediator_links_from_json
    assert callable(extract_direct_mediator_links_from_json)


def test_extract_direct_mediator_links_empty_payload():
    from utils.arrow_effect_validator import extract_direct_mediator_links_from_json
    result = extract_direct_mediator_links_from_json({"snapshot_json": {"interactors": []}})
    assert result == []


def test_extract_direct_mediator_links_uses_full_chain_not_self_pair(monkeypatch):
    from utils import arrow_effect_validator as validator

    seen_pairs = []

    def fake_extract(source, target, chain_interaction):
        seen_pairs.append((source, target))
        return None

    monkeypatch.setattr(validator, "_extract_from_chain_evidence", fake_extract)

    payload = {
        "snapshot_json": {
            "main": "ATXN3",
            "interactors": [
                {
                    "primary": "NPLOC4",
                    "interaction_type": "indirect",
                    "upstream_interactor": "NPLOC4",
                    "mediator_chain": ["NPLOC4"],
                    "functions": [{"function": "VCP adapter relay"}],
                    "chain_context": {
                        "full_chain": ["UFD1", "NPLOC4", "VCP"],
                        "query_protein": "ATXN3",
                        "query_position": None,
                    },
                }
            ],
        }
    }

    result = validator.extract_direct_mediator_links_from_json(payload)

    assert result == []
    assert ("NPLOC4", "NPLOC4") not in seen_pairs
    assert seen_pairs == [("UFD1", "NPLOC4"), ("NPLOC4", "VCP")]


def test_extract_from_chain_evidence_no_functions():
    from utils.arrow_effect_validator import _extract_from_chain_evidence
    result = _extract_from_chain_evidence("MED", "TGT", {"functions": []})
    assert result is None


# ---------------------------------------------------------------------------
# Evidence-only mode tests
# ---------------------------------------------------------------------------

_ALL_SKIP_EXCEPT_EVIDENCE = {
    "skip_chain_tagging": True,
    "skip_chain_link_completeness": True,
    "skip_normalize_function_contexts": True,
    "skip_schema_validation": True,
    "skip_deduplicator": True,
    "skip_pmid_update": True,
    "skip_arrow_validation": True,
    "skip_interaction_metadata": True,
    "skip_clean_names": True,
    "skip_quality_validation": True,
    "skip_finalize_metadata": True,
    # skip_validation deliberately absent → evidence runs
    # S1: skip_bidirectional_split removed — stage no longer exists
}


def test_evidence_only_mode_stage_count():
    """When all stages except evidence_validation are skipped, exactly 1 stage is active."""
    pp = PostProcessor(skip_flags=_ALL_SKIP_EXCEPT_EVIDENCE)
    assert pp.count_steps() == 1
    assert pp.active_stages()[0].name == "evidence_validation"


def test_evidence_only_mode_runs_and_preserves_data(monkeypatch):
    """Full PostProcessor.run() with only evidence_validation active preserves original data."""
    from copy import deepcopy
    from utils.evidence_validator import _merge_validated_interactor

    # Simulate what validate_and_enrich_evidence does: call _merge then update payload
    def fake_validate(payload, api_key, verbose=False, step_logger=None):
        for interactor in payload["ctx_json"]["interactors"]:
            val_int = {
                "primary": interactor["primary"],
                "is_valid": True,
                "mechanism_correction": "Validated mechanism",
                "functions": [
                    {
                        "function": interactor["functions"][0]["function"],
                        "evidence": [{"paper_title": "Test Paper", "year": 2024}],
                        "arrow": "inhibits",  # corrected from original
                    }
                ],
            }
            _merge_validated_interactor(interactor, val_int)

        validated = payload["ctx_json"]["interactors"]
        payload["ctx_json"]["interactors"] = validated
        if "snapshot_json" in payload:
            payload["snapshot_json"]["interactors"] = deepcopy(validated)

        payload.setdefault("_request_metrics", {})["evidence_calls_2_5pro"] = 1
        return payload

    monkeypatch.setattr(
        "utils.evidence_validator.validate_and_enrich_evidence",
        fake_validate,
    )

    payload = {
        "ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {
                    "primary": "VCP",
                    "pmids": ["12345678"],
                    "strength": 0.9,
                    "direction": "forward",
                    "mechanism": "deubiquitination",
                    "functions": [
                        {
                            "function": "Proteasomal Degradation",
                            "arrow": "activates",
                            "pmids": ["99999999"],
                            "custom_field": "preserve_me",
                        }
                    ],
                }
            ],
        },
        "snapshot_json": {
            "main": "ATXN3",
            "interactors": [
                {
                    "primary": "VCP",
                    "pmids": ["12345678"],
                    "strength": 0.9,
                    "direction": "forward",
                    "mechanism": "deubiquitination",
                    "functions": [
                        {
                            "function": "Proteasomal Degradation",
                            "arrow": "activates",
                            "pmids": ["99999999"],
                            "custom_field": "preserve_me",
                        }
                    ],
                }
            ],
        },
    }

    pp = PostProcessor(skip_flags=_ALL_SKIP_EXCEPT_EVIDENCE)
    result, step = pp.run(payload, api_key="test-key")

    interactor = result["ctx_json"]["interactors"][0]

    # Original fields preserved
    assert interactor["pmids"] == ["12345678"]
    assert interactor["strength"] == 0.9
    assert interactor["direction"] == "forward"
    assert interactor["mechanism"] == "deubiquitination"

    # Validation metadata added
    assert interactor["is_valid"] is True
    assert interactor["mechanism_correction"] == "Validated mechanism"

    # Functions enriched, not replaced
    func = interactor["functions"][0]
    assert func["arrow"] == "inhibits"  # corrected
    assert func["evidence"] == [{"paper_title": "Test Paper", "year": 2024}]  # added
    assert func["pmids"] == ["99999999"]  # preserved
    assert func["custom_field"] == "preserve_me"  # preserved

    # snapshot_json is independent copy
    result["ctx_json"]["interactors"][0]["_mutate_test"] = True
    assert "_mutate_test" not in result["snapshot_json"]["interactors"][0]

    # Step counter advanced by 1
    assert step == 1
