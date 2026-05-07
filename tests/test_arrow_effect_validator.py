#!/usr/bin/env python3
"""Tests for arrow validator quota-stop behavior."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import utils.arrow_effect_validator as arrow_module


def test_arrow_validation_stops_scheduling_after_daily_quota(monkeypatch):
    calls = {"count": 0}

    def fake_validate(interactor, main_protein, api_key, verbose=False, **kwargs):
        calls["count"] += 1
        raise arrow_module.DailyQuotaExceededError("daily quota exhausted")

    monkeypatch.setenv("VALIDATION_MAX_WORKERS", "1")
    monkeypatch.setattr(arrow_module, "validate_single_interaction", fake_validate)

    payload = {
        "snapshot_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "A"},
                {"primary": "B"},
                {"primary": "C"},
            ],
        }
    }

    out = arrow_module.validate_arrows_and_effects(payload, api_key="test-key", verbose=False)

    # Primary model quota hit (1 call) → fallback attempted (1 call) → also exhausted → stop
    assert calls["count"] <= 2
    interactors = out["snapshot_json"]["interactors"]
    assert len(interactors) == 3
    assert interactors[0].get("_validation_skipped_reason") == "quota_exhausted"
    assert interactors[1].get("_validation_skipped_reason") == "quota_exhausted"
    assert interactors[2].get("_validation_skipped_reason") == "quota_exhausted"
    assert out["_request_metrics"]["quota_skipped_calls"] >= 3


def _make_fake_gemini_fixtures(monkeypatch):
    """Shared setup: patch Gemini client, config builder, prompt builder, and response parser."""
    captured = {}

    def fake_build_generate_content_config(**kwargs):
        captured["config_kwargs"] = kwargs
        return object()

    class _FakeModels:
        def generate_content(self, model, contents, config):
            captured["model"] = model
            captured["config_obj"] = config
            return object()

    class _FakeClient:
        def __init__(self, api_key=None):
            self.models = _FakeModels()

    monkeypatch.setattr(arrow_module, "build_generate_content_config", fake_build_generate_content_config)
    monkeypatch.setattr(arrow_module, "build_validation_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(arrow_module, "parse_gemini_response", lambda _response: [])
    monkeypatch.setattr(arrow_module, "get_client", lambda api_key=None: _FakeClient(api_key))

    return captured


def test_validate_single_interaction_uses_thinking_level(monkeypatch):
    """Complex interactions use low thinking on the Flash-only path."""
    captured = _make_fake_gemini_fixtures(monkeypatch)

    interactor = {
        "primary": "VCP",
        "interaction_type": "indirect",
        "mediator_chain": ["UBXN6"],
        "functions": [],
    }
    out = arrow_module.validate_single_interaction(
        interactor,
        main_protein="ATXN3",
        api_key="test-key",
        model_id="gemini-3.1-pro-preview",
    )

    assert out["primary"] == "VCP"
    assert captured["model"] == "gemini-3.1-pro-preview"
    assert captured["config_kwargs"]["thinking_level"] == "low"
    assert captured["config_kwargs"].get("response_mime_type") == "application/json"


def test_simple_interaction_disables_thinking(monkeypatch):
    """Simple direct interactions disable thinking to keep Flash calls cheap."""
    captured = _make_fake_gemini_fixtures(monkeypatch)
    monkeypatch.setenv("GEMINI_MODEL_ARROW_FAST", "gemini-3-flash-preview")

    interactor = {"primary": "VCP", "interaction_type": "direct", "functions": [{"function": "f1"}]}
    out = arrow_module.validate_single_interaction(
        interactor,
        main_protein="ATXN3",
        api_key="test-key",
    )

    assert out["primary"] == "VCP"
    assert captured["model"] == "gemini-3-flash-preview"
    assert captured["config_kwargs"]["thinking_level"] == "off"


def test_many_functions_classified_as_complex(monkeypatch):
    """Interactions with >4 functions are complex and use low Flash thinking."""
    captured = _make_fake_gemini_fixtures(monkeypatch)

    interactor = {
        "primary": "VCP",
        "interaction_type": "direct",
        "functions": [{"function": f"f{i}"} for i in range(5)],
    }
    out = arrow_module.validate_single_interaction(
        interactor,
        main_protein="ATXN3",
        api_key="test-key",
    )

    assert captured["config_kwargs"]["thinking_level"] == "low"


def test_classify_interaction_complexity():
    """Unit test for _classify_interaction_complexity helper."""
    assert arrow_module._classify_interaction_complexity(
        {"interaction_type": "direct", "functions": []}
    ) == "simple"
    assert arrow_module._classify_interaction_complexity(
        {"interaction_type": "indirect", "mediator_chain": ["X"], "functions": []}
    ) == "complex"
    assert arrow_module._classify_interaction_complexity(
        {"interaction_type": "direct", "functions": [{"function": f"f{i}"} for i in range(5)]}
    ) == "complex"
    assert arrow_module._classify_interaction_complexity(
        {"interaction_type": "direct", "functions": [{"function": f"f{i}"} for i in range(4)]}
    ) == "simple"
    # indirect without mediator_chain is simple
    assert arrow_module._classify_interaction_complexity(
        {"interaction_type": "indirect", "mediator_chain": [], "functions": []}
    ) == "simple"
