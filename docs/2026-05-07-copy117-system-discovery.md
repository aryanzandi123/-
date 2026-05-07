# Copy117 System Discovery

Date: 2026-05-07

Workspace:

`/Users/aryan/Downloads/untitled folder 8/untitled folder 2 copy 117`

## Scope And Evidence Rules

This document began as a Phase 0 discovery artifact for the copied `copy117`
workspace. It intentionally treats copied docs, copied caches, old audit notes,
old React assumptions, and prior repair conclusions as historical clues only.
The current source code, current runtime behavior, current API payloads, current
DB/schema state, current browser behavior, and current tests in this checkout
are the source of truth.

Initial discovery made no product-code, schema, migration, model, React, or
live-data edits. The Phase 2 repair update below records local legacy
frontend/test/doc edits only. No DB writes, schema migrations, data backfills,
route-default changes, destructive commands, or stored scientific data changes
were run.

Serena status:

- Active project: `dada-propaths-copy117-fullstack`
- Active project path: `/Users/aryan/Downloads/untitled folder 8/untitled folder 2 copy 117`
- Serena initial instructions were read.
- Serena onboarding was absent for this copy. I did not write Serena memories in
  this docs-only phase because the user limited Phase 0 writes to docs and local
  diagnostics.
- Serena semantic inspection worked on current symbols in `app.py`,
  `routes/visualization.py`, `services/data_builder.py`, `models.py`,
  `runner.py`, and legacy frontend files.

## Current Runtime Architecture

The current product surface is the legacy frontend served by Flask.

Primary route:

1. `app.py`
   - Loads `.env`.
   - Configures Flask, SQLAlchemy, compression, static-file cache policy, and
     blueprints.
   - Performs an import-time read-only DB connection probe.
   - Runs startup side effects unless `SKIP_APP_BOOTSTRAP=1`.
   - `bootstrap_app()` can run `db.create_all()`, cache directory creation,
     optional log rotation, and optional startup backfills. Read-only probes
     should keep `SKIP_APP_BOOTSTRAP=1` and avoid accidental boot side effects.

2. `routes/visualization.py`
   - `/api/visualize/<protein>` is the default visualization route.
   - The default route renders the stable legacy shell from
     `templates/visualize_legacy.html`.
   - `?spa=1` opts into the React shell.
   - The route calls `build_full_json_from_db(protein)`.
   - The previous file-cache fallback is removed: PostgreSQL is treated as the
     single source of truth for visualization data.
   - The route has an in-memory HTML cache keyed by protein and payload
     fingerprint.

3. `services/data_builder.py`
   - `SCHEMA_VERSION = "2026-05-07"`.
   - `build_full_json_from_db()` reconstructs the payload returned by
     `/api/results/<protein>` and embedded into `/api/visualize/<protein>`.
   - `_apply_contract_fields()` stamps interaction-level semantic fields:
     `locus`, `is_net_effect`, `chain_members`, `chain_context_pathway`,
     `hop_index`, `hop_local_pathway`, `via`, and `mediators`.
   - `_apply_claim_contract_fields()` propagates locus, chain id, source,
     target, and hop index to serialized claims.

4. `visualizer.py`
   - `create_visualization_from_dict()` renders `templates/visualize_legacy.html`.
   - The template embeds the raw API payload as `RAW`.
   - `RAW.snapshot_json` becomes `SNAP`; `RAW.ctx_json` becomes `CTX`.
   - The template still supports legacy-shape backfill from `SNAP.interactors`
     to `SNAP.interactions`, but modern DB snapshots already emit
     `SNAP.interactions`.
   - `SNAP` is frozen after initialization to prevent cross-script mutation.

5. `static/_legacy/*`
   - `network_topology.js` maintains a shared graph model.
   - `visualizer.js` builds and updates the D3 graph.
   - `card_view.js` builds the pathway/card view and interactor layout.
   - `modal.js` renders single-interaction and aggregate modals.
   - `shared_utils.js`, `force_config.js`, and `cv_diagnostics.js` supply shared
     helpers, force layout policy, and diagnostics.

6. `react-app/*`
   - React exists as an opt-in shell behind `?spa=1`.
   - It is not the current product target for this repair phase.
   - Current React source still expects schema `"2026-05-04"` in
     `react-app/src/app/main.tsx`, while the backend emits `"2026-05-07"`.
   - `react-app/MIGRATION.md` still contains copied/stale cutover language that
     does not match the current default legacy route.

## Data Lifecycle

