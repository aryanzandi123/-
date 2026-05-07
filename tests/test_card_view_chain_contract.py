"""Static contracts for card-view non-query chain rendering."""

import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent


def _extract_js_function(source: str, name: str) -> str:
    """Return a top-level JavaScript function declaration from source."""
    start = source.index(f"function {name}(")
    paren_start = source.index("(", start)
    paren_depth = 0
    paren_end = None
    for index in range(paren_start, len(source)):
        char = source[index]
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
            if paren_depth == 0:
                paren_end = index
                break
    if paren_end is None:
        raise ValueError(f"Could not find parameter list for JavaScript function {name}")
    brace_start = source.index("{", paren_end)
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
            "sameCardProteinSymbol",
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


def _run_card_context_selector_edge_fixture() -> dict[str, list[str]]:
    """Execute the legacy modal selector against fallback/reversed chain rows."""
    visualizer = (PROJECT_ROOT / "static" / "_legacy" / "visualizer.js").read_text()
    functions = "\n\n".join(
        _extract_js_function(visualizer, name)
        for name in (
            "getLegacyInteractionLocus",
            "cardContextChainMatches",
            "getCardContextHopCandidates",
            "getCardRowHopIndex",
            "sameCardProteinSymbol",
            "selectLinksForCardContext",
        )
    )
    script = f"""
{functions}

const chainProteins = ['TDP43', 'PERK', 'ATXN3'];
const interactions = [
  {{
    id: 'all-chains-fallback',
    locus: 'chain_hop_claim',
    hop_index: 0,
    source: 'TDP43',
    target: 'PERK',
    all_chains: [{{ chain_proteins: chainProteins }}],
  }},
  {{
    id: 'entity-fallback',
    locus: 'chain_hop_claim',
    hop_index: 0,
    source: 'TDP43',
    target: 'PERK',
    _chain_entity: {{ chain_proteins: chainProteins }},
  }},
  {{
    id: 'reversed-hop',
    locus: 'chain_hop_claim',
    chain_id: chainProteins.join('->'),
    hop_index: 0,
    source: 'PERK',
    target: 'TDP43',
  }},
  {{
    id: 'stale-exact-direct',
    _interaction_instance_id: 'stale-direct-id',
    locus: 'direct_claim',
    chain_id: chainProteins.join('->'),
    hop_index: 0,
    source: 'TDP43',
    target: 'PERK',
  }},
  {{
    id: 'outbound-hop',
    _db_id: 42,
    locus: 'chain_hop_claim',
    chain_id: chainProteins.join('->'),
    hop_index: 1,
    source: 'ATXN3',
    target: 'PERK',
  }},
];

const ids = rows => rows.map(row => row.id);

console.log(JSON.stringify({{
  fallback: ids(selectLinksForCardContext(interactions, {{
    _chainId: chainProteins.join('->'),
    _chainPosition: 1,
    _chainProteins: chainProteins,
  }})),
  staleExact: ids(selectLinksForCardContext(interactions, {{
    relationshipInteractionId: 'stale-direct-id',
    _chainId: chainProteins.join('->'),
    _chainPosition: 1,
    _chainProteins: chainProteins,
  }})),
  dbExact: ids(selectLinksForCardContext(interactions, {{
    relationshipDbId: 42,
    _chainId: chainProteins.join('->'),
    _chainPosition: 1,
    _chainProteins: chainProteins,
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


def _run_group_chains_pathway_claim_fixture() -> dict[str, object]:
    """Execute chain grouping for an explicit pathway claim without endpoint overlap."""
    card = (PROJECT_ROOT / "static" / "_legacy" / "card_view.js").read_text()
    function = _extract_js_function(card, "groupChainsByChainId")
    script = f"""
var window = {{}};
var SNAP = {{
  interactions: [
    {{
      _is_chain_link: true,
      source: 'ULK1',
      target: 'TBK1',
      chain_id: '2613',
      chain_pathways: ['Autophagy'],
      _chain_entity: {{
        pathway_name: 'Other pathway',
        chain_proteins: ['ULK1', 'TBK1', 'SQSTM1', 'TDP43'],
        chain_with_arrows: [],
      }},
    }},
  ],
}};

{function}

