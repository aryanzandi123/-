# Copy117 Repair Roadmap

Date: 2026-05-07

Workspace:

`/Users/aryan/Downloads/untitled folder 8/untitled folder 2 copy 117`

## Summary

The recommended first repair cluster was legacy modal and legacy graph
correctness, centered on how the current legacy frontend consumes the explicit
API semantic contract. That cluster has now been partially implemented and
verified through the live legacy route for the TDP43/DDX3X net/hop probe.

Reason:

- The current read-only TDP43 API baseline is already much healthier than older
  copied notes suggested: `missing_locus=0`, `TDP43 -> DDX3X` is
  `net_effect_claim`, and `GLE1 -> DDX3X` is a DB-backed `chain_hop_claim`.
- The most likely remaining product risk is browser-visible interpretation:
  pathway filtering, modal grouping, chain-hop display, and edge labels can
  still mislead users even when `/api/results` is correct.
- Legacy is the current product surface. React is opt-in and should wait.

No DB/schema/live-data writes were needed for this first cluster. If later
browser checks prove that visible bugs come from deeper API/data contract
corruption, switch to the smallest backend/API repair first and keep DB writes
behind a backup and explicit approval gate.

## Repair Status: 2026-05-07

Completed in P0-A/P0-C:

- Legacy modal section grouping is now driven by explicit interaction `locus`.
- Chain-hop rows and net-effect rows have their own aggregate sections.
- Direct card/filter mode no longer includes chain-hop or net-effect rows.
- Graph link styling can distinguish net-effect and chain-hop rows through
  `locus`.
- Function cards now receive claim `locus`, so net-effect claims no longer fall
  back to stale `DIRECT PAIR` context badges.
- Single-edge chain-hop function cards render with their actual hop endpoints,
  e.g. `GLE1 -> DDX3X`, not query-relative `TDP43 -> GLE1`.
- Modal close focus handling no longer creates a browser console warning.

Files changed:

- `static/_legacy/modal.js`
- `static/_legacy/card_view.js`
- `static/_legacy/visualizer.js`
- `static/viz-styles.css`
- `services/data_builder.py`
- `tests/test_card_view_chain_contract.py`
- `tests/test_data_builder_chain_links.py`
- `docs/2026-05-07-copy117-system-discovery.md`
- `docs/2026-05-07-copy117-repair-roadmap.md`

Verification:

- Static JS syntax checks passed for `modal.js`, `card_view.js`, and
  `visualizer.js`.
- `tests/test_card_view_chain_contract.py`: `5 passed, 1 warning`.
- Focused backend/route/legacy bundle:
  `tests/test_data_builder_chain_links.py`,
  `tests/test_routes_visualization.py`,
  `tests/test_card_view_chain_contract.py`,
  `tests/test_chain_handling.py`: `34 passed, 1 warning`.
- Live browser route:
  `http://127.0.0.1:5004/api/visualize/TDP43`
  - embedded counts: direct `57`, chain-hop `96`, net-effect `43`
  - `TDP43 -> DDX3X` rendered as `Net-Effect Claims (1)`, not direct
  - `GLE1 -> DDX3X` rendered as `Chain-Hop Claims (1)`, not query-relative
  - aggregate DDX3X modal showed `CHAIN-HOP CLAIMS (1)` and
    `NET-EFFECT CLAIMS (1)`, with no direct section for those rows
  - browser console: `0` errors, `0` warnings after final reload/check
- Screenshot evidence:
  `copy117-ddx3x-chain-net-modal.png`.

No backup gate was required because no live DB write, schema migration,
backfill, destructive cleanup, route-default change, or stored-data mutation was
performed.

Additional P0 repair completed in backend/API reconstruction:

- Issue: duplicate visible rows for a multi-chain hop were legitimate, but each
  per-chain row could carry claims from sibling chains.
- Impact: evidence and function text could appear under the wrong chain context
  or pathway, changing the scientific meaning of a modal card.
- Files changed:
  - `services/data_builder.py`
  - `tests/test_data_builder_chain_links.py`
- Before repair, `/api/visualize/PERK` had `21` claim/row chain-scope
  mismatches. Example: `PERK -> NFE2L2` row for chain `2595` carried a claim
  from sibling chain `2596`.
