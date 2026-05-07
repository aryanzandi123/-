# Chain Topology — The DAG-vs-Tree Problem

This is the single most important technical document in this folder for understanding what the user wants the frontend to become. Read it carefully.

## The user's exact framing

Quoted verbatim from the conversation:

> "frontend card view does not know how to handle chains properly and just doesn't really handle all these changes and edits and fixes and stuff really remake and perfect it to so that it can handle perfectly what i want."

And the load-bearing complaints with specific biological examples:

> "if a protein/interactor is implicated in one sci claim that is independent of a chain under the same pathway as the other sci claim, then the interactor/node only appears once and often not in the said chain. Like in the screenshot i have attached HDAC6 is part of that chain and upstream HSP90AA1 but its shown separate simply due to the fact that there's a separate hdac6 atxn3 sci claim that is under same pathway but diff than the chain sic claim under same pathway so its separated... visually..."

> "stub1 fore xampel int eh same screenshot is like upstream hsp90aa1 but is shown as downstream"

> "i think an issue is that our chains are very linear instead realistically we must understand that these are NOT LINEAR NOTHING SHOULD BE CONSTRICTED TO LINEARITY. and more specifically, we must be able to consider the role of other proteins in the middle of any chain etc."

> "if for example stub1 is upstream hsp90aa1 but part of the same chain in atxn3 interaction is or wtv, then we can either position it upstream hsp90aa1 by shown by card that goes straight down (like not right but down instead) from hsp90aa1 or down and left to implicate its upstream and part of same chain?"

The user is describing a fundamental architectural issue: **the card view forces chains into linear trees, but biology is a DAG**. The two specific failure cases (HDAC6 multi-role, STUB1 direction inversion) are emblematic, not isolated.

---

## The three coupled bugs

### Bug A — Same protein appears once, in the "wrong" subtree (HDAC6 case)

**What the user sees:** HDAC6 has TWO roles under one pathway:
1. A chain participant: chain is `…→HSP90AA1→HDAC6` (HDAC6 at end of cascade).
2. An independent direct claim: `ATXN3 ↔ HDAC6` (HDAC6 as direct partner).

The card view shows HDAC6 only once, as a direct child of ATXN3. The chain `…→HSP90AA1→HDAC6` does not render with HDAC6 visible at the cascade endpoint.

**Why it happens (current code path in `static/card_view.js`):**

```
buildCardHierarchy() {
  // 1. Direct anchors first
  const directInteractors = pathwayInteractors.filter(intId =>
    pwEdgeSet.has(`${SNAP.main}|${intId}`) || pwEdgeSet.has(`${intId}|${SNAP.main}`)
  );
  // → HDAC6 is direct, gets placed as a child of ATXN3
  
  // 2. Chain pre-pass second
  const chainGroups = groupChainsByChainId(pathwayInteractors, _pathwayNameForChains);
  // → THIS GATE IS THE BUG (see below).
  // → If gate filters out the HSP90AA1→HDAC6 chain, the chain pre-pass
  //   never iterates it. HDAC6 stays only as a direct child.
  
  // (Even if gate passes, the duplicate-node logic exists:)
  if (assignedIds.has(protId)) {
    const duplicate = createInteractorNode(protId, prevNode.id);
    duplicate._uid = `${protId}::chain::${chainId}::${k}`;
    duplicate._isChainDuplicate = true;
    addChildIfUnique(prevNode, duplicate);
  }
}
```

**The pathway gate** in `groupChainsByChainId(pathwayInteractorIds, pathwayName)`:

```js
const bothEndpointsInPathway = pwSet.has(src) && pwSet.has(tgt);
const chainAssignedHere = !!pathwayNameNorm && chainPathwayNorm === pathwayNameNorm;
if (!bothEndpointsInPathway && !chainAssignedHere) continue;
```

This drops the chain when:
- Only ONE of the chain's endpoints is in this pathway's interactor set, AND
- The chain entity's own `pathway_name` doesn't match this pathway exactly.