const groups = groupChainsByChainId(['ATG7'], 'Autophagy');
const group = groups.get('2613');
console.log(JSON.stringify({{
  chainIds: Array.from(groups.keys()),
  proteins: group ? group.proteins : [],
  interactions: group ? group.interactions.length : 0,
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _run_group_chains_explicit_pathway_scope_fixture() -> dict[str, object]:
    """Execute chain grouping where endpoint membership would otherwise leak."""
    card = (PROJECT_ROOT / "static" / "_legacy" / "card_view.js").read_text()
    function = _extract_js_function(card, "groupChainsByChainId")
    script = f"""
var window = {{}};
var SNAP = {{
  interactions: [
    {{
      id: 'pqc-chain',
      _is_chain_link: true,
      source: 'TDP43',
      target: 'CYLD',
      all_chains: [{{
        chain_id: 2625,
        pathway_name: 'Protein Quality Control',
        chain_pathways: ['Protein Quality Control'],
        chain_proteins: ['TDP43', 'CYLD', 'HDAC6'],
        chain_with_arrows: [],
      }}],
      _chain_entity: {{
        pathway_name: 'Protein Quality Control',
        chain_proteins: ['TDP43', 'CYLD', 'HDAC6'],
        chain_with_arrows: [],
      }},
    }},
    {{
      id: 'inflammatory-chain',
      _is_chain_link: true,
      source: 'CYLD',
      target: 'TDP43',
      all_chains: [{{
        chain_id: 2626,
        pathway_name: 'Inflammatory Signaling',
        chain_pathways: ['Inflammatory Signaling'],
        chain_proteins: ['TDP43', 'CYLD', 'MAP3K7'],
        chain_with_arrows: [],
      }}],
      _chain_entity: {{
        pathway_name: 'Inflammatory Signaling',
        chain_proteins: ['TDP43', 'CYLD', 'MAP3K7'],
        chain_with_arrows: [],
      }},
    }},
  ],
}};

{function}

const groups = groupChainsByChainId(['TDP43', 'CYLD'], 'Protein Quality Control');
console.log(JSON.stringify({{
  chainIds: Array.from(groups.keys()).map(String),
  interactions: Object.fromEntries(Array.from(groups.entries()).map(([id, group]) => [
    String(id),
    group.interactions.map(interaction => interaction.id),
  ])),
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _chain_prepass_code(source: str) -> str:
    """Return executable-looking lines from the Card View chain pre-pass."""
    start = source.index("const chainGroups = groupChainsByChainId(")
    end = source.index("// --- PASS 2: Extensions", start)
    lines = []
    for line in source[start:end].splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        lines.append(line)
    return "\n".join(lines)


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


def test_card_view_renders_chains_admitted_by_pathway_claim_without_overlap_gate():
    """Chains admitted by pathway membership must reach chain-node construction."""
    admitted = _run_group_chains_pathway_claim_fixture()
    card = (PROJECT_ROOT / "static" / "_legacy" / "card_view.js").read_text()
    prepass = _chain_prepass_code(card)

    assert admitted == {
        "chainIds": ["2613"],
        "proteins": ["ULK1", "TBK1", "SQSTM1", "TDP43"],
        "interactions": 1,
    }
    assert "for (const [chainId, chainGroup] of chainGroups)" in prepass
    assert "chainProteins.some(p => pathwayInteractorSet.has(p))" not in prepass
    assert "chainTouchesPathway" not in prepass
    assert "rootNode._chainId = chainId;" in prepass
    assert "node._chainId = chainId;" in prepass


def test_card_view_does_not_place_explicit_other_pathway_chain_by_endpoint_overlap():
    selected = _run_group_chains_explicit_pathway_scope_fixture()

    assert selected == {
        "chainIds": ["2625"],
        "interactions": {"2625": ["pqc-chain"]},
    }


def test_card_modal_chain_context_accepts_fallback_ids_and_reversed_hops():
    selected = _run_card_context_selector_edge_fixture()

    assert selected == {
        "fallback": ["all-chains-fallback", "entity-fallback", "reversed-hop", "outbound-hop"],
        "staleExact": ["all-chains-fallback", "entity-fallback", "reversed-hop", "outbound-hop"],
        "dbExact": ["outbound-hop"],
    }


def test_middle_chain_instance_modal_selects_inbound_and_outbound_hops():
    """Middle protein cards should show both adjacent chain-hop claims."""
    visualizer = (PROJECT_ROOT / "static" / "_legacy" / "visualizer.js").read_text()
    functions = "\n\n".join(
        _extract_js_function(visualizer, name)
        for name in (
            "getLegacyInteractionLocus",
            "cardContextChainMatches",
            "getCardContextHopCandidates",
            "getCardRowHopIndex",
            "sameCardProteinSymbol",
            "selectLinksForCardContext",
        )
    )
    script = f"""
{functions}

const chainProteins = ['EIF2AK3', 'EWSR1', 'TDP43'];
const interactions = [
  {{
    id: 'inbound',
    locus: 'chain_hop_claim',
    chain_id: 2621,
    hop_index: 0,
    source: 'EIF2AK3',
    target: 'EWSR1',
  }},
  {{
    id: 'outbound',
    locus: 'chain_hop_claim',
    chain_id: 2621,
    hop_index: 1,
    source: 'EWSR1',
    target: 'TDP43',
  }},
  {{
    id: 'other-chain',
    locus: 'chain_hop_claim',
    chain_id: 9999,
    hop_index: 1,
    source: 'EWSR1',
    target: 'TDP43',
  }},
];

const ids = rows => rows.map(row => row.id);
console.log(JSON.stringify({{
  middle: ids(selectLinksForCardContext(interactions, {{
    _chainId: 2621,
    _chainPosition: 1,
    _chainProteins: chainProteins,
  }})),
  root: ids(selectLinksForCardContext(interactions, {{
    _chainId: 2621,
    _chainPosition: 0,
    _chainProteins: chainProteins,
  }})),
  terminal: ids(selectLinksForCardContext(interactions, {{
    _chainId: 2621,
    _chainPosition: 2,
    _chainProteins: chainProteins,
  }})),
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    selected = json.loads(completed.stdout)

    assert selected == {
        "middle": ["inbound", "outbound"],
        "root": ["inbound"],
        "terminal": ["outbound"],
    }


def test_card_relationship_lookup_uses_same_chain_contract_as_modal():
    card = (PROJECT_ROOT / "static" / "_legacy" / "card_view.js").read_text()
    functions = "\n\n".join(
        _extract_js_function(card, name)
        for name in (
            "getCvInteractionLocus",
            "_cvSameSymbol",
            "_cvHopIndex",
            "_cvChainProteinId",
            "_cvInteractionMatchesChainId",
            "getLocalRelationship",
        )
    )
    script = f"""
global.window = {{}};
{functions}

const chainProteins = ['TDP43', 'PERK', 'ATXN3'];
global.SNAP = {{
  interactions: [
    {{
      id: 'wrong-direct',
      locus: 'direct_claim',
      source: 'TDP43',
      target: 'PERK',
      arrow: 'binds',
    }},
    {{
      id: 'reversed-hop',
      locus: 'chain_hop_claim',
      source: 'PERK',
      target: 'TDP43',
      arrow: 'activates',
      hop_index: 0,
      all_chains: [{{ chain_proteins: chainProteins }}],
      _interaction_instance_id: 'hop-instance',
    }},
  ],
}};

const rel = getLocalRelationship('TDP43', 'PERK', {{
  chainId: chainProteins.join('->'),
  chainPosition: 1,
  chainProteins,
}});
console.log(JSON.stringify({{
  id: rel.raw.id,
  interactionId: rel.raw._interaction_instance_id,
  text: rel.text,
  locus: rel.locus,
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    selected = json.loads(completed.stdout)

    assert selected == {
        "id": "reversed-hop",
        "interactionId": "hop-instance",
        "text": "Hop: TDP43 activates PERK",
        "locus": "chain_hop_claim",
    }


def test_legacy_card_layout_css_keeps_duplicate_cues_and_viewport_bounds():
    styles = (PROJECT_ROOT / "static" / "styles.css").read_text()

    assert "body.card-view-active .cv-duplicate-crosslink" in styles
    assert "opacity: 0.18 !important;" in styles
    assert "pointer-events: stroke !important;" in styles
    assert "body.card-view-active .cv-duplicate-crosslink:hover" in styles
    assert "body.card-view-active .header" in styles
    assert "transform: translateY(0) !important;" in styles
    assert "body.card-view-active .pathway-explorer-v2 .pe-item-content" in styles
    assert "max-width: 100% !important;" in styles
    assert "body.card-view-active #card-svg-container" in styles
    assert "max-width: 100vw !important;" in styles


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
    assert "function visualizerLinkId(source, target, arrow, interaction, suffix = '')" in visualizer
    assert "interaction._interaction_instance_id || interaction._display_row_id" in visualizer
    assert "const linkId = visualizerLinkId(source, target, arrow, interaction);" in visualizer
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
    assert "rowHop !== candidate.hopIndex" in visualizer
    assert "sameCardProteinSymbol(interaction.source, candidate.source)" in visualizer
    assert "sameCardProteinSymbol(interaction.target, candidate.target)" in visualizer
    assert "sameCardProteinSymbol(interaction.source, candidate.target)" in visualizer
    assert "sameCardProteinSymbol(interaction.target, candidate.source)" in visualizer
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
