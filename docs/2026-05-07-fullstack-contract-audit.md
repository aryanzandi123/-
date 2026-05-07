# 2026-05-07 Fullstack Contract Audit

Scope: TDP43-only contract audit and first read-side repair. No DB writes,
migrations, backfills, React edits, route-default changes, or PERK/ATXN3 result
payload fetches were performed in this cluster.

## Baseline Contract

- Backend schema version: `2026-05-07`.
- Reference net-effect row: `TDP43 -> DDX3X`, DB row `14655`,
  `interaction_type=indirect`, `function_context=net`,
  `locus=net_effect_claim`, `via=["GLE1"]`.
- Reference hop row: `GLE1 -> DDX3X`, DB row `14656`,
  `locus=chain_hop_claim`, `chain_id=2624`, `hop_index=1`.
- Legacy and React-opt-in routes remain:
  - `/api/visualize/TDP43`: legacy default.
  - `/api/visualize/TDP43?spa=1`: current React shell.

## Findings

### P0: Cross-query API rows skipped contract stamping

TDP43 pathway cross-query rows were appended to `snapshot_json.interactions`
after the main `_apply_contract_fields()` pass in `services/data_builder.py`.
That allowed late-injected rows to miss `locus`, `is_net_effect`,
`chain_members`, pathway-scope fields, and claim-level locus/source/target/hop
metadata. The audited TDP43 snapshot had 94 rows missing `locus` before this
repair.

Repair status: fixed read-side. Cross-query pathway entries and SNAP entries
are now contract-stamped before append, and their claims inherit `locus`,
`chain_id`, `source`, `target`, and `hop_index` where applicable.

### P0: Mixed-case chain symbols hid DB-backed hops

TDP43 chain `2614` stored chain text with `C9orf72`, while the DB protein row
is canonicalized as `C9ORF72`. The read-side hop lookup used case-sensitive
`Protein.symbol` matching, so `C9orf72 -> EIF4G1` could synthesize from parent
JSONB even though DB row `14634` existed.

Repair status: fixed read-side. Chain protein lookup now resolves symbols
case-insensitively while preserving the chain text casing in emitted payloads.
Post-repair `/api/results/TDP43` emits `C9orf72 -> EIF4G1` with `_db_id=14634`,
`locus=chain_hop_claim`, `chain_id=2614`, `hop_index=0`.

### P0: DB/storage semantic contradiction remains parked

TDP43 row `14615` remains a DB/data decision, not a read-side serialization
fix: it is `function_context=net` with no `chain_id`, no memberships, no chain
context, and claim `28492` also has no chain. This requires a backup-gated
DB/data repair decision before any live correction.

### P1: React/current SPA schema drift remains parked

`react-app/src/app/main.tsx` still expects schema `2026-05-04`, while the
backend emits `2026-05-07`. This cluster intentionally did not edit React.

### P1: React migration doc drift remains parked

`react-app/MIGRATION.md` still says SPA is default and legacy is `?spa=0`.
Current `routes/visualization.py` keeps legacy as default and React behind
`?spa=1`.

### P1: React pathway-selection parity gap remains parked

Current React still treats empty pathway selection as an empty canvas and
auto-selects a pathway. Legacy semantics say no auto-selection.

## Schema Status

- `alembic current`: `20260504_0009 (head)`.
- `alembic check`: no new upgrade operations detected.
- Alembic still reports the known SQLAlchemy FK-cycle warning between
  `interactions` and `indirect_chains`.

## First Repair Implemented

Files changed:

- `services/data_builder.py`
- `tests/test_data_builder_chain_links.py`

Behavior changed:

- Cross-query pathway entries are stamped with the same API contract fields as
  normal `interactions_list` rows.
- Late-injected cross-query SNAP entries are stamped after their claims are
  attached, so claim-level locus/source/target/hop metadata is present.
- `function_context=net` rows injected through the cross-query path now emit
  `type=indirect`, `interaction_type=indirect`, `locus=net_effect_claim`.
- Chain-hop membership can infer `hop_index` from chain members and row
  endpoints when explicit hop fields are absent.
- Chain protein lookup in `_reconstruct_chain_links()` is case-insensitive for
  DB resolution and still preserves emitted chain text casing.

## TDP43 Verification

Flask test-client checks against the configured PostgreSQL DB:

- `/api/results/TDP43`: `200`, schema `2026-05-07`, `196` interactions,
  `missing_locus=0`.
- `TDP43 -> DDX3X`: `_db_id=14655`, `type=indirect`,
  `interaction_type=indirect`, `function_context=net`,
  `locus=net_effect_claim`, `is_net_effect=true`, `via=["GLE1"]`.
- `GLE1 -> DDX3X`: `_db_id=14656`, `locus=chain_hop_claim`,
  `chain_id=2624`, `hop_index=1`, claim locus `chain_hop_claim`.
- `C9orf72 -> EIF4G1`: `_db_id=14634`, `locus=chain_hop_claim`,
  `chain_id=2614`, `hop_index=0`.
- `/api/visualize/TDP43`: `200`.
- `/api/visualize/TDP43?spa=1`: `200`.

Verification commands:

```bash
python3 -m py_compile services/data_builder.py tests/test_data_builder_chain_links.py
python3 -m pytest -q -p no:cacheprovider tests/test_data_builder_chain_links.py
python3 -m pytest -q -p no:cacheprovider tests/test_data_builder_chain_links.py tests/test_routes_visualization.py tests/test_card_view_chain_contract.py tests/test_chain_handling.py
env PYTHONDONTWRITEBYTECODE=1 SKIP_APP_BOOTSTRAP=1 python3 -m alembic current
env PYTHONDONTWRITEBYTECODE=1 SKIP_APP_BOOTSTRAP=1 python3 -m alembic check
```

Results:

- `tests/test_data_builder_chain_links.py`: `10 passed, 1 warning`.
- Focused bundle: `31 passed, 1 warning` after rerunning with PostgreSQL
  network access allowed.
- `py_compile`: passed.
