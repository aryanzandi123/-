#!/usr/bin/env python3
"""
Unit tests for Step 2 per-function pathway assignment.

Tests the pure helper functions used in step2_assign_initial_terms.py.
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import scripts.pathway_v2.step2_assign_initial_terms as step2_module

from scripts.pathway_v2.step2_assign_initial_terms import (
    _format_interaction,
    _extract_pathways_from_result,
    _normalize_assignments,
    _normalize_function_pathways,
)


class MockProtein:
    """Mock protein for testing."""
    def __init__(self, symbol: str):
        self.symbol = symbol


class MockInteraction:
    """Mock interaction for testing."""
    def __init__(self, id: int, protein_a_symbol: str, protein_b_symbol: str, data: dict = None):
        self.id = id
        self.protein_a = MockProtein(protein_a_symbol)
        self.protein_b = MockProtein(protein_b_symbol)
        self.data = data or {}


def test_format_interaction_no_functions():
    """Test formatting interaction with no functions."""
    interaction = MockInteraction(1, "ATXN3", "VCP", {})
    result = _format_interaction(interaction)

    assert "ID: 1" in result
    assert "ATXN3" in result
    assert "VCP" in result
    assert "No functions" in result


def test_format_interaction_with_functions():
    """Test formatting interaction with multiple functions."""
    interaction = MockInteraction(2, "ATXN3", "TBP", {
        "functions": [
            {"description": "binds TBP to modulate transcription"},
            {"description": "polyQ-expanded ATXN3 sequesters TBP in aggregates"},
            {"function": "stabilizes TBP protein levels"}
        ]
    })
    result = _format_interaction(interaction)

    assert "ID: 2" in result
    assert "ATXN3" in result
    assert "TBP" in result
    assert "[0]" in result
    assert "[1]" in result
    assert "[2]" in result
    assert "modulate transcription" in result
    assert "aggregates" in result
    assert "stabilizes" in result


def test_format_interaction_truncates_long_descriptions():
    """Test that long function descriptions are truncated to 150 chars."""
    long_desc = "A" * 200
    interaction = MockInteraction(3, "A", "B", {
        "functions": [{"description": long_desc}]
    })
    result = _format_interaction(interaction)

    # Should be truncated
    assert "A" * 150 in result
    assert "A" * 151 not in result


def test_extract_pathways_from_result_empty():
    """Test extracting pathways from empty result."""
    result = {}
    pathways = _extract_pathways_from_result(result)

    assert len(pathways) == 0


def test_extract_pathways_from_result_primary_only():
    """Test extracting pathways with only primary_pathway."""
    result = {"primary_pathway": "DNA Damage Response"}
    pathways = _extract_pathways_from_result(result)

    assert "DNA Damage Response" in pathways
    assert len(pathways) == 1


def test_extract_pathways_from_result_functions_only():
    """Test extracting pathways with only function_pathways."""
    result = {
        "function_pathways": [
            {"function_index": 0, "pathway": "Transcriptional Regulation"},
            {"function_index": 1, "pathway": "Protein Aggregation"}
        ]
    }
    pathways = _extract_pathways_from_result(result)

    assert "Transcriptional Regulation" in pathways
    assert "Protein Aggregation" in pathways
    assert len(pathways) == 2


def test_extract_pathways_from_result_full():
    """Test extracting all unique pathways from complete result."""
    result = {
        "primary_pathway": "Protein Quality Control",
        "function_pathways": [
            {"function_index": 0, "pathway": "Transcriptional Regulation"},
            {"function_index": 1, "pathway": "Protein Aggregation"},
            {"function_index": 2, "pathway": "Protein Quality Control"}  # Duplicate
        ]
    }
    pathways = _extract_pathways_from_result(result)

    assert "Transcriptional Regulation" in pathways
    assert "Protein Aggregation" in pathways
    assert "Protein Quality Control" in pathways
    assert len(pathways) == 3  # No duplicates


def test_extract_pathways_handles_none_values():
    """Test that None pathway values are handled gracefully."""
    result = {
        "primary_pathway": None,
        "function_pathways": [
            {"function_index": 0, "pathway": None},
            {"function_index": 1, "pathway": "Valid Pathway"}
        ]
    }
    pathways = _extract_pathways_from_result(result)

    assert "Valid Pathway" in pathways
    assert None not in pathways
    assert len(pathways) == 1


def test_normalize_function_pathways_rejects_primitive_entries():
    """Primitive malformed entries should be dropped safely."""
    raw = [2, "function_index", {"function_index": "0", "pathway": "Autophagy"}]
    normalized = _normalize_function_pathways(raw, function_count=2)

    assert normalized == [{"function_index": 0, "pathway": "Autophagy"}]


def test_normalize_assignments_skips_non_dict_rows():
    """Non-dict assignments should never crash parsing."""

    class _I:
        def __init__(self):
            self.data = {"functions": [{"function": "x"}]}

    batch_map = {"123": _I()}
    assignments = [
        "bad-row",
        {"interaction_id": "123", "function_pathways": [{"function_index": 0, "pathway": "ERAD"}], "primary_pathway": "ERAD"},
    ]
    normalized = _normalize_assignments(assignments, batch_map)

    assert "123" in normalized
    assert normalized["123"]["primary_pathway"] == "ERAD"


def test_extract_pathways_handles_non_dict_function_entries():
    """Function pathway extraction should ignore malformed non-dict entries."""
    result = {
        "primary_pathway": "Primary",
        "function_pathways": ["bad", {"pathway": "Valid Pathway"}],
    }
    pathways = _extract_pathways_from_result(result)

    assert pathways == {"Primary", "Valid Pathway"}


def test_retry_cascade_falls_back_to_single(monkeypatch):
    """If batch parsing fails, cascade should recover via single-item calls."""
    interactions = [
        MockInteraction(1, "A", "B", {"functions": [{"function": "x"}]}),
        MockInteraction(2, "A", "C", {"functions": [{"function": "y"}]}),
        MockInteraction(3, "A", "D", {"functions": [{"function": "z"}]}),
    ]

    def fake_batch(batch, existing_pathways, pathways_formatted, db):
        # Simulate corruption in all grouped calls.
        return {}

    def fake_single(interaction, existing_pathways, pathways_formatted, db):
        return {
            "function_pathways": [{"function_index": 0, "pathway": f"PW{interaction.id}"}],
            "primary_pathway": f"PW{interaction.id}",
        }

    monkeypatch.setattr(step2_module, "_process_batch", fake_batch)
    monkeypatch.setattr(step2_module, "_process_single", fake_single)

    recovered = step2_module._retry_cascade(interactions, set(), "ctx", None)
    assert set(recovered.keys()) == {"1", "2", "3"}