- After repair, `/api/visualize/PERK` has `0` claim/row chain-scope mismatches
  under the row-visible chain-scope invariant.
- Focused verification:
  - `python3 -m py_compile services/data_builder.py`
  - targeted new regression: `1 passed, 1 warning`
  - focused backend/route/legacy bundle: `35 passed, 1 warning`
  - live browser route/modal verification on `PERK`: `0` console errors,
    `0` warnings
- No backup gate was required: this was a read-side serialization fix only.

## P0 Clusters

### P0-A: Legacy Modal And Graph Semantic Contract Consumption

Status: partially complete for the verified TDP43/DDX3X net/hop path.

Remaining work:

- Broaden browser probes beyond TDP43/PERK.
- Verify pathway-expanded nodes where chain-context and hop-local pathway
  labels diverge.
- Verify arbitrary-length chains with more than two hops.
- Inspect evidence-rich direct, chain-hop, and net-effect cards for PMID
  attachment under real browser interaction.

Problem:

The API now exposes explicit semantic fields (`locus`, `is_net_effect`,
`chain_members`, `hop_index`, `chain_context_pathway`, `hop_local_pathway`,
`via`, claim-level locus/source/target/hop), but legacy graph and modal code
still contains older classification paths based on `interaction_type`, `_is_chain_link`,
pathway interactor membership, and legacy chain fields.

Why first:

This is the shortest path to fixing visible product truth without altering
stored scientific data. It directly targets the current legacy surface.

Likely files:

- `static/_legacy/modal.js`
- `static/_legacy/card_view.js`
- `static/_legacy/visualizer.js`
- `static/_legacy/network_topology.js` if shared edge metadata is missing
- `services/data_builder.py` only if a needed field is absent from the payload

Expected DB-write risk:

- None for the first pass.

Verification:

- API: `/api/results/TDP43` still has `missing_locus=0`.
- API: `TDP43 -> DDX3X` remains `indirect/net_effect_claim` via `GLE1`.
- API: `GLE1 -> DDX3X` remains `chain_hop_claim`, `chain_id=2624`,
  `hop_index=1`, with claim and evidence attached.
- Browser: `/api/visualize/TDP43` graph and modal do not label
  `TDP43 -> DDX3X` as a direct claim.
- Browser: hop-local `GLE1 -> DDX3X` remains visible under the correct hop.
- Browser: aggregate modal sections make direct, chain-hop, and net-effect rows
  distinct.

### P0-B: Pathway Filter Hiding Valid Chain-Hop Claims

Problem:

Current source shows a likely field mismatch. `services/data_builder.py` emits
`chain_context_pathway` and `hop_local_pathway`, but `static/_legacy/modal.js`
chain-link pathway filtering checks `L._chain_pathway_name` or
`L._chain_entity?.pathway_name`, then falls back to "both endpoints are in this
pathway." That can hide valid chain-hop biology under pathway context.

Likely files:

- `static/_legacy/modal.js`
- `static/_legacy/card_view.js`
- `services/data_builder.py` only if the payload lacks required chain pathway
  fields in a specific route shape

Expected DB-write risk:

- None.

Status:

- Partially improved by the first patch: aggregate modal pathway filtering now
  collects current pathway labels from `chain_context_pathway`,
  `hop_local_pathway`, `chain_pathways`, `all_chains`, claim pathways, and
  function pathways before falling back to endpoint membership.
- Still needs browser verification with a real pathway mismatch case.

Verification:

- Browser: pathway-specific aggregate modal shows relevant chain-hop claims.
- Browser: `Show All` reveals all valid claims and clearly indicates when the
  default pathway filter hid them.
- API: sampled chain-hop rows include both chain and hop pathway fields.
- Tests: add focused JS or contract-level regression around pathway filter field
  names when implementation is approved.

### P0-C: Net-Effect Rows Presented As Fake Direct Biology

Problem:

Even if the API emits `net_effect_claim`, the legacy graph can still imply
directness through edge labels, node placement, section grouping, or badge text.
Query-to-terminal net effects must be displayed as chain/net context, not as
direct pair evidence.

Likely files:

- `static/_legacy/modal.js`
- `static/_legacy/visualizer.js`
- `static/_legacy/card_view.js`
- `services/data_builder.py` only if Phase 1 finds a payload regression

Expected DB-write risk:

- None unless a current DB row lacks all chain context, chain membership, and
  claim chain ids. That would require a separate backup-gated data repair.

Status:

- Complete for the TDP43/DDX3X live browser probe.
- Still needs wider route/browser sweeps to catch other proteins or older-row
  fallback shapes.

Verification:

- Browser: net-effect edges and modal cards use explicit net/chain language.
- Browser: no query-to-terminal net-effect claim appears in a direct section.
- API: all net rows have `type=indirect`, `interaction_type=indirect`,
  `function_context=net`, `locus=net_effect_claim`, and `via`.

### P0-D: Evidence/PMID Misassignment Or Loss

Status: partially complete.

Completed:

- Read-side claim serialization now scopes chain-specific claims to the visible
  chain row before the API payload reaches the legacy frontend.
- This prevents sibling-chain evidence from being displayed under the wrong hop
  context.

Remaining work:

- Broaden browser/API probes to direct claims, net-effect claims, and evidence
  rows with PMIDs, not just chain-hop evidence arrays.
- Verify that multi-chain overview rows are understandable enough when they
  intentionally show all visible hop memberships.

Problem:

The product is scientific. Evidence and PMIDs must stay attached to the exact
direct claim, chain hop, or net-effect claim they support. Frontend grouping
must not move evidence between rows or hide it behind a confusing filter state.

Likely files:

- `static/_legacy/modal.js`
- `services/data_builder.py` if serialized claim fields are missing
- `utils/db_sync.py` only if write-path evidence is proven wrong later

Expected DB-write risk:

- None for display repair.
- Possible backup-gated data repair only if live DB rows have evidence stored on
  the wrong claim.

Verification:

- API: claim-level evidence/PMID arrays are present for sampled direct, hop, and
  net rows.
- Browser: the modal renders PMIDs under the corresponding claim card.
- Browser: filtered-out claims show a clear count and can be revealed.

### P0-E: Backend/API Contract Corruption If Found In Wider Probes

Problem:

TDP43 and PERK have `missing_locus=0`, but wider proteins may still expose API
rows with missing locus, synthesized stale hop rows where DB-backed rows exist,
or chain/pathway fields missing from cross-query pathway injections.

Likely files:

- `services/data_builder.py`
- `utils/chain_view.py`
- `utils/chain_resolution.py`
- `utils/db_sync.py` if write-path corruption is confirmed

Expected DB-write risk:

- Read-side API repair: none.
- Stored data repair: possible, but only after schema dump, SELECT-only probe,
  backup, and explicit approval.

Verification:

- API sweeps over TDP43, PERK, and at least one additional chain-heavy protein.
- Invariants:
  - no interaction missing `locus`
  - no `function_context=net` row emitted as direct
  - chain hops have source, target, chain id, and hop index when inferable
  - claim locus matches row locus unless intentionally more specific

### P0-F: Schema/Model Drift That Can Corrupt Runtime

Problem:

Current Alembic is clean, but the schema contains a deliberate FK cycle and a
mix of normalized rows plus JSONB payload fields. Drift between normalized
tables, JSONB, and frontend assumptions can corrupt runtime semantics.

Likely files:

- `models.py`
- `migrations/versions/*`
- `services/data_builder.py`
- `utils/db_sync.py`

Expected DB-write risk:

- High. Any schema or live data change requires backup and explicit approval.

Verification:

- `SKIP_APP_BOOTSTRAP=1` Alembic checks.
- SELECT-only probes comparing interactions, claims, chain participants, and
  indirect chains for sampled proteins.
- No migration or backfill without approval.

## P1 Clusters

### P1-A: Legacy Modal Readability And Product Quality

Problem:

The modal can become dense or confusing even when it is semantically correct.
Users need compact claim cards, readable section headings, visible filter
state, and no wall-of-text dumps.

Likely files:

- `static/_legacy/modal.js`
- `static/viz-styles.css`
- `static/_legacy/card_view.js`

DB-write risk:

- None.

Verification:

- Browser screenshots for direct, aggregate, chain-hop, and net-effect modals.
- Long function names and evidence lists do not overflow or overlap.

