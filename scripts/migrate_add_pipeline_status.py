#!/usr/bin/env python3
"""
Migration: Add Pipeline Status Columns to Proteins Table

Adds the following columns to the proteins table:
- pipeline_status (VARCHAR(20) DEFAULT 'idle') — tracks pipeline lifecycle
- last_pipeline_phase (VARCHAR(50)) — tracks last completed phase

Also adds an index on pipeline_status for fast filtering.

Run: python scripts/migrate_add_pipeline_status.py
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import app, db
from sqlalchemy import text


def migrate():
    with app.app_context():
        conn = db.engine.connect()
        tx = conn.begin()
        try:
            # Check if column already exists
            result = conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'proteins' AND column_name = 'pipeline_status'"
            ))
            if result.fetchone():
                print("Column 'pipeline_status' already exists — skipping.")
                tx.rollback()
                return

            # Add columns
            conn.execute(text(
                "ALTER TABLE proteins "
                "ADD COLUMN pipeline_status VARCHAR(20) DEFAULT 'idle'"
            ))
            conn.execute(text(
                "ALTER TABLE proteins "
                "ADD COLUMN last_pipeline_phase VARCHAR(50)"
            ))

            # Add index
            conn.execute(text(
                "CREATE INDEX ix_proteins_pipeline_status "
                "ON proteins (pipeline_status)"
            ))

            # Set existing rows to 'complete' (they already have full data)
            conn.execute(text(
                "UPDATE proteins SET pipeline_status = 'complete' "
                "WHERE pipeline_status = 'idle' OR pipeline_status IS NULL"
            ))

            tx.commit()
            print("Migration complete: added pipeline_status and last_pipeline_phase columns.")
            print("  - Existing proteins set to pipeline_status='complete'.")

        except Exception as e:
            tx.rollback()
            print(f"Migration failed: {e}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    migrate()
