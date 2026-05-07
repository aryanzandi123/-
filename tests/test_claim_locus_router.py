"""Router behavior tests using the exact claims that triggered the bug report.

Scenario: query ATXN3, chain ATXN3 → PRKN → MFN1. The LLM emitted a claim
under ``chain_link_functions["PRKN->MFN1"]`` describing ATXN3's role in
deubiquitinating MFN1 via Parkin, and a second claim describing pure PRKN
ubiquitination of MFN1. Only the second belongs on PRKN↔MFN1.

The router must reroute the first to the parent indirect (ATXN3↔MFN1) and
keep the second on the hop. These tests lock that behavior.
"""

from utils.claim_locus_router import (
    LocusDecision,
    RoutingResult,
    classify_claim_locus,
    route_chain_link_claims,
)


CHAIN = ["ATXN3", "PRKN", "MFN1"]


ATXN3_VIA_PRKN_CLAIM = {
    "function": "MFN1 Deubiquitination & Mitochondrial Fusion",
    "arrow": "activates",
    "cellular_process": (
        "ATXN3 regulates the ubiquitination status of Mitofusin 1 (MFN1) "
        "through its functional interaction with the E3 ligase Parkin at "
        "the outer mitochondrial membrane. ATXN3 acts as a deubiquitinating "
        "enzyme that removes K48-linked and K63-linked polyubiquitin chains "
        "from MFN1."
    ),
    "effect_description": (
        "Loss of ATXN3-mediated deubiquitination leads to a significant "
        "accumulation of polyubiquitinated MFN1 species."
    ),
}

PURE_PRKN_CLAIM = {
    "function": "MFN1 Ubiquitination & Mitochondrial Fusion",
    "arrow": "inhibits",
    "cellular_process": (
        "Parkin (PRKN) functions as a specific E3 ubiquitin ligase for "
        "Mitofusin 1 (MFN1) on the outer mitochondrial membrane. Following "
        "mitochondrial depolarization, Parkin transfers ubiquitin moieties "
        "to lysine residues on MFN1."
    ),
    "effect_description": (
        "Parkin-mediated ubiquitination leads to a 70-80% reduction in MFN1 "
        "protein levels within 2 hours of mitochondrial stress."
    ),
}


def test_query_mentioning_claim_reroutes_to_parent():
    decision = classify_claim_locus(
        ATXN3_VIA_PRKN_CLAIM,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
        alias_map={"PRKN": ["Parkin"]},  # prose uses common name, not HGNC
    )
    assert decision.locus == "parent_indirect"
    assert decision.reason == "mentions-query-and-hop"
    assert "ATXN3" in decision.mentioned
    assert "PRKN" in decision.mentioned
    assert "MFN1" in decision.mentioned


def test_pure_pair_claim_stays_on_hop():
    decision = classify_claim_locus(
        PURE_PRKN_CLAIM,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
    )
    assert decision.locus == "hop"
    assert "ATXN3" not in decision.mentioned
    assert {"PRKN", "MFN1"}.issubset(decision.mentioned)


def test_query_is_endpoint_skips_filter():
    decision = classify_claim_locus(
        ATXN3_VIA_PRKN_CLAIM,
        main_symbol="ATXN3",
        hop_src="ATXN3",
        hop_tgt="PRKN",
        chain_proteins=CHAIN,
    )
    assert decision.locus == "hop"
    assert decision.reason == "query-is-endpoint"


def test_claim_about_unrelated_proteins_is_dropped():
    unrelated = {
        "function": "Generic Apoptotic Regulation",
        "cellular_process": "BAX and BAK form pores in the outer mitochondrial membrane.",
    }
    decision = classify_claim_locus(
        unrelated,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
    )
    assert decision.locus == "hop"
    assert decision.reason == "no-mentions"


def test_whole_word_match_avoids_substring_false_positives():
    """ATM shouldn't match inside ATMP, ATMosphere, or similar substrings."""
    claim = {
        "cellular_process": "The ATMP-1 protein is active in the atmosphere.",
    }
    decision = classify_claim_locus(
        claim,
        main_symbol="ATM",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=["ATM", "PRKN", "MFN1"],
    )
    assert decision.locus == "hop"
    assert "ATM" not in decision.mentioned


def test_route_returns_three_buckets_correctly():
    result: RoutingResult = route_chain_link_claims(
        [ATXN3_VIA_PRKN_CLAIM, PURE_PRKN_CLAIM],
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
    )
    assert len(result.kept) == 1
    assert len(result.rerouted) == 1
    assert len(result.dropped) == 0
    assert result.kept[0]["function"] == "MFN1 Ubiquitination & Mitochondrial Fusion"
    assert result.rerouted[0]["function"] == "MFN1 Deubiquitination & Mitochondrial Fusion"
    assert result.rerouted[0]["_rerouted_from_hop"] == "PRKN->MFN1"
    assert result.rerouted[0]["_router_reason"] == "mentions-query-and-hop"


