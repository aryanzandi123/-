#!/usr/bin/env python3
"""Tests for runner batch-mode transport behavior."""

import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import runner
from pipeline.types import StepConfig


def _make_step(model: str = "gemini-3.1-pro-preview") -> StepConfig:
    return StepConfig(
        name="batch_test_step",
        model=model,
        prompt_template="x",
        expected_columns=["ctx_json"],
        use_google_search=False,
        max_output_tokens=65536,
        thinking_level="high",
    )


def test_batch_mode_success_returns_text_and_stats(monkeypatch):
    class _Usage:
        prompt_token_count = 123
        thoughts_token_count = 45
        candidates_token_count = 67
        total_token_count = 235

    class _Resp:
        text = '{"ok":true}'
        usage_metadata = _Usage()
        candidates = []

    class _Inline:
        response = _Resp()
        error = None

    class _Dest:
        inlined_responses = [_Inline()]

    class _State:
        name = "JOB_STATE_SUCCEEDED"

    class _BatchJob:
        name = "batches/success"
        state = _State()
        dest = _Dest()
        error = None

    class _FakeBatches:
        def create(self, model, src, config):
            return _BatchJob()

        def get(self, name):
            return _BatchJob()

    class _FakeClient:
        def __init__(self):
            self.batches = _FakeBatches()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(runner, "get_client", lambda api_key=None: _FakeClient())

    text, stats = runner.call_gemini_model(_make_step(), "hello", request_mode="batch")
    assert text == '{"ok":true}'
    assert stats["request_mode"] == "batch"
    assert stats["prompt_tokens"] == 123
    assert stats["thinking_tokens"] == 45
    assert stats["output_tokens"] == 67
    assert stats["total_tokens"] == 235


def test_batch_mode_terminal_failure_raises_pipeline_error(monkeypatch):
    class _State:
        name = "JOB_STATE_FAILED"

    class _BatchJob:
        name = "batches/fail"
        state = _State()
        dest = type("_Dest", (), {"inlined_responses": []})()
        error = "downstream failure"

    class _FakeBatches:
        def create(self, model, src, config):
            return _BatchJob()

        def get(self, name):
            return _BatchJob()

    class _FakeClient:
        def __init__(self):
            self.batches = _FakeBatches()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(runner, "get_client", lambda api_key=None: _FakeClient())

    try:
        runner.call_gemini_model(_make_step(), "hello", request_mode="batch")
        assert False, "Expected PipelineError for failed batch state"
    except runner.PipelineError as exc:
        msg = str(exc)
        assert "batches/fail" in msg
        assert "JOB_STATE_FAILED" in msg


def test_batch_mode_cancel_calls_batch_cancel(monkeypatch):
    cancel_event = threading.Event()
    cancelled = {"called": False}

    class _State:
        name = "JOB_STATE_RUNNING"

    class _BatchJob:
        name = "batches/cancel"
        state = _State()
        dest = type("_Dest", (), {"inlined_responses": []})()
        error = None

    class _FakeBatches:
        def create(self, model, src, config):
            # Simulate user cancellation immediately after submission.
            cancel_event.set()
            return _BatchJob()

        def get(self, name):
            return _BatchJob()

        def cancel(self, name):
            cancelled["called"] = True

    class _FakeClient:
        def __init__(self):
            self.batches = _FakeBatches()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(runner, "get_client", lambda api_key=None: _FakeClient())

    try:
        runner.call_gemini_model(
            _make_step(),
            "hello",
            request_mode="batch",
            cancel_event=cancel_event,
        )
        assert False, "Expected cancellation PipelineError"
    except runner.PipelineError as exc:
        assert "cancelled by user" in str(exc).lower()
    assert cancelled["called"] is True


def test_batch_mode_falls_back_for_non_core_model(monkeypatch):
    calls = {"standard": 0, "batch_create": 0}

    class _Usage:
        prompt_token_count = 10
        thoughts_token_count = 2
        candidates_token_count = 5
        total_token_count = 17

    class _Resp:
        text = '{"ok":true}'
        usage_metadata = _Usage()
        candidates = []

    class _FakeModels:
        def generate_content(self, model, contents, config):
            calls["standard"] += 1
            return _Resp()

    class _FakeBatches:
        def create(self, model, src, config):
            calls["batch_create"] += 1
            raise AssertionError("Batch create should not be called for non-core model")

    class _FakeClient:
        def __init__(self):
            self.models = _FakeModels()
            self.batches = _FakeBatches()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(runner, "get_client", lambda api_key=None: _FakeClient())

    text, stats = runner.call_gemini_model(
        _make_step(model="gemini-2.5-pro"),
        "hello",
        request_mode="batch",
    )
    assert text == '{"ok":true}'
    assert stats["request_mode"] == "standard"
    assert calls["standard"] == 1
    assert calls["batch_create"] == 0
