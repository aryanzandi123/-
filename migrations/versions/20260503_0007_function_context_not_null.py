"""function_context NOT NULL with server_default 'direct' on both tables.

Revision ID: 20260503_0007
Revises: 20260503_0006
Create Date: 2026-05-03

Models declared ``Interaction.function_context`` and
``InteractionClaim.function_context`` ``nullable=True`` (or unspecified,
which defaults to nullable). The 5-col COALESCE unique index on
``interaction_claims`` already collapses NULL to ``''`` for
deduplication purposes, but reads still need to special-case NULL —
and writers occasionally land NULL when the post-processor's
metadata-finalization stage skipped a claim.

Tightening to NOT NULL with ``server_default='direct'``:
  * Eliminates the special-case in readers.
  * Removes one degree of freedom from the unique-index COALESCE.
  * Forces writers to make a deliberate choice (``direct`` /
    ``net`` / ``chain_derived`` / ``mixed``) instead of falling back
    to NULL.

Backfill: every NULL row is coerced to ``'direct'`` before the
constraint is applied. ``'direct'`` is the safe semantic floor —
every interaction expresses pair-level biology by default; chain-
derived / net-effect rows have explicit upstream stamping.
"""
from __future__ import annotations

from alembic import op


revision = "20260503_0007"
down_revision = "20260503_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Backfill NULL → 'direct' on both tables.
    op.execute(
        "UPDATE interactions SET function_context = 'direct' "
        "WHERE function_context IS NULL"
    )
    op.execute(
        "UPDATE interaction_claims SET function_context = 'direct' "
        "WHERE function_context IS NULL"
    )

    # 2) Add server_default and NOT NULL constraint on both tables.
    op.execute(
        "ALTER TABLE interactions ALTER COLUMN function_context "
        "SET DEFAULT 'direct'"
    )
    op.execute(
        "ALTER TABLE interactions ALTER COLUMN function_context "
        "SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE interaction_claims ALTER COLUMN function_context "
        "SET DEFAULT 'direct'"
    )
    op.execute(
        "ALTER TABLE interaction_claims ALTER COLUMN function_context "
        "SET NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE interaction_claims ALTER COLUMN function_context "
        "DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE interaction_claims ALTER COLUMN function_context "
        "DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE interactions ALTER COLUMN function_context "
        "DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE interactions ALTER COLUMN function_context "
        "DROP DEFAULT"
    )
