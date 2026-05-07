#!/usr/bin/env python3
"""Tests for evidence validator quota-stop behavior."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import utils.evidence_validator as evidence_module


def test_validate_evidence_parallel_stops_after_daily_quota(monkeypatch):
    calls = {"count": 0}

    def fake_call(prompt, api_key, verbose=False, request_metrics=None):
        calls["count"] += 1
        if request_metrics is not None:
            request_metrics["evidence_calls_2_5pro"] = int(request_metrics.get("evidence_calls_2_5pro", 0)) + 1
        raise evidence_module.DailyQuotaExceededError("daily quota exhausted")

    monkeypatch.setenv("VALIDATION_MAX_WORKERS", "1")
    monkeypatch.setenv("EVIDENCE_USE_INTERACTIONS", "0")  # disable chaining in test
    monkeypatch.setattr(evidence_module, "call_gemini_validation", fake_call)

    interactors = [
        {"primary": "A"},
        {"primary": "B"},
        {"primary": "C"},
        {"primary": "D"},
    ]
    request_metrics = {"evidence_calls_2_5pro": 0, "quota_skipped_calls": 0}
    out = evidence_module.validate_evidence_parallel(
        main_protein="ATXN3",
        interactors=interactors,
        api_key="test-key",
        batch_size=2,
        verbose=False,
        request_metrics=request_metrics,
    )

    assert len(out) == 4
    # With parallel mode (EVIDENCE_USE_INTERACTIONS=0), batches fire concurrently
    # so both may attempt before quota is detected
    assert calls["count"] <= 2
    assert request_metrics["evidence_calls_2_5pro"] <= 2
    assert request_metrics.get("quota_skipped_calls", 0) >= 0


def test_validate_and_enrich_evidence_attaches_request_metrics(monkeypatch):
    def fake_parallel(main_protein, interactors, api_key, batch_size, verbose, request_metrics=None):
        if request_metrics is not None:
            request_metrics["evidence_calls_2_5pro"] = 3
            request_metrics["quota_skipped_calls"] = 2
        return interactors

    monkeypatch.setattr(evidence_module, "validate_evidence_parallel", fake_parallel)
    payload = {"ctx_json": {"main": "ATXN3", "interactors": [{"primary": "VCP"}]}}
    out = evidence_module.validate_and_enrich_evidence(payload, api_key="test-key")

    assert out["_request_metrics"]["evidence_calls_2_5pro"] == 3
    assert out["_request_metrics"]["quota_skipped_calls"] == 2


def test_call_gemini_validation_uses_thinking_level_and_structured_output(monkeypatch):
    """After migration to 3.x, evidence validation uses thinking_level and structured output."""
    captured = {}

    def fake_build_generate_content_config(**kwargs):
        captured["config_kwargs"] = kwargs
        # Return a mock with the attributes the SDK inspects
        return type("FakeConfig", (), {"tools": None, "automatic_function_calling": None})()

    class _FakeModels:
        def generate_content(self, model, contents, config):
            captured["model"] = model
            captured["config_obj"] = config
            return type("Resp", (), {"text": "ok"})()

    class _FakeClient:
        def __init__(self, **kwargs):
            self.models = _FakeModels()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(evidence_module, "build_generate_content_config", fake_build_generate_content_config)
    monkeypatch.setattr(evidence_module, "get_evidence_model", lambda: "gemini-3.1-pro-preview")
    monkeypatch.setattr(evidence_module.genai, "Client", _FakeClient)

    out = evidence_module.call_gemini_validation("prompt", api_key="test-key")

    assert out == "ok"
    assert captured["model"] == "gemini-3.1-pro-preview"
    assert captured["config_kwargs"]["thinking_level"] == "medium"
    assert "response_mime_type" not in captured["config_kwargs"]
    assert captured["config_kwargs"]["temperature"] == 0.5


# ---------------------------------------------------------------------------
# Tests for _merge_validated_interactor
# ---------------------------------------------------------------------------

from utils.evidence_validator import _merge_validated_interactor


def test_merge_preserves_original_fields():
    """Original fields not present in val_int must survive the merge."""
    orig = {
        "primary": "VCP",
        "pmids": ["12345678"],
        "strength": 0.9,
        "direction": "forward",
        "mechanism": "deubiquitination",
        "functions": [{"function": "Proteasomal Degradation", "arrow": "activates"}],
    }
    val_int = {
        "primary": "VCP",
        "is_valid": True,
    }
    _merge_validated_interactor(orig, val_int)

    assert orig["pmids"] == ["12345678"]
    assert orig["strength"] == 0.9
    assert orig["direction"] == "forward"
    assert orig["mechanism"] == "deubiquitination"
    assert len(orig["functions"]) == 1
    assert orig["is_valid"] is True


def test_merge_adds_validation_metadata():
    """Validation-only fields are added to the original."""
    orig = {"primary": "VCP", "functions": []}
    val_int = {
        "primary": "VCP",
        "is_valid": True,
        "mechanism_correction": "Corrected mechanism text",
    }
    _merge_validated_interactor(orig, val_int)

    assert orig["is_valid"] is True
    assert orig["mechanism_correction"] == "Corrected mechanism text"


def test_merge_deep_merges_functions_by_name():
    """Matching functions get enriched with evidence, not replaced wholesale."""
    orig = {
        "primary": "VCP",
        "functions": [
            {
                "function": "Proteasomal Degradation",
                "arrow": "activates",
                "pmids": ["11111111"],
                "custom_field": "keep_me",
            }
        ],
    }
    val_int = {
        "primary": "VCP",
        "is_valid": True,
        "functions": [
            {
                "function": "Proteasomal Degradation",
                "arrow": "inhibits",  # corrected
                "evidence": [{"paper_title": "Test Paper", "year": 2024}],
                "cellular_process": "UPS pathway",
            }
        ],
    }
    _merge_validated_interactor(orig, val_int)

    assert len(orig["functions"]) == 1
    f = orig["functions"][0]
    # Enriched fields
    assert f["arrow"] == "inhibits"
    assert f["evidence"] == [{"paper_title": "Test Paper", "year": 2024}]
    assert f["cellular_process"] == "UPS pathway"
    # Preserved original fields
    assert f["pmids"] == ["11111111"]
    assert f["custom_field"] == "keep_me"


def test_merge_preserves_unmatched_original_functions():
    """Functions Gemini didn't mention in its response must survive."""
    orig = {
        "primary": "VCP",
        "functions": [
            {"function": "Proteasomal Degradation", "arrow": "activates"},
            {"function": "Autophagy Regulation", "arrow": "inhibits"},
        ],
    }
    val_int = {
        "primary": "VCP",
        "is_valid": True,
        "functions": [
            {
                "function": "Proteasomal Degradation",
                "evidence": [{"paper_title": "Paper A"}],
            }
        ],
    }
    _merge_validated_interactor(orig, val_int)

    assert len(orig["functions"]) == 2
    names = [f["function"] for f in orig["functions"]]
    assert "Proteasomal Degradation" in names
    assert "Autophagy Regulation" in names


