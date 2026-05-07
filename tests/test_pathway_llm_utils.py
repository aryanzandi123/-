#!/usr/bin/env python3
"""Tests for pathway_v2 llm_utils corruption handling and config wiring."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.pathway_v2.llm_utils import (
    _call_gemini_json,
    is_corrupted_json_text,
    safe_extract_json,
)


def test_is_corrupted_json_text_detects_numeric_run_and_key_concat():
    text = '{"assignments":[{"interaction_id":"1461","function_pathways1125740431057630737402808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808080808":[]}]}'
    assert is_corrupted_json_text(text, expected_root_key="assignments")

    text2 = '{"assignments":[{"interaction_id":"1419","function_pathways Kylee ":[2]}]}'
    assert is_corrupted_json_text(text2, expected_root_key="assignments")


def test_is_corrupted_json_text_detects_repeated_primitive_function_pathways():
    text = (
        '{"assignments":[{"interaction_id":"1419",'
        '"function_pathways":[2],'
        '"function_pathways":[2],'
        '"function_pathways":["function_index"],'
        '"primary_pathway":"X"}]}'
    )
    assert is_corrupted_json_text(text, expected_root_key="assignments")


def test_safe_extract_json_rejects_missing_expected_root():
    parsed = safe_extract_json('{"not_assignments":[]}', expected_root_key="assignments")
    assert parsed == {}


def test_call_gemini_json_applies_schema_and_disables_afc(monkeypatch):
    import scripts.pathway_v2.llm_utils as llm_utils_module

    captured = {}

    class _FakeModels:
        def generate_content(self, model, contents, config):
            captured["model"] = model
            captured["contents"] = contents
            captured["config_dump"] = config.model_dump(exclude_none=True)
            return type("Resp", (), {"text": '{"assignments": []}'})()

    class _FakeClient:
        def __init__(self, **kwargs):
            self.models = _FakeModels()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(llm_utils_module, "get_client", lambda api_key=None: _FakeClient())

    schema = {
        "type": "object",
        "properties": {"assignments": {"type": "array"}},
        "required": ["assignments"],
        "additionalProperties": False,
    }

    resp = _call_gemini_json(
        prompt="Return JSON only",
        api_key="test-key",
        max_retries=1,
        response_json_schema=schema,
        model="gemini-3-flash-preview",
        thinking_level="low",
        disable_afc=True,
        expected_root_key="assignments",
    )

    assert resp == {"assignments": []}
    assert captured["model"] == "gemini-3-flash-preview"
    cfg = captured["config_dump"]
    assert cfg["thinking_config"]["thinking_level"] == "LOW"
    assert cfg["response_json_schema"] == schema
    assert cfg["automatic_function_calling"]["disable"] is True