The write path was not executed in this phase. This is the current source-level
map:

1. Query and pipeline
   - `routes/query.py` accepts query options and invokes the pipeline path.
   - `runner.py` orchestrates Gemini/Vertex-backed pipeline phases, chain
     determination, chain-claim generation, post-processing, and web job status.
   - `utils/post_processor.py`, validators, and prompt modules shape the data
     before persistence.

2. DB sync and storage
   - `utils/db_sync.py` writes protein, interaction, pathway, chain, and claim
     data to PostgreSQL.
   - Important tables in `models.py`:
     - `proteins`
     - `protein_aliases`
     - `interactions`
     - `pathways`
     - `pathway_interactions`
     - `pathway_parents`
     - `indirect_chains`
     - `chain_participants`
     - `interaction_claims`
   - `interactions.data` remains non-null JSONB and still carries payload fields
     that are not fully normalized.
   - `InteractionClaim` is the atomic scientific claim table. Evidence and
     PMIDs live on claims and should not drift to the wrong edge or hop.

3. API reconstruction
   - `/api/results/<protein>` and `/api/visualize/<protein>` both use
     `build_full_json_from_db()`.
   - The API emits a `snapshot_json` with `proteins`, `interactions`, and
     `pathways`.
   - It also emits `_schema_version` and diagnostics.
   - Contract stamping happens during reconstruction, not only during DB sync.

4. Legacy frontend rendering
   - The Jinja template embeds the API payload.
   - Legacy JS reads `SNAP.interactions` and `SNAP.pathways`.
   - Graph and modal behavior is therefore only correct if the JS interprets the
     serialized semantic contract exactly.

## Current DB And Schema Evidence

Read-only migration checks:

- `alembic current`: `20260504_0009 (head)`
- `alembic check`: no new upgrade operations detected
- Known warning: SQLAlchemy cannot fully sort the FK cycle between
  `indirect_chains` and `interactions`. This is a warning during autogenerate,
  not a discovered migration diff in this pass.

No schema write, migration, or backfill was performed.

## Read-Only Runtime Evidence

The initial DB/API probe required network permission because the sandbox blocked
the configured PostgreSQL connection. After approval, a Flask test-client probe
against current code and the configured DB succeeded.

TDP43 baseline:

- `/api/results/TDP43`: `200`
- `_schema_version`: `2026-05-07`
- Proteins: `59`
- Interactions: `196`
- Pathways: `242`
- `missing_locus`: `0`
- Direct rows: `57`
- Chain-hop rows: `96`
- Net-effect rows: `43`

Reference TDP43 chain:

| Pair | DB id | Source | Target | Type | Function context | Locus | Chain | Hop | Claims | Functions | Via |
| --- | ---: | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| TDP43/DDX3X | 14655 | TDP43 | DDX3X | indirect | net | net_effect_claim | 2624 | null | 1 | 1 | GLE1 |
| GLE1/DDX3X | 14656 | GLE1 | DDX3X | direct | direct | chain_hop_claim | 2624 | 1 | 1 | 1 | empty |

This is important because older copied notes described a TDP43 net-effect row
being emitted as direct and the GLE1/DDX3X hop being synthesized from stale
JSONB. In this copy, the current API baseline is already repaired for that
specific probe.

PERK baseline:

- `/api/results/PERK`: `200`
- `_schema_version`: `2026-05-07`
- Proteins: `57`
- Interactions: `190`
- Pathways: `262`
- `missing_locus`: `0`
- Direct rows: `52`
- Chain-hop rows: `96`
- Net-effect rows: `42`

Legacy visualization route:

- `/api/visualize/TDP43`: `200`, `X-Viz-Cache: miss`, HTML length about
  `19.1 MB`
- `/api/visualize/PERK`: `200`, `X-Viz-Cache: miss`, HTML length about
  `18.1 MB`

The large rendered HTML size and slow route probe are not proof of a bug, but
they are a real performance and cache-risk signal for Phase 1.

Runtime diagnostics observed during the read-only route build:

- Several `PATHWAY DRIFT` report-only logs for TDP43-related functions, with
  `PATHWAY_AUTO_CORRECT=false`.
- A visualizer data-quality warning for XPO1 function naming versus arrow.

These were read-time/report-only observations. No payload was persisted.

## Phase 2 Repair Evidence

First P0 cluster chosen:

- Legacy modal and graph semantic contract consumption.
- Evidence led to frontend interpretation, not DB/schema repair, for the first
  patch: current TDP43/PERK API payloads already emitted explicit `locus`
  values and correct sampled net/hop rows.