### P1-B: Performance And Cache Hazards

Problem:

Read-only route probes generated about 18 to 19 MB of embedded HTML for PERK
and TDP43 and took noticeable time. Route cache, static cache, and payload
construction can make fixes appear stale or make user-visible navigation slow.

Likely files:

- `routes/visualization.py`
- `visualizer.py`
- `services/data_builder.py`
- `templates/visualize_legacy.html`

DB-write risk:

- None for cache/rendering repair.

Verification:

- Measure `/api/results/<protein>` and `/api/visualize/<protein>` latency.
- Confirm `X-Viz-Cache` hit/miss behavior.
- Confirm browser refresh loads current static assets after edits.

### P1-C: Missing Runtime/Browser Checks

Problem:

Current tests can pass while the actual legacy browser route misgroups claims,
hides rows, or displays misleading edge labels.

Likely files:

- Future test files only after repair approval
- Possibly browser automation scripts or docs

DB-write risk:

- None.

Verification:

- Add targeted browser or route-level checks only after the first repair cluster
  is approved.
- Keep tests focused on actual `/api/visualize/<protein>` behavior and the
  current legacy frontend.

### P1-D: Brittle API Shape And Frontend Contract Drift

Problem:

Legacy and React each consume the API differently. React currently expects
schema `2026-05-04`, while backend emits `2026-05-07`. The legacy frontend is
the product, but future React v2 needs a stable shared contract.

Likely files:

- `services/data_builder.py`
- `react-app/src/app/types/api.ts`
- `react-app/src/app/main.tsx`
- Future docs/specs

DB-write risk:

- None for type/schema docs.

Verification:

- Contract fixtures for direct, chain-hop, net-effect, pathway mismatch, and
  evidence-rich rows.
- React work waits until legacy/API semantics are stable.

### P1-E: Pathway Drift Reporting

Problem:

Read-only route builds report pathway drift warnings. The UI needs to avoid
silently presenting heuristic pathway disagreement as settled truth.

Likely files:

- `services/data_builder.py`
- `static/_legacy/cv_diagnostics.js`
- `static/_legacy/modal.js`

DB-write risk:

- None for surfacing diagnostics.
- Persisting reassignments would be a DB/data write and requires approval.

Verification:

- Drift remains report-only unless explicitly enabled.
- Browser shows diagnostics without rewriting claim pathways at read time.

## P2 Clusters

### P2-A: Documentation Cleanup

Clean stale docs that say React is default or that backend schema is still
`2026-05-04`. This waits until correctness work stabilizes, because stale docs
are not the product bug.

Likely files:

- `README.md`
- `react-app/MIGRATION.md`
- `CLAUDE_DOCS/*`
- older `docs/*`

DB-write risk:

- None.

### P2-B: Refactors After Correctness

Refactor only after user-visible scientific correctness is stable. Candidate
areas include duplicate modal mapping code, repeated claim-to-function mapping,
and legacy pathway filtering helpers.

Likely files:

- `static/_legacy/modal.js`
- `static/_legacy/card_view.js`
- `services/data_builder.py`

DB-write risk:

- None if kept frontend/read-side.

### P2-C: Future React V2 Planning

React v2 should wait until the backend/API and legacy semantics are stable.
If built later, it should rebuild legacy semantics, not inherit current React
schema drift or filtering behavior.

Likely files:

- `react-app/src/app/*`
- API contract docs

DB-write risk:

- None for frontend rebuild.

## Recommended Repair Order

1. Legacy modal/graph semantic contract consumption.
   - First because current API evidence is good for the TDP43 baseline and the
     visible product is legacy.

2. Pathway filter and chain-hop visibility.
   - Often part of the same frontend cluster, but keep the acceptance criteria
     explicit because hiding valid claims is a scientific P0.

3. Net-effect display labels and graph edge semantics.
   - Ensure no graph or modal section presents net-effect rows as direct pair
     evidence.

4. Evidence/PMID card organization.
   - Preserve scientific traceability while making cards readable.

5. Backend/API repair only where Phase 1 proves frontend bugs are caused by bad
   payloads.
   - Keep DB writes out unless a stored-data corruption is proven.

6. Performance/cache hardening.
   - Important, but correctness comes first unless latency blocks verification.

