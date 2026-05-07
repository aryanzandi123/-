# Frontend Migration Roadmap

## Status (2026-05-04 — cutover-flip complete)

The SPA at `react-app/src/app/` is now the **default frontend**. The legacy vanilla-JS / D3 stack is archived under `static/_legacy/` and reachable only via `?spa=0` as an emergency escape hatch. The plan is to delete `static/_legacy/` after a few weeks of daily SPA use.

```
URL                                 Renders
/api/visualize/<protein>            templates/visualize.html (SPA shell)
/api/visualize/<protein>?spa=0      templates/visualize_legacy.html (archived legacy)
/workspace/<list>                   templates/visualize.html (SPA, no hydration)
```

## SPA architecture (one-screen)

```
react-app/src/app/
├── main.tsx                       ← entry; bootstrap hydration; checkSchemaVersion()
├── App.tsx                         ← BrowserRouter + ErrorBoundary
├── styles/tokens.css               ← design tokens (single source of truth for color/space/radius)
├── routes/
│   ├── Visualize.tsx               ← single-protein view
│   ├── Workspace.tsx               ← multi-protein skeleton
│   └── Index.tsx                   ← search box
├── views/card/                     ← CardView + supporting components
│   ├── CardView.tsx
│   ├── ProteinCard.tsx + ChainEdge.tsx + DuplicateCrossLink.tsx
│   ├── PathwayExplorer.tsx + PathwayBreadcrumb.tsx + FilterChips.tsx + DiagnosticsBanner.tsx
│   ├── PipelineEventsDrawer.tsx    ← SPA-native SSE
│   ├── Legend.tsx + EmptyState.tsx
│   ├── buildCardGraph.ts           ← snap → ReactFlow nodes/edges
│   └── layoutEngine.ts             ← lazy-imported elkjs
├── modal/
│   ├── ModalShell.tsx              ← focus trap + escape + ←/→ keyboard nav
│   ├── ClaimRenderer.tsx           ← D/C/E rubric, function_context badge, evidence sort
│   ├── InteractionModal.tsx + AggregatedModal.tsx
│   └── ChainContextBanner.tsx      ← chip drill into AggregatedModal
├── lib/                            ← pure libs (vitest-tested)
│   ├── colors.ts (REVERSE_VERBS, ARROW_COLORS, classifyArrow)
│   ├── pseudo.ts (PSEUDO_NAMES)
│   ├── normalize.ts (canonicalPairKey, normalizePathwayName)
│   ├── claims.ts (PLACEHOLDER_SNIPPETS, isPathwayInContext, classifyClaim)
│   ├── diagnostics.ts (deriveBadges)
│   └── pathwayStats.ts (derivePathwayStats, claimPassScore, sortPathwayStats)
├── store/
│   ├── useSnapStore.ts             ← Map<ProteinKey, SnapshotEntry> + activeProtein; frozen-after-set
│   ├── useViewStore.ts             ← per-protein filters; previewPathway + hoveredBaseProtein
│   └── useModalStore.ts            ← {open, args, history}
├── api/
│   ├── client.ts                   ← getJSON wrapper + ApiError
│   ├── queries.ts                  ← useVisualizeQuery + useVisualizeQueries (writes inside queryFn)
│   └── sse.ts                      ← re-export of shared/useSSE
└── types/
    └── api.ts                      ← hand-typed contract; EXPECTED_SCHEMA_VERSION paired with backend
```

## Stack

- **Vite 5 + React 18 + TypeScript strict.**
- **`@xyflow/react` v12** (custom nodes + edges).
- **`elkjs` v0.9** layered DAG layout, lazy-imported (1.4 MB chunk loads on first canvas render).
- **Zustand v5**, **TanStack Query v5**, **React Router v6**.
- **vitest 2.1** for unit tests (62 cases across the lib/ files).

## Build commands

```bash
cd react-app
npm install            # install deps (run once)
npm run dev            # Vite dev server with HMR; proxies /api → 127.0.0.1:5000
npm run typecheck      # tsc -b --noEmit
npm test               # vitest run (one-shot)
npm run test:watch     # vitest watch mode
npm run build          # tsc -b && vite build → emits to ../static/react/
```

For a single-shot quality gate (typecheck + vitest + build + pytest), run `bash scripts/check.sh` from the project root.

## Schema-version pairing

`react-app/src/app/main.tsx:EXPECTED_SCHEMA_VERSION` is paired with `services/data_builder.SCHEMA_VERSION`. Both should currently read `"2026-05-04"`. The SPA logs a console warning (warn-once per drift value) if the backend emits a different `_schema_version`; render proceeds best-effort. Bump both in lockstep when the snapshot/ctx contract changes.

## Rollback path (if SPA breaks for a user)

1. Visit any `/api/visualize/<protein>?spa=0` to load the archived legacy frontend.
2. The escape hatch is permanent until `static/_legacy/` is deleted.
3. To roll back the default flip globally: in `routes/visualization.py:get_visualization`, change `if request.args.get('spa') != '0':` → `if request.args.get('spa') == '1':`. That puts legacy back as default with `?spa=1` opting into SPA.

## Not yet shipped (deferred)

| | Effort | Notes |
|---|---|---|
| `static/_legacy/` deletion | ~1 hour | After 1-2 weeks of daily SPA use proves stable. |
| CSS Modules extraction (inline → .module.css) | ~2 days | Tokens already enable theme toggle without this. |
| Animation system (modal fade, layout transitions) | ~half day | Polish. |
| Code-gen TS types from backend JSON schema | ~half day | Hand types currently match; codegen frees us from drift. |
| Continuous denormalized-counter correctness | ~half day | sqlalchemy event listeners or scheduled job. |
| vitest component tests | ~1 day | Currently lib-only (62 cases). |
| Playwright E2E + HDAC6 visual regression | ~half day | Daily QA covers this until cutover. |
| Theme toggle UI | ~1 hour | Tokens make it trivial; just need a toggle in the header. |
| Multi-protein workspace UI | ~2-3 days | Architecture wired (Q2 day-one). |
| Light a11y audit (WCAG AA) | ~1 day | We've done a "no-blockers" pass. |

## Why incremental migration worked (post-mortem)

The original plan kept the legacy vanilla JS hot path alive while growing React islands beside it. Once the islands proved that React + Vite + TS shipped cleanly to `static/react/`, scaling to a full SPA in `src/app/` was incremental: the SPA mounted at `?spa=1` while legacy stayed default. Each phase verified end-to-end before the next.

The cutover-flip on 2026-05-04 was the inverse switch (SPA default, legacy escape) — same wiring, different default. Risk-free because both shells are still served and a one-line change reverts the default if needed.

## What we deliberately rejected

- ❌ Cytoscape.js — imperative API, awkward in React.
- ❌ d3-force inside React — exact brittleness this MIGRATION.md was originally written to avoid.
- ❌ Next.js — over-kill for a single-page SPA mounted into Flask.
- ❌ Redux/RTK — Zustand is enough.
- ❌ Material UI / Chakra — design language doesn't match the existing card aesthetic.
- ❌ Dropping `Pathway.protein_count` column — irreversible, and the `int` per row is negligible.