The `pathway_name` comparison is just `.trim().toLowerCase()` — case/whitespace tolerant but not full normalization (no underscore-to-space, no synonym handling). If quick_assign assigned the chain to "Protein Quality Control" but the user is viewing "Protein Quality Control & Aggrephagy" (a sibling), the chain is dropped.

**The duplicate-node logic** at line ~1500-1700 of card_view.js DOES exist to render the same protein twice (once as direct, once in chain). But it only fires when the chain pre-pass actually runs the chain — which doesn't happen when the gate filters it out.

### Bug B — Direction inversion (STUB1 / HSP90AA1 case)

**What the user sees:** STUB1 ubiquitinates HSP90AA1 (causally STUB1 upstream). Card view shows STUB1 below HSP90AA1 (visually downstream).

**Why it happens:**

LLM emits chain in query-centric order. For an ATXN3 query where the cascade is `STUB1 → HSP90AA1 → (effect on ATXN3)`, the LLM might emit `chain_proteins = ["ATXN3", "HSP90AA1", "STUB1"]` because it's framing the cascade starting from the query.

`groupChainsByChainId` iterates `chainProteins[]` left-to-right. The chain pre-pass renders `chain[k+1]` as a CHILD of `chain[k]`. So `STUB1` (at position 2) becomes a child of `HSP90AA1` (at position 1) — VISUALLY downstream.

The arrow labels (`chain_with_arrows[i].arrow`) carry the BIOLOGICAL direction (`{from: "STUB1", to: "HSP90AA1", arrow: "ubiquitinates"}` — note from/to here can also drift from the protein order). But the layout ignores this; it uses array order.

### Bug C — Strict tree layout can't represent real cascade biology

| Topology | Biology requires | What strict tree gives you |
|----------|------------------|---------------------------|
| **Branching** (A → B; A → C in same chain) | Two children of A | OK — tree handles |
| **Convergence** (A → C; B → C) | C appears once with two parents | C duplicated; visual link missing |
| **Mid-chain query** (X → query → Y) | Query has both upstream parent AND downstream children IN SAME CHAIN | Independent chain root + duplicate query node — but upstream and downstream halves render in separate subtrees, no visual continuity |
| **Cross-chain shared protein** (HDAC6 in chain α AND chain β AND direct claim) | Three roles, one symbol, all visible together | 1-3 separate nodes depending on pathway gate; no bridge |
| **Cycles** (A → B → C → A — regulatory feedback) | Loop visible | Cycle detection breaks the loop into a partial tree |
| **Same-protein same-pathway dual-role** (HDAC6 in chain `…→HSP90AA1→HDAC6` AND independent ATXN3↔HDAC6 claim under same pathway) | Both visible | One or the other, never both, when chain gate filters wrong |

The tree layout *forces* linearity. The duplicate-node hack helps but is opaque (no visual line connecting the duplicates).

---

## Why the user cares

The biology is the product. A user investigating ATXN3 wants to see:
- HDAC6 chains TO ATXN3 (chain participant) AND ATXN3 directly binds HDAC6 (direct partner) — these are TWO different claims about the same protein in the same pathway.
- STUB1 acts on HSP90AA1 (upstream) AND HSP90AA1 acts in ATXN3-related processes — the directionality is biologically informative.

Hiding either is hiding biology. The user is a biologist. When the UI hides biology, the UI is broken.

---

## The four-layer solution

### Layer 1 — Backend: canonical biological direction at write-time

**Where:** `utils/chain_resolution.py` (new helper) + `utils/db_sync.py:sync_chain_relationships` (call helper before persisting).

**What:** Normalize `chain_proteins` order so `chain[i] → chain[i+1]` is biological cause → effect:

