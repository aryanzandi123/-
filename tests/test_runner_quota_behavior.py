#!/usr/bin/env python3
"""Tests for runner core quota fail-fast behavior."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import runner
from pipeline.types import StepConfig


def _make_step() -> StepConfig:
    return StepConfig(
        name="quota_test_step",
        model="gemini-3.1-pro-preview",
        prompt_template="x",
        expected_columns=["ctx_json"],
        use_google_search=False,
        max_output_tokens=65536,
        thinking_level="high",
    )


def test_core_daily_quota_is_non_retryable(monkeypatch):
    calls = {"count": 0}

    class _FakeModels:
        def generate_content(self, model, contents, config):
            calls["count"] += 1
            raise Exception(
                "429 RESOURCE_EXHAUSTED. "
                "Quota exceeded for metric: "
                "generativelanguage.googleapis.com/generate_requests_per_model_per_day, limit: 0"
            )

    class _FakeClient:
        def __init__(self):
            self.models = _FakeModels()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(runner, "get_client", lambda api_key=None: _FakeClient())

    step = _make_step()
    try:
        runner.call_gemini_model(step, prompt="hello")
        assert False, "Expected PipelineError for daily quota exhaustion"
    except runner.PipelineError as exc:
        msg = str(exc)
        assert "daily quota exhausted" in msg.lower()
        assert "no fallback is allowed for core stages" in msg.lower()
    assert calls["count"] == 1
