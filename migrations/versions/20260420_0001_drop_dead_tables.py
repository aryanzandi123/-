"""drop 7 dead tables that were never wired to ORM classes or readers.

Revision ID: 20260420_0001
Revises: 000000000000
Create Date: 2026-04-20

The following tables carry zero rows, zero writers, and zero readers across
the codebase as of the persistence-layer audit on 2026-04-20:

  - interaction_chains           (legacy parallel to indirect_chains)
  - interaction_pathways         (legacy parallel to pathway_interactions)
  - interaction_query_hits       (tracking table, never wired)
  - pathway_canonical_names      (pre-quick_assign normalization)
  - pathway_hierarchy            (legacy parallel to pathway_parents)
  - pathway_hierarchy_history    (audit scaffold, never populated)
  - pathway_initial_assignments  (pre-quick_assign assignment log)

Dropping them frees operators from defensive workarounds in
``scripts/clear_pathway_tables.py`` (which currently issues DELETE against
each for safety) and lets us squash the schema to only the tables the app
actually touches.

Safe because every one is verified empty by the audit query:
    SELECT COUNT(*) FROM <table>  →  0
and no Python module imports an ORM class for any of them (``models.py``
has no corresponding class).

The downgrade path is intentionally a no-op: we cannot reconstruct schema
that no code ever created, and recreating empty shells would just re-enter
the same dead state we're cleaning up.
"""
from __future__ import annotations

from alembic import op


# Alembic revision identifiers
revision = "20260420_0001"
down_revision = "000000000000"
branch_labels = None
depends_on = None


DEAD_TABLES = (
    "interaction_chains",
    "interaction_pathways",
    "interaction_query_hits",
    "pathway_canonical_names",
    "pathway_hierarchy",
    "pathway_hierarchy_history",
    "pathway_initial_assignments",
)


def upgrade() -> None:
    """Drop each dead table with ``IF EXISTS CASCADE`` so the migration is
    idempotent across environments where some of these tables may have
    already been hand-dropped via the legacy one-shot ``scripts/migrate_*``
    helpers.
    """
    for table_name in DEAD_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE')


def downgrade() -> None:
    """No-op — these tables were never live. See module docstring."""