Files changed:

- `static/_legacy/modal.js`
- `static/_legacy/card_view.js`
- `static/_legacy/visualizer.js`
- `static/viz-styles.css`
- `services/data_builder.py`
- `tests/test_card_view_chain_contract.py`
- `tests/test_data_builder_chain_links.py`

Contract fixes made:

- Legacy modal grouping now classifies rows through explicit `locus`, with
  fallback heuristics only for older payloads.
- Aggregate modals split `direct_claim`, `chain_hop_claim`,
  `net_effect_claim`, `indirect`, and `shared` into separate sections.
- Claim-to-function mapping now carries `c.locus`, so function cards can label
  net-effect and chain-hop claims without falling back to stale
  `context_data.type`.
- Single-edge chain-hop and net-effect modals render function-card endpoints
  from the row's biological source/target instead of query-relative direct-pair
  grouping. This prevents `GLE1 -> DDX3X` from appearing as
  `TDP43 -> GLE1`.
- Card-view filters now use `locus`; direct mode excludes chain-hop and
  net-effect rows.
- Graph link classes now use row `locus` and preserve `interaction_type` from
  API rows, so chain and net rows can receive distinct styling.
- Modal close focus handling was adjusted so closing an active modal no longer
  hides a focused descendant with `aria-hidden`.

Live browser verification through `/api/visualize/TDP43` on 2026-05-07:

- Browser route: `http://127.0.0.1:5004/api/visualize/TDP43`
- Runtime counts from embedded `SNAP.interactions`:
  - `net_effect_claim`: `43`
  - `direct_claim`: `57`
  - `chain_hop_claim`: `96`
- `TDP43 -> DDX3X` resolves to `locus=net_effect_claim`, section `net`.
- `GLE1 -> DDX3X` resolves to `locus=chain_hop_claim`, section `chain`.
- Single net-effect modal title/body:
  - header: `Net-Effect Claims (1)`
  - badge: `NET EFFECT`
  - endpoint: `TDP43 -> DDX3X`
  - no `DIRECT PAIR` badge.
- Single chain-hop modal title/body:
  - title: `GLE1 -> DDX3X`
  - header: `Chain-Hop Claims (1)`
  - badge: `CHAIN HOP`
  - endpoint: `GLE1 -> DDX3X`
  - no false `TDP43 -> GLE1 (1)` grouping.
- Aggregate DDX3X modal over the net row plus hop row:
  - sections: `CHAIN-HOP CLAIMS (1)`, `NET-EFFECT CLAIMS (1)`
  - no direct section for those two rows
  - no `DIRECT PAIR` badge in the aggregate body.
- Browser console after the final reload and modal open/close check:
  - errors: `0`
  - warnings: `0`
- Screenshot captured: `copy117-ddx3x-chain-net-modal.png`.

Verification commands:

- `node --check static/_legacy/modal.js`
- `node --check static/_legacy/card_view.js`
- `node --check static/_legacy/visualizer.js`
- `python3 -m pytest -q -p no:cacheprovider tests/test_card_view_chain_contract.py`
  - `5 passed, 1 warning`
- `env PYTHONDONTWRITEBYTECODE=1 SKIP_APP_BOOTSTRAP=1 python3 -m pytest -q -p no:cacheprovider tests/test_data_builder_chain_links.py tests/test_routes_visualization.py tests/test_card_view_chain_contract.py tests/test_chain_handling.py`
  - `34 passed, 1 warning`

The warning in Python tests is a dependency deprecation from
`google/genai/types.py`, not a ProPaths contract failure.

## Phase 2 Continuation: Chain-Scoped Claim Reconstruction

Next issue found after the first locus repair:

- PERK exposed a higher-impact API reconstruction bug.
- The same DB interaction can legitimately be emitted more than once when a
  hop participates in multiple indirect chains, but chain-specific duplicate
  rows were carrying claims from sibling chains.
- This is a false-evidence-placement bug: the browser could show a claim and
  evidence under the wrong chain context/pathway.

Read-only evidence before repair, from live `/api/visualize/PERK`:

- `SNAP.interactions.length`: `190`
- duplicate DB interaction ids: `15`
- claim/row chain-scope mismatches: `21`
- Example:
  - DB interaction `14515`, `PERK -> NFE2L2`
  - row chain `2595`, chain pathway
    `Antioxidant Response / UPR Crosstalk`
  - incorrectly carried sibling claim `28411` from chain `2596`
    (`Antioxidant Response`, `PERK -> NFE2L2 -> HMOX1`)

