# Frontend Deep Dive

The frontend is mid-migration from a 19,000-line vanilla-JS-and-D3 stack to a typed React SPA. Two paths are live in parallel:

| Path | URL | Status |
|---|---|---|
| Legacy vanilla JS + D3 | `/api/visualize/<protein>` (default) | Frozen — no new features. Will be deleted at cutover. |
| React SPA (Phase 1–3 + polish) | `/api/visualize/<protein>?spa=1` and `/workspace/<list>` | Active development. Used for daily investigation. |

Read `11_CHAIN_TOPOLOGY.md` together with this doc — the chain DAG architecture is the load-bearing visual decision.

---

## React SPA — current architecture

### Stack

- **Vite 5** + **React 18** + **TypeScript strict** in `react-app/`.
- **`@xyflow/react` v12** as the graph canvas (custom nodes + edges).
- **`elkjs` v0.9** layered DAG layout, lazy-imported (1.4 MB chunk loads on first canvas render).
- **Zustand v5** for state. Three stores: `useSnapStore`, `useViewStore`, `useModalStore`.
- **TanStack Query v5** for `/api/results/<protein>` fetches.
- **React Router v6** with `BrowserRouter`. Routes: `/`, `/visualize/:protein`, `/api/visualize/:protein`, `/workspace/:proteinList`.

### File layout

```
react-app/src/
├── app/
│   ├── main.tsx                           ← entry; hydrates from window.__PROPATHS_BOOTSTRAP__
│   ├── App.tsx                             ← router + ErrorBoundary
│   ├── EXPECTED_SCHEMA_VERSION             ← exported, mirrors services/data_builder.SCHEMA_VERSION
│   ├── routes/
│   │   ├── Visualize.tsx                  ← single-protein view (header → diagnostics → chips → breadcrumb → sidebar+canvas)
│   │   ├── Workspace.tsx                   ← multi-protein skeleton (route reserved, UI is Phase 5)
│   │   ├── Index.tsx                       ← search box landing
│   │   └── ErrorBoundary.tsx
│   ├── views/card/
│   │   ├── CardView.tsx                   ← orchestrates buildCardGraph → elkjs → ReactFlow
│   │   ├── ProteinCard.tsx                ← custom node; variants query/direct/chain/pathway-header
│   │   ├── ChainEdge.tsx                  ← custom edge; verb label + arrow color
│   │   ├── DuplicateCrossLink.tsx         ← faint dashed Bezier between same-protein instances
│   │   ├── buildCardGraph.ts              ← snap → ReactFlow nodes/edges (always-full-chains + cross-links)
│   │   ├── layoutEngine.ts                ← lazy-imported elkjs wrapper
│   │   ├── PathwayExplorer.tsx            ← left sidebar (stat-rich rows, smart sort, member search)
│   │   ├── PathwayBreadcrumb.tsx          ← selected + ancestor chips above canvas
│   │   ├── FilterChips.tsx                ← top-bar mode + pseudo + cross-query toggles
│   │   ├── DiagnosticsBanner.tsx          ← per-snap pass-rate / drift / partial counts
│   │   ├── EmptyState.tsx                 ← shown when 0 pathways selected; suggests top pathway
│   │   └── Legend.tsx                     ← collapsible verb-color reference
│   ├── modal/
│   │   ├── ModalShell.tsx                 ← focus trap + backdrop + escape + ←/→ keyboard nav
│   │   ├── ClaimRenderer.tsx              ← single claim with D/C/E rubric; placeholders filtered
│   │   ├── InteractionModal.tsx           ← opened from edge click; one ChainContextBanner per chain
│   │   ├── AggregatedModal.tsx            ← opened from node click; lists every interaction for protein
│   │   └── ChainContextBanner.tsx         ← chain chips drill into AggregatedModal on click
│   ├── lib/
│   │   ├── colors.ts                      ← arrow class → ARROW_COLORS + classifyArrow + isReverseVerb
│   │   ├── pseudo.ts                      ← PSEUDO_NAMES + isPseudoProtein (mirrors utils/db_sync._PSEUDO_WHITELIST)
│   │   ├── normalize.ts                   ← canonicalPairKey, normalizePathwayName, pathwayNameMatches
│   │   ├── claims.ts                      ← PLACEHOLDER_SNIPPETS, isPathwayInContext, classifyClaim, pickEvidence
│   │   ├── diagnostics.ts                 ← deriveBadges(snap) → byProtein + byHop maps for per-node badges
│   │   └── pathwayStats.ts                ← derivePathwayStats(snap) + sortPathwayStats; powers PathwayExplorer
│   ├── store/
│   │   ├── useSnapStore.ts                ← Map<ProteinKey, SnapshotEntry> + activeProtein; frozen-after-set
│   │   ├── useViewStore.ts                ← per-protein view state + previewPathway + hoveredBaseProtein
│   │   └── useModalStore.ts               ← {open, args, history} + push/close/pop
│   ├── api/
│   │   ├── client.ts                      ← getJSON wrapper + ApiError
│   │   ├── queries.ts                     ← useVisualizeQuery + useVisualizeQueries; writes inside queryFn
│   │   └── sse.ts                         ← re-export of shared/useSSE
│   └── types/
│       ├── api.ts                         ← Snapshot, Interaction, Pathway, ChainSummary, Claim, Diagnostics
│       └── workspace.ts                   ← ProteinKey brand, SnapshotEntry, parseProteinList
├── shared/
│   └── useSSE.ts                          ← thin EventSource hook (shared with legacy pipeline-events island)
├── islands/                                ← legacy mount points (cardview-badges, pipeline-events) — phased out at cutover
└── ...
```

