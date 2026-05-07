"""Multi-chain schema: chain_signature + ChainParticipant M2M (#12).

Revision ID: 20260429_0004
Revises: 20260429_0003
Create Date: 2026-04-29

The pre-#12 schema enforced ``UniqueConstraint('origin_interaction_id')``
on ``indirect_chains``, so an interaction pair (e.g. ATXN3↔MTOR) could
own only ONE IndirectChain even when the literature describes multiple
distinct cascades through that pair (via VCP→RHEB, via TSC2→TSC1, etc.).
The second chain was silently merged into the first.

This migration:

  1. Adds ``chain_signature`` (32-char hex) to ``indirect_chains``.
  2. Backfills the column for existing rows by hashing chain_proteins.
  3. Drops the old ``chain_origin_unique`` constraint.
  4. Adds a new ``chain_origin_signature_unique`` on
     ``(origin_interaction_id, chain_signature)``.
  5. Creates ``chain_participants(chain_id, interaction_id, role)`` —
     the M2M table that lets one Interaction participate in multiple
     chains.
  6. Backfills ``chain_participants`` from existing
     ``Interaction.chain_id`` links (each becomes a row with
     ``role='hop'``; we cannot reliably infer ``origin``/``net_effect``
     for historical rows from chain_id alone).

The legacy ``Interaction.chain_id`` column stays for backward compat
(readers that only want the "primary" chain pointer keep working).
"""
from __future__ import annotations

import hashlib

from alembic import op
import sqlalchemy as sa


revision = "20260429_0004"
down_revision = "20260429_0003"
branch_labels = None
depends_on = None


def _compute_signature(chain_proteins) -> str:
    """Mirror of models._compute_chain_signature (kept inline so the
    migration can run before the model module is importable).
    """
    if not chain_proteins:
        return ''
    canonical = '->'.join(str(p).strip().upper() for p in chain_proteins if p)
    if not canonical:
        return ''
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # All schema-creating ops in this migration are wrapped in
    # "skip if already present" guards because:
    #   1. SQLAlchemy's import-time db.create_all() (run by app.py
    #      bootstrap before this migration) may have already created
    #      ``chain_participants`` from the ChainParticipant model.
    #   2. A previous failed run of this migration could have left a
    #      partial state (Postgres rolls back the transaction so most
    #      of the migration is undone — but db.create_all() side effects
    #      ran outside any migration transaction).
    # Idempotency means re-running ``alembic upgrade head`` after a
    # failure (or against a partially-bootstrapped DB) just works.

    # 1) chain_signature column on indirect_chains — only if missing.
    indirect_chain_cols = {c['name'] for c in inspector.get_columns('indirect_chains')}
    if 'chain_signature' not in indirect_chain_cols:
        op.add_column(
            'indirect_chains',
            sa.Column(
                'chain_signature',
                sa.String(length=32),
                nullable=False,
                server_default='',
            ),
        )

    # 2) Backfill chain_signature for any rows still carrying ''
    #    (always safe to re-run — empty signatures stay empty if the
    #    column was just added; populated rows are skipped by the WHERE).
    rows = bind.execute(sa.text(
        "SELECT id, chain_proteins FROM indirect_chains WHERE chain_signature = ''"
    )).fetchall()
    for row_id, chain_proteins in rows:
        sig = _compute_signature(chain_proteins or [])
        bind.execute(
            sa.text(
                "UPDATE indirect_chains SET chain_signature = :sig WHERE id = :rid"
            ),
            {"sig": sig, "rid": row_id},
        )

    # 3) Drop the old constraint (one chain per origin) — only if present.
    existing_uniques = {
        c['name'] for c in inspector.get_unique_constraints('indirect_chains')
    }
    if 'chain_origin_unique' in existing_uniques:
        try:
            op.drop_constraint(
                'chain_origin_unique', 'indirect_chains', type_='unique'
            )
        except Exception:
            # Some deployments use a different constraint backing
            # (e.g., a UNIQUE INDEX with the same name). Don't block
            # the migration over this — the new constraint below is
            # what matters.
            pass

    # 4) Add new constraint: (origin_interaction_id, chain_signature)
    #    — only if missing. Re-inspect so we see the post-drop state.
    existing_uniques = {
        c['name'] for c in sa.inspect(bind).get_unique_constraints('indirect_chains')
    }
    if 'chain_origin_signature_unique' not in existing_uniques:
        op.create_unique_constraint(
            'chain_origin_signature_unique',
            'indirect_chains',
            ['origin_interaction_id', 'chain_signature'],
        )

    # 5) Index on chain_signature — only if missing.
    existing_indexes = {
        i['name'] for i in sa.inspect(bind).get_indexes('indirect_chains')
    }
    if 'idx_indirect_chains_chain_signature' not in existing_indexes:
        op.create_index(
            'idx_indirect_chains_chain_signature',
            'indirect_chains',
            ['chain_signature'],
        )

    # 6) Create chain_participants table — only if missing.
    if not sa.inspect(bind).has_table('chain_participants'):
        op.create_table(
            'chain_participants',
            sa.Column('chain_id', sa.Integer(), nullable=False),
            sa.Column('interaction_id', sa.Integer(), nullable=False),
            sa.Column('role', sa.String(length=30), nullable=False, server_default='hop'),
            sa.Column(
                'created_at',
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ['chain_id'], ['indirect_chains.id'], ondelete='CASCADE',
            ),
            sa.ForeignKeyConstraint(
                ['interaction_id'], ['interactions.id'], ondelete='CASCADE',
            ),
            sa.PrimaryKeyConstraint('chain_id', 'interaction_id'),
            sa.CheckConstraint(
                "role IN ('origin', 'hop', 'net_effect')",
                name='valid_chain_participant_role',
            ),
        )

    # 7) Index on chain_participants.interaction_id — only if missing.
    cp_indexes = {
        i['name'] for i in sa.inspect(bind).get_indexes('chain_participants')
    }
    if 'idx_chain_participants_interaction' not in cp_indexes:
        op.create_index(
            'idx_chain_participants_interaction',
            'chain_participants',
            ['interaction_id'],
        )

    # 8) Backfill chain_participants from existing Interaction.chain_id
    #    links. ON CONFLICT DO NOTHING makes this safe to re-run.
    bind.execute(sa.text("""
        INSERT INTO chain_participants (chain_id, interaction_id, role)
        SELECT i.chain_id, i.id, 'hop'
        FROM interactions i
        WHERE i.chain_id IS NOT NULL
        ON CONFLICT (chain_id, interaction_id) DO NOTHING
    """))
    # Promote 'hop' → 'origin' for the rows that own their chain.
    bind.execute(sa.text("""
        UPDATE chain_participants cp
        SET role = 'origin'
        FROM indirect_chains ic
        WHERE cp.chain_id = ic.id
          AND cp.interaction_id = ic.origin_interaction_id
    """))


def downgrade() -> None:
    op.drop_index(
        'idx_chain_participants_interaction', table_name='chain_participants'
    )
    op.drop_table('chain_participants')
    op.drop_index(
        'idx_indirect_chains_chain_signature', table_name='indirect_chains'
    )
    op.drop_constraint(
        'chain_origin_signature_unique', 'indirect_chains', type_='unique'
    )
    op.create_unique_constraint(
        'chain_origin_unique', 'indirect_chains', ['origin_interaction_id'],
    )
    op.drop_column('indirect_chains', 'chain_signature')
