"""Unit tests for services/chat_service.py helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from services.chat_service import (
    _MAP_TTL,
    _interaction_map,
    extract_compact_functions,
    get_interaction_id,
    normalize_arrow_value,
    normalize_direction_value,
    store_interaction_id,
)


# ── normalize_arrow_value ────────────────────────────────────────────────


class TestNormalizeArrowValue:
    @pytest.mark.parametrize(
        "input_val,expected",
        [
            ("activates", "act"),
            ("inhibits", "inh"),
            ("regulates", "reg"),
            ("binds", "bind"),
            ("unknown", "unk"),
            (None, "unk"),
            ("", "unk"),
        ],
    )
    def test_normalization(self, input_val, expected):
        assert normalize_arrow_value(input_val) == expected


# ── normalize_direction_value ────────────────────────────────────────────


class TestNormalizeDirectionValue:
    @pytest.mark.parametrize(
        "input_val,expected",
        [
            ("bidirectional", "bidir"),
            ("main_to_primary", "m2p"),
            ("primary_to_main", "p2m"),
            ("unknown", "unk"),
        ],
    )
    def test_normalization(self, input_val, expected):
        assert normalize_direction_value(input_val) == expected


# ── extract_compact_functions ────────────────────────────────────────────


class TestExtractCompactFunctions:
    def test_extracts_up_to_5(self):
        raw = [{"function": f"fn{i}", "arrow": "activates"} for i in range(10)]
        result = extract_compact_functions(raw)
        assert len(result) == 5

    def test_handles_non_list(self):
        assert extract_compact_functions("not a list") == []
        assert extract_compact_functions(None) == []

    def test_skips_non_dict_entries(self):
        raw = [{"function": "kinase", "arrow": "activates"}, "bad", 42]
        result = extract_compact_functions(raw)
        assert len(result) == 1
        assert result[0]["name"] == "kinase"

    def test_correct_field_extraction(self):
        raw = [
            {
                "function": "phosphorylation",
                "arrow": "activates",
                "confidence": 0.9,
                "pmids": ["123", "456"],
                "effect_description": "promotes growth",
                "biological_consequence": ["proliferation"],
                "specific_effects": ["increased kinase activity"],
            }
        ]
        result = extract_compact_functions(raw)
        assert len(result) == 1
        fn = result[0]
        assert fn["name"] == "phosphorylation"
        assert fn["arrow"] == "act"
        assert fn["confidence"] == 0.9
        assert fn["pmids"] == ["123", "456"]
        assert fn["effect"] == "promotes growth"
        assert fn["biological_consequence"] == ["proliferation"]
        assert fn["specific_effects"] == ["increased kinase activity"]


# ── Session mapping (store / get / TTL) ──────────────────────────────────


class TestSessionMapping:
    @pytest.fixture(autouse=True)
    def _clear_map(self):
        """Ensure a clean interaction map for every test."""
        _interaction_map.clear()
        yield
        _interaction_map.clear()

    def test_store_and_retrieve(self):
        store_interaction_id("TP53", "sess1", "inter_abc")
        assert get_interaction_id("TP53", "sess1") == "inter_abc"

    def test_returns_none_for_unknown(self):
        assert get_interaction_id("UNKNOWN", "sess_x") is None

    def test_ttl_expiration(self, monkeypatch):
        import time as _time
        import services.chat_service as _mod

        now = 1000000.0

        # Store at t=now
        monkeypatch.setattr(_time, "time", lambda: now)
        monkeypatch.setattr(_mod.time, "time", lambda: now)
        store_interaction_id("TP53", "sess1", "inter_abc")

        # Retrieve just before expiry — should still be valid
        almost_expired = now + _MAP_TTL - 1
        monkeypatch.setattr(_time, "time", lambda: almost_expired)
        monkeypatch.setattr(_mod.time, "time", lambda: almost_expired)
        assert get_interaction_id("TP53", "sess1") == "inter_abc"

        # Retrieve after expiry — should return None
        expired = now + _MAP_TTL + 1
        monkeypatch.setattr(_time, "time", lambda: expired)
        monkeypatch.setattr(_mod.time, "time", lambda: expired)
        assert get_interaction_id("TP53", "sess1") is None
