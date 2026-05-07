# 2026-05-04 Schema And Frontend Recovery

## Context

The live app had two overlapping issues:

- The default visualization route had been switched to the React SPA, whose card view and modals were not production-ready.
- The live Postgres schema was one Alembic migration behind the repo head, and API payloads were inconsistently omitting top-level `function_context` even when the DB column was populated.

All actions below were performed directly in the local checkout and against the configured live Postgres database. No Git or GitHub workflow was used.

## Backup

Before applying the live DB migration, a custom-format Postgres dump was created and its catalog was verified with `pg_restore -l`.

- Backup file: `backup_20260504_004834_pre_0007.dump`
- Size: `1,235,400` bytes
- Archive timestamp: `2026-05-04 00:48:34 PDT`
- Dump format: PostgreSQL custom archive
- Database dumped: `railway`

## Database Migration

Pre-migration state:

- Live DB revision: `20260503_0006`
- Repo head: `20260503_0007`
- Drift: `interactions.function_context` and `interaction_claims.function_context` were nullable and lacked a DB server default, while `models.py` expected NOT NULL with default `'direct'`.

Applied:

```bash
alembic upgrade head
```

Result:

- Live DB revision is now `20260503_0007 (head)`.
- `interactions.function_context` is `NOT NULL` with default `'direct'`.
- `interaction_claims.function_context` is `NOT NULL` with default `'direct'`.
- No nulls or orphaned chain/claim rows were found after migration.

Post-migration critical counts:

- `proteins`: 108
- `interactions`: 150
- `interaction_claims`: 199
- `indirect_chains`: 35
- `chain_participants`: 112
- `pathways`: 956
- `pathway_interactions`: 153
- Null `interactions.data`: 0
- Null `interactions.direction`: 0
- Null `interactions.function_context`: 0
- Null `interaction_claims.function_context`: 0
- Orphan `interactions.chain_id`: 0
- Orphan `interaction_claims.chain_id`: 0
- Orphan `interaction_claims.interaction_id`: 0

## Code Changes

### Stable visualization default

`routes/visualization.py` now defaults `/api/visualize/<protein>` to the stable legacy vanilla-JS visualization shell. The React SPA remains available only via explicit `?spa=1`.

This restores the compact legacy card/modal UI as the normal app path while preserving the React rewrite for later repair.

### Payload function_context repair

`services/data_builder.py` now stamps top-level `function_context` onto interaction-shaped payloads from the SQLAlchemy column instead of trusting older JSON blobs.

Covered payload paths:

- Main query interactions
- DB-backed chain-link interactions
- Synthesized chain-link interactions
- Shared interactor interactions
- Cross-query interactions
- Pathway-local chain interactions

Observed before the fix:

- PERK `/api/results` had 51 `snapshot_json.interactions[]` entries with missing top-level `function_context`.

Observed after the fix:

- PERK `/api/results` has 0 entries missing top-level `function_context`.
- PERK context distribution in the rebuilt payload:
  - `direct`: 90
  - `net`: 26
  - `chain_derived`: 18

### Model/index metadata alignment

After the migration, `alembic check` exposed index metadata drift. The live DB had useful migration-created indexes that `models.py` did not declare, while a few `index=True` flags would have recreated wrong `ix_*` names.

`models.py` now declares the live indexes explicitly, including:

- `idx_indirect_chains_chain_signature`
- `idx_interactions_function_context`
- `idx_interactions_upstream`
- `idx_interactions_data_gin`
- `idx_claims_function_context`
- `idx_claims_interaction_context`
- `idx_claims_evidence_gin`
- `idx_claims_pmids_gin`

The stale auto-index declarations for `interactions.chain_id`, `interaction_claims.chain_id`, and `indirect_chains.chain_signature` were removed from the model.

Result:

```bash
alembic check
```

returns:

```text
No new upgrade operations detected.
```

## Tests And Runtime Verification

Commands run:

```bash
python3 -m py_compile models.py services/data_builder.py tests/test_data_builder_chain_links.py tests/test_routes_visualization.py
pytest -q tests/test_data_builder_chain_links.py tests/test_routes_visualization.py tests/test_card_view_chain_contract.py
pytest -q
```

Results:

- Focused tests: `10 passed, 1 warning`
- Full suite: `698 passed, 1 warning`

Live endpoint checks:

- `GET /api/results/PERK`
  - Status: 200
  - Payload size: 15,186,978 bytes
  - Time: about 40.5 seconds
  - Missing top-level `function_context`: 0
  - `_schema_version`: `2026-05-04`
  - Interactions: 134
  - Pathways: 254

- `GET /api/visualize/PERK`
  - Status: 200
  - Payload size: 14,052,052 bytes
  - Time: about 40.7 seconds
  - Legacy static refs: 8
  - React static refs: 0
  - Contains `Card View`: true

## Remaining Notes

- The visualization/result build is still slow for PERK, around 40 seconds in the checked live app path. That is not fixed by this migration.
- Alembic emits a SQLAlchemy warning about the known FK cycle between `indirect_chains` and `interactions` during autogenerate checks. The check still completes successfully.
- The React SPA remains intentionally parked behind `?spa=1` until its card view and modal behavior are repaired.
