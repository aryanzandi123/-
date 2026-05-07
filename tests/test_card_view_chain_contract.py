"""Static contracts for card-view non-query chain rendering."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent


def test_card_view_preserves_full_non_query_chain_order():
    """Each chain renders as its own independent lane under a pathway.

    F2/F7: the old `chainIncludesMain && chainProteins[0] === SNAP.main`
    branch attached every query-led chain to a SHARED centralMainNode,
    visually merging unrelated cascades into one blob under the pathway.
    The new contract: every chain (query-led or not) gets its own
    `_isIndependentChainRoot` so the same protein (e.g. ATXN3, PERK)
    appears separately per chain instance — matching the user's "show
    every chain as its own lane" requirement.
    """
    src = (PROJECT_ROOT / "static" / "_legacy" / "card_view.js").read_text()

    assert "const chainProteins = [...chainGroup.proteins].filter(Boolean);" in src
    assert "_isIndependentChainRoot" in src
    # The old shared-anchor branch must NOT exist anymore. Inline matches
    # only — comments/docstrings can mention the variable name in
    # post-mortem context without re-introducing the runtime bug.
    code_lines = [
        line for line in src.splitlines()
        if not line.lstrip().startswith("//") and not line.lstrip().startswith("*")
    ]
    code = "\n".join(code_lines)
    assert "chainIncludesMain && chainProteins[0] === SNAP.main" not in code, (
        "F2: shared-anchor branch must remain removed — chains render as "
        "independent lanes per chain_id"
    )
    # Affirmative check: each chain becomes an independent root via the
    # uid pattern that includes chain_id, so duplicate proteins across
    # chain instances render separately.
    assert "::chain::${chainId}::" in src, (
        "F7: chain-scoped UIDs must be present so the same protein can "
        "render once per chain instance"
    )


def test_pathway_modal_relevance_includes_chain_link_endpoints():
    visualizer = (PROJECT_ROOT / "static" / "_legacy" / "visualizer.js").read_text()
    card = (PROJECT_ROOT / "static" / "_legacy" / "card_view.js").read_text()

    assert "collectPathwayInteractorIds(pw)" in visualizer
    assert "(pw?.interactions || []).forEach" in visualizer
    assert "(pw?.cross_query_interactions || []).forEach" in visualizer
    assert "addPathwayEndpoint(ix.source);" in card
    assert "addPathwayEndpoint(ix.target);" in card


def test_legacy_modal_groups_by_locus_not_pair_type():
    modal = (PROJECT_ROOT / "static" / "_legacy" / "modal.js").read_text()

    assert "function getInteractionLocus(interaction)" in modal
    assert "function getInteractionSectionType(interaction)" in modal
    assert "locus === 'chain_hop_claim'" in modal
    assert "locus === 'net_effect_claim'" in modal
    assert "CHAIN-HOP CLAIMS" in modal
    assert "NET-EFFECT CLAIMS" in modal
    assert "DIRECT PAIR CLAIMS" in modal
    assert "locus: c.locus" in modal
    assert "if (isIndirectInteraction || isChainHopInteraction)" in modal
    assert "Net-Effect Claims" in modal
    assert "Chain-Hop Claims" in modal
    assert '<span class="context-badge net">NET EFFECT</span>' in modal
    assert '<span class="context-badge chain">CHAIN HOP</span>' in modal


def test_card_filter_direct_mode_excludes_chain_hops_and_net_effects():
    card = (PROJECT_ROOT / "static" / "_legacy" / "card_view.js").read_text()

    assert "function getCvInteractionLocus(interaction)" in card
    assert "if (p.mode === 'direct') return locus === 'direct_claim';" in card
    assert "if (p.mode === 'chain') return locus === 'chain_hop_claim';" in card
    assert (
        "if (p.mode === 'indirect') return locus === 'net_effect_claim' "
        "|| (interaction.interaction_type || interaction.type) === 'indirect';"
    ) in card


def test_card_view_labels_net_effects_and_avoids_fake_direct_anchors():
    card = (PROJECT_ROOT / "static" / "_legacy" / "card_view.js").read_text()

    assert "const locus = getCvInteractionLocus(interaction);" in card
    assert "Net via ${via}: ${sourceId} ${action} ${targetId}" in card
    assert "Hop: ${sourceId} ${action} ${targetId}" in card
    assert "Chain-backed net effects are deliberately not direct anchors" in card
    assert "getCvInteractionLocus(i) === 'net_effect_claim'" in card
    assert "Array.isArray(i.chain_members)" in card
    assert "child.pathwayId = inheritedContext.id;" in card
    assert "child._pathwayContext = inheritedContext;" in card


def test_graph_link_classes_use_locus_for_net_and_chain_rows():
    visualizer = (PROJECT_ROOT / "static" / "_legacy" / "visualizer.js").read_text()

    assert "function getLegacyInteractionLocus(interaction)" in visualizer
    assert "locus === 'net_effect_claim') classes += ' link-net-effect'" in visualizer
    assert "locus === 'chain_hop_claim') classes += ' link-indirect-chain'" in visualizer
    assert "interactionType: interaction.interaction_type || interaction.type || 'direct'" in visualizer
