"""Integration tests for the pipeline runner end-to-end flow."""

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from utils.json_helpers import PipelineError

# ---------------------------------------------------------------------------
# Canned response that satisfies parse_json_output expectations
# ---------------------------------------------------------------------------

_CANNED_CTX = json.dumps({
    "ctx_json": {
        "main": "TESTPROT",
        "interactors": [
            {
                "primary": "PARTNER1",
                "confidence": 0.9,
                "functions": [
                    {
                        "function": "Test function",
                        "arrow": "activates",
                        "confidence": 0.8,
                    }
                ],
            }
        ],
        "interactor_history": ["PARTNER1"],
    },
    "step_json": {"step": "test"},
})


# ---------------------------------------------------------------------------
# Fake Gemini SDK objects
# ---------------------------------------------------------------------------

class _FakeUsage:
    prompt_token_count = 100
    thoughts_token_count = 50
    candidates_token_count = 200
    total_token_count = 350
    cached_content_token_count = 0


class _FakeResp:
    def __init__(self, text=_CANNED_CTX):
        self.text = text
        self.usage_metadata = _FakeUsage()
        self.candidates = []


class _FakeModels:
    def __init__(self):
        self.call_count = 0

    def generate_content(self, *, model, contents, config):
        self.call_count += 1
        return _FakeResp()


class _FakeCaches:
    def get(self, **kwargs):
        raise Exception("not found")

    def create(self, **kwargs):
        class C:
            name = "fake-cache-id"
        return C()


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()
        self.caches = _FakeCaches()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_gemini(monkeypatch):
    """Patch Gemini runtime so no real API calls are made."""
    client = _FakeClient()
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr("utils.gemini_runtime.get_client", lambda *a, **kw: client)
    monkeypatch.setattr("runner.get_client", lambda *a, **kw: client)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPipelineCompletesMinimalConfig:
    """run_pipeline should complete with a minimal step list and fake client."""

    def test_pipeline_completes_minimal_config(self, monkeypatch):
        client = _patch_gemini(monkeypatch)

        # Disable step logger to avoid side effects
        monkeypatch.setattr("runner.STEP_LOGGER_AVAILABLE", False)

        from pipeline.config_dynamic import generate_pipeline
        from runner import run_pipeline

        # Use smallest possible pipeline: 1 interactor round, 1 function round
        # generate_pipeline includes deep research + QC + snapshot steps too.
        # We monkeypatch call_gemini_model to bypass the real SDK entirely.
        monkeypatch.setattr(
            "runner.call_gemini_model",
            lambda step, prompt, **kw: (_CANNED_CTX, {
                "prompt_tokens": 100,
                "thinking_tokens": 50,
                "output_tokens": 200,
                "total_tokens": 350,
                "request_mode": "standard",
            }),
        )

        cancel = threading.Event()
        payload, _step_logger = run_pipeline(
            "TESTPROT",
            verbose=False,
            stream=False,
            num_interactor_rounds=1,
            num_function_rounds=1,
            cancel_event=cancel,
        )

        assert payload is not None
        assert "ctx_json" in payload
        assert payload["ctx_json"]["main"] == "TESTPROT"


class TestPipelineCancellation:
    """Setting cancel_event before calling run_pipeline raises PipelineError."""

    def test_pipeline_cancellation(self, monkeypatch):
        _patch_gemini(monkeypatch)
        monkeypatch.setattr("runner.STEP_LOGGER_AVAILABLE", False)

        from runner import run_pipeline

        cancel = threading.Event()
        cancel.set()  # pre-cancel

        with pytest.raises(PipelineError, match="(?i)cancel"):
            run_pipeline(
                "TESTPROT",
                verbose=False,
                stream=False,
                num_interactor_rounds=1,
                num_function_rounds=1,
                cancel_event=cancel,
            )


class TestSnapshotStepNoApiCall:
    """step3_snapshot must NOT call the model — it assembles locally."""

    def test_snapshot_step_no_api_call(self, monkeypatch):
        client = _patch_gemini(monkeypatch)
        monkeypatch.setattr("runner.STEP_LOGGER_AVAILABLE", False)

        call_log = []

        def fake_call_gemini(step, prompt, **kw):
            call_log.append(step.name)
            return (_CANNED_CTX, {
                "prompt_tokens": 100,
                "thinking_tokens": 50,
                "output_tokens": 200,
                "total_tokens": 350,
                "request_mode": "standard",
            })

        monkeypatch.setattr("runner.call_gemini_model", fake_call_gemini)

        from runner import run_pipeline

        cancel = threading.Event()
        payload, _ = run_pipeline(
            "TESTPROT",
            verbose=False,
            stream=False,
            num_interactor_rounds=1,
            num_function_rounds=1,
            cancel_event=cancel,
        )

        # step3_snapshot should NOT appear in call_log because it runs locally
        assert "step3_snapshot" not in call_log
        # Snapshot should still have produced output
        assert payload is not None
