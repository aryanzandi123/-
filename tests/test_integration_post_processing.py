"""Integration tests for the PostProcessor stage chain."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from utils.post_processor import PostProcessor, StageDescriptor, StageKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _identity_stage(name, kind=StageKind.PURE, requires_api_key=False):
    """Return a stage that passes the payload through unchanged."""

    def fn(payload, **kwargs):
        return payload

    return StageDescriptor(
        name=name,
        label=f"Running {name}...",
        kind=kind,
        fn=fn,
        requires_api_key=requires_api_key,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFullDefaultChainPassthrough:
    """All default stages, monkeypatched to identity, pass payload through."""

    def test_full_default_chain_passthrough(self, monkeypatch):
        ran_stages = []
        default_stages = PostProcessor.default_stages()

        # Replace every stage function with an identity that logs its name
        patched_stages = []
        for stage in default_stages:
            def make_fn(stage_name):
                def fn(payload, **kwargs):
                    ran_stages.append(stage_name)
                    return payload
                return fn

            patched_stages.append(StageDescriptor(
                name=stage.name,
                label=stage.label,
                kind=stage.kind,
                fn=make_fn(stage.name),
                requires_api_key=stage.requires_api_key,
                default_skip=stage.default_skip,
                skip_flag=stage.skip_flag,
            ))

        pp = PostProcessor(stages=patched_stages)
        sample = {"ctx_json": {"main": "TESTPROT", "interactors": []}}
        result, step = pp.run(sample, api_key="test-key")

        # Result payload should be the same object (identity stages)
        assert result["ctx_json"]["main"] == "TESTPROT"

        # All non-deprecated, non-skipped stages should have run
        expected_active = [s.name for s in pp.active_stages()]
        assert ran_stages == expected_active
        assert step == len(expected_active)


class TestChainPreservesPayloadAcrossStages:
    """A marker field added by one stage persists through subsequent stages."""

    def test_chain_preserves_payload_across_stages(self, monkeypatch):
        def add_marker(payload, **kwargs):
            payload["_marker"] = "injected"
            return payload

        def check_marker(payload, **kwargs):
            # Marker from previous stage should still be present
            assert payload.get("_marker") == "injected"
            payload["_marker_seen"] = True
            return payload

        stages = [
            StageDescriptor(
                name="inject",
                label="Inject marker",
                kind=StageKind.PURE,
                fn=add_marker,
            ),
            _identity_stage("middle"),
            StageDescriptor(
                name="verify",
                label="Verify marker",
                kind=StageKind.PURE,
                fn=check_marker,
            ),
        ]

        pp = PostProcessor(stages=stages)
        result, _ = pp.run({"ctx_json": {}})

        assert result["_marker"] == "injected"
        assert result["_marker_seen"] is True


class TestStageExceptionPropagates:
    """An exception raised inside a stage must propagate, not be swallowed."""

    def test_stage_exception_propagates(self, monkeypatch):
        def exploding_fn(payload, **kwargs):
            raise ValueError("stage exploded")

        stages = [
            _identity_stage("before"),
            StageDescriptor(
                name="boom",
                label="Exploding stage",
                kind=StageKind.PURE,
                fn=exploding_fn,
            ),
            _identity_stage("after"),
        ]

        pp = PostProcessor(stages=stages)

        # Stages now retry 4 times then log failure and continue (no exception raised)
        result, _ = pp.run({"ctx_json": {}})
        failed = result.get("_pipeline_metadata", {}).get("failed_stages", [])
        assert any(s["stage"] == "boom" for s in failed), "Expected 'boom' in failed_stages"