7. React v2 planning.
   - Deferred until legacy/API semantics are stable.

## Cluster Verification Matrix

| Cluster | API checks | Browser checks | Tests | DB write risk |
| --- | --- | --- | --- | --- |
| P0-A modal/graph contract | TDP43/PERK locus counts and sampled rows | Direct, hop, net modal and graph screenshots | Focused legacy contract tests after approval | None expected |
| P0-B pathway filters | Hop rows include chain and hop pathways | Pathway-only and Show All behavior | Filter fixture tests after approval | None expected |
| P0-C net effects | Net rows stay indirect/net | No fake direct edge/card | Route/data-builder tests if needed | None expected |
| P0-D evidence/PMIDs | Claim evidence and PMIDs present | Evidence appears under correct card | Claim rendering tests if added | Possible only if DB rows are wrong |
| P0-E API contract corruption | Wider protein sweeps | Browser confirms payload fixes | Data-builder tests | None for read-side, high for data repair |
| P0-F schema/model drift | Alembic plus SELECT-only probes | Not primary | Migration checks | High, approval required |

## Approval Gates

Stop and ask before:

- schema migrations
- live DB writes
- data backfills
- destructive cleanup
- route default changes
- removing legacy fallback behavior
- broad React rewrite
- stored scientific data changes

No approval is needed for a local read-side/frontend repair that only changes
code interpretation and rendering after the first repair cluster is explicitly
approved.

## Top P0 Recommendation

Start with P0-A plus the P0-B acceptance criteria:

Make the legacy modal and graph consume the current API semantic contract
directly. Specifically, the UI should drive display decisions from `locus`,
`is_net_effect`, `chain_members`, `hop_index`, `chain_context_pathway`,
`hop_local_pathway`, and claim-level locus/evidence fields. It should stop
relying on legacy-only pathway names or endpoint membership when deciding
whether valid chain-hop biology belongs in a pathway modal.

Expected user-visible improvement:

- Query-to-terminal net effects are not presented as direct claims.
- Chain-hop claims stay visible under the correct hop.
- Pathway mismatch is visible instead of silently hiding valid claims.
- Evidence and PMIDs remain attached to the correct claim.
- The legacy graph and modal tell the same scientific story as
  `/api/results/<protein>`.

## 2026-05-07 TDP43 Acceptance Update

Status: the first P0 legacy locus/card cluster is repaired against TDP43 as
the primary acceptance probe. PERK remains the secondary regression case
because it exposed the general backend claim-scope bug, but TDP43 is the gate
for declaring this cluster complete.

Completed safe repairs:

- Backend read-side chain scoping:
  `services/data_builder.py` now scopes serialized claims to the visible
  scalar `chain_id`, prefers the emitted chain entity over stale copied JSONB
  chain context, and filters cross-query injected claims through the same
  claim-scope helper.
- TDP43 card-view false directness:
  `static/_legacy/card_view.js` no longer promotes chain-backed net effects
  to direct query-to-terminal anchors. Relationship subtitles now label
  `Net via ...` and `Hop: ...` explicitly.
- TDP43 modal pathway context:
  card nodes inherit `pathwayId` and `_pathwayContext` when inserted into a
  pathway tree, so aggregate modals can apply Pathway Only / Show All logic
  from the actual card context.
- Regression coverage:
  `tests/test_data_builder_chain_links.py` now asserts scalar chain rows keep
  claims and chain members scoped to their visible chain; 
  `tests/test_card_view_chain_contract.py` asserts card-view net/hop labels
  and net-effect anchor suppression.

Before/after TDP43 evidence:

- Before: Stress Granule Dynamics card view rendered DDX3X as
  `TDP43 activates DDX3X`, even though the API row was an indirect
  `net_effect_claim` via GLE1.
- After: the fake direct DDX3X card count is 0; DDX3X appears through
  `Hop: GLE1 activates DDX3X`, and the aggregate modal shows separate
  `GLE1 -> DDX3X` chain-hop and `TDP43 -> DDX3X` net-effect sections.
- API invariant after repair:
  59 proteins, 196 interactions, 242 pathways;
  `net_effect_claim=43`, `direct_claim=57`, `chain_hop_claim=96`.
