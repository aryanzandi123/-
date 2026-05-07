"""Unit tests for canonicalize_chain_direction (Layer 1 of chain topology fix)."""
from __future__ import annotations

import pytest

from utils.chain_resolution import canonicalize_chain_direction


def _arrows(*pairs):
    """Build a chain_with_arrows list from (from, to, arrow) triples."""
    return [{"from": f, "to": t, "arrow": a} for f, t, a in pairs]


def test_forward_only_chain_is_unchanged():
    proteins = ["ATXN3", "VCP", "LAMP2"]
    arrows = _arrows(("ATXN3", "VCP", "binds"), ("VCP", "LAMP2", "activates"))
    out_proteins, out_arrows, was_reversed = canonicalize_chain_direction(proteins, arrows)
    assert out_proteins == proteins
    assert out_arrows == arrows
    assert was_reversed is False


def test_reverse_only_chain_is_reversed():
    proteins = ["ATXN3", "HSP90AA1", "STUB1"]
    arrows = _arrows(
        ("ATXN3", "HSP90AA1", "is_substrate_of"),
        ("HSP90AA1", "STUB1", "is_ubiquitinated_by"),
    )
    out_proteins, out_arrows, was_reversed = canonicalize_chain_direction(proteins, arrows)
    assert out_proteins == ["STUB1", "HSP90AA1", "ATXN3"]
    assert out_arrows == list(reversed(arrows))
    assert was_reversed is True


def test_mixed_more_reverse_is_reversed():
    proteins = ["A", "B", "C", "D"]
    arrows = _arrows(
        ("A", "B", "is_phosphorylated_by"),
        ("B", "C", "is_activated_by"),
        ("C", "D", "binds"),
    )
    out_proteins, out_arrows, was_reversed = canonicalize_chain_direction(proteins, arrows)
    assert out_proteins == ["D", "C", "B", "A"]
    assert was_reversed is True


def test_mixed_more_forward_is_unchanged():
    proteins = ["A", "B", "C", "D"]
    arrows = _arrows(
        ("A", "B", "phosphorylates"),
        ("B", "C", "activates"),
        ("C", "D", "is_substrate_of"),
    )
    out_proteins, out_arrows, was_reversed = canonicalize_chain_direction(proteins, arrows)
    assert out_proteins == proteins
    assert was_reversed is False


def test_tied_counts_keep_original_order():
    proteins = ["A", "B", "C"]
    arrows = _arrows(
        ("A", "B", "activates"),
        ("B", "C", "is_substrate_of"),
    )
    out_proteins, out_arrows, was_reversed = canonicalize_chain_direction(proteins, arrows)
    assert out_proteins == proteins
    assert was_reversed is False


def test_empty_arrows_is_noop():
    proteins = ["A", "B"]
    out_proteins, out_arrows, was_reversed = canonicalize_chain_direction(proteins, [])
    assert out_proteins == proteins
    assert out_arrows == []
    assert was_reversed is False


def test_none_inputs_handled_gracefully():
    out_proteins, out_arrows, was_reversed = canonicalize_chain_direction(None, None)
    assert out_proteins == []
    assert out_arrows == []
    assert was_reversed is False


def test_single_element_chain_is_unchanged():
    out_proteins, out_arrows, was_reversed = canonicalize_chain_direction(
        ["A"], _arrows(("A", "B", "binds"))
    )
    assert out_proteins == ["A"]
    assert was_reversed is False


def test_unknown_verbs_do_not_influence_count():
    proteins = ["A", "B", "C"]
    arrows = _arrows(
        ("A", "B", "unknown_verb_one"),
        ("B", "C", "is_substrate_of"),
    )
    out_proteins, out_arrows, was_reversed = canonicalize_chain_direction(proteins, arrows)
    assert was_reversed is True


def test_arrow_case_is_normalized():
    proteins = ["A", "B"]
    arrows = _arrows(("A", "B", "IS_SUBSTRATE_OF"))
    _, _, was_reversed = canonicalize_chain_direction(proteins, arrows)
    assert was_reversed is True


def test_malformed_entries_are_skipped():
    proteins = ["A", "B", "C"]
    arrows = [
        "not a dict",
        {"from": "A", "to": "B", "arrow": "is_substrate_of"},
        {"from": "B", "to": "C"},
        {"from": "B", "to": "C", "arrow": None},
    ]
    out_proteins, _, was_reversed = canonicalize_chain_direction(proteins, arrows)
    assert was_reversed is True
    assert out_proteins == ["C", "B", "A"]


def test_idempotent_on_already_canonical_chain():
    proteins = ["STUB1", "HSP90AA1", "ATXN3"]
    arrows = _arrows(
        ("STUB1", "HSP90AA1", "ubiquitinates"),
        ("HSP90AA1", "ATXN3", "binds"),
    )
    p1, a1, rev1 = canonicalize_chain_direction(proteins, arrows)
    p2, a2, rev2 = canonicalize_chain_direction(p1, a1)
    assert (p1, a1, rev1) == (proteins, arrows, False)
    assert (p2, a2, rev2) == (p1, a1, False)


@pytest.mark.parametrize(
    "verb, expected_reversed",
    [
        ("activates", False),
        ("phosphorylates", False),
        ("ubiquitinates", False),
        ("deubiquitinates", False),
        ("is_substrate_of", True),
        ("is_phosphorylated_by", True),
        ("is_ubiquitinated_by", True),
        ("is_activated_by", True),
    ],
)
def test_verb_classification(verb: str, expected_reversed: bool):
    proteins = ["A", "B"]
    arrows = _arrows(("A", "B", verb))
    _, _, was_reversed = canonicalize_chain_direction(proteins, arrows)
    assert was_reversed is expected_reversed
