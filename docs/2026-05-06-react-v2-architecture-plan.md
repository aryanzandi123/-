# React v2 Architecture Plan

Date: 2026-05-06 PDT  
Workspace: `/Users/aryan/Downloads/untitled folder 8/untitled folder 2 copy 117`  
Mode: architecture plan only. No product code edits are part of this plan pass.

## Top-Level Rule

React v2 is legacy-first semantically, not pixel-perfect legacy.

React v2 equals legacy semantics plus a new product-quality implementation.
It must preserve the legacy biological hierarchy, modal/card concepts,
pathway/chain/direct semantics, and evidence/PMID visibility, while replacing
the old implementation and presentation weaknesses.

The Legacy Parity Gate must pass before enhancements are allowed:

- `PERK -> ESYT1` is visible under the correct pathway context.
- KEAP1 aggregate behavior stays compact, grouped, and biologically readable.
- TDP43 chain and net-effect semantics stay distinct.
- Pathway filtering preserves legacy cascade and inclusion semantics.
- Graph and card views preserve the legacy biological reading model.
- Enhancements come only after parity is proven against these cases.

Do not copy legacy's old imperative architecture, cramped layout, fragile modal
sizing, visual clutter, stale quirks, or bugs.

## Decision

Build React v2 as a new isolated frontend shell, side-by-side with both stable legacy and the current experimental React app.

Target comparison URLs:

- Legacy source of truth: `/api/visualize/TDP43`
- Current experimental React: `/api/visualize/TDP43?spa=1`
- New React v2: `/api/visualize/TDP43?spa=2`

This gives a controlled three-way comparison without changing the default route, without deleting `static/_legacy/`, and without treating the current `?spa=1` app as nearly ready.

## Current Blocker

React v2 is blocked on the API chain-contract repair documented in
`docs/2026-05-06-api-chain-contract-repair-plan.md`.

The 2026-05-06 chain-semantics audit found two P0 API contract bugs:

- DB stores `TDP43 -> DDX3X` as an `indirect` row with `function_context = net`,
  but `/api/results/TDP43` emits that same row as `type = direct` and
  `interaction_type = direct`.
- DB has a real `GLE1 -> DDX3X` hop row and claim for chain `2624`, but
  `/api/results/TDP43` synthesizes that hop from stale parent JSONB and exposes
  the wrong pathway source.

React must not paper over `/api/results` changing indirect/net records into
direct records. The next implementation work is backend/API contract repair,
not UI coding. React v2 can resume only after the payload preserves direct,
chain-hop, and net-effect locus explicitly.

React v2 should live in a separate source tree and build output:

- Source: `react-app/src/v2/`
- Template: `templates/visualize_v2.html`
- Built assets: `static/react-v2/`
- Build config: either `react-app/vite.v2.config.ts` or a clearly isolated v2 mode in Vite that writes only to `static/react-v2/`.
- Flask route switch: `?spa=2` renders the v2 shell and sets `X-Viz-Shell: react-v2`.

Current React may be mined for helpers only. Its component architecture should not be the foundation for v2.

## Product Spec Source

Legacy frontend is the semantic/product reference, not a pixel-perfect UI
target. React v2 should preserve the biological reading model and information
hierarchy, while rebuilding the implementation, layout, interactions,
performance model, and state boundaries with product-quality React architecture.

React v2 equals legacy semantics plus a new product-quality implementation.

Preserve from legacy:

- biological information hierarchy
- single-edge focused modals
- node aggregate modals
- pathway grouping and filtering behavior
- chain/direct/indirect distinction
- evidence and PMID visibility
- function/mechanism/effect/cascade organization
- card/graph reading model
- dense scientific detail without turning modals into walls of text

Do not preserve from legacy:

- old imperative JS architecture
- cramped layout
- inconsistent styling
- fragile modal sizing
- confusing duplicated sections
- visual clutter
- pathway/chain bugs
- any behavior where indirect or net effects look direct
- stale or accidental data grouping quirks

If legacy has the right data but bad presentation, keep the data model and
redesign the presentation. If legacy has a bug, document the bug and define the
corrected React v2 behavior. If current React has a feature legacy lacks, keep
it only when it strengthens the legacy mental model.

React v2 should port semantic behavior from these legacy files in this order:

1. `static/_legacy/card_view.js`
   - Card graph data model, pathway selection semantics, chain layout, cross-query controls, pseudo filter, direct/indirect/shared/chain handling.
2. `static/_legacy/modal.js`
   - Single-edge modal, aggregate/node modal, chain context banner, pathway-filtered row handling, claim/function/evidence rendering.
3. `static/_legacy/visualizer.js`
   - Data hydration helpers, pathway hierarchy maps, interactor/network mode behavior, table/chat/graph fallback behavior.
4. `templates/visualize_legacy.html`
   - Shell anatomy, query workflow controls, tabs, research settings, modal root, legacy script loading.
5. `services/data_builder.py`
   - Payload truth source when UI behavior and API data disagree.

The current React audit proved that isolated helper fixes are not enough:

- PERK-ESYT1 exists in API data but disappears from the current React pathway graph.
- TDP43 chain-hop rows can have `functions.length > 0` and `claims.length == 0`; current React can still render an empty modal after pathway filtering.
- KEAP1 aggregate in current React becomes a full-width text dump instead of a compact legacy-like interaction list.
- TDP43 chain semantics can blur: `TDP43 -> DDX3X` appears in the current payload with `interaction_type: direct` but `function_context: net` and chain context `TDP43 -> GLE1 -> DDX3X`. React v2 must not render that as a normal direct interaction.

React v2 must be designed around these contracts from the start.

## Architecture

### 1. Runtime Isolation

Use a new v2 route and static root:

- `routes/visualization.py`
  - Keep default legacy path unchanged.
  - Keep `?spa=1` current React unchanged.
  - Add `?spa=2` that renders `templates/visualize_v2.html`.
  - Use no-store headers during v2 development.
  - Use a distinct response header: `X-Viz-Shell: react-v2`.

- `templates/visualize_v2.html`
  - Same server-side payload injection pattern as current React: `window.__PROPATHS_BOOTSTRAP__`.
  - Distinct mount root, for example `#propaths-v2-root`.
  - Loads `/static/react-v2/app-v2.js` and `/static/react-v2/assets/app-v2.css`.
  - No dependency on the current `templates/visualize.html`.

- `react-app/src/v2/`
  - Own app root, routes, store adapters, graph renderer, modal components, and styles.
  - May import verified shared helpers from `react-app/src/app/lib` and `react-app/src/app/types`.
  - Must not import current React modal/card components.

### 2. Data Model Boundary

Create a v2 normalized view model before rendering:

- `v2/domain/snapshotAdapter.ts`
  - Converts raw `VisualizeApiPayload` into a stable `V2Snapshot`.
  - Builds indexes:
    - `interactionsByPair`
    - `directInteractions`
    - `chainRowsByChainId`
    - `chainRowsByHopKey`
    - `pathwaysById`
    - `pathwaysByName`
    - `pathwayMembershipByProtein`
    - `claimsByInteraction`
    - `functionsByInteraction`
  - Keeps raw interaction references for traceability.

- `v2/domain/legacyParity.ts`
  - Ports legacy inclusion rules as pure functions:
    - direct interaction inclusion
    - chain-hop inclusion
    - pathway context matching
    - current pathway vs other pathway claim bucketing
    - direct/indirect/shared grouping
    - pseudo filtering
    - cross-query handling
  - TDP43 chain behavior must be a first-class fixture, not special-case UI logic.

- `v2/domain/graphModel.ts`
  - Produces a graph/card model independent of D3/ReactFlow.
  - Node types: query, direct protein, pathway, chain protein instance, pseudo token, cross-query protein.
  - Edge types: direct, indirect, shared, chain hop, duplicate/cross-link, pathway membership.
  - Edge identity must carry source protein, target protein, chain id, hop index, interaction row id when available, and pathway context.

- `v2/domain/modalModel.ts`
  - Produces modal-ready models from graph selections:
    - `InteractionModalModel`
    - `AggregateModalModel`
    - `ChainContextModel`
    - `ClaimCardModel`
  - This layer decides what rows are visible, hidden under other pathways, or shown as no-citation / placeholder / thin / synthetic.

Rendering components consume only v2 view models. They should not re-decide scientific inclusion rules.

### 3. Graph/Card Rendering

Prefer porting the legacy card arrangement behavior before choosing a new visual engine.

Recommended engine choice for v2:

- Use SVG/D3 for the v2 card graph, because legacy behavior already depends on D3/SVG concepts and the current ReactFlow layer introduced edge click/layout issues.
- Use React for state, panels, modals, and orchestration.
- Use D3 only inside a constrained `GraphCanvas` adapter for zoom, pan, links, labels, and hit targets.

Why not build v2 on current ReactFlow components:

- Current graph filtering already drops valid science.
- Chain edge hit targets can be hard to click when the SVG path has a zero-height bounding box.
- Legacy already has working card graph semantics in `card_view.js`; the v2 goal is to port the product spec, not re-interpret it through the current app.

Graph requirements:

- Empty pathway selection behaves like legacy, not current React auto-selection.
- Pathway explorer selection drives graph filtering using legacy cascade rules.
- Direct, indirect, shared, chain-hop, duplicate/cross-link, and cross-query edges all have explicit model types.
- Chain rendering keeps separated vs merged modes.
- TDP43 partial-chain and unrecoverable-chain diagnostics are shown at graph and modal levels.
- Pseudo entities are filterable/labelable but must not become ordinary proteins.

### 4. Pathway Explorer v2

Port legacy `PathwayState` and `PathwayExplorer` behavior into typed React state:

- No automatic default pathway selection.
- Hierarchy tree with expand/collapse, ancestor/descendant cascade rules, search, keyboard navigation, selected-pathway sync, hidden-card state, and "show all" behavior.
- Counts must match legacy:
  - direct interactions
  - indirect/chain involvement
  - activated/inhibited/binding/regulatory dots
  - partial-chain indicators
  - drift/issue indicators
- Pathway filtering must include:
  - interaction-level pathway fields
  - claim-level pathway fields
  - function-level pathway fields
  - chain entity pathway
  - chain pathway list
  - pathway interactor membership

### 5. Modal System

React v2 modal anatomy should follow legacy, not current React:

- `InteractionModal`
  - Header: `source -> target`, direct/indirect/shared/chain badges, inferred-arrow badge where applicable.
  - Metadata and chain context.
  - Compact function rows by direction.
  - Expandable claim cards, initially collapsed or compact by default unless the legacy case expands.
  - Evidence with PMID/quote/year/no-citation warning.

- `AggregateModal`
  - Group by direct, indirect, shared, and chain interactions.
  - Compact interaction rows first.
  - Show current-pathway rows separately from rows assigned to other pathways.
  - `Show All` / `Pathway Only` toggle.
  - Query/expand/collapse actions where legacy supports them.
  - KEAP1 under TDP43 is the reference case.

- `ChainContextBanner`
  - Full chain with typed arrows.
  - Current hop highlighted.
  - Previous/next hop navigation.
  - Partial-chain / missing-hop explanation when diagnostics indicate it.
  - TDP43 chain rows must render hop-specific functions even when DB claims are absent.

### 6. State

Use a small v2 store set rather than reusing current stores wholesale:

- `v2/state/snapshotStore.ts`
  - Can be based on current `useSnapStore` if kept simple and immutable.
- `v2/state/viewStore.ts`
  - Should be rebuilt around legacy pathway/card state:
    - selected pathways
    - expanded pathways
    - hidden cards
    - graph mode
    - filter mode
    - pseudo toggle
    - cross-query toggle
    - merge/chain split toggle
    - focused pathway context
- `v2/state/modalStore.ts`
  - Can reuse the concept of a modal history stack, but payload types must be v2-specific.

The current `useViewStore` is too shallow for legacy parity and should not define v2 behavior.

### 7. Testing and Browser QA

Do not use unit tests as proof of parity. Use them to protect pure contracts.

Required v2 checks:

- Unit tests for domain adapters:
  - TDP43 chain rows with `functions` but no `claims`.
  - PERK-ESYT1 claim-level pathway inclusion.
  - KEAP1 aggregate grouping under TDP43 Antioxidant Response.
  - partial-chain diagnostics mapped to graph/modal badges.
  - pseudo filtering.
- Browser checks for:
  - legacy vs v2 screenshot comparison on desktop.
  - graph edge click opens the correct modal.
  - pathway selection changes graph and modal counts correctly.
  - no empty chain-hop cards when hop functions exist.

Desktop correctness is the priority. Mobile can be deferred unless it blocks desktop behavior.

## Feature Parity Matrix

| Area | Legacy Source | Current React Status From Audit | React v2 Target |
|---|---|---|---|
| Default route | `routes/visualization.py`, legacy shell | Legacy default confirmed | Keep legacy default unchanged |
| v2 access | N/A | Current React is `?spa=1` | Add isolated `?spa=2` |
| Query/start workflow | `templates/visualize_legacy.html`, `script.js` | Not audited | Port after card/modal P0s |
| Progress stream | `script.js`, current SSE helpers | Current React has helper code | Mine SSE/data helper only if verified |
| Card graph arrangement | `card_view.js` | Graph renders but misses PERK-ESYT1 | Port legacy graph model and layout semantics |
| Empty pathway selection | `card_view.js` | Current React auto-selects a pathway | Match legacy: no auto-select |
| Pathway Explorer | `PathwayState`, `PathwayExplorer` in `card_view.js` | Functional but behavior differs | Port cascade, counts, filters, search, keyboard |
| Direct interactions | `card_view.js`, `modal.js` | PERK-ESYT1 missing under pathway | Include interaction and claim/function pathways |
| Indirect interactions | `card_view.js`, `modal.js` | Partial coverage | Explicit indirect model and modal grouping |
| Shared interactions | `modal.js` | Partial/unclear | Preserve legacy grouping and row messaging |
| Chain-hop interactions | `card_view.js`, `modal.js` | TDP43 hop can render empty | Chain rows are first-class; functions are primary for hop cards |
| Net-effect interactions | `models.py`, `services/data_builder.py`, `modal.js` | Can look direct when raw type says direct | Net effects are separate `INDIRECT VIA` records, never direct |
| Partial chains | `cv_diagnostics.js`, `modal.js` | Top badge appears, modal handling incomplete | Graph and modal explanations for missing hops |
| Arbitrary chain length | `chain_view.py`, `shared_blocks.py`, `card_view.js` | Partly supported but fragile | N proteins produce N-1 preserved hop records |
| Cross-query handling | `card_view.js`, route/API data | Not audited | Port cross-query toggle and merge behavior |
| Pseudo entities | `card_view.js`, backend validators | Payload spot-check clean | Preserve filter/label semantics, never ordinary proteins |
| Arrows/directions | `modal.js`, `card_view.js` | Visible cases partly OK | Central direction resolver copied from legacy semantics |
| Multi-arrow rows | `modal.js` | Not fully audited | Explicit row badges and grouping |
| Single-edge modal | `modal.js` | PERK-ESYT1 blocked by graph omission | Legacy anatomy and density |
| Aggregate modal | `modal.js` | KEAP1 becomes too expanded/text-heavy | Compact grouped row anatomy first |
| Claim rendering | `modal.js` | Useful helpers exist, components too opinionated | Rebuild cards using legacy display contract |
| Evidence rendering | `modal.js` | No-citation warning exists | PMID links, quotes, year sorting, no-citation warnings |
| D/C/E quality | Current React helper | Current React has dots | Mine if verified, adapt to legacy row density |
| Desktop responsiveness | Legacy stable | Current React mixed | Desktop-first parity |
| Mobile | Legacy mixed | Current React poor | Defer unless desktop is blocked |

## Implementation Stages

### P0: Isolated v2 Shell and Contract Port

Goal: create a non-breaking v2 route and a typed domain model that can reproduce legacy decisions for the reference cases.

Tasks:

1. Add isolated v2 shell:
   - `templates/visualize_v2.html`
   - `react-app/src/v2/main.tsx`
   - isolated build output under `static/react-v2/`
   - route switch for `?spa=2`
2. Build v2 domain adapters:
   - raw payload to normalized snapshot
   - pathway indexes
   - interaction/pair/chain indexes
   - claim/function normalization
   - direct vs chain-hop vs net-effect claim locus classification
3. Port legacy inclusion rules into pure functions:
   - direct/indirect/shared/chain
   - net-effect vs direct display separation
   - pathway context
   - chain-hop function selection
   - partial-chain diagnostics
   - arbitrary chain length hop preservation
4. Establish fixture checks:
   - TDP43 chain `GRN -> SORT1`
   - TDP43 chain `TDP43 -> GLE1 -> DDX3X`
   - TDP43 KEAP1 aggregate under Antioxidant Response
   - PERK-ESYT1 under Mitochondrial Bioenergetics
   - pathway-filtered graph counts