```python
def canonicalize_chain_direction(chain_proteins, chain_with_arrows):
    """Reverse chain order if dominated by reverse-direction verbs.
    
    Returns (canonical_proteins, canonical_arrows, was_reversed).
    """
    if not chain_with_arrows:
        return chain_proteins, [], False
    
    REVERSE_VERBS = {
        'is_substrate_of', 'is_activated_by', 'is_inhibited_by',
        'is_phosphorylated_by', 'is_ubiquitinated_by',
        'is_degraded_by', 'is_cleaved_by', 'is_regulated_by',
    }
    FORWARD_VERBS = {
        'activates', 'inhibits', 'phosphorylates', 'ubiquitinates',
        'cleaves', 'degrades', 'stabilizes', 'destabilizes',
        'represses', 'induces', ...
    }
    
    forward_count = sum(1 for a in chain_with_arrows
                        if a.get('arrow', '').lower() in FORWARD_VERBS)
    reverse_count = sum(1 for a in chain_with_arrows
                        if a.get('arrow', '').lower() in REVERSE_VERBS)
    
    if reverse_count > forward_count:
        # Reverse the chain
        return (
            list(reversed(chain_proteins)),
            list(reversed(chain_with_arrows)),  # also reverse arrow order
            True,
        )
    return chain_proteins, chain_with_arrows, False
```

For mixed-direction chains, KEEP the original order but stamp `_canonical_direction = "LLM-asis-mixed"` in `extra_data` so the frontend renders both arrows visibly.

**Why this fix:** every reader (card view, modal, visualizer) inherits the corrected order. STUB1 ends up at position 0 of `chain_proteins`, HSP90AA1 at position 1 → STUB1 visually upstream.

**Effort:** ~80-120 lines + 4-5 unit tests with known-direction biology.

### Layer 2 — Backend: chain-pathway gate fix at write-time + payload extension

**Where:** `scripts/pathway_v2/quick_assign.py` (call `recompute_pathway_name`) + `services/data_builder.py:_chain_fields_for` (emit `chain_pathways` array).

**What:**

1. Make `IndirectChain.pathway_name` always equal the majority-vote of its chain-derived claims by calling `recompute_pathway_name()` (already exists in models.py) after every `quick_assign_pathways` pass.

2. In `_chain_fields_for`, emit a NEW array field:
```python
chain_pathways = []
for m in memberships:
    ch = m.chain
    if not ch:
        continue
    # Collect every distinct pathway any of this chain's claims landed in
    chain_claim_pathways = (
        InteractionClaim.query
        .filter_by(chain_id=ch.id)
        .with_entities(InteractionClaim.pathway_name)
        .distinct()
        .all()
    )
    chain_pathways.extend(p[0] for p in chain_claim_pathways if p[0])
result['chain_pathways'] = list(set(chain_pathways))
```

3. Update the frontend gate in `groupChainsByChainId`:
```js
// New rule: include chain in pathway P when:
//   EITHER any chain claim landed in P (via inter.chain_pathways)
//   OR     both chain endpoints are direct interactors in P
const chainTouchesPathway =
  bothEndpointsInPathway ||
  (Array.isArray(inter.chain_pathways) &&
   inter.chain_pathways.some(p => normalize(p) === normalize(pathwayName)));
if (!chainTouchesPathway) continue;
```

**Why this fix:** the chain becomes visible in EVERY pathway it has biology in, not just the one its primary `pathway_name` happens to point to. HDAC6's chain renders under HDAC6's direct-claim pathway because at least one chain claim landed there.

**Effort:** ~30-50 lines.

### Layer 3 — Card view: always-render-full-chains + cross-links + edge labels

**Where:** `static/card_view.js:buildCardHierarchy` (chain pre-pass rewrite) + new post-layout pass + edge label rendering + new CSS.

**Three coordinated changes:**

**3a. Always render full chains.** Currently chain pre-pass tries to anchor at the first already-assigned protein. This means many chains render as partial sequences when their first protein is already a direct interactor. Replace with: every chain renders as a complete sequence using `_uid = ${proteinId}::chain::${chainId}::${position}` for every node, regardless of direct-pass assignment. Direct nodes coexist with chain nodes; both are real D3 nodes.