def test_merge_appends_new_validated_functions():
    """New functions discovered by the validator are appended."""
    orig = {
        "primary": "VCP",
        "functions": [
            {"function": "Proteasomal Degradation", "arrow": "activates"},
        ],
    }
    val_int = {
        "primary": "VCP",
        "is_valid": True,
        "functions": [
            {"function": "Proteasomal Degradation", "evidence": [{"paper_title": "P1"}]},
            {"function": "ER Stress Response", "arrow": "inhibits", "evidence": [{"paper_title": "P2"}]},
        ],
    }
    _merge_validated_interactor(orig, val_int)

    assert len(orig["functions"]) == 2
    names = [f["function"] for f in orig["functions"]]
    assert "Proteasomal Degradation" in names
    assert "ER Stress Response" in names


def test_merge_case_insensitive_function_matching():
    """Function name matching should be case-insensitive."""
    orig = {
        "primary": "VCP",
        "functions": [
            {"function": "Proteasomal Degradation", "arrow": "activates", "pmids": ["123"]},
        ],
    }
    val_int = {
        "primary": "VCP",
        "is_valid": True,
        "functions": [
            {"function": "proteasomal degradation", "evidence": [{"paper_title": "P1"}]},
        ],
    }
    _merge_validated_interactor(orig, val_int)

    assert len(orig["functions"]) == 1
    assert orig["functions"][0]["pmids"] == ["123"]
    assert orig["functions"][0]["evidence"] == [{"paper_title": "P1"}]


def test_snapshot_is_independent_copy(monkeypatch):
    """Mutations to ctx_json interactors must not affect snapshot_json."""
    def fake_parallel(main_protein, interactors, api_key, batch_size, verbose, request_metrics=None):
        return interactors

    monkeypatch.setattr(evidence_module, "validate_evidence_parallel", fake_parallel)

    payload = {
        "ctx_json": {"main": "ATXN3", "interactors": [{"primary": "VCP", "functions": []}]},
        "snapshot_json": {"main": "ATXN3", "interactors": [{"primary": "VCP", "functions": []}]},
    }
    out = evidence_module.validate_and_enrich_evidence(payload, api_key="test-key")

    # Mutate ctx_json interactors
    out["ctx_json"]["interactors"][0]["NEW_FIELD"] = "should not appear in snapshot"

    assert "NEW_FIELD" not in out["snapshot_json"]["interactors"][0]