- `TDP43 -> DDX3X` remains indirect/net with `chain_id=2624` and
  `via=["GLE1"]`; `GLE1 -> DDX3X` remains the DB-backed hop with
  `hop_index=1`.
- Broad TDP43 chain-scope checks report 0 claim leaks, 0 chain-member
  mismatches against the row's matching chain summary, and 0 fake direct
  net-effect API rows.

Verification run:

- `node --check static/_legacy/card_view.js`
- `SKIP_APP_BOOTSTRAP=1 PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_card_view_chain_contract.py::test_card_view_labels_net_effects_and_avoids_fake_direct_anchors`
- `SKIP_APP_BOOTSTRAP=1 PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_data_builder_chain_links.py tests/test_card_view_chain_contract.py tests/test_routes_visualization.py tests/test_claim_locus_router.py tests/test_chain_orientation.py`
  -> 80 passed, 1 warning.
- Browser route `/api/visualize/TDP43` on the live legacy UI.

Remaining P1/P0 candidates:

- P1 graph positioning/default visibility: Graph View starts effectively empty
  except the query until roots are selected; selected root nodes are placed on
  a large SVG canvas outside the initial viewport. This distorts usability but
  was not fixed because it is a graph-layout/default-view cluster.
- P1 modal close accessibility: closing the modal leaves focus inside an
  `aria-hidden` ancestor, producing a Chrome accessibility warning.
- P1 performance/cache: `/api/visualize/TDP43` still takes about 54-56 seconds
  in local runtime.
- Data quality review: TDP43 runtime logs still report pathway drift for VCP,
  DNAJB1, TBK1, and CYLD as report-only, plus XPO1 arrow/content warnings.
  Persisted pathway or arrow reassignment would be stored scientific-data work
  and requires the explicit DB/data gate.

## 2026-05-07 Path-Scoped Card Modal Repair

Status: completed as a code-only legacy frontend slice. No schema migration,
live DB write, data backfill, route-default change, or React v2 work was
performed.

Problem:

- Card View already rendered separate chain/path instances, including duplicate
  protein cards by `_uid`.
- The click handoff collapsed that path instance back to only a protein id.
- The modal rebuilt from global protein membership and could therefore select
  the wrong hop, a query-to-terminal net effect, or aggregate context.

Repair performed:

- `static/_legacy/card_view.js`
  - Adds `makeCardModalContext()`.
  - Passes clicked `_uid`, `_chainId`, `_chainPosition`, `_chainLength`,
    `_chainProteins`, pathway context, parent id, and visible relationship
    metadata into `openModalForCard()`.
- `static/_legacy/visualizer.js`
  - Adds `selectLinksForCardContext()`.
  - When a chain card has `_chainId` and `_chainPosition`, selects the specific
    hop row by chain id, hop index, and ordered source/target pair.
  - Chain-scoped misses return no scoped rows instead of falling back to
    aggregate protein lookup; aggregate fallback remains only for unscoped
    protein cards.
- `static/_legacy/modal.js`
  - Preserves scoped card links through pathway filtering.
  - Normalizes hop labels through `hop_index ?? _chain_position`.
  - Keeps chain-hop rows out of indirect mediator perspective logic.
  - Fixes chain navigation to reopen with a clicked-node/context object instead
    of a bare protein string.

TDP43 browser/API acceptance:

- Direct case: `TBK1 -> TDP43` remains `DIRECT PAIR CLAIMS` with the
  TBK1/TDP43 phosphorylation claim.
- Chain-hop case: scoped Autophagy `TBK1` card in chain `2613` opens
  `ULK1 -> TBK1`, `CHAIN-HOP CLAIMS`, `CHAIN HOP 1`, `ACTIVATES`, with the
  Ser172/TBK1 activation claim and no `TDP43 -> TBK1` false arrow.
- Chain-hop regression case: scoped Stress Granule Dynamics `DDX3X` card in
  chain `2624` opens `GLE1 -> DDX3X`, `CHAIN-HOP CLAIMS`, `CHAIN HOP 2`,
  `ACTIVATES`, with the GLE1/DDX3X RNA-helicase claim and no
  `TDP43 -> DDX3X` false hop arrow.