```js
// New chain pre-pass logic:
for (const [chainId, chainGroup] of chainGroups) {
    const chainProteins = chainGroup.proteins;
    const chainColor = getChainColor(chainId);
    let prevNode = null;
    
    for (let k = 0; k < chainProteins.length; k++) {
        const protId = chainProteins[k];
        const uid = `${protId}::chain::${chainId}::${k}`;
        
        const node = createInteractorNode(protId, prevNode ? prevNode.id : parentNode.id);
        node._uid = uid;
        node._chainId = chainId;
        node._chainPosition = k;
        node._chainLength = chainProteins.length;
        node._chainColor = chainColor;
        node._chainProteins = chainProteins;
        node._isChainNode = true;
        if (k === 0) node._isChainRoot = true;
        
        // Edge label: use chain_with_arrows[k-1] for the edge from prev → this
        if (k > 0 && chainGroup.arrows[k-1]) {
            node._inboundChainArrow = chainGroup.arrows[k-1].arrow;
        }
        
        addChildIfUnique(prevNode || parentNode, node);
        nodesById.set(uid, node);
        prevNode = node;
    }
}
```

**3b. Visual cross-links between duplicate-of-same-protein nodes.** After D3 tree layout, walk all nodes and group by base protein symbol. For any symbol with N>1 representations:

```js
function drawDuplicateCrossLinks(nodes, svg) {
    const byProtein = new Map();
    nodes.forEach(d => {
        const protein = d.data.id || d.data.label;
        if (!byProtein.has(protein)) byProtein.set(protein, []);
        byProtein.get(protein).push(d);
    });
    
    for (const [protein, instances] of byProtein) {
        if (instances.length < 2) continue;
        // Connect each pair with a faint dashed path
        for (let i = 0; i < instances.length - 1; i++) {
            for (let j = i + 1; j < instances.length; j++) {
                const a = instances[i], b = instances[j];
                svg.append('path')
                    .attr('class', 'cv-duplicate-crosslink')
                    .attr('d', cubicBezier(a.x, a.y, b.x, b.y))
                    .style('stroke-dasharray', '2,3')
                    .style('opacity', 0.35);
            }
        }
    }
    
    // Hover any instance highlights all instances of the same protein
    cvG.selectAll('.cv-node').on('mouseenter', function(event, d) {
        const protein = d.data.id || d.data.label;
        cvG.selectAll('.cv-node').classed('cv-protein-dim', true);
        instances = byProtein.get(protein) || [];
        instances.forEach(inst => {
            d3.select(inst.node).classed('cv-protein-dim', false);
            d3.select(inst.node).classed('cv-protein-active', true);
        });
    });
}
```

**3c. Direction-aware chain edge labels.** Render arrow verb on each tree edge from chain[k-1] → chain[k]:

```js
// On edges between chain nodes, append a label
linkSel.filter(d => d.target.data._inboundChainArrow)
    .each(function(d) {
        const arrow = d.target.data._inboundChainArrow;
        const midX = (d.source.y + d.target.y) / 2;
        const midY = (d.source.x + d.target.x) / 2;
        cvG.append('text')
            .attr('class', `cv-chain-edge-label arrow-${arrow}`)
            .attr('x', midX)
            .attr('y', midY)
            .text(arrow);
    });
```

**Why this fix:** HDAC6 in chain context AND HDAC6 in direct context both render with a connecting cross-link line — user sees they're the SAME protein. STUB1's chain edge has the verb "ubiquitinates" rendered on the edge so direction is unambiguous even when spatial layout is constrained.

**Effort:** ~150-250 lines + 50 lines CSS.

### Layer 4 (optional) — Sub-DAG layout for dense cascades

**When to pursue:** only if Layer 3's cross-links don't visually solve the user's "non-linearity" perception.

**What:** For pathways where a single chain has ≥2 cycles or ≥3 distinct branches, drop strict tree for that chain's subgraph. Use `dagre.js` (small, ~50KB) for proper DAG layout.

**Why:** strict tree can't draw cycles. PI3K/AKT/mTOR/TSC1/TSC2 has a known feedback loop where mTOR negatively regulates PI3K via S6K1; strict tree would break this loop.

**Effort:** ~300 lines + dagre dependency.

---

## CSS for new elements (Layer 3)

