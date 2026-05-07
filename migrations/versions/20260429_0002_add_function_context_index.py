"""add function_context indexes on interaction_claims and interactions.

Revision ID: 20260429_0002
Revises: 20260420_0001
Create Date: 2026-04-29

The data builder's chain-rendering path filters InteractionClaim by
``function_context = 'chain_derived'`` to assemble per-hop claims for
modal navigation. Without an index, that runs as a sequential scan of
the entire claims table — which is fine at 271 rows but quadratic in
queries-by-protein when the app surfaces many proteins per session.

A similar pattern exists on Interaction.function_context which the
unified storage path queries when computing chain-aware groupings.

Both indexes are non-unique B-trees over a low-cardinality enum
(direct/net/chain_derived/mixed). They are tiny on disk and pure win
for the scan-heavy queries.

CONCURRENTLY because we may have many rows already; the operation is
non-blocking and can be safely re-run (IF NOT EXISTS).
"""
from __future__ import annotations

from alembic import op


revision = "20260429_0002"
down_revision = "20260420_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Note: CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    # Alembic runs migrations in a transaction by default. Use
    # ``op.execute`` with autocommit_block to bypass.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_claims_function_context "
            "ON interaction_claims (function_context)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_interactions_function_context "
            "ON interactions (function_context)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_interactions_function_context"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_claims_function_context"
        )