Acceptance:

- Legacy default still loads at `/api/visualize/<protein>`.
- Current React still loads at `?spa=1`.
- v2 loads at `?spa=2`.
- v2 fixture model output matches legacy decisions before visual polish.
- v2 fixture model treats `TDP43 -> DDX3X` as a net effect via `GLE1`, not
  as a direct claim.
- v2 fixture model keeps `GLE1 -> DDX3X` as its own chain-hop claim and labels
  chain-context vs hop-local pathways separately.
- v2 fixture model preserves every adjacent hop for chains longer than three
  proteins.

### P0: TDP43-First Graph and Modal Core

Goal: make the core TDP43 cases correct before broadening.

Tasks:

1. Build desktop graph shell from v2 graph model.
2. Render TDP43 chain rows with click targets that open correct v2 modals.
3. Implement chain context banner and hop function rendering.
4. Implement aggregate modal grouping for KEAP1 under TDP43.
5. Implement pathway context toggle and other-pathway row messaging.

Acceptance:

- TDP43 chain-hop rows do not render empty when hop-specific functions exist.
- TDP43 net-effect rows do not render as direct interactions.
- Partial-chain diagnostics are visible and honest.
- KEAP1 aggregate is compact and grouped like legacy.

### P0: PERK-ESYT1 and Pathway Filtering

Goal: prove direct claim-level pathway inclusion works.

Tasks:

1. Ensure claim/function pathways affect graph inclusion.
2. Render PERK-ESYT1 under Mitochondrial Bioenergetics.
3. Open a single-edge modal that matches legacy anatomy and content order.

Acceptance:

- PERK-ESYT1 edge exists in v2 under the selected pathway.
- Modal shows the correct claim/function/evidence without being a wall of text.

### P1: Full Legacy Workflow Coverage

Tasks:

1. Query/start workflow and progress stream.
2. Header controls and research settings.
3. Table/chat/graph tabs, if still required for product parity.
4. Cross-query and merge-chain behavior.
5. Interactor Explorer behavior.
6. Query/expand/collapse actions from modals.

Acceptance:

- A user can perform the same desktop workflows in v2 as in legacy for audited proteins.

### P1: Design System and Density

Tasks:

1. Tokenize legacy visual hierarchy.
2. Build compact row/card primitives.
3. Align modal typography, section headers, badges, and evidence density.
4. Remove full-width wall-of-text default states.

Acceptance:

- v2 feels like a refined version of legacy, not current React with larger cards.

### P2: Performance, Broader Fixtures, and Mobile Later

Tasks:

1. Measure large payload load and graph render time.
2. Add browser screenshots for more proteins after TDP43/PERK are correct.
3. Add mobile layout only after desktop behavior is stable.
4. Consider code-generated API types once the backend contract stabilizes.

Acceptance:

- Desktop parity is repeatable and fast enough for normal app use.

## Current React File Disposition

### Salvage Directly If Verified

- `react-app/src/app/types/api.ts`
  - Keep as initial payload type source, but v2 should add stricter normalized domain types.
- `react-app/src/app/lib/colors.ts`
  - Salvage arrow classification after checking against legacy `arrowKind` / CSS behavior.
- `react-app/src/app/lib/normalize.ts`
  - Salvage pathway and pair normalization.
- `react-app/src/app/lib/pseudo.ts`
  - Salvage only if kept synchronized with backend pseudo whitelist.
- `react-app/src/app/api/client.ts`
  - Simple JSON wrapper is reusable.
- `react-app/src/app/api/queries.ts`
  - Mine fetch/store pattern, but v2 should not inherit current schema warnings or store writes blindly.

### Salvage As Reference, Not As Architecture

- `react-app/src/app/lib/claims.ts`
  - Useful placeholder filtering, evidence picking, endpoint mention detection.
  - Pathway context matching is too narrow for v2 until chain context and function pathway behavior are ported from legacy.
- `react-app/src/app/lib/interactionSurface.ts`
  - Keep the insight: chain-hop rows prefer `functions`.
  - Do not use it as the final v2 interaction resolver; v2 needs a broader legacy-derived selector.
- `react-app/src/app/lib/diagnostics.ts`
  - Mine badge concepts if verified against `cv_diagnostics.js`.
- `react-app/src/app/lib/pathwayStats.ts`
  - Mine D/C/E or stats ideas, but rebuild counts from legacy semantics.
- `react-app/src/app/store/useSnapStore.ts`
  - Immutable map store is sane; can inspire v2 snapshot store.
- `react-app/src/app/store/useModalStore.ts`
  - History stack concept is useful, but v2 modal payloads need typed models.
- `react-app/src/app/styles/tokens.css`
  - Mine tokens after aligning to legacy density.

### Rewrite For v2

- `react-app/src/app/views/card/buildCardGraph.ts`
- `react-app/src/app/views/card/CardView.tsx`
- `react-app/src/app/views/card/ProteinCard.tsx`
- `react-app/src/app/views/card/ChainEdge.tsx`
- `react-app/src/app/views/card/DuplicateCrossLink.tsx`
- `react-app/src/app/views/card/PathwayExplorer.tsx`
- `react-app/src/app/views/card/PathwayNavigator.tsx`
- `react-app/src/app/modal/InteractionModal.tsx`
- `react-app/src/app/modal/AggregatedModal.tsx`
- `react-app/src/app/modal/FunctionCard.tsx`
- `react-app/src/app/modal/MetadataGrid.tsx`
- `react-app/src/app/modal/ChainContextBanner.tsx`

Reason:

- These components already encode current React assumptions that conflict with legacy parity. Reusing them risks carrying over the same missing-edge, empty-hop, and text-density failures.

### Abandon For v2 Unless Re-proven

- Current ReactFlow graph architecture as the main v2 graph engine.
- First-hydration pathway auto-selection.
- Current aggregate modal default expansion behavior.
- Current graph click/hit-target assumptions.
- Any component-level pathway filtering that happens after graph/model selection instead of in a shared domain layer.

## Open Questions Before Implementation

1. Should v2 include table/chat/graph tabs in P1, or is card/pathway/modal parity enough before those are ported?
2. Should v2 use D3 SVG for graph rendering, as recommended here, or should a different graph engine be evaluated after the domain model exists?
3. Should `?spa=2` be the only v2 entrypoint, or should a named route such as `/api/visualize-v2/<protein>` also exist for easier manual testing?

## Non-Goals For First Implementation Pass

- No default route flip.
- No deletion of legacy assets.
- No current React cleanup.
- No mobile polish unless it blocks desktop correctness.
- No broad backend schema changes unless the v2 domain adapter proves the payload is wrong.

## Execution-Ready Build Spec

This section turns the architecture plan into the concrete build contract for
React v2. It is still documentation only. Product code changes begin only after
explicit implementation approval, and the first approved implementation slice is
Stage 0 only.

### 1. Exact Route And Shell Design

React v2 is served only through the existing visualization route with a new
query-string opt-in:

| URL | Shell | Status |
|---|---|---|
| `/api/visualize/<protein>` | legacy generated HTML | default, unchanged |
| `/api/visualize/<protein>?spa=1` | current React shell | experimental, unchanged |
| `/api/visualize/<protein>?spa=2` | React v2 shell | new isolated path |

The route design is intentionally narrow:

1. Keep `get_visualization(protein)` as the only visualization entrypoint for
   this work.
2. Add a new helper next to `_render_spa_shell`, named
   `_render_spa_v2_shell(protein, raw_json)`.
3. In `get_visualization(protein)`, after `result = build_full_json_from_db(protein)`
   succeeds and before the legacy generated-HTML branch, branch only on
   `request.args.get("spa") == "2"` to call `_render_spa_v2_shell(...)`.
4. Leave the existing `request.args.get("spa") == "1"` branch pointing at
   `_render_spa_shell(...)`.
5. Leave the no-query-string path untouched so it still reaches
   `create_visualization_from_dict(result)`.

The v2 shell helper has these exact responsibilities:

- Render `templates/visualize_v2.html`.
- Pass `main_protein`, `raw_json`, and `cache_bust` into the template.
- Set `Cache-Control: no-store, max-age=0`.
- Set `X-Viz-Shell: react-v2`.
- Use the same server-side payload shape as current React, but under a v2
  namespaced global:
  `window.__PROPATHS_V2_BOOTSTRAP__ = { protein, payload }`.

The v2 template contract:

- File: `templates/visualize_v2.html`.
- Mount root: `<div id="propaths-v2-root">`.
- Loading state text: compact and neutral, for example `Loading ProPaths v2...`.
- Script: `/static/react-v2/app-v2.js?v={{ cache_bust }}`.
- Stylesheet: `/static/react-v2/assets/app-v2.css?v={{ cache_bust }}`.
- No script or stylesheet from `/static/react/`.
- No script or stylesheet from `/static/_legacy/`.
- No dependency on `templates/visualize.html` or `templates/visualize_legacy.html`.

The Vite design:

- Add `react-app/vite.v2.config.ts`.
- Set `base: "/static/react-v2/"`.
- Set `build.outDir` to `../static/react-v2`.
- Set `build.emptyOutDir` to `true`; this is safe because the output directory is
  only `static/react-v2/`.
- Use one entry named `app-v2` pointing to `react-app/src/v2/main.tsx`.
- Use stable entry output names:
  - JS entry: `app-v2.js`
  - CSS entry: `assets/app-v2.css`
  - shared chunks: `chunks/[name]-[hash].js`
- Keep current `react-app/vite.config.ts` unchanged for `?spa=1`.
- Add v2 aliases only in the v2 config:
  - `@/v2` -> `src/v2`
  - `@/app-types` -> `src/app/types`
  - `@/app-lib` -> `src/app/lib`
  - `@/app-api` -> `src/app/api`

The package script design for the implementation pass:

- Add a v2 build command that calls the v2 config, for example
  `vite build --config vite.v2.config.ts`.
- Do not change the existing current React build command until v2 is proven.
- Do not let the v2 command remove or rewrite `/static/react/`.

Isolation proof required at the end of Stage 0:

- `/api/visualize/TDP43` still serves the legacy shell and has no
  `X-Viz-Shell: react-v2` header.
- `/api/visualize/TDP43?spa=1` still serves the current React shell and has
  `X-Viz-Shell: spa`.
- `/api/visualize/TDP43?spa=2` serves the new shell and has
  `X-Viz-Shell: react-v2`.

### 2. Exact Directory Structure

React v2 source lives under `react-app/src/v2/`. It does not sit inside
`react-app/src/app/`.

Target structure:

```text
react-app/
  vite.v2.config.ts
  src/
    v2/
      main.tsx
      AppV2.tsx
      bootstrap.ts
      env.ts
      domain/
        types.ts
        snapshotAdapter.ts
        normalizeKeys.ts
        pathwayContext.ts
        interactionResolver.ts
        graphModel.ts
        modalModel.ts
        chainDiagnostics.ts
        evidenceModel.ts
        displayFlags.ts
      state/
        snapshotStore.ts
        viewStore.ts
        modalStore.ts
      graph/
        GraphWorkspace.tsx
        GraphCanvas.tsx
        graphCanvasD3.ts
        graphHitTargets.ts
        GraphLegend.tsx
        CardNode.tsx
        EdgeLabel.tsx
      pathway/
        PathwayExplorerV2.tsx
        PathwayTree.tsx
        PathwaySearch.tsx
        PathwayCounts.tsx
      modal/
        ModalHost.tsx
        ModalShellV2.tsx
        InteractionModalV2.tsx
        AggregateModalV2.tsx
        ChainContextBannerV2.tsx
        MechanismCard.tsx
        EvidenceList.tsx
      components/
        Badge.tsx
        IconButton.tsx
        Toggle.tsx
        EmptyState.tsx
      styles/
        v2.css
        graph.css
        modal.css
        pathway.css
      __fixtures__/
        PERK.results.json
        TDP43.results.json
      __tests__/
        snapshotAdapter.test.ts
        pathwayContext.test.ts
        graphModel.test.ts
        modalModel.test.ts
```

New non-React files in Stage 0:

- `templates/visualize_v2.html`
- `react-app/vite.v2.config.ts`
- `static/react-v2/` generated by the v2 build command

Old React files that v2 may import directly, after verification:

- `react-app/src/app/types/api.ts`
  - Import as raw backend payload types only.
  - V2 domain types must live in `react-app/src/v2/domain/types.ts`.
- `react-app/src/app/lib/normalize.ts`
  - Import simple string/pathway/pair normalization helpers if tests confirm
    behavior matches legacy.
- `react-app/src/app/lib/colors.ts`
  - Import arrow class color helpers only after checking visible arrow direction
    against legacy.
- `react-app/src/app/lib/pseudo.ts`
  - Import pseudo detection only after checking it against backend payload rules.
- `react-app/src/app/api/client.ts`
  - Import only if v2 needs a small JSON fetch wrapper outside the hydrated route.

Old React files that v2 may read for reference but must not import:

- `react-app/src/app/views/card/**`
- `react-app/src/app/modal/**`
- `react-app/src/app/store/useViewStore.ts`
- `react-app/src/app/store/useModalStore.ts`
- `react-app/src/app/store/useSnapStore.ts`
- `react-app/src/app/routes/**`
- `react-app/src/app/App.tsx`
- `react-app/src/app/main.tsx`

Files explicitly off-limits for implementation unless the user approves a later
scope:

- `static/_legacy/**`
- `templates/visualize_legacy.html`
- `templates/visualize.html`
- `static/react/**`
- Existing current React component files under `react-app/src/app/views/card/**`
  and `react-app/src/app/modal/**`
- Backend data-building code such as `services/data_builder.py`, unless a v2
  adapter test proves the payload itself is wrong rather than misread.

### 3. Data Contract

React v2 consumes one normalized model, `V2Snapshot`, produced before any graph
or modal rendering.

Core model:

```ts
type V2Snapshot = {
  schemaVersion: string | null;
  queryProtein: ProteinId;
  raw: VisualizeApiPayload;
  proteins: Map<ProteinId, V2Protein>;
  pathways: Map<PathwayId, V2Pathway>;
  pathwaysByName: Map<CanonicalPathwayName, PathwayId>;
  interactions: V2InteractionRow[];
  interactionsById: Map<RowId, V2InteractionRow>;
  interactionsByPair: Map<PairKey, V2InteractionRow[]>;
  chainRowsByChainId: Map<ChainId, V2InteractionRow[]>;
  chainRowsByHopKey: Map<HopKey, V2InteractionRow[]>;
  diagnostics: V2Diagnostics;
};
```

`ProteinId`, `PathwayId`, `PairKey`, `HopKey`, and pathway names are canonical
display-safe strings. The raw API object stays attached for traceability, but UI
components should consume v2 model fields.

Interaction row model:

```ts
type V2InteractionRow = {
  rowId: RowId;
  rowKind:
    | "direct"
    | "indirect"
    | "net-effect"
    | "shared"
    | "chain-hop"
    | "cross-query"
    | "pathway-membership";
  source: ProteinId;
  target: ProteinId;
  pairKey: PairKey;
  arrow: ArrowClass;
  direction: V2Direction;
  depth: number;
  queryContext: ProteinId;
  isDirect: boolean;
  isIndirect: boolean;
  isNetEffect: boolean;
  isShared: boolean;
  isChainHop: boolean;
  isCrossQuery: boolean;
  isPseudoEndpoint: boolean;
  interactionPathways: V2PathwayRef[];
  chainContext: V2ChainContext | null;
  pathwayMembership: V2PathwayMembership[];
  mechanisms: V2MechanismUnit[];
  evidence: V2Evidence[];
  displayFlags: V2DisplayFlags;
  raw: Interaction;
};
```

Direct rows:

- `rowKind: "direct"`.
- `source` and `target` are the raw row endpoints.
- `isDirect: true`.
- `function_context` must resolve to `direct`.
- `chain_id` must be absent, or chain membership must be role `hop` for an
  actual adjacent chain edge where one endpoint is the query.
- `chainContext: null`.
- Included in pathway views if the row, either endpoint membership, any claim,
  or any function touches the selected pathway.
- PERK-ESYT1 is the reference case: the interaction row may not have a pathway
  array, but its claim/function pathway still makes the edge visible under
  `Mitochondrial Bioenergetics`.

Net-effect rows:

- `rowKind: "net-effect"`.
- `isNetEffect: true`.
- Represents query-to-terminal consequence through a chain.
- Never renders as an ordinary direct edge or a direct claim, even if the raw
  payload says `interaction_type: "direct"` or `type: "direct"`.
- Detection prefers `function_context: "net"`, chain membership role
  `net_effect` or `origin`, `_net_effect`, `_display_badge: "NET EFFECT"`, or
  a chain context whose endpoints are query and terminal target.