def test_adjacent_mediator_mention_stays_on_hop():
    """P1.4 — a claim that mentions an ADJACENT chain mediator (the
    immediately upstream or downstream neighbor of the hop) is describing
    legitimate hop biology, NOT scope creep. The hop is the transition
    between its source's input and its target's output, so naming those
    proteins is on-topic.

    For chain [ATXN3, PRKN, VDAC1, SQSTM1] and hop PRKN→VDAC1, SQSTM1
    is the immediately downstream neighbor of VDAC1 — adjacent. Keep
    on the hop.
    """
    adjacent = {
        "function": "VDAC1 Clearance via Parkin–Mitophagy Axis",
        "cellular_process": (
            "VDAC1 is targeted by Parkin-mediated ubiquitination, subsequently "
            "engaging SQSTM1 for autophagosomal delivery."
        ),
    }
    decision = classify_claim_locus(
        adjacent,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1", "SQSTM1"],
    )
    assert decision.locus == "hop"
    assert decision.reason == "adjacent-mediators-ok"
    assert "SQSTM1" in decision.mentioned


def test_two_non_adjacent_mediators_route_to_parent():
    """P1.4 — when a claim mentions ≥2 NON-adjacent mediators (proteins
    that aren't immediate neighbors of the hop), it's truly cascade-level
    scope creep and reroutes to the parent indirect.

    For chain [ATXN3, PRKN, VDAC1, MFN1, OPA1, OMA1] and hop PRKN→VDAC1,
    adjacent = {ATXN3, MFN1}. A claim mentioning OPA1 AND OMA1 (both
    non-adjacent) is talking about the whole mitochondrial dynamics
    cascade, not the PRKN→VDAC1 hop specifically.
    """
    cross_hop = {
        "function": "Mitochondrial Dynamics Coordination",
        "cellular_process": (
            "PRKN ubiquitinates VDAC1, but OPA1 cleavage by OMA1 is the "
            "downstream commitment to fusion failure."
        ),
    }
    decision = classify_claim_locus(
        cross_hop,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="VDAC1",
        chain_proteins=["ATXN3", "PRKN", "VDAC1", "MFN1", "OPA1", "OMA1"],
    )
    assert decision.locus == "parent_indirect"
    assert decision.reason == "cross-hop-mediators"
    assert "OPA1" in decision.mentioned
    assert "OMA1" in decision.mentioned


def test_claim_only_mentioning_query_is_dropped():
    """Query-only mention with no hop endpoint → claim doesn't describe this
    pair at all and shouldn't live on it."""
    query_only = {
        "function": "ATXN3 Catalytic Activity",
        "cellular_process": "ATXN3 uses its Josephin domain to cleave ubiquitin chains.",
    }
    decision = classify_claim_locus(
        query_only,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=["ATXN3", "PRKN", "MFN1"],
    )
    assert decision.locus == "drop"
    assert decision.reason == "mentions-query-only"


def test_alias_map_catches_common_names():
    """The LLM writes 'Parkin' not 'PRKN' in prose. With an alias map
    supplied, the router detects the common-name mention."""
    decision = classify_claim_locus(
        ATXN3_VIA_PRKN_CLAIM,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
        alias_map={"PRKN": ["Parkin", "PARK2"]},
    )
    assert "PRKN" in decision.mentioned


def test_default_alias_map_catches_p53():
    """The built-in HARDCODED_ALIAS_SEEDS covers p53 → TP53."""
    claim = {
        "cellular_process": "p53 binds the DNA damage response machinery.",
    }
    decision = classify_claim_locus(
        claim,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=["ATXN3", "TP53", "PRKN", "MFN1"],
    )
    assert "TP53" in decision.mentioned


def test_kept_claims_get_direct_function_context():
    """A claim kept on the hop is a pair-specific binary interaction → 'direct'."""
    result = route_chain_link_claims(
        [PURE_PRKN_CLAIM],
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
    )
    assert len(result.kept) == 1
    assert result.kept[0]["function_context"] == "direct"


def test_rerouted_claims_get_net_function_context():
    """A claim rerouted to the parent indirect is a cascade-level → 'net'."""
    result = route_chain_link_claims(
        [ATXN3_VIA_PRKN_CLAIM],
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
    )
    assert len(result.rerouted) == 1
    assert result.rerouted[0]["function_context"] == "net"


def test_function_context_overrides_llm_label():
    """LLM-emitted 'chain_derived' is corrected to 'direct' when the claim
    actually describes pair-only biology."""
    llm_mislabeled = {**PURE_PRKN_CLAIM, "function_context": "chain_derived"}
    result = route_chain_link_claims(
        [llm_mislabeled],
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
    )
    assert result.kept[0]["function_context"] == "direct"


def test_router_is_idempotent():
    """Running the router on already-routed output leaves decisions stable."""
    claims = [ATXN3_VIA_PRKN_CLAIM, PURE_PRKN_CLAIM]
    first = route_chain_link_claims(
        claims,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
    )
    second = route_chain_link_claims(
        first.kept + first.rerouted + first.dropped,
        main_symbol="ATXN3",
        hop_src="PRKN",
        hop_tgt="MFN1",
        chain_proteins=CHAIN,
    )
    assert len(second.kept) == len(first.kept)
    assert len(second.rerouted) == len(first.rerouted) + len(first.kept) * 0
