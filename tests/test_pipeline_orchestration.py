"""Unit tests for pipeline orchestration helpers in runner.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import pytest

from pipeline.types import StepConfig
from utils.json_helpers import PipelineError
from runner import (
    _coerce_token_count,
    _get_run_request_metrics,
    _increment_run_request_metric,
    _reset_run_request_metrics,
    create_snapshot_from_ctx,
    find_interactors_without_functions,
    validate_steps,
)


def _make_step(name: str = "step1", **overrides) -> StepConfig:
    """Helper to build a minimal StepConfig for testing."""
    defaults = dict(
        name=name,
        model="gemini-3-pro",
        prompt_template="Do something with {user_query}",
        expected_columns=["ctx_json", "step_json"],
    )
    defaults.update(overrides)
    return StepConfig(**defaults)


# ── validate_steps ───────────────────────────────────────────────────────


class TestValidateSteps:
    def test_valid_steps_pass(self):
        steps = [_make_step("a"), _make_step("b")]
        result = validate_steps(steps)
        assert len(result) == 2

    def test_duplicate_name_raises(self):
        steps = [_make_step("dup"), _make_step("dup")]
        with pytest.raises(PipelineError, match="Duplicate step name"):
            validate_steps(steps)

    def test_empty_list_raises(self):
        with pytest.raises(PipelineError, match="empty"):
            validate_steps([])


# ── create_snapshot_from_ctx ─────────────────────────────────────────────


class TestCreateSnapshotFromCtx:
    def test_basic_creation_with_interactors(self):
        ctx = {
            "main": "TP53",
            "interactors": [
                {
                    "primary": "MDM2",
                    "direction": "main_to_primary",
                    "arrow": "inhibits",
                    "confidence": 0.95,
                    "functions": [
                        {
                            "function": "ubiquitination",
                            "arrow": "activates",
                            "cellular_process": "degradation",
                        }
                    ],
                }
            ],
        }
        result = create_snapshot_from_ctx(ctx, ["ctx_json", "snapshot_json", "ndjson"], "test_step")
        assert result["snapshot_json"]["main"] == "TP53"
        assert len(result["snapshot_json"]["interactors"]) == 1
        assert result["snapshot_json"]["interactors"][0]["primary"] == "MDM2"
        assert result["step_json"]["step"] == "test_step"
        assert result["step_json"]["rows"] == 1

    def test_empty_interactors(self):
        ctx = {"main": "TP53", "interactors": []}
        result = create_snapshot_from_ctx(ctx, ["ctx_json", "snapshot_json", "ndjson"], "s")
        assert result["snapshot_json"]["interactors"] == []
        assert result["ndjson"] == []
        assert result["step_json"]["rows"] == 0

    def test_ndjson_format_correct(self):
        ctx = {
            "main": "EGFR",
            "interactors": [
                {"primary": "KRAS", "functions": []},
                {"primary": "BRAF", "functions": []},
            ],
        }
        result = create_snapshot_from_ctx(ctx, ["ctx_json", "snapshot_json", "ndjson"], "s")
        assert result["ndjson"] == []


# ── find_interactors_without_functions ───────────────────────────────────


class TestFindInteractorsWithoutFunctions:
    def test_identifies_missing_functions(self):
        ctx = {
            "interactors": [
                {"primary": "A", "functions": []},
                {"primary": "B"},
            ]
        }
        result = find_interactors_without_functions(ctx)
        names = [r["name"] for r in result]
        assert "A" in names
        assert "B" in names

    def test_skips_interactors_with_functions(self):
        ctx = {
            "interactors": [
                {"primary": "A", "functions": [{"function": "kinase"}]},
                {"primary": "B", "functions": []},
            ]
        }
        result = find_interactors_without_functions(ctx)
        names = [r["name"] for r in result]
        assert "A" not in names
        assert "B" in names


# ── Token metrics helpers ────────────────────────────────────────────────


class TestTokenMetrics:
    def test_coerce_int(self):
        assert _coerce_token_count(42) == 42

    def test_coerce_none(self):
        assert _coerce_token_count(None) == 0

    def test_coerce_string(self):
        assert _coerce_token_count("100") == 100

    def test_coerce_invalid_string(self):
        assert _coerce_token_count("abc") == 0

    def test_metrics_reset_and_increment_lifecycle(self):
        _reset_run_request_metrics()
        metrics = _get_run_request_metrics()
        assert metrics["core_calls_3pro"] == 0

        _increment_run_request_metric("core_calls_3pro", 3)
        metrics = _get_run_request_metrics()
        assert metrics["core_calls_3pro"] == 3

        _reset_run_request_metrics()
        metrics = _get_run_request_metrics()
        assert metrics["core_calls_3pro"] == 0
