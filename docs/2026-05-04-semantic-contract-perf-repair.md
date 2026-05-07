# 2026-05-04 Semantic Contract and PERK Performance Repair

## Why this was needed

The 0007 recovery fixed structural schema drift, but a deeper semantic drift
remained between PostgreSQL, API reconstruction, and the legacy/React
frontends.

Observed before this repair:

| Probe | Bad rows |
| --- | ---: |
| `interaction_claims.direction IS NULL` | 91 |
| invalid `interaction_claims.arrow` | 3 |
| scalar `interactions.arrow` missing from JSONB `interactions.arrows` | 26 |
| `interactions.data.arrow` disagreeing with scalar `interactions.arrow` | 9 |
| `indirect_chains.chain_with_arrows` containing `complex` | 2 |
| `interactions` chain JSON containing `complex` | 3 |

`complex` was the main contract leak: biologically it meant physical/co-complex
binding, but the UI arrow vocabulary only has four values:
`activates`, `inhibits`, `binds`, and `regulates`.

## Backup and migration

Created live DB backup before writes:

`backup_20260504_135107_pre_0008.dump`

Applied Alembic revision:

`20260504_0008_semantic_interaction_contract.py`

Follow-up JSON cleanup backup:

`backup_20260504_141025_pre_0009.dump`

Follow-up Alembic revision:

`20260504_0009_normalize_interaction_data_json.py`

The migration:

- Normalizes legacy `complex`/co-complex arrows to `binds`.
- Normalizes unknown/modulatory arrows to `regulates`.
- Backfills claim directions to semantic `main_to_primary` /
  `primary_to_main`.
- Realigns scalar `interactions.arrow`, JSONB `interactions.arrows`, and
  `interactions.data.arrow`.
- Normalizes chain hop JSON in `indirect_chains`, `interactions`, and
  `interactions.data`.
- Adds check constraints for claim arrows, claim directions, claim contexts,
  and interaction arrows.
- Normalizes remaining raw `interactions.data` copies inside nested
  `functions` and `chain_link_functions`.

Post-migration probes:

| Probe | Bad rows |
| --- | ---: |
| null claim directions | 0 |
| invalid claim directions | 0 |
| invalid claim arrows | 0 |
| invalid claim contexts | 0 |
| invalid interaction arrows | 0 |
| invalid interaction contexts | 0 |
| scalar arrow missing from JSONB arrows | 0 |
| data arrow mismatch | 0 |
| indirect-chain `complex` hop arrows | 0 |
| interaction-chain `complex` hop arrows | 0 |
| raw function JSON `complex` arrows/effects | 0 |
| raw function JSON `modulates` arrows/effects | 0 |

`alembic check` reports: `No new upgrade operations detected.`

## Runtime code changes

Added `utils/interaction_contract.py` as the shared vocabulary for DB sync,
validators, models, route payloads, and data builder reads.

Writer/read-side repairs:

- Pipeline schemas/prompts no longer allow `complex` as an arrow enum.
- DB sync normalizes arrows, claim directions, chain hop arrows, and scalar /
  JSONB arrow mirrors before writing.
- SQLAlchemy model helpers normalize `primary_arrow` and `set_primary_arrow`.
- API/result/detail builders normalize claim/function arrows, claim directions,
  chain hop arrows, cross-query claims, and `_chain_entity`.
- Legacy modal fallback now treats old `complex` data as binding display, not a
  separate arrow class.

## Performance finding and fix

The proposed disk-cache plan assumed `/api/results/<protein>` was already
writing `cache/<P>.json`; it was not. The actual dominant cost was remote
Postgres round trips.

Before:

| Path | Time |
| --- | ---: |
| `build_full_json_from_db("PERK")` profile | 40.779s |
| `/api/results/PERK` | 40.775s |
| `/api/visualize/PERK` legacy | 39.915s |
| `/api/visualize/PERK?spa=1` | 40.005s |

Profiler evidence: 257 SQL executions; about 39s spent in
`psycopg2.cursor.execute`.

Fixes:

- Batch-load `ChainParticipant` rows and linked `IndirectChain` rows instead
  of iterating dynamic `Interaction.chain_memberships`.
- Reuse the batch index inside chain metadata reconstruction.
- Batch-load cross-query pathway claims instead of querying once per injected
  interaction.
- Batch-load chain claims inside cross-protein chain injection.
- Batch-load `_chain_entity` rows by `origin_interaction_id`.

After:

| Path | Time |
| --- | ---: |
| `build_full_json_from_db("PERK")` profile | 5.203s |
| `/api/results/PERK` | 5.505s |
| `/api/visualize/PERK` legacy | 4.929s |
| `/api/visualize/PERK?spa=1` | 4.780s |

The query count for the builder dropped from 257 to 24.

## Route contract

Visualization route default was not flipped.

- `/api/visualize/PERK`: legacy shell, no `X-Viz-Shell: spa` header.
- `/api/visualize/PERK?spa=1`: React SPA shell, `X-Viz-Shell: spa`.

## Verification

Focused checks run:

- `python3 -m py_compile` on changed Python modules and migration.
- `node --check static/_legacy/modal.js`
- `node --check static/_legacy/visualizer.js`
- `pytest -q tests/test_interaction_contract.py tests/test_data_builder_chain_links.py tests/test_routes_visualization.py tests/test_card_view_chain_contract.py`
- Live Flask test-client probe for `/api/results/PERK`,
  `/api/visualize/PERK`, and `/api/visualize/PERK?spa=1`.
- `pytest -q`: 703 passed, 1 warning.
- `alembic current`: `20260504_0009 (head)`.
- `alembic check`: `No new upgrade operations detected.`

PERK payload probe after repair:

- 134 interactions.
- 0 bad top-level arrows.
- 0 bad claim/function arrows.
- 0 bad claim/function directions.
- 0 bad chain hop arrows.
- `_schema_version`: `2026-05-04`.

## Rollback note

For DB rollback, restore `backup_20260504_141025_pre_0009.dump` for the state
immediately before JSON cleanup, or `backup_20260504_135107_pre_0008.dump` for
the state before semantic constraints. Alembic downgrade from 0009 leaves
cleaned JSON values in place; downgrade from 0008 drops the new constraints and
makes `interaction_claims.direction` nullable again, but does not reverse
normalized data values.

For code rollback, revert the local edits touching the shared interaction
contract, `services/data_builder.py`, `utils/db_sync.py`, model constraints,
validator/prompt arrow enums, result route normalization, and the legacy modal
fallbacks.

## Frontend modal/card rebuild

The React SPA modal at `react-app/src/app/modal/` was rebuilt to match the
legacy modal anatomy (single-edge focus, metadata grid, MECHANISM /
EFFECT / BIOLOGICAL CASCADE / SPECIFIC EFFECTS / EVIDENCE sections). The
default visualization route was not flipped: legacy stays default, the
React SPA stays opt-in via `?spa=1`. No backend, schema, or DB changes.

New tokens in `react-app/src/app/styles/tokens.css`:

- `--font-serif` (Charter / Iowan Old Style fallback) for editorial titles.
- `--leading-tight`, `--leading-normal`, `--leading-relaxed`.
- `--glass-bg`, `--glass-border`, `--backdrop-blur`, `--shadow-card` for
  the modal surface.
- `--section-mechanism`, `--section-effect-{positive,negative,binding,
  regulatory,neutral}`, `--section-specifics` aliases used by the per-claim
  sub-cards.

New files:

- `modal/MetadataGrid.tsx` + `MetadataGrid.module.css` — the compact
  TYPE / DIRECTION / FUNCTIONS / EVIDENCE / PATHWAYS / CONTEXT rail.
- `modal/FunctionCard.tsx` + `FunctionCard.module.css` — per-claim card
  with the legacy section anatomy. Owns `data-claim-header`, RubricDots,
  FunctionContextBadge, the `data-uncited` flag, and the per-cascade
  `mentionedEndpoints` labelling. Replaces the inner body of the previous
  `ClaimRenderer`; `ClaimRenderer` now only dispatches normal vs.
  synthetic / thin / router placeholders to keep callers stable.
- `modal/cascade.module.css` — vertical-timeline CSS with
  `@keyframes cascadePulse`. Honors the global `prefers-reduced-motion`
  override.
- `modal/ModalShell.module.css` — modal width is now `min(90vw, 900px)`,
  height capped at `90vh`. Glass background + accent left-border driven
  by per-modal `--modal-accent` set from the click payload's arrow.
- `modal/InteractionModal.module.css` and rewritten
  `modal/InteractionModal.tsx` — header strip with arrow chip in the
  middle, MetadataGrid, optional lead block (italic first sentence of
  `cellular_process` and bold first sentence of `effect_description`,
  rendered only when non-placeholder), Functions(N) header, claim list.
- `modal/AggregatedModal.module.css` and rewritten
  `modal/AggregatedModal.tsx` — node view is now compact expandable rows
  rendered in biological direction (clicked protein bolded). Header
  carries the same MetadataGrid; chain-leg label and "X of Y" filter
  counts preserved.