Repair:

- Added read-side claim scoping in `services/data_builder.py`.
- Chain-specific emitted rows now serialize only claims whose `chain_id` belongs
  to that row's visible chain scope.
- Multi-chain overview rows can still carry claims from all visible hop
  memberships; per-chain rows stay single-chain scoped.
- `_inject_cross_protein_chain_claims()` and `build_protein_detail_json()` now
  use the same scope helper, preventing later claim injection from reintroducing
  sibling-chain evidence.

Live browser verification after repair:

- Browser route: `http://127.0.0.1:5005/api/visualize/PERK`
- `SNAP.interactions.length`: `190`
- duplicate DB interaction ids: `15` (still present; multi-chain rendering can
  require multiple visible rows)
- claim/row chain-scope mismatches: `0`
- For `PERK -> NFE2L2`:
  - multi-chain overview row scope: `[2595, 2596]`, claims from both chains
  - per-chain row `2595`: only claim `28360`
  - per-chain row `2596`: only claim `28411`
- The chain `2595` modal shows
  `NFE2L2 Phosphorylation & Keap1 Dissociation & Nuclear Translocation` and no
  longer shows the sibling-chain claim
  `NFE2L2 Neh2 Domain Phosphorylation & Keap1 Dissociation`.
- Browser console after route/modal verification:
  - errors: `0`
  - warnings: `0`

Additional verification:

- `python3 -m py_compile services/data_builder.py`
- `python3 -m pytest -q -p no:cacheprovider tests/test_data_builder_chain_links.py::test_multi_chain_hop_rows_scope_claims_to_visible_chain`
  - `1 passed, 1 warning`
- `env PYTHONDONTWRITEBYTECODE=1 SKIP_APP_BOOTSTRAP=1 python3 -m pytest -q -p no:cacheprovider tests/test_data_builder_chain_links.py tests/test_routes_visualization.py tests/test_card_view_chain_contract.py tests/test_chain_handling.py`
  - `35 passed, 1 warning`

No DB writes, schema changes, migrations, backfills, route-default changes, or
stored scientific-data edits were performed.

## Biological Graph Semantics

The current backend/API contract supports three visible loci:

1. `direct_claim`
   - Real pair-level direct biology.
   - Should render as a direct edge or direct interaction row.
   - Can have `chain_id` if the pair also participates in a chain, but the
     visible locus must still be explicit.

2. `chain_hop_claim`
   - An adjacent hop inside an indirect chain.
   - Example: `GLE1 -> DDX3X` inside `TDP43 -> GLE1 -> DDX3X`.
   - Must render under the hop/edge it actually describes.
   - Should carry `chain_id`, `chain_members`, and `hop_index`.

3. `net_effect_claim`
   - Query-to-terminal chain consequence.
   - Example: `TDP43 -> DDX3X` via `GLE1`.
   - Must not be presented as a fake direct interaction.
   - Should carry `via` and `mediators`.

The API now emits explicit `locus` fields, but the legacy frontend still has
large older code paths that classify by `type`, `interaction_type`, local graph
role, pathway membership, and chain-link flags. The first frontend repair
should verify and then harden all legacy modal/graph decisions around `locus`
and the chain fields, instead of inferring scientific meaning from older
shortcuts.

## Chain Support

Current model/source support:

- `Interaction.depth` is documented as number of hops with no upper cap.
- `IndirectChain.chain_proteins` stores the full ordered chain.
- `IndirectChain.chain_with_arrows` stores typed arrows per hop.
- `ChainParticipant` stores interaction-to-chain membership and role.
- `utils/chain_view.py` defines `full_chain` as the authoritative chain shape
  and derives `mediator_chain`, `upstream_interactor`, and `depth` from it.
- `services/data_builder.py` emits `chain_members`, `hop_index`,
  `chain_context_pathway`, and `hop_local_pathway`.
- `static/_legacy/card_view.js` includes specific logic for chain interior
  nodes and notes that iterative expansion is needed for 3+ hop chains.

Current risk:

- The storage/API structure can represent arbitrary-length chains, but legacy
  graph and modal behavior still needs browser verification for 4+ protein
  chains. In particular, click targets, pathway filtering, chain navigation,
  and aggregate modals can still be wrong even when the payload is correct.

## Pathway Semantics

The current API exposes two different pathway ideas:

- `chain_context_pathway`: whole-chain grouping/display pathway.
- `hop_local_pathway`: pathway for the hop claim/function itself.

These can differ and must not be silently collapsed. A mismatch can be real
biology or a data-quality signal. Either way, the UI must make the distinction
visible and should not hide a valid hop just because its local pathway differs
from the chain-level pathway.

Legacy modal risk found in current source:

- `modal.js` chain-link pathway filtering currently checks
  `L._chain_pathway_name` or `L._chain_entity?.pathway_name`, plus a fallback
  where both endpoints are pathway interactors.
- `services/data_builder.py` emits `chain_context_pathway` and
  `hop_local_pathway`.
- There is no current-source match for `_chain_pathway_name` in
  `services/data_builder.py`.
- This mismatch can cause chain-hop rows to be filtered out in pathway context
  unless both endpoints are already in the pathway interactor set.

This is a strong candidate for the first legacy-modal/graph repair cluster.

## Evidence, PMID, And Function Context

Current API shape:

- Claims are serialized from `interaction_claims`.
- Claim fields include function name, mechanism, effect description,
  consequences, specific effects, evidence, PMIDs, pathway name, confidence,
  function context, context data, chain id, locus, source, target, and hop index
  where applicable.
- Legacy modals prefer `L.claims` over raw JSONB `L.functions`.
- Raw `functions` remain important as fallback for chain rows that have no
  claim rows but do have hop-specific function payloads.

Primary risk:

- Modal filtering and grouping can still hide valid claims or render them under
  the wrong section even when claim evidence is correctly attached in the API.
- The first repair should preserve claim evidence and PMIDs on the correct
  direct row, chain hop, or net-effect row. It should not move evidence in the
  frontend to make cards easier to render.

## Legacy Modal And Graph Data Flow

Single interaction modal:

- `showInteractionModal(link, clickedNode = null)` reads `link.data || link`.
- It derives source/target display names from semantic source/target where
  available, otherwise D3 link geometry.
- It prefers `claims` and maps claims into function-card shape.
- It renders chain context banners when `_is_chain_link` is present.
- It uses `interaction_type === "indirect"` for indirect display logic.

Aggregate modal:

- `showAggregatedInteractionsModal(nodeLinks, clickedNode, options = {})`
  reconstructs actual links from `SNAP.interactions` when a pathway-expanded
  node is clicked.
- It applies pathway filtering unless `Show All` is active.
- It splits claim rendering into pathway-specific and other-claim sections.
- It emits stubs when pathway filtering hides every claim for an interaction,
  which is better than silent disappearance but still needs browser QA because
  the user can still miss valid biology under default filter state.

Graph/card view:

- `network_topology.js` normalizes query and databased edges into a shared
  graph model.
- `visualizer.js` builds the D3 graph and exports card-view helpers.
- `card_view.js` builds pathway and interactor modes from `SNAP.interactions`
  and `SNAP.pathways`.
- Direct arrows are prioritized for node coloring, and chain links can override
  some fallback node arrow state.

Current risk:

- The graph can imply directness through edge labels, node coloring, or section
  grouping even if the API correctly marks a net-effect row as indirect.
- Browser QA must check the actual visible labels and modal rows, not just the
  JSON payload.

## Cache Risks

Current cache layers:

- Flask static cache:
  - `SEND_FILE_MAX_AGE_DEFAULT` is `0` when `FLASK_DEBUG` is true.
  - Production keeps a 24 hour static max age.
- Visualization HTML route cache:
  - `routes/visualization.py` caches generated legacy HTML in memory by protein
    and payload fingerprint.
  - Responses include `X-Viz-Cache` with hit/miss status.
- Template cache bust:
  - `visualizer.py` injects a timestamp `cache_bust` into static script URLs.
- Browser/local state:
  - legacy JS stores the last queried protein and some query options in
    localStorage.

Risks:

- A product-code fix can appear absent in-browser if the route HTML cache or
  browser cache serves an old shell.
- Backend edits require restarting the running Flask process.
- The very large embedded HTML payload makes cache invalidation and route
  latency user-visible.

## React Current State

React v2 is not the product target for the first repair. Current source facts:

- React is available only through `?spa=1`.
- React `main.tsx` expects schema `"2026-05-04"`.
- Backend emits `"2026-05-07"`.
- React source contains its own pathway filtering, graph building, modal, claim,
  and chain components.
- React may be useful as reference material for future structure, but its
  current source and copied migration docs do not define product truth.

Repair implication:

- Do not polish or rewrite React until legacy/API semantic correctness is
  stable.
- Any future React v2 should consume the same explicit API contract that the
  legacy repair stabilizes: `locus`, `chain_members`, `hop_index`,
  `chain_context_pathway`, `hop_local_pathway`, claim evidence, and claim-level
  PMIDs.

## Tests That May Pass While Runtime Is Wrong

Current test coverage is useful but not sufficient:

- Data-builder and route tests can prove payload contract fields exist.
- JS or Python tests may not click actual legacy nodes or exercise browser
  modal grouping.
- Mocked payloads can miss the real DB/API pathway shape, especially
  chain-level versus hop-local pathway differences.
- Passing tests do not prove the route-level HTML cache, browser static cache,
  D3 graph geometry, or modal filter UI behaves correctly.

Phase 1 must include browser-visible checks through `/api/visualize/<protein>`.

## High-Value Unknowns For Phase 1

1. Does the legacy graph visually label net-effect rows as indirect/net, or does
   edge styling still imply directness?
2. Does the legacy aggregate modal hide valid chain-hop claims by default when
   `chain_context_pathway` and `hop_local_pathway` differ?
3. Does `Show All` reveal all hidden valid claims, and is the hidden state clear
   enough to avoid scientific misunderstanding?
4. Do arbitrary-length chains render with correct hop order, chain navigation,
   and modal scoping?
5. Are PMIDs/evidence rendered only with their correct claim, hop, or net-effect
   row?
6. Are pathway drift warnings purely report-only, or do they correlate with
   visible pathway mismatch in the legacy UI?
7. Does the HTML route cache ever serve a stale page after API payload changes?
8. Do current tests cover the actual modal/graph path, or only the serialized
   API contract?

## 2026-05-07 TDP43 Acceptance Evidence

TDP43 is now the primary acceptance probe for the first P0 legacy locus/card
cluster. PERK exposed the general claim-scope bug and remains a secondary
regression/proof-of-generalization, but cluster completion is gated on TDP43
through the real legacy route.

Read-only route evidence after the repair:

- `/api/results/TDP43` returns wrapper keys `_diagnostics`, `_schema_version`,
  `ctx_json`, and `snapshot_json`.
- `snapshot_json` contains 59 proteins, 196 interactions, and 242 pathways.
- Locus counts are `net_effect_claim=43`, `direct_claim=57`, and
  `chain_hop_claim=96`.
- `TDP43 -> DDX3X` remains one indirect/net row:
  `locus=net_effect_claim`, `function_context=net`,
  `interaction_type=indirect`, `chain_id=2624`, `via=["GLE1"]`,
  `chain_members=["TDP43","GLE1","DDX3X"]`.
- `GLE1 -> DDX3X` remains one DB-backed hop row:
  `locus=chain_hop_claim`, `function_context=direct`,
  `interaction_type=direct`, `chain_id=2624`, `hop_index=1`,
  `chain_members=["TDP43","GLE1","DDX3X"]`.

Before the current repair, TDP43 browser/card evidence showed that the Stress
Granule card view rendered a terminal DDX3X card as `TDP43 activates DDX3X`.
The payload was correct (`net_effect_claim` via GLE1), but card-view anchor
selection treated the chain-backed net-effect row as a direct query-to-terminal
anchor. That was false directness in the current product surface.

After the card-view repair:

- Stress Granule Dynamics card view no longer renders a fake direct
  `TDP43 activates DDX3X` terminal card.
- DDX3X is shown through the chain lane as `Hop: GLE1 activates DDX3X`.
- TDP43/GLE1 chain cards are labeled with `Hop:` rather than unqualified direct
  text.
- DDX3X card nodes inherit `pathway_Stress_Granule_Dynamics` and
  `_pathwayContext={id,name}`, so aggregate modal pathway filtering has the
  context it needs.
- DDX3X pathway-only modal shows the Stress Granule Dynamics filter, one
  `GLE1 -> DDX3X` chain-hop section, and one `TDP43 -> DDX3X` net-effect
  section. It does not show the Nucleocytoplasmic Transport pathway as a
  pathway assignment leak.

Additional TDP43 payload checks after the backend scoping fix:

- wrong-chain claim leaks: 0
- scalar-row chain-member mismatches against matching `all_chains`: 0
- terminal sibling leakage rows: 0
- fake direct net-effect rows in the API payload: 0

Remaining runtime/UI risks from this pass:

- `/api/visualize/TDP43` still takes roughly 54-56 seconds to build in the
  local dev app.