### Data flow

```
Flask /api/visualize/<protein>?spa=1
        ↓ (renders templates/visualize_spa.html)
        window.__PROPATHS_BOOTSTRAP__ = { protein, payload }
        ↓
main.tsx hydrateFromBootstrap → checkSchemaVersion → useSnapStore.setEntry
        ↓
React Router → Visualize.tsx
        ↓
PathwayExplorer auto-selects highest-relevance non-catch-all pathway
        ↓
useViewStore.byProtein.<key>.selectedPathways changes
        ↓
CardView.useEffect rebuilds graph + runs elkjs (debounced 80ms)
        ↓
ReactFlow renders nodes + edges with custom types
        ↓
User click → useModalStore.push → ModalShell renders InteractionModal/AggregatedModal
```

For workspace mode (`/workspace/A,B?spa=1`):

```
Server renders SPA shell with NO bootstrap (workspace shell skips the inject).
        ↓
useVisualizeQueries(['A', 'B']) fires N parallel /api/results fetches.
        ↓
On each success, queryFn writes into useSnapStore (Map gains entries).
        ↓
Workspace.tsx renders skeleton + status (UI is Phase 5).
```

### Frozen-snap discipline

- `useSnapStore.setEntry(protein, snap, ctx, …)` calls `Object.freeze(snap)` and `Object.freeze(ctx)`. Mutating these throws in strict mode.
- The legacy `Object.freeze(SNAP)` in `templates/visualize.html` enforces the same invariant on the vanilla side.
- Every selector returns either a primitive, a stable Map ref, or a `useMemo`'d derivation. **Never** do `Array.from(...)` inside a Zustand selector — `useSyncExternalStore` will see a fresh reference every render and infinite-loop.

### Frontend ↔ Backend contract

The SPA reads `_schema_version` from each payload. `EXPECTED_SCHEMA_VERSION` in `main.tsx` mirrors `services/data_builder.SCHEMA_VERSION` (currently `"2026-05-04"`). Mismatch → console.warn (warn-once per drift value); render proceeds best-effort.

`SNAP.interactions[*]` shape (verified, hand-typed in `react-app/src/app/types/api.ts`):