- `modal/ChainContextBanner.module.css` — token-driven density polish.
- `src/global.d.ts` — ambient declaration for `*.module.css` imports.

Behavior preserved end-to-end: Escape close, Tab focus trap, ←/→/j/k
claim keyboard nav, pathway filter with "Show all" toggle, chain-leg
badge, "No citations" warning + `data-uncited` flag, RubricDots,
FunctionContextBadge, special-case placeholders for synthetic / thin /
router claims, `mentionedEndpoints` cascade tagging, `data-claim-header`
attribute on every claim/row toggle.

Verification:

- `cd react-app && npm run typecheck` — clean.
- `npm test` — 67 / 67 vitest cases green.
- `npm run build` — emitted to `static/react/`. CSS bundle grew from
  18.0 kB to 34.3 kB (the new module CSS); JS bundle 87.8 kB → 89.3 kB.
- Flask test client (`/api/visualize/PERK`):
  - Default: status 200, `X-Viz-Shell` absent, 3 references to legacy
    `visualizer.js`, 0 references to `/static/react/app.js`.
  - `?spa=1`: status 200, `X-Viz-Shell: spa`, 0 references to legacy
    `visualizer.js`, 1 reference to `/static/react/app.js`.

Out of scope and unchanged:

- `static/_legacy/`, the legacy templates, the visualization route
  default, any backend / DB / migration, `ProteinCard` and any other
  canvas-graph component.

## TDP43 log follow-up

The 2026-05-06 TDP43 run completed and saved, but it was not a fully clean
data run. The persisted DB contract was healthy at the coarse schema level:
Alembic was at `20260504_0009`, and there were zero invalid persisted
interaction arrows, zero invalid persisted claim arrows, and zero null
`function_context` rows in `interactions` or `interaction_claims`.

The run-level diagnostics still reported partial chain evidence:
`TDP43->XPO7` was unrecoverable, and two indirect interactors had incomplete
hop entries:

- `GRN`: `TDP43->SORT1`, `CTSD->GRN`.
- `ULK1`: `TDP43->TBK1`, `SQSTM1->ULK1`.

That explains partial-chain badges in the frontend. It is data-quality
fallout from the chain-claim phases, not a schema failure. The log also
showed repeated `MAX_TOKENS` / truncation recovery in AX/AZ chain-claim
generation because `gemini-3-flash-preview` clamps the requested 24000 output
tokens to an 8192 server cap for this call path.

One React-side bug did fall out of this log audit. Chain-hop rows in
`snapshot_json.interactions` carry their hop-specific card data in
`functions`, while the broader DB-backed `claims` collection can be empty or
can contain wider direct-pair claims. The rebuilt modal was using `claims`
first for every row, which meant TDP43 chain-hop cards could render empty or
render the wrong wider claim set. In the captured TDP43 payload, 19 of 42
chain-hop rows would have rendered empty and 22 of 42 would have rendered
extra broader claims under that rule.

The React fix is in `react-app/src/app/lib/interactionSurface.ts`:

- `claimsForInteraction()` now uses `functions` first for `_is_chain_link`
  rows, and still uses persisted `claims` first for direct rows.
- The helper normalizes DB claim field names (`function_name`, `mechanism`,
  `biological_consequence`, `pathway_name`) into the card-facing shape
  (`function`, `cellular_process`, `biological_consequences`, `pathway`) so
  the React card does not lose titles or mechanism text when it consumes
  persisted claims.
- `selectInteractionForEdge()` now prefers the exact `_is_chain_link` row
  when a clicked edge carries a chain id, so a direct row that merely shares
  `chain_id` metadata cannot steal the chain-hop modal.

Modal callers were updated in `InteractionModal.tsx` and
`AggregatedModal.tsx`, with regression coverage in
`interactionSurface.test.ts`.

The log also showed the live Flask app running on port 5003. The Vite dev
server proxy still pointed at 5000, so `npm run dev` could send API/static
requests to the wrong backend. `react-app/vite.config.ts` now defaults the
proxy target to `http://127.0.0.1:5003` and allows local override through
`VITE_BACKEND_ORIGIN`.

Verification after the fix:

- `cd react-app && npm run typecheck` — clean.
- `npm test -- --run` — 72 / 72 vitest cases green.
- `npm run build` — emitted to `static/react/`.
- Live `http://127.0.0.1:5003/api/visualize/TDP43?spa=1` returned 200 with
  `X-Viz-Shell: spa` and React bundle references.
