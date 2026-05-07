# Copy117 Path-Instance Visualization Plan

Date: 2026-05-07

## Summary

Legacy Card View should treat every displayed relationship as a path instance:

- Direct interaction: length-1 path.
- Chain-hop interaction: one ordered hop within a longer path.
- Net effect: query-to-terminal summary over a path.
- Aggregate node/modal: summary over many path instances.

The current code-only repair preserves clicked Card View path context into the
modal and fixes the immediate TDP43 directionality bug. The larger schema model
should be additive and gated later; it is scientifically useful but not required
to complete this legacy modal repair.

React v2 still waits. It should consume the stabilized legacy/API semantics,
not become the place where path semantics are invented.

## Current Truth

The live DB and migrations are current:

- Live DB revision: `20260504_0009`.
- Repo head: `20260504_0009`.
- `alembic check`: no new upgrade operations detected.
- No migration or backfill was needed for this repair.

TDP43 payload truth from `/api/results/TDP43`:

- 59 proteins, 196 interactions, 242 pathways.
- `direct_claim=57`, `chain_hop_claim=96`, `net_effect_claim=43`.
- Required read-side fields are present on TDP43 rows:
  `locus`, `is_net_effect`, `chain_members`, `chain_id`, `hop_index`,
  `chain_context_pathway`, `hop_local_pathway`, claim-level `locus`,
  claim-level `chain_id`, claim-level `source`, claim-level `target`, and
  claim-level `hop_index`.
- TDP43 has arbitrary-depth evidence in the payload: max `chain_members`
  length is 6 and max `hop_index` is 4.

## Code-Only Repair Now

The immediate implementation should remain frontend/read-side only:

- Preserve Card View click context:
  `_uid`, `_chainId`, `_chainPosition`, `_chainLength`, `_chainProteins`,
  pathway context, parent id, and visible relationship metadata.
- Select modal rows from clicked chain context first:
  `chain_id`, ordered hop pair, and `hop_index`.
- Only use aggregate protein lookup when the user clicked an aggregate/non-chain
  card.
- Keep chain-hop rows out of query-relative indirect perspective logic.
- Label net-effect and aggregate content explicitly, never as direct hop claims.

TDP43 acceptance after repair:

- Scoped Autophagy `TBK1` card opens `ULK1 -> TBK1`, `CHAIN-HOP CLAIMS`,
  `CHAIN HOP 1`, `ACTIVATES`, with the Ser172/TBK1 claim.
- Scoped Stress Granule Dynamics `DDX3X` card opens `GLE1 -> DDX3X`,
  `CHAIN-HOP CLAIMS`, `CHAIN HOP 2`, `ACTIVATES`, with the GLE1/DDX3X claim.
- Aggregate DDX3X modal shows both `GLE1 -> DDX3X` chain-hop and
  `TDP43 -> DDX3X` net-effect sections, clearly separated.
- Direct `TBK1 -> TDP43` remains a direct pair claim.

## Future Gated Schema

The durable source-of-truth model should be additive first:

- `pipeline_runs`
  - Stable provenance for a generated path/claim/evidence set.
  - Nullable run FKs on `interactions`, `indirect_chains`, and
    `interaction_claims`.
- `chain_hops` or `path_hops`
  - `chain_id` / `path_instance_id`
  - `hop_index`
  - `interaction_id`
  - ordered `source`, `target`
  - role: `direct`, `hop`, `net_effect`
  - optional pathway assignment and provenance JSON.
- `claim_chain_memberships` or `path_claims`
  - Links a claim to a specific path instance and hop position.
  - Avoids duplicating claims when one claim belongs to multiple chain
    occurrences.
- `claim_evidence`
  - Normalized PMID/evidence rows with assay/finding/provenance fields.
  - Keep existing JSONB mirrors until readers migrate.

Backup gate before any schema/data work:

```bash
pg_dump "$DATABASE_URL" -Fc -f "backup_$(date +%Y%m%d_%H%M%S)_pre_path_instance.dump"
```

Rollback plan:

- Restore the pre-migration dump if migration/backfill corrupts live semantics.
- Keep additive columns/tables nullable at first so code can roll back to the
  current read-side reconstruction without destructive downgrade pressure.

Validation plan:

- Alembic current/head/check before migration.
- SELECT-only audit of TDP43 chain rows, chain participants, claims, and
  pathway assignments.
- Backfill dry-run counts by chain id and hop count.
- `/api/results/TDP43` invariants:
  no missing `locus`, chain hops have `chain_id` and `hop_index`, net rows stay
  net, claims remain scoped to the visible row.
- Browser `/api/visualize/TDP43` checks for direct, chain-hop, and net-effect
  modals.

## Cache And Runtime Notes

Normal `.env` has `FLASK_DEBUG=false`, so static files and generated
visualization HTML can appear stale in a long-running process. For frontend
verification, restart the server after edits and force no static max-age. The
safe local pattern is to run with `SKIP_APP_BOOTSTRAP=1` and avoid startup
backfills or `db.create_all`.

## Next Slice

Recommended next implementation slice:

- Add a small browser/runtime regression harness for TDP43 Card View contexts if
  the project accepts browser automation files.
- Then handle P1 modal close accessibility and route/cache performance.
- Defer schema work until the user explicitly approves the backup-gated
  migration/backfill plan.