- Graph View starts with only the query node until roots are selected; after
  selecting all roots, root pathway nodes exist but are positioned far out on
  the large SVG canvas, outside the initial viewport. This is a P1 graph
  usability/positioning issue, not repaired in this cluster.
- TDP43 currently has no positive PMID placement to verify; evidence arrays
  are present on the sampled DDX3X/GLE1 rows, but PMIDs are empty.
- Browser console has no JavaScript errors in the checked pass, but Chrome
  reports one accessibility warning when closing the modal because focus
  remains inside an `aria-hidden` modal ancestor.

## 2026-05-07 Path-Scoped Modal Discovery

Fresh TDP43-only investigation found that the remaining card/modal direction
risk was frontend context loss, not schema drift.

Currency gate:

- Live DB Alembic revision: `20260504_0009`.
- Repo Alembic head: `20260504_0009`.
- Live DB is at head.
- `alembic check`: no new upgrade operations detected.
- The only Alembic warning is the known FK-cycle sort warning between
  `indirect_chains` and `interactions`.
- `models.py` metadata matches the current migration head.
- `/api/results/TDP43` emits `locus`, `is_net_effect`, `chain_members`,
  `chain_id`, `hop_index`, `chain_context_pathway`, `hop_local_pathway`, and
  claim-level `locus`, `chain_id`, `source`, `target`, evidence, and PMIDs
  where present. Nonblocking caveat: some direct/net claims do not carry
  claim-level `hop_index`, which is acceptable for this UI repair because the
  row-level chain context is present where needed.

Root cause:

- Card View creates path-specific chain nodes with `_uid`, `_chainId`,
  `_chainPosition`, `_chainLength`, `_chainProteins`, `pathwayId`, and
  `_pathwayContext`.
- Before this repair, `handleCardClick()` called
  `window.openModalForCard(d.data.id, pathwayContext)` and discarded the
  clicked path instance.
- `openModalForCard()` then rebuilt modal links by scanning every
  `SNAP.interactions` row involving that protein or containing it anywhere in
  a chain. That aggregate lookup could select a sibling hop, net-effect row, or
  query-relative context instead of the clicked Card View position.
- `showAggregatedInteractionsModal()` also treated some chain-hop rows with
  `interaction_type=indirect` as indirect aggregate rows, which could invoke
  mediator perspective logic on a hop-local claim.

TDP43 API evidence used for the repair:

- Direct/length-1 probe: `TBK1 -> TDP43`, `locus=direct_claim`,
  `interaction_type=direct`, claim `source=TBK1`, `target=TDP43`.
- Chain-hop probe: `ULK1 -> TBK1`, `locus=chain_hop_claim`, `chain_id=2613`,
  `hop_index=0`, `chain_members=["ULK1","TBK1","SQSTM1","TDP43"]`.
- Chain-hop regression probe: `GLE1 -> DDX3X`, `locus=chain_hop_claim`,
  `chain_id=2624`, `hop_index=1`,
  `chain_members=["TDP43","GLE1","DDX3X"]`.
- Net-effect probe: `TDP43 -> DDX3X`, `locus=net_effect_claim`,
  `chain_id=2624`, `chain_members=["TDP43","GLE1","DDX3X"]`.
- Net-effect probe: `TDP43 -> ULK1`, `locus=net_effect_claim`,
  `chain_id=2613`, `chain_members=["ULK1","TBK1","SQSTM1","TDP43"]`.

TDP43 arbitrary-depth evidence:

- Payload max `chain_members` length: 6.
- Payload max `hop_index`: 4.
- TDP43 payload includes hop 3+ rows from cross-query chain instances; these
  are emitted through `snapshot_json.interactions` and should not be silently
  truncated by Card View.

Browser/runtime evidence after the path-scoped modal repair:

- Local route: `http://127.0.0.1:5004/api/visualize/TDP43`.
- Server was started with `SKIP_APP_BOOTSTRAP=1`; no migration, backfill, or DB
  write was run.
- Browser console after route load and modal probes: 0 JavaScript errors in the
  checked pass. A later focus audit still reported the known nonblocking
  `aria-hidden` modal focus warning.
- Scoped Autophagy card context for `TBK1` in chain `2613`, position `1`,
  renders one modal row: `ULK1 -> TBK1`, `CHAIN-HOP CLAIMS`, `CHAIN HOP 1`,
  `ACTIVATES`, with the `Ser172 Transphosphorylation & TBK1 Kinase Activation`
  claim. It does not render `TDP43 -> TBK1`.
