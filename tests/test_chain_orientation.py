"""Tests for chain-handling helpers that must NOT force the query to be
at the head of a chain. Chains where the query sits in the middle or tail
are valid biology (e.g. ``VCP → TDP43 → GRN`` with query=TDP43) and the
pipeline must preserve them.

Covers:

1. ``_validate_chain_intermediaries`` — only drops the target itself as
   a self-reference. The query, existing direct interactors, and
   everything else are allowed through so query-in-middle chains survive.
2. ``_promote_chain_interactors`` — promotes new proteins from chains
   without an orientation check (chains with the query at any position
   are accepted).
3. ``infer_direction_from_arrow`` / ``is_more_specific_direction`` —
   position-agnostic direction helpers that stop the bidirectional
   placeholder from leaking into chain-derived claims.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# _validate_chain_intermediaries (runner.py)
# ---------------------------------------------------------------------------
#
# This helper was previously restrictive (dropped the query, dropped
# existing direct interactors). That broke query-in-middle chains. The
# current contract is much looser: only drop the target itself.


def test_validate_intermediaries_drops_target_self():
    from runner import _validate_chain_intermediaries

    result = _validate_chain_intermediaries(
        main_query="TDP43",
        interactor_name="GRN",
        intermediaries=["HNRNPA1", "GRN"],  # GRN is the target self-reference
        existing_interactors=[],
    )
    assert result == ["HNRNPA1"]


def test_validate_intermediaries_preserves_query_in_chain():
    """The query protein is a valid chain member when it sits in the middle
    of a chain (e.g. ``VCP → TDP43 → GRN`` with query=TDP43). Do NOT drop it."""
    from runner import _validate_chain_intermediaries

    # When step2ab returns a chain VCP → TDP43 → GRN for interactor GRN,
    # intermediaries = ["VCP", "TDP43"] (both valid — TDP43 is the query
    # but it's in the middle of the chain, not duplicating the target).
    result = _validate_chain_intermediaries(
        main_query="TDP43",
        interactor_name="GRN",
        intermediaries=["VCP", "TDP43"],
        existing_interactors=[],
    )
    assert result == ["VCP", "TDP43"]


def test_validate_intermediaries_preserves_existing_direct_interactors():
    """Existing direct interactors are valid chain mediators — they can
    participate in indirect chains involving other proteins. The previous
    behavior that dropped them was the bug."""
    from runner import _validate_chain_intermediaries

    existing = [
        {"primary": "VCP", "interaction_type": "direct"},
        {"primary": "HNRNPA1", "interaction_type": "direct"},
    ]
    result = _validate_chain_intermediaries(
        main_query="TDP43",
        interactor_name="GRN",
        intermediaries=["VCP", "HNRNPA1"],
        existing_interactors=existing,
    )
    assert result == ["VCP", "HNRNPA1"]


def test_validate_intermediaries_strips_marker_characters():
    from runner import _validate_chain_intermediaries

    result = _validate_chain_intermediaries(
        main_query="TDP43",
        interactor_name="GRN",
        intermediaries=["^HNRNPA1^", "**VCP**"],
        existing_interactors=[],
    )
    assert result == ["HNRNPA1", "VCP"]


def test_validate_intermediaries_empty_input_returns_empty():
    from runner import _validate_chain_intermediaries

    assert _validate_chain_intermediaries("TDP43", "GRN", [], []) == []


def test_validate_intermediaries_case_insensitive_target_drop():
    from runner import _validate_chain_intermediaries

    result = _validate_chain_intermediaries(
        main_query="TDP43",
        interactor_name="GRN",
        intermediaries=["grn", "HNRNPA1", "Grn"],
        existing_interactors=[],
    )
    # Both 'grn' and 'Grn' are the target self-reference; only HNRNPA1 survives.
    assert result == ["HNRNPA1"]


def test_validate_intermediaries_rejects_non_string_entries():
    from runner import _validate_chain_intermediaries

    result = _validate_chain_intermediaries(
        main_query="TDP43",
        interactor_name="GRN",
        intermediaries=["HNRNPA1", None, 42, "", "  ", "VCP"],
        existing_interactors=[],
    )
    assert result == ["HNRNPA1", "VCP"]


def test_chain_ingest_rejects_non_protein_entities():
    from utils.chain_resolution import validate_chain_on_ingest

    cleaned, errors = validate_chain_on_ingest(
        ["TDP43", "RNA", "STMN2"],
        query_protein="TDP43",
    )
    assert cleaned == []
    assert any("non-protein" in err for err in errors)

    cleaned, errors = validate_chain_on_ingest(
        ["TBK1", "OPTN", "Ubiquitin", "TDP43"],
        query_protein="TDP43",
    )
    assert cleaned == []
    assert any("Ubiquitin" in err for err in errors)


def test_hidden_candidate_extractor_skips_generic_entities():
    from utils.chain_resolution import extract_candidate_proteins

    claim = {
        "cellular_process": (
            "TDP43 binds FUS and RNA, with generic Ubiquitin context, "
            "while U2AF2 participates in spliceosome assembly."
        )
    }
    result = extract_candidate_proteins(claim, query="TDP43", interactor="FUS")

    assert "U2AF2" in result
    assert "RNA" not in result
    assert "Ubiquitin" not in result


# ---------------------------------------------------------------------------
# _promote_chain_interactors (runner.py)
# ---------------------------------------------------------------------------
#
# No orientation check — chains at any orientation are promoted. The
# function still derives mediator_chain / upstream_interactor / depth via
# the legacy chain[1:-1] / chain[-2] / len(chain)-1 slicing, but that's
# now only a LEGACY view; the full chain is preserved in chain_context
# downstream so query-in-middle cases can be rendered correctly.


def _build_payload(main: str, pairs_data_key: str, pair_results: list) -> dict:
    return {
        "ctx_json": {
            "main": main,
            "interactors": [],
            "interactor_history": [],
            pairs_data_key: pair_results,
        }
    }


def test_promote_accepts_well_formed_chain():
    from runner import _promote_chain_interactors

    payload = _build_payload(
        main="TDP43",
        pairs_data_key="_explicit_pairs_data",
        pair_results=[
            {
                "new_protein": "GRN",
                "new_indirects": [{"chain": ["TDP43", "HNRNPA1", "GRN"]}],
                "new_directs": [],
            },
        ],
    )
    promoted = _promote_chain_interactors(payload)
    assert promoted == ["GRN"]
    grn = payload["ctx_json"]["interactors"][0]
    assert grn["primary"] == "GRN"
    assert grn["interaction_type"] == "indirect"
    assert grn["mediator_chain"] == ["HNRNPA1"]
    assert grn["depth"] == 2


def test_promote_accepts_4_element_chain():
    from runner import _promote_chain_interactors

    payload = _build_payload(
        main="ATXN3",
        pairs_data_key="_explicit_pairs_data",
        pair_results=[
            {
                "new_protein": "TARGET",
                "new_indirects": [{"chain": ["ATXN3", "VCP", "LAMP2", "TARGET"]}],
                "new_directs": [],
            },
        ],
    )
    promoted = _promote_chain_interactors(payload)
    assert promoted == ["TARGET"]
    target = payload["ctx_json"]["interactors"][0]
    assert target["mediator_chain"] == ["VCP", "LAMP2"]
    assert target["depth"] == 3


def test_promote_accepts_5_element_chain_no_length_cap():
    """Regression: chain length is NOT capped at 3 or 4. A 5-element chain
    with 3 mediators must be promoted with depth=4."""
    from runner import _promote_chain_interactors

    payload = _build_payload(
        main="ATXN3",
        pairs_data_key="_explicit_pairs_data",
        pair_results=[
            {
                "new_protein": "LAMP1",
                "new_indirects": [
                    {"chain": ["ATXN3", "VCP", "LAMP2", "RAB7", "LAMP1"]},
                ],
                "new_directs": [],
            },
        ],
    )
    promoted = _promote_chain_interactors(payload)
    assert promoted == ["LAMP1"]
    lamp1 = payload["ctx_json"]["interactors"][0]
    assert lamp1["mediator_chain"] == ["VCP", "LAMP2", "RAB7"]
    assert lamp1["depth"] == 4


def test_promote_accepts_query_in_middle_of_chain():
    """Key regression: chains where the query protein is in the MIDDLE
    (not at the head) must be promoted. Example: VCP → TDP43 → GRN with
    query=TDP43 and new_protein=GRN. The code does not reject this."""
    from runner import _promote_chain_interactors

    payload = _build_payload(
        main="TDP43",
        pairs_data_key="_hidden_pairs_data",
        pair_results=[
            {
                "new_protein": "GRN",
                "new_indirects": [{"chain": ["VCP", "TDP43", "GRN"]}],
                "new_directs": [],
            },
        ],
    )
    promoted = _promote_chain_interactors(payload)
    # The chain was accepted and GRN was promoted.
    assert "GRN" in promoted


def test_promote_accepts_query_at_tail_of_chain():
    """Chains where the query is at the TAIL (query is the endpoint of
    an upstream cascade) must also be promoted."""
    from runner import _promote_chain_interactors

    payload = _build_payload(
        main="TDP43",
        pairs_data_key="_hidden_pairs_data",
        pair_results=[
            {
                "new_protein": "VCP",
                "new_indirects": [{"chain": ["VCP", "HSP70", "TDP43"]}],
                "new_directs": [],
            },
        ],
    )
    promoted = _promote_chain_interactors(payload)
    assert "VCP" in promoted


def test_promote_falls_through_to_direct_when_no_indirects():
    from runner import _promote_chain_interactors

    payload = _build_payload(
        main="TDP43",
        pairs_data_key="_hidden_pairs_data",
        pair_results=[
            {
                "new_protein": "NEW_PROT",
                "new_indirects": [],
                "new_directs": [{"partner": "TDP43"}],
            },
        ],
    )
    promoted = _promote_chain_interactors(payload)
    assert promoted == ["NEW_PROT"]
    new_prot = payload["ctx_json"]["interactors"][0]
    assert new_prot["interaction_type"] == "direct"
    assert new_prot["mediator_chain"] == []
    assert new_prot["depth"] == 1


# ---------------------------------------------------------------------------
# infer_direction_from_arrow (utils/direction.py) — position-agnostic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arrow,expected",
    [
        ("activates", "main_to_primary"),
        ("inhibits", "main_to_primary"),
        ("regulates", "main_to_primary"),
        ("phosphorylates", "main_to_primary"),
        ("degrades", "main_to_primary"),
        ("recruits", "main_to_primary"),
        # S1: ALL arrows → main_to_primary. Bidirectional is dead.
        ("binds", "main_to_primary"),
        ("complex", "main_to_primary"),
        ("interacts", "main_to_primary"),
        ("colocalizes", "main_to_primary"),
        ("mystery", "main_to_primary"),
        (None, "main_to_primary"),
        ("", "main_to_primary"),
        ("   ", "main_to_primary"),
        ("ACTIVATES", "main_to_primary"),
        ("Binds", "main_to_primary"),
    ],
)
def test_infer_direction_from_arrow(arrow, expected):
    from utils.direction import infer_direction_from_arrow

    assert infer_direction_from_arrow(arrow) == expected


def test_infer_direction_non_string_returns_main_to_primary():
    from utils.direction import infer_direction_from_arrow

    assert infer_direction_from_arrow(42) == "main_to_primary"
    assert infer_direction_from_arrow(["binds"]) == "main_to_primary"


# ---------------------------------------------------------------------------
# is_more_specific_direction (utils/direction.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "new_dir,existing_dir,expected",
    [
        ("main_to_primary", None, True),
        ("primary_to_main", None, True),
        ("a_to_b", None, True),
        ("main_to_primary", "bidirectional", True),
        ("primary_to_main", "bidirectional", True),
        ("bidirectional", "main_to_primary", False),
        ("bidirectional", "a_to_b", False),
        ("bidirectional", None, False),
        ("main_to_primary", "primary_to_main", False),
        (None, None, False),
        (None, "main_to_primary", False),
    ],
)
def test_is_more_specific_direction(new_dir, existing_dir, expected):
    from utils.direction import is_more_specific_direction

    assert is_more_specific_direction(new_dir, existing_dir) is expected