- UI label must be `NET EFFECT` or `INDIRECT VIA <chain>`.
- TDP43 `TDP43 -> DDX3X` through `GLE1` is the reference case.

Indirect rows:

- `rowKind: "indirect"` when the row is indirect but not a hop-specific
  `_is_chain_link` row.
- `depth > 1` or `interaction_type: "indirect"` marks the row indirect.
- `chainContext` is filled when the row has `mediator_chain`,
  `chain_context.full_chain`, `_chain_entity`, or `all_chains`.
- Aggregates display these rows separately from direct rows.

Shared rows:

- `rowKind: "shared"` is a v2 grouping classification, not necessarily a raw
  API type.
- A row is shared when the same partner, pair, or pathway-local interaction is
  relevant to multiple query/pathway contexts.
- The raw row remains unchanged. The grouping layer adds shared membership and
  display labels.

Chain-hop rows:

- `rowKind: "chain-hop"`.
- `isChainHop: true`.
- Requires `_is_chain_link` or a row that can be unambiguously mapped to a
  specific chain hop.
- `chainContext` must include:
  - `chainId`
  - `chainIndex`
  - `hopIndex`
  - `hopSource`
  - `hopTarget`
  - `chainProteins`
  - `chainArrows`
  - `chainPathways`
  - `discoveredInQuery`
  - `missingHopStatus`
- Mechanisms for chain-hop rows are built from `functions` first, then `claims`.
- TDP43 `GRN -> SORT1`, chain `2612`, is the reference case:
  `claims.length == 0` and `functions.length == 1` must produce a non-empty
  modal and a visible hop row.

Cross-query rows:

- `rowKind: "cross-query"` or `isCrossQuery: true`.
- Preserve `discoveredInQuery` from chain summaries or pathway cross-query
  fields when available.
- Cross-query rows can appear in aggregates and pathway views, but they must be
  labeled as cross-query rather than ordinary direct neighbors.

Partial-chain rows:

- Partial-chain state lives in `V2Diagnostics`, then gets linked into
  `V2ChainContext`.
- `missingHopStatus` is one of:
  - `"complete"`
  - `"partial-chain"`
  - `"missing-validated-claim"`
  - `"unrecoverable-pair"`
- A partial chain is not a reason to hide the visible validated hops.
- Missing hop explanations render as badges and modal text, not as empty cards.

No-claim rows:

- A row with `functions.length > 0` and `claims.length == 0` is valid.
- It becomes `displayFlags.claimState: "function-only"`.
- It can render evidence if function evidence or PMIDs exist.
- It renders an explicit no-citation/no-claim warning only when no evidence is
  attached.
- It must never be filtered out merely because claim-level pathway fields are
  missing.

Required claim-locus model:

```ts
type V2ClaimLocus =
  | {
      kind: "direct_claim";
      interactionId: RowId;
      source: ProteinId;
      target: ProteinId;
      pathway: V2PathwayRef | null;
    }
  | {
      kind: "chain_hop_claim";
      chainId: ChainId;
      hopIndex: number;
      source: ProteinId;
      target: ProteinId;
      chainContextPathway: V2PathwayRef | null;
      hopLocalPathway: V2PathwayRef | null;
    }
  | {
      kind: "net_effect_claim";
      chainId: ChainId;
      query: ProteinId;
      terminal: ProteinId;
      via: ProteinId[];
      chainContextPathway: V2PathwayRef | null;
    };
```

Terms:

- `direct_claim`: real direct pair evidence only. It can be rendered as a
  direct interaction.
- `chain_hop_claim`: belongs to one chain id, one hop index, one adjacent
  source/target pair, and one function/mechanism unit. It may have a
  hop-local pathway that differs from the whole-chain pathway.
- `net_effect_claim`: query-to-terminal consequence through a chain. It is
  never rendered as direct. It is always labeled `NET EFFECT` or
  `INDIRECT VIA <chain>`.
- `chain_context_pathway`: the pathway used to group or display the whole
  chain. This comes from `IndirectChain.pathway_name`, `all_chains[].pathway_name`,
  or the chain pathway union.
- `hop_local_pathway`: the pathway attached to the individual hop claim or
  function. This comes from the mechanism unit's own pathway.

Chain pathway divergence rule:

- A chain hop may legitimately have `hop_local_pathway != chain_context_pathway`
  when the local biochemical pair participates in one process while the full
  cascade is grouped under another.
- React v2 must label both values explicitly, for example:
  `Chain context: Stress Granule Dynamics` and
  `Hop pathway: RNA Metabolism & Translation Control`.
- React v2 must also surface a diagnostics flag when the divergence looks like
  a contract drift: missing `chain_id`, missing hop index, missing
  `function_context`, or a hop-local pathway that escaped chain assignment
  without any explicit chain context.
- The TDP43 chain `TDP43 -> GLE1 -> DDX3X` is the reference case:
  the net-effect record is `TDP43 -> DDX3X`, while the hop claim is
  `GLE1 -> DDX3X`; these are separate records and separate UI concepts.

Arbitrary chain length rule:

- Do not assume `query -> mediator -> target`.
- A chain of N proteins has N-1 adjacent hop records.
- Every hop keeps its own source, target, hop index, arrow, mechanism list,
  evidence, hop-local pathway, and chain-context pathway.
- Chains longer than three proteins must preserve every hop separately in graph
  state, modal state, pathway filtering, and acceptance tests.

Claims and functions are unified into `V2MechanismUnit`:

```ts
type V2MechanismUnit = {
  mechanismId: string;
  sourceKind: "claim" | "function";
  title: string;
  cellularProcess: string | null;
  effectDescription: string | null;
  biologicalConsequences: string[];
  specificEffects: string[];
  arrow: ArrowClass | null;
  functionContext: "direct" | "net" | "chain_derived" | "mixed" | string | null;
  pathways: V2PathwayRef[];
  evidence: V2Evidence[];
  pmids: string[];
  flags: {
    synthetic: boolean;
    thinClaim: boolean;
    syntheticFromRouter: boolean;
    shallow: boolean;
    placeholderOnly: boolean;
    noCitation: boolean;
  };
  raw: Claim;
};
```

Unification rules:

- Normalize every object in `raw.functions` as `sourceKind: "function"`.
- Normalize every object in `raw.claims` as `sourceKind: "claim"`.
- Do not deduplicate by title alone. Deduplicate only by stable row id plus
  source kind plus normalized title plus pathway plus evidence key.
- If a function and a claim describe the same mechanism, the modal may group
  them visually, but the model keeps both source records.
- The graph uses row-level presence; the modal uses mechanism-level detail.

Pathway filtering is a pure domain function:

```ts
type PathwayMatch = {
  visible: boolean;
  reasons: Array<
    | "interaction-pathway"
    | "claim-pathway"
    | "function-pathway"
    | "chain-pathway"
    | "chain-entity-pathway"
    | "pathway-interactor-membership"
    | "cross-query-membership"
    | "diagnostic-chain-context"
  >;
  currentPathwayMechanisms: V2MechanismUnit[];
  otherPathwayMechanisms: V2MechanismUnit[];
};
```

Filtering rules:

- Empty pathway selection means show the full legacy card graph. No auto-select.
- A row is visible if any selected pathway touches:
  - raw `interaction.pathways`
  - raw `interaction.chain_pathways`
  - raw `_chain_entity.pathway_name`
  - raw `all_chains[].pathway_name`
  - raw `all_chains[].chain_pathways`
  - normalized claim pathways
  - normalized function pathways
  - pathway `interactor_ids`
  - pathway `cross_query_interactor_ids`
  - pathway-local `interactions`
  - pathway-local `cross_query_interactions`
- Modal filtering is context-aware:
  - Current-pathway mechanisms are shown first.
  - Other-pathway mechanisms are kept behind an explicit section or toggle.
  - Chain-hop functions inherit chain pathway context when their own pathway
    field is absent.
  - Function-only chain rows remain visible if the row's chain context matches
    the selected pathway.

These rules prevent the audited failures:

- PERK-ESYT1 survives because claim/function pathway membership is included.
- TDP43 `GRN -> SORT1` survives because chain context and functions are valid
  visibility sources even without claims.
- TDP43 `TDP43 -> DDX3X` through `GLE1` is not mislabeled as direct because
  `function_context: "net"` and chain membership override raw
  `interaction_type: "direct"` for v2 classification.

### 3A. No-Edit Current Chain Contract Audit

This audit was performed before React v2 implementation. It defines the
non-negotiable contract v2 must consume.

Backend model contract:

- `models.Interaction` has `interaction_type` and `function_context`. The
  documented dual-track system distinguishes `direct` pair-specific evidence
  from `net` cascade-level effects.
- `models.IndirectChain` stores the ordered `chain_proteins`,
  `chain_with_arrows`, `pathway_name`, and `discovered_in_query`.
- `models.ChainParticipant` separates chain participation roles:
  `origin`, `hop`, and `net_effect`.
- `models.InteractionClaim` carries `function_context` and `chain_id`, so the
  same visible pair/function can exist as both a direct claim and a
  chain-derived claim without being the same biological record.

DB sync and chain-claim contract:

- `pipeline/prompts/shared_blocks.py` tells the model to emit one top-level
  `net` function list for the query-to-terminal story and one
  `chain_link_functions` entry per adjacent hop.
- `utils.claim_locus_router` routes hop claims that mention the wider cascade
  back to the parent indirect/net-effect row.
- `utils.chain_view` treats `full_chain` as the authoritative ordered chain
  representation and supports the query at the head, middle, or tail.
- `utils.chain_resolution` allows non-adjacent protein revisits as distinct
  chain positions and does not cap chain length.
- `utils.db_sync` creates `IndirectChain` rows, registers chain participants,
  writes adjacent hop rows, and tags parent claims with the chain id.
- `utils.db_sync` currently converts kept hop functions to downstream
  pair-specific rows, but the serialized read path can still expose mixed
  signals such as `interaction_type: direct` plus `function_context: net`.
- `utils.post_processor` audits chain-hop completeness before DB sync and tags
  missing hops rather than silently deleting chain state.
- `scripts/pathway_v2/quick_assign.py` can preserve per-hop pathway diversity
  when chain pathway unification is disabled, or enforce a dominant chain
  pathway when that behavior is enabled. React v2 cannot assume hop-local
  pathway and chain-context pathway are always the same.

Read-side reconstruction contract:

- `services.data_builder` is the payload source React v2 will consume.
- It reconstructs chain summaries, `all_chains`, `chain_ids`,
  `chain_pathways`, hop rows, function payloads, and no-claim/stub flags.
- It correctly avoids rendering parent net-effect text on a mid-chain hop when
  that text mentions the query.
- It can inject whole-chain pathway context onto hop rows while preserving the
  function's own pathway. React v2 must distinguish those labels instead of
  collapsing them into one pathway field.

Observed TDP43 contract case:

- DB stores `TDP43 -> DDX3X` as:
  - `interaction_type: "indirect"`
  - `function_context: "net"`
  - `chain_id: 2624`
  - chain proteins `TDP43 -> GLE1 -> DDX3X`
  - chain pathway `Stress Granule Dynamics`
- `/api/results/TDP43` currently emits that same DB row as:
  - `interaction_type: "direct"`
  - `type: "direct"`
  - `function_context: "net"`
  - `_net_effect: true`
- That API shape is false. React v2 must wait for the API repair instead of
  working around it in UI code.
- DB has a real adjacent hop row for `GLE1 -> DDX3X`:
  - interaction id `14656`
  - `interaction_type: "direct"`
  - `function_context: "direct"`
  - `chain_id: 2624`
  - claim `28556`
  - claim pathway `Stress Granule Dynamics`
- `/api/results/TDP43` currently synthesizes the same hop with `_db_id: null`,
  `claims.length == 0`, and a stale parent-JSONB function pathway
  `RNA Metabolism & Translation Control`.
- React v2 must consume an API payload that prefers DB-backed hop rows when
  they exist and exposes explicit `chain_context_pathway` and
  `hop_local_pathway` fields.

Legacy frontend assumptions:

- `static/_legacy/card_view.js` has useful chain reading behavior: independent
  chain instances, duplicate chain participants, chain position badges, and
  support for query-at-head, query-in-middle, and query-at-tail chains.
- `static/_legacy/modal.js` has useful modal concepts: chain banners, hop
  navigation, aggregate sections, function/evidence rows, and placeholder
  handling.
- Legacy also has presentation and semantic risks v2 must correct:
  perspective transformations can make indirect or net-effect rows look like
  direct pair rows; claim deduplication can hide `function_context` differences;
  chain-link detail sections can count claims without making hop-vs-net locus
  clear.

Current React assumptions:

- `buildCardGraph.ts` currently treats empty pathway selection as empty graph
  and only checks interaction/chain pathway fields, not claim/function pathway
  fields.
- `interactionSurface.ts` correctly prefers `functions` for `_is_chain_link`
  rows, but it still selects by pair and chain id rather than a full v2
  claim-locus model.
- `claims.ts` has useful placeholder and pathway helpers, but pathway matching
  is claim-local and cannot by itself classify direct, hop, and net-effect
  records.

Required v2 correction:

- Classification order is:
  1. explicit v2 claim locus
  2. `function_context`
  3. chain participant role
  4. `_is_chain_link` and hop index
  5. raw `interaction_type`
- Raw `interaction_type: "direct"` is not sufficient to render a direct edge.
- Raw pair equality is not sufficient to merge rows. Chain id, hop index, claim
  locus, function context, and pathway labels are part of identity.

### 4. UX Spec

Desktop is the target for v2 parity. Do not spend Stage 0 through Stage 4 time
on mobile unless desktop behavior is blocked by a responsive bug.

Graph/card layout:

- Use the legacy card graph as the reading-model target, not the visual skin.
- Query protein remains the primary anchor.
- Direct proteins are visually distinct from indirect/chain participants.
- Net-effect records are visually distinct from direct edges and must include
  an `INDIRECT VIA` or `NET EFFECT` label.
- Pathway cards group relevant proteins and interactions with compact spacing.
- Chain participants show hop badges such as `C1`, `C2`, and amber chain accents.
- Direction labels appear on chain edges where legacy shows biological arrow
  text.
- Edge hit targets must be wider than visible strokes so every visible edge is
  clickable.
- Empty selection shows the all-card graph. Selecting a pathway narrows the
  graph without changing the selected pathway automatically.
- Separated vs merged chain display is a graph state, not a data filter.
- No valid row may disappear because its evidence lives on a function instead
  of a claim.

Pathway explorer:

- No default pathway selection on first load.
- Tree supports expand/collapse, search, show all, select all, clear selection,
  hide card, and reveal card.
- Parent/child cascade follows legacy `PathwayState` behavior.
- Counts use v2 pathway matches, not component-local edge counts.
- Counts distinguish direct, chain/indirect, shared, and cross-query involvement.
- A selected pathway updates graph and modal context in one shared state update.
- Recently changed pathways may be highlighted, matching legacy behavior where
  useful.

Edge modal:

- Opens from an edge, chain hop, compact interaction row, or pathway-local edge.
- Header format: `SOURCE -> TARGET`.
- Header badges include direct/indirect/shared/chain/cross-query plus inferred
  or synthetic status when applicable.
- Net-effect modals must say `NET EFFECT` or `INDIRECT VIA <chain>` in the
  header area and must never reuse the direct-interaction visual badge.
- Body order:
  1. Chain context banner, when present.
  2. Compact metadata row.
  3. Current-pathway functions/claims.
  4. Other-pathway functions/claims behind an explicit section.
  5. Evidence and PMID details.
  6. Diagnostics and no-citation warnings.
- Function rows are compact by default.
- Claim details expand inline.
- Function-only rows render as real content, not empty placeholders.

Node aggregate modal:

- Opens from a protein card or pathway card aggregate action.
- Header format: `<PROTEIN> interactions`.
- Body groups:
  - direct interactions
  - indirect interactions
  - shared interactions
  - chain-hop interactions
  - cross-query interactions
- Default view is compact row list, not expanded prose.
- Current-pathway rows appear before other-pathway rows.
- `Show All` / `Pathway Only` toggle is required.
- KEAP1 under TDP43 `Antioxidant Response` is the reference aggregate.

Chain context display:

- Render one banner per chain when a hop participates in multiple chains.
- Banner includes chain id, chain context pathway, discovered-in query, and full
  chain.
- Hop-local pathway appears near the mechanism row when it differs from the
  chain context pathway.
- Current hop is highlighted.
- Prev/next hop controls navigate within the same chain id.
- Protein chips in the banner re-scope the modal to the matching hop when a
  matching hop row exists.
- Partial-chain and unrecoverable-pair diagnostics render in the banner and
  aggregate rows.
- TDP43 `GRN -> SORT1 -> CTSD -> TDP43` behavior is a core reference case.
- TDP43 `TDP43 -> GLE1 -> DDX3X` behavior is a core chain-semantics reference:
  the terminal net effect and the `GLE1 -> DDX3X` hop are separate records.

