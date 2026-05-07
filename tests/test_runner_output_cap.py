#!/usr/bin/env python3
"""Regression tests for runner output-token cap handling."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import runner
from pipeline.types import StepConfig


def _make_step(max_output_tokens: int = 65536) -> StepConfig:
    return StepConfig(
        name="test_step",
        model="gemini-3.1-pro-preview",
        prompt_template="x",
        expected_columns=["ctx_json"],
        use_google_search=False,
        max_output_tokens=max_output_tokens,
        thinking_level="high",
    )


def test_call_gemini_model_clamps_to_server_cap(monkeypatch):
    calls = []

    class _FakeUsage:
        cached_content_token_count = 0
        candidates_token_count = 10
        total_token_count = 30
        prompt_token_count = 20

    class _FakeResp:
        text = '{"ok":true}'
        usage_metadata = _FakeUsage()
        candidates = []

    class _FakeModels:
        def __init__(self):
            self._count = 0

        def generate_content(self, model, contents, config):
            self._count += 1
            dump = config.model_dump(exclude_none=True)
            calls.append(dump.get("max_output_tokens"))
            if self._count == 1:
                raise Exception(
                    "400 INVALID_ARGUMENT. {'error': {'code': 400, "
                    "'message': 'The answer candidate length is too long with 9676 tokens, "
                    "which exceeds the maximum token limit of 8192.', 'status': 'INVALID_ARGUMENT'}}"
                )
            return _FakeResp()

    class _FakeClient:
        def __init__(self):
            self.models = _FakeModels()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_ALLOW_SERVER_OUTPUT_CLAMP", "true")
    monkeypatch.setattr(runner, "get_client", lambda api_key=None: _FakeClient())

    step = _make_step(max_output_tokens=65536)
    text, stats = runner.call_gemini_model(step, prompt="hello")

    assert text == '{"ok":true}'
    assert stats["output_tokens"] == 10
    assert calls[0] == 65536
    assert calls[1] == 8192


def test_call_gemini_model_batch_clamps_to_server_cap(monkeypatch):
    create_caps = []

    class _FakeUsage:
        prompt_token_count = 20
        thoughts_token_count = 3
        candidates_token_count = 10
        total_token_count = 33

    class _FakeResp:
        text = '{"ok":true}'
        usage_metadata = _FakeUsage()
        candidates = []

    class _FakeInlineResponse:
        def __init__(self):
            self.response = _FakeResp()
            self.error = None

    class _FakeDest:
        def __init__(self):
            self.inlined_responses = [_FakeInlineResponse()]

    class _FakeState:
        name = "JOB_STATE_SUCCEEDED"

    class _FakeBatchJob:
        def __init__(self):
            self.name = "batches/fake"
            self.state = _FakeState()
            self.dest = _FakeDest()
            self.error = None

    class _FakeBatches:
        def __init__(self):
            self._create_calls = 0

        def create(self, model, src, config):
            self._create_calls += 1
            inlined_cfg = src[0].config.model_dump(exclude_none=True)
            create_caps.append(inlined_cfg.get("max_output_tokens"))
            if self._create_calls == 1:
                raise Exception(
                    "400 INVALID_ARGUMENT. {'error': {'code': 400, "
                    "'message': 'The answer candidate length is too long with 9676 tokens, "
                    "which exceeds the maximum token limit of 8192.', 'status': 'INVALID_ARGUMENT'}}"
                )
            return _FakeBatchJob()

        def get(self, name):
            return _FakeBatchJob()

    class _FakeClient:
        def __init__(self):
            self.batches = _FakeBatches()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_ALLOW_SERVER_OUTPUT_CLAMP", "true")
    monkeypatch.setattr(runner, "get_client", lambda api_key=None: _FakeClient())

    step = _make_step(max_output_tokens=65536)
    text, stats = runner.call_gemini_model(step, prompt="hello", request_mode="batch")

    assert text == '{"ok":true}'
    assert stats["request_mode"] == "batch"
    assert stats["output_tokens"] == 10
    assert create_caps[0] == 65536
    assert create_caps[1] == 8192


def test_call_gemini_model_does_not_auto_clamp_by_default(monkeypatch):
    class _FakeModels:
        def generate_content(self, model, contents, config):
            raise Exception(
                "400 INVALID_ARGUMENT. {'error': {'code': 400, "
                "'message': 'The answer candidate length is too long with 9676 tokens, "
                "which exceeds the maximum token limit of 8192.', 'status': 'INVALID_ARGUMENT'}}"
            )

    class _FakeClient:
        def __init__(self):
            self.models = _FakeModels()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.delenv("GEMINI_ALLOW_SERVER_OUTPUT_CLAMP", raising=False)
    monkeypatch.setattr(runner, "get_client", lambda api_key=None: _FakeClient())

    step = _make_step(max_output_tokens=60000)
    try:
        runner.call_gemini_model(step, prompt="hello")
        assert False, "Expected PipelineError when server cap is hit and clamping is disabled"
    except runner.PipelineError as exc:
        msg = str(exc)
        assert "Server cap for gemini-3.1-pro-preview is 8192 tokens" in msg
        assert "Auto-clamping is disabled" in msg
