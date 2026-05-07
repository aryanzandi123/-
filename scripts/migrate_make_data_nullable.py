#!/usr/bin/env python3
"""S4d Migration: Make Interaction.data column nullable.

The ``data`` JSONB column (50-100KB per row) stored the full pipeline
output before claims, arrows, chain_with_arrows, and chain state were
denormalized into proper columns. Now that all critical fields are in
their own columns, the blob is redundant for most reads.

This migration drops the NOT NULL constraint so new code paths can
skip writing the full blob. Historical rows keep their data untouched.

Idempotent: running on a column that's already nullable is a no-op.
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

        if dialect == "sqlite":
            print("[MIGRATE] SQLite: ALTER TABLE ... DROP NOT NULL not supported — skipping.")
            print("[MIGRATE] The models.py schema already declares nullable=True; SQLite will")
            print("[MIGRATE] use it on next db.create_all() (fresh DB or test DB).")
            return

        try:
            db.session.execute(text(
                "ALTER TABLE interactions ALTER COLUMN data DROP NOT NULL"
            ))
            db.session.commit()
            print("[MIGRATE] Interaction.data is now nullable.")
        except Exception as e:
            db.session.rollback()
            if "not a not-null" in str(e).lower() or "already" in str(e).lower():
                print("[MIGRATE] Interaction.data is already nullable — no change needed.")
            else:
                print(f"[MIGRATE] Failed: {e}")


if __name__ == "__main__":
    run_migration()
