# Codex Frontend Parity Handoff

Date: 2026-05-06  
Workspace: `/Users/aryan/Desktop/DADA/untitled folder 2 copy 54`

This is a local-only operating brief for the next Codex session. It is not a
Git/GitHub/PR plan. Make direct local file edits only when the user explicitly
approves the implementation phase.

## User Intent

The user wants the React frontend to become a truly complete replacement for
the working legacy frontend, not a rough rewrite. It should preserve every
feature and complexity the legacy UI already handles, while becoming simpler,
cleaner, more reliable, and eventually more capable.

Legacy is the product reference. React is the experimental replacement until it
passes parity.

## Hard Rules

- No Git/GitHub framing. Do not ask the user to commit, branch, PR, stage, or
  reset anything.
- Work locally in this checkout only.
- Do not flip the visualization default route unless the user explicitly asks.
  Legacy stays default; React stays opt-in via `?spa=1` during parity work.
- Do not delete `static/_legacy/`.
- Do not start with styling guesses. Start with a parity audit.
- Do not treat React unit tests as proof the user-facing frontend is fixed.
  Browser-visible behavior matters.
- Trust live source and runtime behavior over stale docs or stale Serena notes.

## Current Reality To Verify First

Earlier Serena memory says the SPA became default. That note is stale after the
schema/frontend recovery. Current expected state:

- `python3 app.py` runs Flask on `http://127.0.0.1:5003`.
- `/api/visualize/<protein>` serves legacy by default.
- `/api/visualize/<protein>?spa=1` serves React.
- `routes/visualization.py` should keep that behavior.
- React dev proxy should point to port 5003 unless overridden.

Confirm before changing anything:

```bash
curl -s -D /tmp/legacy.headers -o /tmp/legacy.html http://127.0.0.1:5003/api/visualize/PERK
curl -s -D /tmp/spa.headers -o /tmp/spa.html 'http://127.0.0.1:5003/api/visualize/PERK?spa=1'
grep -i 'X-Viz-Shell' /tmp/legacy.headers /tmp/spa.headers || true
grep -o '_legacy' /tmp/legacy.html | wc -l
grep -o '/static/react' /tmp/spa.html | wc -l
```

## Serena Usage

Use Serena for repository navigation and memory/context, not as authority over
the current state.

Useful Serena/local memory files:

- `.serena/memories/project_overview.md`
- `.serena/memories/frontend_overhaul/phase_progress.md`
- `.serena/memories/react-modal-rebuild/current-inventory.md`

Read them, then verify against live files:

- `routes/visualization.py`
- `services/data_builder.py`
- `react-app/src/app/`
- `static/_legacy/`
- `templates/`

## Known Frontend/Data Boundary

Many apparent frontend bugs originate in the DB-to-payload reconstruction
layer. Inspect `services/data_builder.py` before assuming React/D3 is solely at
fault.

Important recent repair:

- React now has `react-app/src/app/lib/interactionSurface.ts`.
- `claimsForInteraction()` uses hop-specific `functions` for `_is_chain_link`
  rows, and persisted DB `claims` for direct rows.
- `selectInteractionForEdge()` prefers the exact chain-hop row when a clicked
  edge carries a chain id.

Do not undo this. It fixed TDP43 chain-hop cards where React previously showed
empty cards or broader direct claims.

## Reference Cases For Parity

Use these as required audit fixtures:

1. `PERK` edge modal: PERK ↔ ESYT1
   - Legacy screenshot/reference: clean single-edge modal.
   - React must match the structure, density, metadata grid, function cards,
     cascade timeline, evidence sections, and close/scroll behavior.

2. `KEAP1` aggregated/node modal
   - Legacy-like goal: compact interaction rows, not a full-width text dump.
   - React must handle multiple interactions, claim counts, PMIDs, pathway
     filtering, expansion, keyboard navigation, and no-citation badges.

3. `TDP43` chain-hop / partial-chain case
   - Must show incomplete-chain state honestly.
   - Must not mix broad direct claims into hop-specific chain cards.
   - Must not render empty cards when hop-specific `functions` exist.

4. A pathway-filtered case
   - Select a pathway and confirm both legacy and React filter the same claims,
     interactions, counts, and breadcrumbs.

5. A pseudo/generic-node case if present
   - Pseudo entities such as RNA/Ubiquitin must not become normal graph nodes.
   - If they appear, trace `services/data_builder.py`, `utils/chain_resolution.py`,
     and React pseudo filters before patching visuals.

## Required No-Edit Audit

Before any code changes, produce a local audit doc:

`docs/YYYY-MM-DD-react-legacy-parity-audit.md`

Include:

- Exact URL, viewport, and route tested.
- Legacy screenshot observations.
- React screenshot observations.
- API payload notes from `/api/results/<protein>` or `/api/visualize/<protein>?spa=1`.
- File/source suspects.
- Severity:
  - P0: React shows wrong science, wrong edge, wrong claims, missing claims,
    wrong arrow/direction, or broken interaction.
  - P1: React lacks a legacy feature or key workflow.
  - P2: visual density, typography, spacing, polish.
  - P3: future enhancement beyond legacy.

## Parity Checklist

React must support at least these legacy capabilities before default cutover:

- Query landing/start workflow and progress stream.
- Card/network view with stable layout, fit/reset, hover, selection, badges.
- Direct, indirect, shared, cross-query, and chain-hop interactions.
- Correct arrows and direction semantics.
- Multi-arrow/multi-claim interactions where applicable.
- Node aggregate modal.
- Edge/single-interaction modal.
- Chain context banner and chain-hop drilldown.
- Pathway Explorer V2 behavior:
  - hierarchy, breadcrumbs, relevance/sort/filter/search,
  - selected-pathway canvas filtering,
  - counts and pathway stats.