```ts
{
  source: string;
  target: string;
  arrow: string;                              // legacy
  arrows?: { a_to_b?: string[]; b_to_a?: string[] };
  direction: 'main_to_primary' | 'primary_to_main' | 'a_to_b' | 'b_to_a';
  type: 'direct' | 'indirect';
  interaction_type: 'direct' | 'indirect';   // duplicate of `type`
  depth: number;
  functions?: Claim[];                        // alternate field
  claims?: Claim[];                           // preferred when present
  pathways?: string[];
  pmids?: string[];

  // Chain fields (only for chain rows)
  _is_chain_link?: boolean;
  _chain_position?: number;
  _chain_length?: number;
  _chain_entity?: ChainSummary;               // fallback when all_chains absent
  chain_id?: number | null;
  chain_ids?: number[];
  all_chains?: ChainSummary[];                // multi-chain (M2M); preferred reader
  chain_pathways?: string[];                  // distinct pathways across this row's chains (Phase A.1)
  mediator_chain?: string[];                  // legacy
  upstream_interactor?: string;
  chain_context?: { full_chain?: string[]; role?: string };

  _source_is_pseudo?: boolean;
  _target_is_pseudo?: boolean;
  _partner_is_pseudo?: boolean;
}
```

`SNAP.pathways[*]` shape:

```ts
{
  id: 'pathway_<safe_name>';
  name: string;
  description?: string | null;
  hierarchy_level?: number;
  is_leaf?: boolean;
  parent_ids?: string[];
  child_ids?: string[];
  ancestor_ids?: string[];
  interactor_ids?: string[];                  // protein symbols, upper-case
  cross_query_interactor_ids?: string[];
  interactions?: Interaction[];
  cross_query_interactions?: Interaction[];
  ontology_id?: string | null;
  ontology_source?: string | null;
}
```

(Note: `protein_count` was removed from this shape on 2026-05-04. The DB column still exists — just no longer emitted to the API. Re-add at `services/data_builder.py:_chain_fields_for` if a UI ever needs it.)

### Chain DAG rendering — the load-bearing decision

`buildCardGraph` always renders chains as **complete sequences**. Each chain participant gets a node with `_uid = chain::<chainId>::<position>::<protein>`, regardless of whether the protein already appears as a direct interactor.

When the same protein has both roles (canonical case: HDAC6 is a direct interactor of ATXN3 AND a chain participant in ATXN3 → HDAC6 → SQSTM1), it renders TWICE — once as a direct child of the query, once as a chain hop. After elkjs lays out the nodes, a post-pass walks every node and groups by `data.baseProtein`. Any base protein with N>1 instances generates `DuplicateCrossLink` edges between each pair (capped at 5 cross-links per protein for readability).

Hovering any instance (`hoveredBaseProtein` in `useViewStore`) brightens all matching instances + their cross-links. This is how the user sees that the two HDAC6 cards are the SAME protein.

`ChainEdge` reads the verb from `data.arrow` and renders the verb label at the edge midpoint, color-coded by `lib/colors.ARROW_COLORS`. Reverse verbs (`is_*_by`) render italic so direction is visible even when spatial layout has the source below the target.

### PathwayExplorer V2 — the investigative panel

Replaces the old flat checkbox list. Each row is computed by `derivePathwayStats(snap)` (one walk over `snap.interactions`) and shows:

- **Interactor count** + **mini-bar** (direct=indigo / chain=cyan, segment widths proportional)
- **Letter grade** A+/A/B/C from per-claim D/C/E pass-rate (≥6 sentences, ≥3 cascades, ≥3 PMIDs)
- **Three dots** for drift (corrected=green, report-only=amber), partial chain (amber), pseudo-touching (slate)

Six smart sorts: relevance / alphabetical / hierarchy / most drift / lowest pass / most chains. Composable filter chips: `Has interactors` (default ON), `Has chains`, `Has drift`, `Has issues`, `Mine only`. Two-mode search: by pathway name, or by member protein (e.g. typing `HDAC6` shows only pathways HDAC6 belongs to). Auto-selects the highest-relevance non-catch-all pathway on first hydration so the canvas is never empty on entry.

### Modal — claim rendering

