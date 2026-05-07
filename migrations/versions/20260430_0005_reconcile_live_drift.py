"""Reconcile live-DB drift with the locally-saved models.

Revision ID: 20260430_0005
Revises: 20260429_0004
Create Date: 2026-04-30

Background
----------
A schema-only dump on 2026-04-30 plus a SELECT-only data probe revealed
three classes of drift between the live Postgres and ``models.py``:

  Class A — uniqueness shape disagreements:
    * ``interaction_claims`` had two parallel 5-col COALESCE unique indexes
      (``uq_claim_natural`` + ``uq_claim_interaction_fn_pw_ctx_chain``)
      plus the legacy partial ``uq_claim_fn_null_pw_ctx`` and the orphan
      partial ``uq_one_overarching_per_chain``. The model declared a single
      4-col index that was nowhere on live. The 5-col rule (with
      ``chain_id`` as the 5th key column) is the biologically correct
      shape. Drop the redundants and rename the survivor to the canonical
      model name.
    * ``interactions`` had ``interaction_type_unique (a_id, b_id, direction, arrow)``
      while the model declared ``interaction_unique (a_id, b_id)``. The
      runtime audit confirmed every writer enforces canonical 1-row-per-pair
      via ``_save_interaction``'s pair-only ``.first()`` lookup. The live
      4-col is strictly looser than what the runtime emits, so we tighten
      to match the model.

  Class B — 14 drift columns and 4 dependent indexes that the runtime never
  touches and that the data probe confirmed empty (zero populated rows
  across all 14). Drop them.

  Class C — ``interactions.direction`` had ``DEFAULT 'bidirectional' NOT NULL``
  with no CHECK constraint. The legacy ``migrate_kill_bidirectional.py``
  removed values but never touched the default. The runtime audit
  confirmed every Interaction writer sets direction explicitly
  (``utils/db_sync.py:_save_interaction`` falls back to ``'a_to_b'``,
  never to NULL or 'bidirectional'; ``utils/direction.py:infer_direction_from_arrow``
  never returns 'bidirectional'). Dropping the default and adding the
  ``no_bidirectional_direction`` CHECK is therefore safe.

Idempotency
-----------
Every operation is wrapped in ``IF EXISTS`` / ``IF NOT EXISTS`` guards
or guarded by introspection of ``pg_indexes`` / ``pg_constraint`` so
the revision can be re-run safely after a partial failure.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260430_0005"
down_revision = "20260429_0004"
branch_labels = None
depends_on = None


# Drift columns to drop — confirmed empty by the 2026-04-30 data probe.
_DRIFT_COLUMNS = (
    ("interaction_claims", "chain_position"),
    ("interaction_claims", "chain_role"),
    ("interaction_claims", "pathway_ids"),
    ("interaction_claims", "is_chain_overarching"),
    ("pathways",          "normalized_name"),
    ("pathways",          "depth"),
    ("pathways",          "interaction_count"),
    ("pathways",          "direct_interaction_count"),
    ("pathways",          "hierarchy_locked"),
    ("pathways",          "pathway_type"),
    ("pathways",          "hierarchy_chain"),
    ("pathway_parents",   "is_primary_chain"),
    ("indirect_chains",   "query_position"),
    ("indirect_chains",   "max_depth_at_discovery"),
)


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. Reconcile interaction_claims uniqueness ─────────────────────
    # Drop the redundant 5-col twin and the legacy partials. Then rename
    # the canonical 5-col survivor to the model's expected index name.
    op.execute("DROP INDEX IF EXISTS uq_claim_interaction_fn_pw_ctx_chain")
    op.execute("DROP INDEX IF EXISTS uq_claim_fn_null_pw_ctx")
    op.execute("DROP INDEX IF EXISTS uq_one_overarching_per_chain")

    src_exists = bind.execute(sa.text(
        "SELECT 1 FROM pg_indexes WHERE schemaname='public' "
        "AND indexname='uq_claim_natural'"
    )).scalar()
    dst_exists = bind.execute(sa.text(
        "SELECT 1 FROM pg_indexes WHERE schemaname='public' "
        "AND indexname='uq_claim_interaction_fn_pw_ctx'"
    )).scalar()
    if src_exists and not dst_exists:
        op.execute(
            "ALTER INDEX uq_claim_natural "
            "RENAME TO uq_claim_interaction_fn_pw_ctx"
        )
    elif not src_exists and not dst_exists:
        # Neither survivor exists — recreate canonical from scratch.
        op.execute(
            "CREATE UNIQUE INDEX uq_claim_interaction_fn_pw_ctx "
            "ON public.interaction_claims ("
            "  interaction_id, function_name, "
            "  COALESCE(pathway_name, ''::varchar), "
            "  COALESCE(function_context, ''::varchar), "
            "  COALESCE(chain_id, 0)"
            ")"
        )
    # If both exist (shouldn't happen post-drop), leave the canonical and
    # let the explicit DROP IF EXISTS above clean up the twin.

    # ── 2. Drop the 14 inert drift columns ─────────────────────────────
    # Drop dependent indexes first to be explicit across PG versions.
    op.execute("DROP INDEX IF EXISTS idx_claims_pathway_ids_gin")
    op.execute("DROP INDEX IF EXISTS idx_pathways_depth")
    op.execute("DROP INDEX IF EXISTS idx_pathways_normalized_name")
    for table, column in _DRIFT_COLUMNS:
        op.execute(f'ALTER TABLE {table} DROP COLUMN IF EXISTS "{column}"')

    # ── 3. Reconcile interactions uniqueness (4-col → 2-col) ──────────
    # Audit confirmed runtime expects pair-only uniqueness (canonical
    # ordering, single .first() lookup, single update path). Live table
    # has zero rows; replacement is a pure DDL change.
    op.execute(
        "ALTER TABLE interactions DROP CONSTRAINT IF EXISTS interaction_type_unique"
    )
    iu_exists = bind.execute(sa.text(
        "SELECT 1 FROM pg_constraint c JOIN pg_class t ON c.conrelid=t.oid "
        "WHERE t.relname='interactions' AND c.conname='interaction_unique'"
    )).scalar()
    if not iu_exists:
        op.execute(
            "ALTER TABLE interactions ADD CONSTRAINT interaction_unique "
            "UNIQUE (protein_a_id, protein_b_id)"
        )

    # ── 4. Reconcile interactions.direction default + CHECK ──────────
    # Audit A2: every writer sets direction explicitly; dropping the
    # default cannot break any current code path.
    op.execute("ALTER TABLE interactions ALTER COLUMN direction DROP DEFAULT")
    nbd_exists = bind.execute(sa.text(
        "SELECT 1 FROM pg_constraint c JOIN pg_class t ON c.conrelid=t.oid "
        "WHERE t.relname='interactions' AND c.conname='no_bidirectional_direction'"
    )).scalar()
    if not nbd_exists:
        op.execute(
            "ALTER TABLE interactions ADD CONSTRAINT no_bidirectional_direction "
            "CHECK (direction IN "
            "('a_to_b','b_to_a','main_to_primary','primary_to_main'))"
        )


def downgrade() -> None:
    # ── 4. Restore the (wrong) default and drop the CHECK ─────────────
    op.execute(
        "ALTER TABLE interactions DROP CONSTRAINT IF EXISTS no_bidirectional_direction"
    )
    op.execute(
        "ALTER TABLE interactions ALTER COLUMN direction "
        "SET DEFAULT 'bidirectional'"
    )

    # ── 3. Restore interaction_type_unique (4-col), drop interaction_unique ─
    op.execute(
        "ALTER TABLE interactions DROP CONSTRAINT IF EXISTS interaction_unique"
    )
    op.execute(
        "ALTER TABLE interactions ADD CONSTRAINT interaction_type_unique "
        "UNIQUE (protein_a_id, protein_b_id, direction, arrow)"
    )

    # ── 2. Re-add drift columns with their original defaults ──────────
    op.execute(
        "ALTER TABLE interaction_claims ADD COLUMN IF NOT EXISTS "
        "chain_position integer"
    )
    op.execute(
        "ALTER TABLE interaction_claims ADD COLUMN IF NOT EXISTS "
        "chain_role varchar(20)"
    )
    op.execute(
        "ALTER TABLE interaction_claims ADD COLUMN IF NOT EXISTS "
        "pathway_ids jsonb DEFAULT '[]'::jsonb NOT NULL"
    )
    op.execute(
        "ALTER TABLE interaction_claims ADD COLUMN IF NOT EXISTS "
        "is_chain_overarching boolean DEFAULT false NOT NULL"
    )
    op.execute(
        "ALTER TABLE pathways ADD COLUMN IF NOT EXISTS "
        "normalized_name varchar(200)"
    )
    op.execute(
        "ALTER TABLE pathways ADD COLUMN IF NOT EXISTS "
        "depth integer DEFAULT 0 NOT NULL"
    )
    op.execute(
        "ALTER TABLE pathways ADD COLUMN IF NOT EXISTS "
        "interaction_count integer DEFAULT 0 NOT NULL"
    )
    op.execute(
        "ALTER TABLE pathways ADD COLUMN IF NOT EXISTS "
        "direct_interaction_count integer DEFAULT 0 NOT NULL"
    )
    op.execute(
        "ALTER TABLE pathways ADD COLUMN IF NOT EXISTS "
        "hierarchy_locked boolean DEFAULT false NOT NULL"
    )
    op.execute(
        "ALTER TABLE pathways ADD COLUMN IF NOT EXISTS "
        "pathway_type varchar(20) DEFAULT 'main' NOT NULL"
    )
    op.execute(
        "ALTER TABLE pathways ADD COLUMN IF NOT EXISTS hierarchy_chain jsonb"
    )
    op.execute(
        "ALTER TABLE pathway_parents ADD COLUMN IF NOT EXISTS "
        "is_primary_chain boolean DEFAULT true NOT NULL"
    )
    op.execute(
        "ALTER TABLE indirect_chains ADD COLUMN IF NOT EXISTS "
        "query_position integer"
    )
    op.execute(
        "ALTER TABLE indirect_chains ADD COLUMN IF NOT EXISTS "
        "max_depth_at_discovery integer"
    )
    # Re-create the dropped indexes on the restored columns.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_claims_pathway_ids_gin "
        "ON interaction_claims USING gin (pathway_ids)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pathways_depth "
        "ON pathways USING btree (depth)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pathways_normalized_name "
        "ON pathways USING btree (normalized_name)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_overarching_per_chain "
        "ON interaction_claims USING btree (chain_id) "
        "WHERE ((is_chain_overarching = true) AND (chain_id IS NOT NULL))"
    )

    # ── 1. Restore the redundant claim-uniqueness shape ───────────────
    op.execute(
        "ALTER INDEX IF EXISTS uq_claim_interaction_fn_pw_ctx "
        "RENAME TO uq_claim_natural"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_interaction_fn_pw_ctx_chain "
        "ON interaction_claims USING btree ("
        "  interaction_id, function_name, "
        "  COALESCE(pathway_name, ''::character varying), "
        "  COALESCE(function_context, ''::character varying), "
        "  COALESCE((chain_id)::text, ''::text)"
        ")"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_fn_null_pw_ctx "
        "ON interaction_claims USING btree ("
        "  interaction_id, function_name, function_context"
        ") WHERE (pathway_name IS NULL)"
    )
