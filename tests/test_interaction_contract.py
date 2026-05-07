"""Regression tests for the shared DB/API/frontend interaction contract."""

from models import Interaction
from utils.interaction_contract import (
    normalize_arrow,
    normalize_arrows_map,
    normalize_chain_arrows,
    semantic_claim_direction,
)


def test_arrow_contract_collapses_legacy_complex_to_binds():
    assert normalize_arrow("complex") == "binds"
    assert normalize_arrow("forms complex") == "binds"
    assert normalize_arrow("modulates") == "regulates"
    assert normalize_arrow("") == "regulates"


def test_jsonb_arrow_contract_normalizes_nested_payloads():
    assert normalize_arrows_map({"a_to_b": ["complex", "activates"]}) == {
        "a_to_b": ["binds", "activates"]
    }
    assert normalize_chain_arrows([
        {"from": "A", "to": "B", "arrow": "complex"},
        {"from": "B", "to": "C", "arrow": "modulates"},
    ]) == [
        {"from": "A", "to": "B", "arrow": "binds"},
        {"from": "B", "to": "C", "arrow": "regulates"},
    ]


def test_claim_direction_contract_is_semantic_only():
    assert semantic_claim_direction("main_to_primary") == "main_to_primary"
    assert semantic_claim_direction("primary_to_main") == "primary_to_main"
    assert semantic_claim_direction("b_to_a") == "primary_to_main"
    assert semantic_claim_direction(None) == "main_to_primary"


def test_interaction_primary_arrow_normalizes_legacy_values():
    interaction = Interaction(arrows={"a_to_b": ["complex"]}, arrow="complex")

    assert interaction.primary_arrow == "binds"

    interaction.set_primary_arrow("complex formation")
    assert interaction.arrow == "binds"
    assert interaction.arrows == {"a_to_b": ["binds"]}
