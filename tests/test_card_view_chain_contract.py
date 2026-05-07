"""Static contracts for card-view non-query chain rendering."""

import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent


def _extract_js_function(source: str, name: str) -> str:
    """Return a top-level JavaScript function declaration from source."""
    start = source.index(f"function {name}(")
    brace_start = source.index("{", start)
    depth = 0
    for index in range(brace_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise ValueError(f"Could not extract JavaScript function {name}")


def _run_card_context_selector_fixture() -> dict[str, list[str]]:
    """Execute the legacy modal selector against a minimal chain fixture."""
    visualizer = (PROJECT_ROOT / "static" / "_legacy" / "visualizer.js").read_text()
    functions = "\n\n".join(
        _extract_js_function(visualizer, name)
        for name in (
            "getLegacyInteractionLocus",
            "cardContextChainMatches",
            "getCardContextHopCandidates",
            "getCardRowHopIndex",
            "selectLinksForCardContext",
        )
    )
    script = f"""
{functions}

const interactions = [
  {{
    id: 'scoped-hop',
    locus: 'chain_hop_claim',
    chain_id: 'chain-A',
    hop_index: 0,
    source: 'TDP43',
    target: 'PERK',
  }},
  {{
    id: 'aggregate-same-pair',
    locus: 'direct_claim',
    source: 'TDP43',
    target: 'PERK',
  }},
  {{
    id: 'aggregate-next-pair',
    locus: 'direct_claim',
    source: 'PERK',
    target: 'ATXN3',
  }},
  {{
    id: 'other-chain-hop',
    locus: 'chain_hop_claim',
    chain_id: 'chain-B',
    hop_index: 1,
    source: 'PERK',
    target: 'ATXN3',
  }},
];

const chainProteins = ['TDP43', 'PERK', 'ATXN3'];
const ids = rows => rows.map(row => row.id);

console.log(JSON.stringify({{
  matching: ids(selectLinksForCardContext(interactions, {{
    _chainId: 'chain-A',
    _chainPosition: 1,
    _chainProteins: chainProteins,
  }})),
  nonmatching: ids(selectLinksForCardContext(interactions, {{
    _chainId: 'chain-A',
    _chainPosition: 2,
    _chainProteins: chainProteins,
  }})),
  aggregate: ids(selectLinksForCardContext(interactions, {{
    label: 'PERK',
  }})),
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


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


def test_card_view_passes_chain_context_to_modal_open():
    card = (PROJECT_ROOT / "static" / "_legacy" / "card_view.js").read_text()
    visualizer = (PROJECT_ROOT / "static" / "_legacy" / "visualizer.js").read_text()

    assert "function makeCardModalContext(d, pathwayContext = null)" in card
    assert "_chainId: data._chainId ?? null" in card
    assert "_chainPosition: data._chainPosition ?? null" in card
    assert "_chainProteins: Array.isArray(data._chainProteins)" in card
    assert "const cardContext = makeCardModalContext(d, pathwayContext);" in card
    assert "window.openModalForCard(d.data.id, pathwayContext, cardContext);" in card
    assert "window.openModalForCard = (nodeId, pathwayContext = null, cardContext = null)" in visualizer


def test_modal_selection_prefers_chain_id_and_hop_context():
    visualizer = (PROJECT_ROOT / "static" / "_legacy" / "visualizer.js").read_text()
    modal = (PROJECT_ROOT / "static" / "_legacy" / "modal.js").read_text()

    assert "function selectLinksForCardContext(interactions, cardContext)" in visualizer
    assert "const chainId = cardContext?._chainId;" in visualizer
    assert "const hopCandidates = getCardContextHopCandidates(cardContext);" in visualizer
    assert "rowHop === candidate.hopIndex" in visualizer
    assert "interaction.source === candidate.source && interaction.target === candidate.target" in visualizer
    assert "const selectedInteractions = selectLinksForCardContext(interactionData, cardContext);" in visualizer
    assert "const hasChainScopedCardContext = clickedCardContext?._chainId != null;" in modal
    assert "if (!showAll && !hasChainScopedCardContext)" in modal


def test_modal_snap_fallback_is_gated_by_empty_chain_scope():
    """Empty chain-scoped modal selections must not hydrate aggregate rows."""
    modal = (PROJECT_ROOT / "static" / "_legacy" / "modal.js").read_text()
    scoped_context_line = next(
        line for line in modal.splitlines()
        if "const hasChainScopedCardContext =" in line
    )

    assert "clickedCardContext?._chainId != null" in scoped_context_line
    assert "nodeLinks.length" not in scoped_context_line
    assert (
        "if (clickedNode.pathwayId && SNAP && SNAP.interactions && !hasChainScopedCardContext)"
        in modal
    )


def test_modal_selector_does_not_fallback_for_unmatched_chain_scoped_hop():
    """Chain-scoped modal clicks must not broaden to aggregate protein rows."""
    selected = _run_card_context_selector_fixture()

    assert selected["matching"] == ["scoped-hop"]
    assert selected["nonmatching"] == []
    assert selected["aggregate"] == [
        "scoped-hop",
        "aggregate-same-pair",
        "aggregate-next-pair",
        "other-chain-hop",
    ]


def test_modal_hop_labels_and_chain_nav_preserve_context_object():
    modal = (PROJECT_ROOT / "static" / "_legacy" / "modal.js").read_text()

    assert "function getDisplayHopIndex(L)" in modal
    assert "const value = L && (L.hop_index ?? L._chain_position);" in modal
    assert "getDisplayHopIndex(L)" in modal
    assert "const navClickedNode = buildChainNavClickedNode(target, hopLink);" in modal
    assert "showAggregatedInteractionsModal(" in modal
    assert "navClickedNode," in modal
    assert "cardContext: nextCardContext" in modal
    assert "const isIndirectInteraction = !isChainHopInteraction && (L.interaction_type === 'indirect' || isNetEffectInteraction);" in modal
    assert "const isIndirect = !isChainHopInteraction && (L.interaction_type === 'indirect' || isNetEffectInteraction);" in modal
    assert "const isIndirect = !isChainHop && (L.interaction_type === 'indirect' || isNetEffect);" in modal