- Scoped Stress Granule Dynamics card context for `DDX3X` in chain `2624`,
  position `2`, renders one modal row: `GLE1 -> DDX3X`, `CHAIN-HOP CLAIMS`,
  `CHAIN HOP 2`, `ACTIVATES`, with the GLE1/DDX3X RNA-helicase claim. It does
  not render `TDP43 -> DDX3X` as the clicked hop.
- Aggregate DDX3X modal still intentionally shows both sections:
  `CHAIN-HOP CLAIMS` for `GLE1 -> DDX3X` and `NET-EFFECT CLAIMS` for
  `TDP43 -> DDX3X`.
- Aggregate TBK1 modal shows the direct `TBK1 -> TDP43` claim under
  `DIRECT PAIR CLAIMS`.

Cache/staleness note:

- `.env` has `FLASK_DEBUG=false`, which enables static and generated HTML
  caching in normal `app.py` runs.
- The stale static `TDP43.json` cache is not used by `/api/results/TDP43` or
  `/api/visualize/TDP43`; static browser/cache refresh can still matter for
  legacy JS and generated visualization HTML.
- The verification route used a fresh local process after code edits. If a
  browser appears stale, restart the backend process and force-refresh because
  backend code edits are not hot-reloaded in the existing process.

## 2026-05-07 Final TDP43 Legacy Card View Evidence

The initial completion claim was incomplete. Extra audits found three remaining
holes: the visualizer still had an aggregate fallback when a chain-scoped
selector returned empty, the modal layer could hydrate from aggregate `SNAP`
rows for empty scoped contexts, and the browser proof had not yet shown actual
chain-scoped rendered nodes for the TDP43 acceptance chain.

Final DB/API gate:

- Live DB is at Alembic head `20260504_0009`; `alembic check` is clean except
  the known FK-cycle sort warning.
- `/api/results/TDP43` has chain `2613` data plus the required direct, hop, and
  net evidence for the Card View repair.
- Nonblocking data caveats: claim-level `hop_index` is missing on some
  direct/net claims, and two shared direct rows omit `chain_id`; neither blocks
  scoped Card View repair because the needed row-level chain context exists for
  the audited hops.
- TDP43-owned hop3+ rows are absent in the DB. The TDP43 API does expose some
  global/shared `hop_index>=3` rows from PERK reconstruction, so global hop3+
  exists but is not TDP43-owned chain evidence.
- Stale `TDP43.json` is not used by `/api/results` or `/api/visualize`; static
  JS and generated HTML caches can still make a browser session look stale.

Final root causes and fixes:

1. Card View now passes `cardContext` into modal opening.
2. The visualizer selector no longer falls back to aggregate rows when a
   chain-scoped lookup misses.
3. The modal layer suppresses `SNAP` aggregate hydration even for empty
   chain-scoped contexts.
4. Card View chain pre-pass removed the redundant `chainTouchesPathway`
   endpoint-overlap gate, so pathway-admitted chains render scoped duplicate
   nodes even when only interior chain members overlap the pathway.

Final browser evidence:

- Route: `/api/visualize/TDP43`; Autophagy expanded.
- Chain `2613` rendered `ULK1`, `TBK1`, `SQSTM1`, and `TDP43`.
- Actual rendered TBK1 chain node had `_chainId=2613`, `_chainPosition=1`, and
  `_chainProteins=["ULK1","TBK1","SQSTM1","TDP43"]`.
- Clicking that rendered chain node opened `TBK1 - Interactions (1)` with
  `ULK1 -> TBK1`, `CHAIN-HOP CLAIMS`, `CHAIN HOP 1`, and `ACTIVATES`.
- Aggregate TBK1 still opened the direct `TBK1 -> TDP43` modal, proving scoped
  and aggregate behavior now diverge correctly.

Final checks recorded:

- `node --check` passed for `static/_legacy/card_view.js`,
  `static/_legacy/modal.js`, and `static/_legacy/visualizer.js`.
- Focused pytest bundle eventually reached `86 passed, 1 warning`.
- Browser audits included a negative scoped-miss check and the full UI-click
  pass above.

Remaining risks:

- `chain_with_arrows` contains stale semantic labels that can differ from the
  claim effect.
- The modal `aria-hidden` focus warning remains.
- Cache/static refresh remains a verification caveat.
- React v2 still waits.
- Future schema/path-instance work remains backup-gated and out of this slice.