Three special types render as honest placeholder cards (no fake biology):
- `_synthetic` → "No pipeline-generated mechanism for this interaction yet."
- `_thin_claim` → "Pair biology not characterized in cascade context."
- `_synthetic_from_router` → "Router placeholder — Awaiting curation."

Normal claims show: effect description (color-coded by arrow), mechanism (cellular_process), biological cascade (numbered ol from `biological_consequences`), specific effects (ul), evidence (PMID links to pubmed.ncbi.nlm.nih.gov, sorted year desc), pathway badge.

The claim header carries the **D/C/E rubric** — three colored dots:
- **D**epth: count of `effect_description` sentences (≥6 green, 3-5 amber, <3 red)
- **C**ascade: count of `biological_consequences` (≥3 / 1-2 / 0)
- **E**vidence: count of unique PMIDs (≥3 / 1-2 / 0)

Tooltips show actual counts. This encodes the user's PhD-depth standard right next to each claim.

`ChainContextBanner` chips drill: clicking a protein chip in a chain banner closes the current modal and pushes `AggregatedModal` for that protein, scoped to the same chain. Clicking the query-protein chip is a no-op (drilling into yourself isn't useful).

`ModalShell` wires keyboard nav: ←/→ (or j/k) cycle through claims with auto-expand + scroll-into-view. Doesn't hijack typing in inputs.

---

## Legacy vanilla JS + D3 (frozen)

These files still serve at no-`?spa` URLs and will be deleted at cutover. Do not add features here.

| File | LOC | Status |
|---|---|---|
| `static/card_view.js` | 5,541 | Replaced by `react-app/src/app/views/card/` |
| `static/modal.js` | 2,812 | Replaced by `react-app/src/app/modal/` |
| `static/visualizer.js` | 10,702 | Out of scope (Graph view skipped per user) |
| `static/cv_diagnostics.js` | 430 | Replaced by `react-app/src/app/views/card/DiagnosticsBanner.tsx` + `lib/diagnostics.ts` |
| `static/script.js` | (page glue) | Pipeline-events drawer integration moves into SPA at Tier C of the cutover plan |
| `static/shared_utils.js`, `force_config.js`, `network_topology.js` | (helpers) | Deleted at cutover |
| `static/neural_particles.js` | (decorative) | Kept if user wants the animation |

`templates/visualize.html` (legacy shell, ~626 lines) renders the legacy frontend. `templates/visualize_spa.html` is the thin SPA shell that sets `window.__PROPATHS_BOOTSTRAP__` and loads `static/react/app.js`. Cutover renames the SPA shell to `visualize.html` and deletes the old.

`react-app/src/islands/cardview-badges/` and `pipeline-events/` are React **islands** mounted into legacy DOM — pre-SPA stepping stones. The cardview-badges island is gated behind `window.__USE_REACT_BADGES__` (default off). The pipeline-events island looks for `[id^='pipeline-events-']` DOM elements that `static/script.js` creates when a new query kicks off.

**Pipeline-events gap (cutover blocker):** the SPA does NOT create those DOM elements. So new-query kickoff inside the SPA has no live pipeline drawer today. Tier C of the cutover plan integrates `useSSE` into a dedicated SPA component before deleting `static/script.js`.

---

## Cutover plan — short version

See conversation history for the full Tier A/B/C/D plan. Short version:

- **A (safe additive, shipped 2026-05-04):** stop emitting `Pathway.protein_count`, emit `_schema_version`, hoist chain-pathways N+1 query, run `repair_denormalized_counters.py`, doc sync (this file).
- **C (cutover-blocker):** integrate pipeline-events SSE into SPA. Light a11y pass.
- **B (confidence):** vitest unit tests for `lib/`, component tests for ProteinCard / ChainEdge / ClaimRenderer / PathwayExplorer.
- **D (cutover):** flip `?spa=1` to default in `routes/visualization.py`. Watch one week. Delete legacy `static/*.js`. Rename `visualize_spa.html` → `visualize.html`. Drop the query-param routing.

Until D ships: SPA stays opt-in via `?spa=1`. The default URL serves the legacy frontend.
