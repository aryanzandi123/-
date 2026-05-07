"""End-to-end regression guard for the VDAC1 bug class.

The VDAC1 bug: a claim describing the query-to-target cascade
(``ATXN3 → PRKN → VDAC1``) was written to the PRKN↔VDAC1 hop row
because the LLM filed it under ``chain_link_functions["PRKN->VDAC1"]``.
The reader then surfaced it on the PRKN↔VDAC1 modal, showing "ATXN3
regulates VDAC1 via Parkin" as if it were pure PRKN-biology.

Fix shipped: Locus Router + read-time filter + content-derived
function_context. This test verifies the fix still holds end-to-end
against the exact LLM output that caused the original bug.

Test is unit-level (no Flask app or DB dependency) — exercises the full
classification-and-routing pipeline as if the LLM had just emitted the
ATXN3 chain_link_functions dict.
"""

from utils.claim_locus_router import (
    LocusDecision,
    RoutingResult,
    classify_claim_locus,
    derive_function_context,
    route_chain_link_claims,
)


# The real LLM output that triggered the VDAC1 bug report.
VDAC1_BUG_CHAIN_LINK_FUNCTIONS = {
    "PRKN->VDAC1": [
        {
            "function": "VDAC1 Deubiquitination & Mitophagy",
            "arrow": "inhibits",
            "function_context": "chain_derived",
            "cellular_process": (
                "VDAC1 is a primary substrate of Parkin-mediated ubiquitination "
                "during the early stages of mitophagy at the outer mitochondrial "
                "membrane. ATXN3 is recruited to these sites through its direct "
                "interaction with the RING1 domain of Parkin, where it acts to "
                "trim ubiquitin chains from VDAC1. This deubiquitination "
                "specifically targets K63-linked chains."
            ),
            "effect_description": (
                "The absence of ATXN3 leads to a 3-fold increase in the "
                "association of p62/SQSTM1 with mitochondria following "
                "depolarization."
            ),
        },
        {
            # A second claim that SHOULD stay on the hop — pure PRKN-VDAC1.
            "function": "VDAC1 K48-Linked Ubiquitination",
            "arrow": "activates",
            "function_context": "chain_derived",
            "cellular_process": (
                "Parkin's RING2 domain transfers K48-linked polyubiquitin onto "
                "VDAC1's cytosolic face. The modification serves as a signal "
                "for proteasomal handoff."
            ),
            "effect_description": (
                "Time-course Western blots show VDAC1 polyubiquitination peaks "
                "within 30 min of CCCP treatment."
            ),
        },
    ],
}


def test_vdac1_cascade_claim_routed_to_parent():
    """The cascade claim mentioning ATXN3 must land on the parent indirect row."""
    result = route_chain_link_claims(
        VDAC1_BUG_CHAIN_LINK_FUNCTIONS["PRKN->VDAC1"],
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1"],
    )
    # Exactly one claim should be kept on the hop (the pure PRKN one).
    assert len(result.kept) == 1
    assert result.kept[0]["function"] == "VDAC1 K48-Linked Ubiquitination"
    # Exactly one claim should be rerouted to the parent.
    assert len(result.rerouted) == 1
    assert result.rerouted[0]["function"] == "VDAC1 Deubiquitination & Mitophagy"
    assert result.rerouted[0]["_router_reason"] == "mentions-query-and-hop"


def test_function_context_overridden_to_net_for_rerouted_claim():
    """Even though the LLM labeled it 'chain_derived', the router must
    override to 'net' because the claim actually describes the cascade."""
    result = route_chain_link_claims(
        VDAC1_BUG_CHAIN_LINK_FUNCTIONS["PRKN->VDAC1"],
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1"],
    )
    rerouted = result.rerouted[0]
    assert rerouted["function_context"] == "net"


def test_function_context_corrected_to_direct_for_kept_claim():
    """Kept-on-hop claims are binary direct interactions → function_context='direct'."""
    result = route_chain_link_claims(
        VDAC1_BUG_CHAIN_LINK_FUNCTIONS["PRKN->VDAC1"],
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1"],
    )
    kept = result.kept[0]
    assert kept["function_context"] == "direct"


def test_parkin_alias_recognized_via_hardcoded_seeds():
    """'Parkin' in prose must resolve to PRKN via HARDCODED_ALIAS_SEEDS."""
    claim = {
        "function": "VDAC1 Deubiquitination",
        "cellular_process": (
            "ATXN3 binds Parkin and trims K63-linked chains from VDAC1."
        ),
    }
    decision = classify_claim_locus(
        claim,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1"],
    )
    # Must detect PRKN via "Parkin" alias so the routing decision is "cross
    # mention", which lands in parent_indirect.
    assert decision.locus == "parent_indirect"
    assert "PRKN" in decision.mentioned


def test_end_to_end_idempotency():
    """Running the routing twice on the same input yields the same partition."""
    first = route_chain_link_claims(
        VDAC1_BUG_CHAIN_LINK_FUNCTIONS["PRKN->VDAC1"],
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1"],
    )
    # Re-running over just `kept` should keep the same claims.
    second = route_chain_link_claims(
        first.kept,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1"],
    )
    assert len(second.kept) == len(first.kept)
    assert len(second.rerouted) == 0
    assert len(second.dropped) == 0


def test_no_claim_leaks_onto_hop_when_query_is_subject():
    """Claims where the query is the subject of an agent verb must reroute."""
    claim = {
        "function": "VDAC1 substrate cleanup",
        "cellular_process": (
            "ATXN3 deubiquitinates VDAC1 at the outer mitochondrial membrane."
        ),
    }
    decision = classify_claim_locus(
        claim,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1"],
    )
    assert decision.locus == "parent_indirect"


def test_hop_endpoint_as_query_disables_filter():
    """When the query itself IS an endpoint of the hop, don't filter at all."""
    claim = {
        "function": "ATXN3 Josephin Deubiquitinase Activity",
        "cellular_process": "ATXN3 hydrolyzes K63-linked chains on substrates.",
    }
    decision = classify_claim_locus(
        claim,
        main_symbol="ATXN3",
        hop_src="ATXN3",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1"],
    )
    assert decision.locus == "hop"
    assert decision.reason == "query-is-endpoint"
