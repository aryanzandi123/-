"""interaction.data NOT NULL with server_default '{}'.

Revision ID: 20260429_0003
Revises: 20260429_0002
Create Date: 2026-04-29

Models declared ``Interaction.data`` ``nullable=True`` (S4d deprecation
of the JSONB blob), but every reader still calls ``interaction.data.copy()``
/ ``interaction.data.get(...)`` without null guards. With nullable=True
the first writer to skip the blob crashes every read.

Reverting: ``data`` is now ``NOT NULL`` with a JSONB ``'{}'`` default.
The blob stays as a stable bag for fields that don't have first-class
columns; writers may pass ``data={}`` (or omit, the default fills in)
and readers stop needing null guards.

Backfill: any rows that already carry NULL get coerced to ``'{}'``
before the constraint is added.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260429_0003"
down_revision = "20260429_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Backfill NULL → '{}' so the NOT NULL add doesn't fail.
    op.execute("UPDATE interactions SET data = '{}'::jsonb WHERE data IS NULL")
    # 2) Add server default + NOT NULL.
    op.alter_column(
        "interactions",
        "data",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )


def downgrade() -> None:
    op.alter_column(
        "interactions",
        "data",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=True,
        server_default=None,
    )
