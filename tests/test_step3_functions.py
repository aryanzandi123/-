#!/usr/bin/env python3
"""Tests for step3_refine_pathways pure functions."""

import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import scripts.pathway_v2.step3_refine_pathways as step3_module

from scripts.pathway_v2.step3_refine_pathways import (
    _format_interaction_for_step3,
    _normalize_function_refinements,
    _normalize_refinements,
)

class MockInteraction:
    def __init__(self, id, data):
        self.id = id
        self.data = data

def test_format_interaction_with_proposals():
    """Test formatting when step2_function_proposals exists."""
    interaction = MockInteraction(123, {
        'step2_function_proposals': [
            {'function_index': 0, 'pathway': 'Autophagy'},
            {'function_index': 1, 'pathway': 'Protein Aggregation'}
        ],
        'step2_proposal': 'Fallback Pathway'
    })
    result = _format_interaction_for_step3(interaction)
    assert '123' in result
    assert '[0] Autophagy' in result
    assert '[1] Protein Aggregation' in result

def test_format_interaction_fallback():
    """Test fallback to step2_proposal when no function proposals."""
    interaction = MockInteraction(456, {
        'step2_proposal': 'Fallback Pathway'
    })
    result = _format_interaction_for_step3(interaction)
    assert '456' in result
    assert 'Fallback Pathway' in result

def test_format_interaction_no_data():
    """Test handling when data is None."""
    interaction = MockInteraction(789, None)
    result = _format_interaction_for_step3(interaction)
    assert '789' in result
    assert 'Unknown' in result


def test_format_interaction_with_malformed_proposals():
    """Malformed non-dict proposal entries should be ignored safely."""
    interaction = MockInteraction(321, {
        'step2_function_proposals': [
            'bad-row',
            {'function_index': 0, 'pathway': 'Autophagy'},
        ],
        'step2_proposal': 'Fallback Pathway'
    })
    result = _format_interaction_for_step3(interaction)
    assert '[0] Autophagy' in result
    assert 'bad-row' not in result


def test_normalize_function_refinements_skips_non_dict():
    """Normalization should ignore malformed refinement rows."""
    raw = ['bad', {'function_index': 0, 'finalized_pathway': 'ERAD'}]
    normalized = _normalize_function_refinements(raw, function_count=2)
    assert normalized == [{'function_index': 0, 'finalized_pathway': 'ERAD'}]


def test_normalize_refinements_skips_bad_rows():
    """Batch normalization should not crash on mixed malformed items."""
    class _I:
        def __init__(self):
            self.data = {"functions": [{"function": "x"}]}

    batch_map = {"123": _I()}
    refinements = [
        "bad-row",
        {"interaction_id": "123", "function_refinements": [{"function_index": 0, "finalized_pathway": "ERAD"}], "primary_pathway": "ERAD"},
    ]
    normalized = _normalize_refinements(refinements, batch_map)
    assert normalized["123"]["primary_pathway"] == "ERAD"


def test_retry_cascade_falls_back_to_single(monkeypatch):
    """Step3 cascade should recover via singles when grouped calls fail."""
    interactions = [
        MockInteraction(1, {"functions": [{"function": "x"}], "step2_proposal": "P1"}),
        MockInteraction(2, {"functions": [{"function": "y"}], "step2_proposal": "P2"}),
    ]

    def fake_batch(batch, context_str):
        return {}

    def fake_single(interaction, context_str):
        return {
            "function_refinements": [{"function_index": 0, "finalized_pathway": f"PW{interaction.id}"}],
            "primary_pathway": f"PW{interaction.id}",
        }

    monkeypatch.setattr(step3_module, "_process_batch", fake_batch)
    monkeypatch.setattr(step3_module, "_process_single", fake_single)

    recovered = step3_module._retry_cascade(interactions, "ctx")
    assert set(recovered.keys()) == {"1", "2"}