- Net/aggregate case: aggregate DDX3X modal intentionally shows separate
  `CHAIN-HOP CLAIMS` (`GLE1 -> DDX3X`) and `NET-EFFECT CLAIMS`
  (`TDP43 -> DDX3X`) sections.
- Browser console after route load and modal probes: 0 JavaScript errors in the
  checked pass; the nonblocking modal `aria-hidden` focus warning remains a P1
  accessibility item.

Verification run:

- `node --check static/_legacy/card_view.js`
- `node --check static/_legacy/modal.js`
- `node --check static/_legacy/visualizer.js`
- `SKIP_APP_BOOTSTRAP=1 PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_card_view_chain_contract.py`
  -> 9 passed, 1 warning.
- `SKIP_APP_BOOTSTRAP=1 PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_data_builder_chain_links.py tests/test_card_view_chain_contract.py tests/test_routes_visualization.py tests/test_claim_locus_router.py tests/test_chain_orientation.py`
  -> 83 passed, 1 warning.
- Browser route `/api/visualize/TDP43` with a local `SKIP_APP_BOOTSTRAP=1`
  server.

Remaining risks:

- Current schema still lacks a first-class path/hop occurrence model. Code-only
  repair is good enough for this UI bug because TDP43 already emits the needed
  read-side fields, but durable provenance should be handled in a gated schema
  slice.
- `chain_with_arrows` can still contain stale semantic labels that differ from
  the claim effect.
- PMIDs are empty on the sampled TDP43 rows, so this pass verified evidence and
  claim placement, but not positive PMID rendering.
- Static and generated visualization caches can make the browser look stale
  after JS/backend edits; stale `TDP43.json` is not used by `/api/results` or
  `/api/visualize`.
- React v2 still waits. The legacy API/modal semantics must remain the source
  of truth before React consumes or replaces them.

## 2026-05-07 Final TDP43 Legacy Card View Closeout

Status: final for the bounded legacy Card View repair evidence. The earlier
completion note was incomplete: follow-up subagents found a selector empty
fallback, a modal empty fallback, and no proof that actual chain-scoped rendered
nodes were present in the browser.

Final fixes captured by this lane:

- Card View passes `cardContext`.
- `static/_legacy/visualizer.js` no longer falls back to aggregate rows for
  chain-scoped misses.
- `static/_legacy/modal.js` suppresses aggregate `SNAP` hydration even for
  empty chain-scoped contexts.
- Card View chain pre-pass removed/replaced the redundant
  `chainTouchesPathway` endpoint-overlap gate, allowing pathway-admitted chains
  to render scoped duplicate nodes.

Final DB/API gate:

- Live DB revision and repo head are both `20260504_0009`; `alembic check` is
  clean.
- TDP43 payload includes chain `2613` plus required direct, hop, and net
  evidence.
- Nonblocking caveats: claim-level `hop_index` is missing on some direct/net
  claims, two shared direct rows omit `chain_id`, and TDP43-owned hop3+ rows are
  absent in DB. The TDP43 API still exposes some global/shared `hop_index>=3`
  rows from PERK reconstruction.

Final browser acceptance:

- `/api/visualize/TDP43`, Autophagy expanded, rendered chain `2613` nodes
  `ULK1/TBK1/SQSTM1/TDP43`.
- The actual rendered TBK1 chain node carried `_chainId=2613`,
  `_chainPosition=1`, and
  `_chainProteins=["ULK1","TBK1","SQSTM1","TDP43"]`.
- Clicking that node opened `TBK1 - Interactions (1)` with `ULK1 -> TBK1`,
  `CHAIN-HOP CLAIMS`, `CHAIN HOP 1`, and `ACTIVATES`.
- Aggregate TBK1 still opened the direct `TBK1 -> TDP43` modal.

Final verification record:

- `node --check` passed for the three legacy JS files.
- Focused pytest bundle eventually reached `86 passed, 1 warning`.
- Browser audits included a negative scoped-miss check and the full UI-click
  pass for the rendered chain node.

Recommended main-lane next move:

- Keep React v2 and schema/path-instance work gated. The smallest useful next
  move is a short browser/runtime regression harness for the TDP43 scoped Card
  View contexts, followed by the P1 modal focus warning or cache refresh lane.