- Evidence display:
  - PMID links, quotes, year sorting, no-citation warnings.
- Claim quality display:
  - D/C/E depth/cascade/evidence indicators,
  - shallow/thin/router/synthetic placeholders.
- Partial-chain/incomplete-chain badges and explanations.
- Pseudo-protein filtering/labeling.
- Keyboard behavior:
  - Esc close,
  - Tab focus trap,
  - arrow/j/k claim navigation,
  - close/back behavior.
- Mobile and desktop responsive behavior.
- No text overflow or incoherent overlap.
- No full-width wall-of-text modals.
- No route/static cache confusion during development.

## Design Target

The React UI should feel like a refined scientific workbench, not a marketing
page and not a raw data dump.

Principles:

- Legacy modal anatomy is the visual contract.
- Dense but readable.
- Progressive disclosure for complexity.
- Cards only for repeated items or real framed tools.
- Avoid nested cards.
- Use stable dimensions for graph controls, modals, chips, and toolbars.
- Use icons for toolbar actions where possible.
- Avoid giant hero-scale type inside modals/panels.
- No single-hue decorative theme.
- Scientific content should be scannable and inspectable.

## Suggested Implementation Stages

### Stage 0: Audit Only

No edits. Compare legacy and React on the reference cases. Save the audit doc.

### Stage 1: Contract Stabilization

Fix React data selection and normalization issues only.

Likely files:

- `react-app/src/app/lib/interactionSurface.ts`
- `react-app/src/app/types/api.ts`
- `react-app/src/app/lib/claims.ts`
- `services/data_builder.py` only if payload reconstruction is proven wrong.

Acceptance:

- React renders the same claims/counts/arrows as legacy for reference cases.
- Add focused tests for any helper/contract changes.

### Stage 2: Modal Parity

Bring InteractionModal and AggregatedModal to legacy-level structure.

Likely files:

- `react-app/src/app/modal/InteractionModal.tsx`
- `react-app/src/app/modal/AggregatedModal.tsx`
- `react-app/src/app/modal/FunctionCard.tsx`
- `react-app/src/app/modal/MetadataGrid.tsx`
- modal CSS modules.

Acceptance:

- PERK ↔ ESYT1 React modal visually matches legacy anatomy.
- KEAP1 aggregate is compact rows, not a text dump.
- TDP43 chain-hop cards show hop-specific claims and partial-chain state.

### Stage 3: Card/Graph View Parity

Only after modals are sound. Bring React graph behavior up to legacy.

Likely files:

- `react-app/src/app/views/card/buildCardGraph.ts`
- `react-app/src/app/views/card/CardView.tsx`
- `react-app/src/app/views/card/ProteinCard.tsx`
- `react-app/src/app/views/card/ChainEdge.tsx`
- `react-app/src/app/lib/pathwayStats.ts`

Acceptance:

- Layout is stable and readable.
- All expected edge/link types appear.
- Pathway filtering and selected pathway state match legacy.
- No pseudo/generic nodes leak.

### Stage 4: Pathway Explorer + Workflow Parity

Make the surrounding React app match or exceed legacy workflows.

Acceptance:

- Query flow, progress drawer, pathway explorer, graph, and modals work as one
  coherent app.

### Stage 5: Performance + Browser QA

Run browser checks across desktop/mobile viewports. Fix overlap, slow rendering,
and cache confusion.

Acceptance:

- `cd react-app && npm run typecheck`
- `cd react-app && npm test -- --run`
- `cd react-app && npm run build`
- Focused Python tests for any backend/payload changes.
- Browser screenshots for reference cases.
- Legacy default route unchanged.

## Pasteable Codex Prompt

Use this as the next Codex task prompt:

```text
We are in /Users/aryan/Desktop/DADA/untitled folder 2 copy 54.

Goal: fully repair the React frontend so it reaches true parity with the
working legacy frontend, then eventually exceeds it. Do not start coding yet.
Do a no-edit parity audit first and save the audit locally.

Hard rules:
- No Git/GitHub/PR/stage/commit/reset framing.
- Local files only.
- Legacy visualization stays default.
- React stays opt-in via ?spa=1 until it passes parity.
- Do not delete static/_legacy/.
- Use Serena/project memory for navigation, but verify live source/runtime
  because some Serena frontend notes are stale.
- Browser-visible behavior matters more than unit tests alone.

First read:
- docs/2026-05-06-codex-frontend-parity-handoff.md
- .serena/memories/project_overview.md
- .serena/memories/frontend_overhaul/phase_progress.md
- .serena/memories/react-modal-rebuild/current-inventory.md
- routes/visualization.py
- services/data_builder.py
- react-app/src/app/
- static/_legacy/

Then audit these reference cases:
- PERK edge modal: PERK ↔ ESYT1
- KEAP1 node aggregate modal
- TDP43 chain-hop / partial-chain case
- one pathway-filtered case
- one pseudo/generic-node case if present

Compare legacy /api/visualize/<protein> against React
/api/visualize/<protein>?spa=1. Capture exact mismatches in:
- claims/functions selection
- arrows/directions
- chain-hop handling
- pathway filtering/counts
- modal/card layout and density
- graph layout and interaction behavior
- keyboard/accessibility behavior
- cache/dev-server gotchas

Save the audit to docs/YYYY-MM-DD-react-legacy-parity-audit.md.
Do not edit product code during the audit. After the audit, propose a staged
local-only implementation plan with P0/P1/P2/P3 priorities.
```

## What Success Looks Like

React can become default only when the user can open the same proteins in
legacy and React and feel that React is at least as complete, as readable, and
as trustworthy as legacy, with no missing scientific context or broken chain
behavior.