```css
/* Cross-link between same-protein duplicates */
.cv-duplicate-crosslink {
  fill: none;
  stroke: var(--color-text-secondary, #94a3b8);
  stroke-width: 1px;
  stroke-dasharray: 2 3;
  opacity: 0.35;
  pointer-events: none;
}
.cv-duplicate-crosslink.highlighted {
  opacity: 0.9;
  stroke-width: 2px;
}

/* Same-protein highlight on hover */
.cv-protein-active rect {
  stroke: var(--color-accent, #6366f1);
  stroke-width: 3px;
  filter: drop-shadow(0 0 8px var(--color-accent, #6366f1));
}
.cv-protein-dim {
  opacity: 0.5;
}

/* Chain edge label */
.cv-chain-edge-label {
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  fill: var(--color-text-secondary, #94a3b8);
  text-anchor: middle;
  pointer-events: none;
}
.cv-chain-edge-label.arrow-activates { fill: var(--color-activation, #10b981); }
.cv-chain-edge-label.arrow-inhibits { fill: var(--color-inhibition, #ef4444); }
.cv-chain-edge-label.arrow-binds { fill: var(--color-binding, #a78bfa); }
.cv-chain-edge-label.arrow-regulates { fill: var(--color-regulation, #f59e0b); }
.cv-chain-edge-label.arrow-ubiquitinates { fill: var(--color-inhibition, #ef4444); font-weight: 700; }
.cv-chain-edge-label.arrow-phosphorylates { fill: var(--color-activation, #10b981); font-weight: 700; }
```

## Test cases that must pass after Layer 1+2+3

1. Query ATXN3 (assuming HDAC6 chain + direct claim under same pathway):
   - HDAC6 appears in BOTH the direct subtree AND the chain subtree of that pathway.
   - A faint dashed line connects the two HDAC6 cards.
   - Hovering one HDAC6 card highlights both.
2. Query REST or ATXN3 (assuming a chain `STUB1 → HSP90AA1 → ...`):
   - STUB1 appears VISUALLY UPSTREAM of HSP90AA1 in the chain rendering (parent of HSP90AA1).
   - The edge from STUB1 to HSP90AA1 is labeled "ubiquitinates" in red bold.
3. Query a protein with a multi-chain interaction (e.g. ATXN3↔MTOR via VCP→RHEB AND via TSC2→TSC1):
   - The card view renders TWO separate chain subtrees.
   - Modal renders TWO chain context banners stacked.
   - Each banner has its own prev/next nav.

## What it should NOT do

- Don't try to render multi-chain as a single merged graph — that destroys the per-chain biology narrative.
- Don't render so many cross-link lines that the visual becomes unreadable. Cap at e.g. 5 cross-links per protein; if N>5, group into a "+N more instances" badge.
- Don't reorganize the existing pathway tree just to enable the chain DAG. Pathway tree is correct; the FIX is at chain rendering layer only.

## Background — why D3 tree was the original choice

The original card view used `d3.tree()` for two reasons:
1. **Hierarchy is intuitive for pathways.** Pathway → child pathways → interactors → chain hops feels like a tree.
2. **Expand/collapse semantics.** Tree nodes have natural expand/collapse subtree affordances.

Both reasons are still valid. The fix is NOT to throw out tree layout — it's to extend tree layout with cross-links and edge labels so it CAN represent DAG biology when needed.

If Layer 4 ends up necessary, it'll be a sub-layout INSIDE a tree node — pathway is still a tree, but the chain subgraph inside one pathway expansion uses dagre.

## How to brief the user before coding

If you're a fresh Claude reading this and considering Layer 3, say something like:

> "The cleanest path is Layers 1-3 together. Layer 1 normalizes chain direction at write time so the data is correct (fixes STUB1 inversion). Layer 2 widens the pathway gate so HDAC6's chain stops being silently dropped. Layer 3 always renders chains as full sequences and adds cross-links between duplicate-of-same-protein nodes, plus arrow labels on edges. Layer 4 is optional fallback for cycles. ~450 lines total. Want me to do all three coordinated, or A1 (token cap fix) first then come back to this?"

That's the right framing. The user will likely say "do A1 first then come back" because A1 is needed to have CLEAN chain data to render in Layer 3 anyway.