Evidence and PMID display:

- PMIDs render as PubMed links with `target="_blank"` and safe rel attributes.
- Evidence cards show title when available, year when available, quote when
  available, and PMID badges.
- Row-level `pmids` are shown even when evidence objects are absent.
- No-citation warnings are compact, visible, and do not replace real mechanism
  text.
- Evidence order prefers current-pathway evidence, then row-level evidence,
  then other-pathway evidence.

Shallow, thin, router, and synthetic states:

- `_synthetic` renders as a subdued synthetic pathway-only stub.
- `_thin_claim` renders as a thin-claim stub with the available mechanism text.
- `_synthetic_from_router` renders as a router-summary stub using
  `_router_outcome_summary` when present.
- Shallow/depth issue states render small badges with expandable detail.
- Placeholder-only strings such as generic chain-resolution notes are filtered
  from bullet lists, but the row itself remains visible if it is scientifically
  relevant.

Keyboard behavior:

- Escape closes the active modal.
- Tab stays trapped inside the modal while it is open.
- Closing a modal restores focus to the element that opened it.
- Enter and Space activate graph edges, card buttons, pathway rows, toggles,
  and modal controls.
- Arrow Up/Down moves through visible pathway explorer rows.
- Arrow Left collapses an expanded pathway branch or moves to the parent.
- Arrow Right expands a collapsed pathway branch or moves to the first child.
- Home/End jump to the first/last visible pathway row.
- `/` focuses pathway search when no text input is active.

### 5. Implementation Stages

Non-negotiable P0 blocker:

- React v2 cannot move beyond Stage 1 until the API preserves DB-backed direct,
  chain-hop, and net-effect loci, and the adapter proves those loci remain
  separated.
- The frontend adapter is not allowed to treat a known-false API payload as the
  stable contract. Backend/API repair must come first.
- Specifically, `TDP43 -> DDX3X` through `GLE1` must classify as a
  `net_effect_claim`, and `GLE1 -> DDX3X` must classify as a separate
  `chain_hop_claim`.
- A raw row with `interaction_type: "direct"` and `function_context: "net"`
  is never considered a direct claim.
- Hop-local pathway and chain-context pathway divergence must be modeled and
  displayed explicitly rather than silently unified or hidden.
- Arbitrary chain length support is mandatory in the adapter before graph work
  begins.

#### Stage 0: Route And Shell Only

Goal: create the isolated v2 entrypoint with no product behavior beyond loading
the shell.

Allowed code scope for this stage only:

- `routes/visualization.py`
- `templates/visualize_v2.html`
- `react-app/vite.v2.config.ts`
- `react-app/package.json`
- `react-app/src/v2/main.tsx`
- `react-app/src/v2/AppV2.tsx`
- `react-app/src/v2/bootstrap.ts`
- `react-app/src/v2/styles/v2.css`
- generated `static/react-v2/**`

Required behavior:

- `?spa=2` renders a minimal v2 shell with query protein and payload metadata.
- No graph implementation.
- No modal implementation.
- No pathway explorer implementation.
- No changes to the default legacy shell.
- No changes to current `?spa=1`.

Acceptance checks:

- `curl -s -D - http://127.0.0.1:5003/api/visualize/TDP43 -o /tmp/legacy.html`
  shows the legacy shell and not the v2 header.
- `curl -s -D - http://127.0.0.1:5003/api/visualize/TDP43?spa=1 -o /tmp/spa1.html`
  still shows `X-Viz-Shell: spa`.
- `curl -s -D - http://127.0.0.1:5003/api/visualize/TDP43?spa=2 -o /tmp/spa2.html`
  shows `X-Viz-Shell: react-v2`.
- Browser screenshot of `?spa=2` shows the v2 loading/app shell, the protein
  name `TDP43`, and basic payload counts.

#### Stage 1: Data Adapter And Fixtures

Goal: prove the normalized model before any serious UI work.

Allowed code scope:

- `react-app/src/v2/domain/**`
- `react-app/src/v2/__fixtures__/**`
- `react-app/src/v2/__tests__/**`
- minimal `AppV2.tsx` display of adapter diagnostics

Required behavior:

- Save deterministic local fixtures for `PERK` and `TDP43` from `/api/results`.
- Build `V2Snapshot`.
- Build pathway indexes, pair indexes, chain indexes, mechanism units, evidence
  units, and diagnostics mapping.
- Add pure acceptance tests for the reference cases.

Acceptance checks:

- PERK fixture contains a visible `PERK -> ESYT1` direct row under
  `Mitochondrial Bioenergetics`.
- TDP43 fixture contains a visible `GRN -> SORT1` chain-hop row for chain
  `2612` with one function and zero claims.
- TDP43 fixture classifies `TDP43 -> DDX3X` through `GLE1` as
  `net_effect_claim`, not `direct_claim`, despite raw `interaction_type:
  "direct"`.
- TDP43 fixture classifies `GLE1 -> DDX3X` as `chain_hop_claim` with
  `chain_context_pathway: Stress Granule Dynamics` and `hop_local_pathway:
  RNA Metabolism & Translation Control`.
- Fixture tests include at least one chain longer than three proteins and prove
  every adjacent hop is preserved as a separate v2 hop record.
- TDP43 fixture maps KEAP1 aggregate rows under `Antioxidant Response`.
- Pathway filtering with an empty selection returns the full legacy-equivalent
  graph model input.
- Default legacy URL and `?spa=1` are checked again and remain unchanged.

#### Stage 2: Graph Parity

Goal: render the desktop card graph from v2 model data.

Allowed code scope:

- `react-app/src/v2/graph/**`
- graph-related v2 styles
- graph model tests as needed

Required behavior:

- Render query, direct, indirect, shared, chain, and cross-query card nodes.
- Render direction-aware edges.
- Render chain hop badges and partial-chain badges.
- Clickable edge and card hit targets open a temporary debug panel or modal
  stub with the selected row id.
- Pathway filter input may be a simple developer control until Stage 4.

Acceptance checks:

- `PERK?spa=2` with `Mitochondrial Bioenergetics` selected shows
  `PERK -> ESYT1`.
- `TDP43?spa=2` with `Lysosomal Transport` selected shows `GRN -> SORT1`.
- `TDP43?spa=2` shows `TDP43 -> DDX3X` as an indirect/net-effect relation via
  `GLE1`, not as a normal direct edge.
- `TDP43?spa=2` keeps the adjacent `GLE1 -> DDX3X` chain hop separate from the
  terminal net effect.
- `TDP43?spa=2` with `Antioxidant Response` selected shows KEAP1 as an
  aggregate-capable node.
- Empty pathway selection shows the broad card graph, not an empty graph.
- Default legacy URL and `?spa=1` are checked again and remain unchanged.

#### Stage 3: Modal Parity

Goal: replace the debug panel with legacy-like edge and aggregate modals.

Allowed code scope:

- `react-app/src/v2/modal/**`
- `react-app/src/v2/domain/modalModel.ts`
- modal-related v2 styles

Required behavior:

- Implement `InteractionModalV2`.
- Implement `AggregateModalV2`.
- Implement `ChainContextBannerV2`.
- Implement mechanism cards and evidence display.
- Implement modal keyboard behavior.

Acceptance checks:

- PERK-ESYT1 edge modal shows the function/claim for
  `ER-Mitochondria Lipid Trafficking & Mitochondrial Bioenergetics`.
- TDP43 `GRN -> SORT1` modal shows chain `2612`, function
  `C-terminal Binding & Endocytic Sorting`, and no empty-claims failure.
- TDP43 `TDP43 -> DDX3X` modal is labeled `NET EFFECT` / `INDIRECT VIA GLE1`
  and does not display as a direct interaction.
- TDP43 `GLE1 -> DDX3X` hop modal displays the chain context pathway and
  hop-local pathway as separate labels when they differ.
- TDP43 KEAP1 aggregate under `Antioxidant Response` opens as compact grouped
  rows with `Show All` / `Pathway Only`.
- Evidence PMIDs render as links where present.
- Escape closes modal, Tab is trapped, focus restores on close.
- Default legacy URL and `?spa=1` are checked again and remain unchanged.

#### Stage 4: Pathway Explorer Parity

Goal: replace temporary pathway controls with the legacy-like explorer.

Allowed code scope:

- `react-app/src/v2/pathway/**`
- `react-app/src/v2/state/viewStore.ts`
- pathway-related v2 styles

