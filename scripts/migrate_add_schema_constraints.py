#!/usr/bin/env python3
"""Add missing CHECK constraints and indexes to existing databases.

Covers:
  - S1: no_bidirectional_direction CHECK on interactions.direction
  - S4b: valid_confidence_range CHECK on interactions.confidence
         and interaction_claims.confidence
  - S4c: idx_claims_chain_pathway composite index

All operations are idempotent — re-running on a DB that already has
these constraints is a no-op (catches and skips duplicate errors).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def run_migration():
    from app import app
    from models import db
    from sqlalchemy import text

    with app.app_context():
        dialect = db.engine.dialect.name

        constraints = [
            (
                "no_bidirectional_direction",
                "interactions",
                "direction IS NULL OR direction IN ('a_to_b', 'b_to_a', 'main_to_primary', 'primary_to_main')",
            ),
            (
                "valid_confidence_range",
                "interactions",
                "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            ),
            (
                "valid_claim_confidence_range",
                "interaction_claims",
                "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            ),
        ]

        for name, table, expr in constraints:
            try:
                if dialect == "sqlite":
                    print(f"[MIGRATE] SQLite: CHECK constraint '{name}' on '{table}' — skipped (SQLite doesn't support ALTER TABLE ADD CONSTRAINT)")
                else:
                    db.session.execute(text(
                        f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expr})"
                    ))
                    db.session.commit()
                    print(f"[MIGRATE] Added CHECK constraint '{name}' on '{table}'")
            except Exception as e:
                db.session.rollback()
                if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                    print(f"[MIGRATE] CHECK constraint '{name}' already exists — skipping")
                else:
                    print(f"[MIGRATE] Failed to add '{name}': {e}")

        # Composite index
        try:
            if dialect == "sqlite":
                db.session.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_claims_chain_pathway ON interaction_claims (chain_id, pathway_id)"
                ))
            else:
                db.session.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_claims_chain_pathway ON interaction_claims (chain_id, pathway_id)"
                ))
            db.session.commit()
            print("[MIGRATE] Added composite index idx_claims_chain_pathway")
        except Exception as e:
            db.session.rollback()
            if "already exists" in str(e).lower():
                print("[MIGRATE] Index idx_claims_chain_pathway already exists — skipping")
            else:
                print(f"[MIGRATE] Failed to add index: {e}")

        print("[MIGRATE] Schema constraints migration complete.")


if __name__ == "__main__":
    run_migration()
