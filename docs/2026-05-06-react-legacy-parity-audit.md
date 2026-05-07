# React Legacy Parity Audit

Date: 2026-05-06 PDT  
Workspace: `/Users/aryan/Desktop/DADA/untitled folder 2 copy 54`  
Audit mode: no code edits before this file was saved.

## Route Verification

Commands from the handoff were run against the live Flask app:

- `http://127.0.0.1:5003/api/visualize/PERK` returned `200 OK`, `Cache-Control: no-store, max-age=0`, `X-Viz-Cache: miss`, and legacy HTML with 19 `_legacy` references.
- `http://127.0.0.1:5003/api/visualize/PERK?spa=1` returned `200 OK`, `Cache-Control: no-store, max-age=0`, `X-Viz-Shell: spa`, and React HTML with 2 `/static/react` references.
- `routes/visualization.py` matches this behavior: legacy is default, React is opt-in via `?spa=1`.
- `react-app/vite.config.ts` defaults the proxy target to `http://127.0.0.1:5003`.

## Screenshot Artifacts

Screenshots were saved outside the repo:

- `/tmp/propaths-parity-audit-2026-05-06/perk-legacy-initial-1440x1000.png`
- `/tmp/propaths-parity-audit-2026-05-06/perk-spa-initial-1440x1000.png`
- `/tmp/propaths-parity-audit-2026-05-06/perk-legacy-esyt1-modal-1440x1000.png`
- `/tmp/propaths-parity-audit-2026-05-06/perk-spa-mbio-esyt1-modal-1440x1000.png`
- `/tmp/propaths-parity-audit-2026-05-06/tdp43-legacy-keap1-aggregate-antioxidant-1440x1000.png`
- `/tmp/propaths-parity-audit-2026-05-06/tdp43-spa-keap1-aggregate-antioxidant-1440x1000.png`
- `/tmp/propaths-parity-audit-2026-05-06/tdp43-legacy-chain-grn-sort1-1440x1000.png`
- `/tmp/propaths-parity-audit-2026-05-06/tdp43-spa-chain-grn-sort1-lysosomal-dispatch-1440x1000.png`
- `/tmp/propaths-parity-audit-2026-05-06/perk-spa-initial-390x844.png`

## API Payload Notes

Fetched from `/api/results/<protein>`:

- `PERK`: schema `2026-05-04`, 57 proteins, 191 interactions, 254 pathways.
- `KEAP1`: schema `2026-05-04`, 7 proteins, 120 interactions, 104 pathways.
- `TDP43`: schema `2026-05-04`, 60 proteins, 199 interactions, 242 pathways.
- `TDP43` has 42 `_is_chain_link` rows. Nineteen chain rows have `functions.length > 0` and `claims.length == 0`, so React must render hop-specific `functions` when claims are absent.
- No RNA, UBIQUITIN, or DNA graph nodes/endpoints were present in the PERK, KEAP1, or TDP43 payloads checked.
- `TDP43` diagnostics report partial chains for `GRN` and `ULK1`, plus unrecoverable pair `TDP43->XPO7`.

## Findings

### P0: React Drops PERK-ESYT1 From The Selected Pathway Graph

Exact route and viewport:

- Legacy: `http://127.0.0.1:5003/api/visualize/PERK`, 1440x1000, legacy default route.
- React: `http://127.0.0.1:5003/api/visualize/PERK?spa=1`, 1440x1000, selected `Mitochondrial Bioenergetics`.

Legacy screenshot observation:

- `PERK -> ESYT1` opens a clean single-edge modal.
- Modal shows one function card: `ER-Mitochondria Lipid Trafficking & Mitochondrial Bioenergetics`.
- It preserves direction, direct badge, function card density, effect badges, cascade, specific effects, and evidence sections.

React screenshot observation:

- Selecting `Mitochondrial Bioenergetics` produces an 8-node/11-edge graph with MFN2/ITPR1/VDAC1 content.
- There is no `PERK -> ESYT1` edge or ESYT1 node to click.
- The attempted React PERK-ESYT1 modal could not open because the edge was absent from the rendered graph.

API payload note:

- `/api/results/PERK` contains direct `PERK -> ESYT1`, `arrow: binds`, `direction: main_to_primary`, `claims.length: 1`, `functions.length: 1`.
- The claim pathway is `Mitochondrial Bioenergetics`.

File/source suspects:

- `react-app/src/app/views/card/buildCardGraph.ts`
- `react-app/src/app/lib/pathwayStats.ts`
- `services/data_builder.py` only if interaction-level pathway fields are proven missing incorrectly.

Likely local cause:

- `buildCardGraph.ts:pathwayTouches()` checks `inter.pathways`, `inter.chain_pathways`, and `all_chains`, but not claim-level `claim.pathway` / `claim.pathway_name`.
- PERK-ESYT1 carries the pathway at claim level, while the interaction row has no interaction-level `pathways`, so the React graph filter drops a scientifically valid edge.

### P0: React Chain-Hop Modal Renders Empty Under Pathway Filter Even Though Hop Functions Exist

Exact route and viewport:

- Legacy: `http://127.0.0.1:5003/api/visualize/TDP43`, 1440x1000, chain row `GRN -> SORT1`, chain `2612`.
- React: `http://127.0.0.1:5003/api/visualize/TDP43?spa=1`, 1440x1000, selected `Lysosomal Transport`, clicked `chain::2612::edge::1`.

Legacy screenshot observation:

- `GRN -> SORT1` modal shows chain context: `GRN -> SORT1 -> CTSD -> TDP43`.
- It renders one hop function card: `C-terminal Binding & Endocytic Sorting`.
- It does not mix broad direct claims into the hop card.

React screenshot observation:

- React opens the chain-hop modal and shows the chain context.
- Metadata says `FUNCTIONS 0 of 1`.
- Body says `No claims to render`.
- This violates the handoff requirement that chain-hop cards must not render empty when hop-specific `functions` exist.

API payload note:

- `/api/results/TDP43` chain row `GRN -> SORT1`, `chain_id: 2612`, has `claims.length: 0`, `functions.length: 1`.
- The hop function exists, but its own pathway text is `Lysosomal Function & Proteostasis` while the chain context is `Lysosomal Transport`.

File/source suspects:

- `react-app/src/app/modal/InteractionModal.tsx`
- `react-app/src/app/lib/interactionSurface.ts`
- `react-app/src/app/lib/claims.ts`

Likely local cause:

- `claimsForInteraction()` correctly chooses `functions` for chain rows.
- `InteractionModal` then filters those functions through `isPathwayInContext(c, pathwayContext)`.
- For chain-hop rows, pathway filtering needs to accept the chain pathway/context as well as the claim's own pathway, otherwise valid hop functions disappear.

### P1: React Defaults To A Selected Pathway, Legacy Defaults To No Selection

Exact route and viewport:

- Legacy: `http://127.0.0.1:5003/api/visualize/PERK`, 1440x1000.
- React: `http://127.0.0.1:5003/api/visualize/PERK?spa=1`, 1440x1000.

Legacy screenshot observation:

- Starts with no pathway selected and displays the empty pathway canvas message.

React screenshot observation:

- Starts with `Integrated Stress Response (ISR)` selected automatically.
- The graph is filtered immediately on first load.

File/source suspects:

- `react-app/src/app/views/card/PathwayExplorer.tsx`
- `react-app/src/app/views/card/buildCardGraph.ts`

Likely local cause:

- `PathwayExplorer.tsx` has first-hydration auto-selection.
- `buildCardGraph.ts` comments say empty selection should emit all, but the implementation returns an empty graph for empty selection.

### P1: React KEAP1 Aggregate Is Less Compact Than Legacy

Exact route and viewport:

- Legacy: `http://127.0.0.1:5003/api/visualize/TDP43`, 1440x1000, pathway context `Antioxidant Response`, node `KEAP1`.
- React: `http://127.0.0.1:5003/api/visualize/TDP43?spa=1`, 1440x1000, selected `Antioxidant Response`, node `KEAP1`.

Legacy screenshot observation:

- Compact grouped sections: `DIRECT INTERACTIONS`, `INDIRECT INTERACTIONS`, `SHARED INTERACTIONS`.
- Collapsed rows remain scannable and rows assigned to other pathways are explicitly marked.
- The first expanded direct claim still keeps a compact row anatomy.

React screenshot observation:

- Opens `KEAP1` aggregate with `6 interactions`.
- Shows `3 of 10` visible claims, `1 of 5` pathways, and a `Show all` toggle.
- First row is expanded by default into large mechanism/effect/cascade prose, so the modal quickly becomes a wall-of-text compared with legacy.

File/source suspects:

- `react-app/src/app/modal/AggregatedModal.tsx`
- `react-app/src/app/modal/FunctionCard.tsx`
- `react-app/src/app/modal/cascade.module.css`

### P1: Mobile React Layout Is Not Usable Enough For Parity

Exact route and viewport:

- React: `http://127.0.0.1:5003/api/visualize/PERK?spa=1`, 390x844.

React screenshot observation:

- Header wraps awkwardly (`ProPaths` and `PERK` split).
- Filter controls and checkboxes crowd the top rows.
- Pathway sidebar consumes nearly the full mobile viewport.
- Graph is mostly hidden/clipped to a narrow sliver behind the sidebar.

File/source suspects:

- `react-app/src/app/views/card/PathwayExplorer.tsx`
- `react-app/src/app/views/card/CardView.tsx`
- app-level shell styles / responsive layout tokens.

## Parity Checklist Snapshot

- Query landing/start workflow and progress stream: not audited in this pass.
- Card/network layout: partial; React renders graph, but pathway filtering drops PERK-ESYT1 and mobile layout is poor.
- Direct interactions: failing for PERK-ESYT1 under selected pathway.
- Chain-hop interactions: failing for GRN-SORT1 under selected pathway because hop function is hidden.
- Correct arrows/direction: React shows arrows in visible cases; PERK-ESYT1 cannot be verified because edge is absent.
- Multi-claim interactions: partially present in KEAP1 aggregate; compact legacy anatomy not matched.
- Node aggregate modal: functional but less compact than legacy.
- Edge/single-interaction modal: legacy verified; React blocked by absent PERK-ESYT1 edge.
- Chain context banner and drilldown: context banner appears; current pathway filtering hides valid hop functions.
- Pathway Explorer V2: functional but default selection differs from legacy.
- Evidence display: React shows no-citation warnings; legacy evidence sections remain richer/clearer in PERK-ESYT1.
- Partial-chain badges: React top diagnostic badge appears (`partial chains 2`, `unrecoverable 1`).
- Pseudo-protein filtering: payload spot-check passed for RNA/UBIQUITIN/DNA.
- Keyboard behavior: not audited in this pass.
- Mobile/desktop responsive behavior: desktop usable; mobile not parity-ready.

## Stage Recommendation

Start with Stage 1 contract stabilization before modal styling:

1. Make React pathway filtering include claim-level pathways for direct interactions, so PERK-ESYT1 appears under `Mitochondrial Bioenergetics`.
2. Preserve chain-hop `functions` under the selected chain pathway/context, so GRN-SORT1 does not render an empty card.
3. Revisit default pathway auto-selection after the data-selection fixes, because legacy starts unselected and the React code comments contradict the implementation.