Required behavior:

- No auto-selected pathway on first load.
- Search, expand/collapse, select/clear, hide/reveal, show all.
- Cascade behavior follows legacy `PathwayState`.
- Counts use the same v2 pathway match function as graph and modal.
- Keyboard navigation works.

Acceptance checks:

- Selecting `Mitochondrial Bioenergetics` in PERK keeps `PERK -> ESYT1`.
- Selecting `Lysosomal Transport` in TDP43 keeps `GRN -> SORT1`.
- Selecting `Stress Granule Dynamics` in TDP43 includes the
  `TDP43 -> GLE1 -> DDX3X` chain context without flattening it into a direct
  `TDP43 -> DDX3X` edge.
- Selecting the hop-local pathway for `GLE1 -> DDX3X`, if present in the
  explorer, keeps the hop visible with an explicit cross-pathway label rather
  than hiding it.
- Selecting `Antioxidant Response` in TDP43 keeps KEAP1 aggregate available.
- Clearing selection returns to the broad graph.
- Current-pathway and other-pathway modal sections update correctly.
- Default legacy URL and `?spa=1` are checked again and remain unchanged.

#### Stage 5: Polish And Performance

Goal: make v2 usable for repeated desktop comparison.

Allowed code scope:

- v2 styles and components
- v2 performance helpers
- v2 browser QA scripts or docs artifacts

Required behavior:

- Reduce layout shifts.
- Keep modal rows compact.
- Improve graph pan/zoom smoothness.
- Add loading and error states.
- Add screenshot comparison artifacts for the reference cases.

Acceptance checks:

- TDP43 v2 renders without long blank periods on a normal local run.
- Graph pan/zoom remains responsive at 1440x1000.
- Modal opening does not re-layout the graph.
- Reference screenshots are saved for legacy, `?spa=1`, and `?spa=2`.
- Desktop pass/fail criteria below are satisfied.

### 6. Acceptance Tests By Reference Case

PERK `<->` ESYT1:

- URL: `http://127.0.0.1:5003/api/visualize/PERK?spa=2`.
- Select pathway: `Mitochondrial Bioenergetics`.
- Expected graph: ESYT1 visible and connected to PERK.
- Expected modal: single-edge modal opens from the edge and shows the
  ER-mitochondria lipid trafficking / mitochondrial bioenergetics mechanism.
- Failure: ESYT1 missing, edge missing, modal empty, or mechanism hidden under
  unrelated pathway-only filtering.

TDP43 `GRN -> SORT1`:

- URL: `http://127.0.0.1:5003/api/visualize/TDP43?spa=2`.
- Select pathway: `Lysosomal Transport`.
- Expected graph: `GRN -> SORT1` chain hop visible.
- Expected modal: chain `2612`, full chain context including
  `GRN -> SORT1 -> CTSD -> TDP43`, one function, zero claims allowed.
- Failure: hop missing, modal says no claims to render, function absent, or chain
  context points to the wrong hop.

TDP43 `TDP43 -> GLE1 -> DDX3X`:

- URL: `http://127.0.0.1:5003/api/visualize/TDP43?spa=2`.
- Select pathway: `Stress Granule Dynamics`.
- Expected graph: the chain is visible as `TDP43 -> GLE1 -> DDX3X`; the
  terminal `TDP43 -> DDX3X` record is labeled as net effect or indirect via
  `GLE1`, not as a normal direct edge.
- Expected hop modal: `GLE1 -> DDX3X` is a separate chain-hop modal with
  chain context pathway `Stress Granule Dynamics` and hop-local pathway
  `RNA Metabolism & Translation Control`.
- Expected net-effect modal: `TDP43 -> DDX3X` says `NET EFFECT` or
  `INDIRECT VIA GLE1` and keeps the cascade prose out of direct-interaction
  sections.
- Failure: v2 renders `TDP43 -> DDX3X` as direct, merges the hop and net effect
  into one row, hides the hop because its local pathway differs, or assumes the
  chain has exactly one mediator.

TDP43 KEAP1 aggregate:

- URL: `http://127.0.0.1:5003/api/visualize/TDP43?spa=2`.
- Select pathway: `Antioxidant Response`.
- Expected graph: KEAP1 visible as an aggregate-capable protein.
- Expected modal: compact grouped rows, current-pathway rows first, Show All
  toggle available, not a full-width prose dump.
- Failure: KEAP1 missing, aggregate unavailable, row grouping lost, or first
  row expands into a wall of text by default.

Pathway-filtered graph behavior:

- Empty selection shows the broad graph.
- Selecting one pathway narrows graph using v2 pathway matching.
- Clearing selection restores the broad graph.
- No valid row is dropped just because its pathway appears on a claim/function
  rather than on `interaction.pathways`.
- Chain-hop functions inherit selected chain context when claim pathways are
  absent.

Legacy and current React isolation:

- Default URL remains visually and behaviorally legacy.
- `?spa=1` remains visually and behaviorally current React.
- `?spa=2` is the only route that loads v2 assets.
- `static/_legacy/**` remains present and unused by v2.
- `static/react/**` remains the current React output only.

### 7. Browser QA Checklist

Use these exact URLs for desktop QA:

- `http://127.0.0.1:5003/api/visualize/TDP43`
- `http://127.0.0.1:5003/api/visualize/TDP43?spa=1`
- `http://127.0.0.1:5003/api/visualize/TDP43?spa=2`
- `http://127.0.0.1:5003/api/visualize/PERK`
- `http://127.0.0.1:5003/api/visualize/PERK?spa=1`
- `http://127.0.0.1:5003/api/visualize/PERK?spa=2`

Primary viewport sizes:

- `1440x1000`: primary desktop acceptance viewport.
- `1280x900`: smaller desktop stress viewport.
- `1600x1000`: wider desktop density check.

Optional viewport only if desktop work exposes responsive breakage:

- `390x844`: mobile smoke check, not a parity target yet.

Save screenshots under a temporary local QA folder, for example:

```text
/tmp/propaths-react-v2-qa/
  stage-0/
    tdp43-legacy-initial-1440x1000.png
    tdp43-spa1-initial-1440x1000.png
    tdp43-spa2-shell-1440x1000.png
  stage-2/
    perk-spa2-mito-bioenergetics-esyt1-1440x1000.png
    tdp43-spa2-lysosomal-grn-sort1-1440x1000.png
    tdp43-spa2-stress-granule-gle1-ddx3x-1440x1000.png
    tdp43-spa2-antioxidant-keap1-1440x1000.png
  stage-3/
    perk-spa2-esyt1-modal-1440x1000.png
    tdp43-spa2-grn-sort1-modal-1440x1000.png
    tdp43-spa2-ddx3x-net-effect-modal-1440x1000.png
    tdp43-spa2-gle1-ddx3x-hop-modal-1440x1000.png
    tdp43-spa2-keap1-aggregate-1440x1000.png
  stage-4/
    perk-spa2-pathway-explorer-1440x1000.png
    tdp43-spa2-pathway-explorer-1440x1000.png
  stage-5/
    tdp43-spa2-final-1280x900.png
    tdp43-spa2-final-1600x1000.png
```

Visible pass/fail criteria:

- Legacy default still has the legacy card/modal UI.
- Current `?spa=1` still has the current React UI.
- V2 shell has v2 identity and loads only `/static/react-v2/` assets.
- No blank white or dark empty canvas after data load.
- TDP43 partial-chain diagnostics are visible and honest.
- `GRN -> SORT1` is visible under the relevant TDP43 pathway and opens a
  non-empty modal.
- `TDP43 -> DDX3X` is labeled as a net effect or indirect via `GLE1`, never as
  a normal direct interaction.
- `GLE1 -> DDX3X` is visible as its own chain hop, with separate chain-context
  and hop-local pathway labels when they differ.
- Chains longer than three proteins render every adjacent hop separately.
- KEAP1 aggregate under TDP43 is compact and grouped.
- PERK-ESYT1 is visible under `Mitochondrial Bioenergetics`.
- Edge arrows and labels are directionally readable.
- Modals do not cover essential context without a clear close path.
- Text fits inside cards, badges, buttons, and modal rows at all three desktop
  viewport sizes.
- No row that has valid functions/evidence is hidden only because it lacks
  claim-level rows.

Implementation approval boundary:

- The next implementation approval, when given, applies to Stage 0 only.
- Stage 0 must stop after the isolated v2 route/shell is working and verified.
- Stage 1 and later require separate approval after Stage 0 evidence is saved.
