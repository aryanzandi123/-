"""Add ``interactions.chain_id`` column and backfill from existing chain data.

Refactor #6: chains become first-class ``IndirectChain`` rows with every
participating interaction linking via a new ``chain_id`` FK, instead of
every interaction carrying its own full ``chain_context`` JSONB copy.

This migration:

  1. Adds the ``chain_id`` column to ``interactions`` (nullable,
     FK to ``indirect_chains.id`` with ``ON DELETE SET NULL``).
  2. Creates an index on the new column (``idx_interactions_chain_id``).
  3. Backfills ``chain_id`` for existing rows:
     - For every ``IndirectChain`` row, the ``origin_interaction_id``
       gets ``chain_id`` set to that chain.
     - Additionally, for every protein pair in
       ``IndirectChain.chain_proteins`` that has a matching Interaction
       row but no ``chain_id`` yet, link it to the same chain (this
       reconstructs "participant" links that were previously only
       expressed indirectly via ``chain_context`` JSONB).
  4. Is idempotent — safe to run repeatedly.

Usage: ``cd /path/to/project && python3 scripts/migrate_add_interaction_chain_id.py``
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)


def run_migration(app) -> None:
    from models import db

    with app.app_context():
        dialect = db.engine.dialect.name

        # Step 1: add the column. On PostgreSQL we can do this with a
        # single ALTER TABLE; on SQLite (used only in tests) db.create_all
        # handles it, so this step is a no-op there.
        if dialect == "postgresql":
            db.session.execute(db.text("""
                ALTER TABLE interactions
                ADD COLUMN IF NOT EXISTS chain_id INTEGER
            """))
            db.session.execute(db.text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.table_constraints
                        WHERE table_name = 'interactions'
                          AND constraint_name = 'interactions_chain_id_fkey'
                    ) THEN
                        ALTER TABLE interactions
                        ADD CONSTRAINT interactions_chain_id_fkey
                        FOREIGN KEY (chain_id)
                        REFERENCES indirect_chains(id)
                        ON DELETE SET NULL;
                    END IF;
                END
                $$;
            """))
            db.session.execute(db.text("""
                CREATE INDEX IF NOT EXISTS idx_interactions_chain_id
                ON interactions (chain_id)
            """))
            print("[MIGRATION] Added interactions.chain_id column + FK + index")
        else:
            # SQLite: create_all() covers new columns on an in-memory DB.
            # Nothing to do here.
            print(f"[MIGRATION] Dialect={dialect}, skipping DDL (handled by create_all)")

        # Step 2: backfill. Even on SQLite we can do this for
        # consistency in tests.
        from models import Interaction, IndirectChain, Protein

        # 2a. Set chain_id on every origin interaction.
        origin_updates = 0
        for chain_row in IndirectChain.query.all():
            origin = db.session.get(Interaction, chain_row.origin_interaction_id)
            if origin and origin.chain_id != chain_row.id:
                origin.chain_id = chain_row.id
                origin_updates += 1
        if origin_updates:
            print(
                f"[MIGRATION] Linked {origin_updates} origin interaction(s) "
                f"to their IndirectChain rows"
            )

        # 2b. For every chain, link every participating protein pair
        # (adjacent in the chain, OR query→target non-adjacent) whose
        # Interaction row already exists but has chain_id=NULL.
        participant_updates = 0
        for chain_row in IndirectChain.query.all():
            proteins = chain_row.chain_proteins or []
            if not isinstance(proteins, list) or len(proteins) < 2:
                continue
            # Resolve every symbol to its Protein.id up front.
            proteins_by_symbol = {
                p.symbol: p.id
                for p in Protein.query.filter(
                    Protein.symbol.in_([s for s in proteins if s])
                ).all()
            }

            def _link_pair(a_sym: str, b_sym: str) -> int:
                a_id = proteins_by_symbol.get(a_sym)
                b_id = proteins_by_symbol.get(b_sym)
                if not a_id or not b_id or a_id == b_id:
                    return 0
                canon_a, canon_b = min(a_id, b_id), max(a_id, b_id)
                inter = Interaction.query.filter_by(
                    protein_a_id=canon_a, protein_b_id=canon_b,
                ).first()
                if inter and inter.chain_id is None:
                    inter.chain_id = chain_row.id
                    return 1
                return 0

            # Adjacent hops
            for i in range(len(proteins) - 1):
                participant_updates += _link_pair(proteins[i], proteins[i + 1])
            # Full end-to-end (net effect) pair
            if len(proteins) >= 2:
                participant_updates += _link_pair(proteins[0], proteins[-1])

        if participant_updates:
            print(
                f"[MIGRATION] Linked {participant_updates} participating "
                f"interaction(s) to their IndirectChain rows"
            )

        db.session.commit()
        print("[MIGRATION] Done.")


if __name__ == "__main__":
    from app import app
    run_migration(app)
