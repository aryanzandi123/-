"""Close deferred drift: server defaults, duplicate indexes, updated_at align.

Revision ID: 20260503_0006
Revises: 20260430_0005
Create Date: 2026-05-03

Closes the three classes of drift deferred from migration 20260430_0005:

* Class C — 11 server-default mismatches. Live columns lack the DEFAULT
  clause that ``models.py`` already declares Python-side via ``default=``;
  any non-ORM writer (raw psql, ad-hoc SQL) that omits these columns
  currently fails. This aligns the DB with every Python default we already
  declare so the two are no longer drifting.

* Audit A3 — 4 auto-named ``ix_*`` indexes that mirror manually-named
  ``idx_*`` indexes on the same columns. The model historically declared
  both ``index=True`` on FK columns AND ``db.Index(...)`` in
  ``__table_args__``, so SQLAlchemy created two B-trees per column. Drops
  the redundant ``ix_*`` half; the ``idx_*`` survivor stays. Companion
  edits in ``models.py`` remove the now-unused ``index=True`` flags so
  ``db.create_all()`` (used in tests) doesn't recreate the duplicates.

* ``indirect_chains.updated_at`` — was nullable + no default on live, but
  the model declares NOT NULL with ``default=_utcnow`` and
  ``onupdate=_utcnow``. Sets the server default to ``now()`` and tightens
  to NOT NULL. Probe on 2026-04-30 confirmed the table is empty, so no
  row backfill is needed; we still issue an UPDATE … WHERE IS NULL for
  belt-and-braces in case rows landed since the probe.

Idempotency: every operation guards via ``IF EXISTS`` / ``IF NOT EXISTS``
or no-op-on-already-set semantics. Re-running after partial failure is
safe.
"""
from __future__ import annotations

from alembic import op


revision = "20260503_0006"
down_revision = "20260430_0005"
branch_labels = None
depends_on = None


# (table, column, default_sql_expr) tuples. Every entry mirrors a Python
# ``default=`` already declared in ``models.py`` — this revision just
# pushes the same value down to the live column DEFAULT clause.
_SERVER_DEFAULTS = (
    ("proteins",             "query_count",           "0"),
    ("proteins",             "total_interactions",    "0"),
    ("protein_aliases",      "source",                "'curated'"),
    ("interactions",         "discovery_method",      "'pipeline'"),
    ("pathway_interactions", "assignment_confidence", "0.80"),
    ("pathway_interactions", "assignment_method",     "'ai_pipeline'"),
    ("pathway_parents",      "relationship_type",     "'is_a'"),
    ("pathway_parents",      "confidence",            "1.0"),
    ("pathways",             "ai_generated",          "true"),
    ("pathways",             "usage_count",           "0"),
)


# Auto-named single-column ``ix_*`` indexes that exactly duplicate a
# manually-named ``idx_*`` index already declared in ``__table_args__``.
# Drop these from the DB; the canonical ``idx_*`` versions remain and
# serve every query the auto ones did.
_DUPLICATE_INDEXES = (
    "ix_interaction_claims_interaction_id",   # = idx_claims_interaction
    "ix_interaction_claims_pathway_id",       # = idx_claims_pathway
    "ix_pathway_parents_child_pathway_id",    # = idx_pathway_parents_child
    "ix_pathway_parents_parent_pathway_id",   # = idx_pathway_parents_parent
)


def upgrade() -> None:
    # ── 1. Add 11 server defaults (Class C) ────────────────────────────
    for table, column, default_expr in _SERVER_DEFAULTS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"SET DEFAULT {default_expr}"
        )

    # ── 2. indirect_chains.updated_at: default + NOT NULL ──────────────
    # Belt-and-braces UPDATE in case any row landed between the probe
    # and this run. The probe (2026-04-30) found 0 rows.
    op.execute(
        "UPDATE indirect_chains SET updated_at = now() "
        "WHERE updated_at IS NULL"
    )
    op.execute(
        "ALTER TABLE indirect_chains ALTER COLUMN updated_at "
        "SET DEFAULT now()"
    )
    op.execute(
        "ALTER TABLE indirect_chains ALTER COLUMN updated_at SET NOT NULL"
    )

    # ── 3. Drop 4 duplicate ix_* indexes (Audit A3) ────────────────────
    for index_name in _DUPLICATE_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")


def downgrade() -> None:
    # Reverse order — recreate dropped indexes first.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_interaction_claims_interaction_id "
        "ON interaction_claims USING btree (interaction_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_interaction_claims_pathway_id "
        "ON interaction_claims USING btree (pathway_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pathway_parents_child_pathway_id "
        "ON pathway_parents USING btree (child_pathway_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pathway_parents_parent_pathway_id "
        "ON pathway_parents USING btree (parent_pathway_id)"
    )

    # Restore indirect_chains.updated_at to its pre-upgrade shape.
    op.execute(
        "ALTER TABLE indirect_chains ALTER COLUMN updated_at DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE indirect_chains ALTER COLUMN updated_at DROP DEFAULT"
    )

    # Drop the 11 server defaults.
    for table, column, _default_expr in _SERVER_DEFAULTS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT"
        )
