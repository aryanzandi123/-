#!/usr/bin/env python3
"""One-time schema migration for existing PostgreSQL databases.

Adds new indexes and CHECK constraints from recent model changes.
Safe to re-run — all operations check before acting.

Usage:
    python3 scripts/migrate_schema.py
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def migrate():
    with app.app_context():
        print("=" * 60)
        print("[MIGRATE] Starting schema migration...")
        print("=" * 60)

        conn = db.session.connection()
        changes = 0

        # --- G2: Composite index on (protein_a_id, protein_b_id) ---
        result = conn.execute(db.text(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_interaction_pair_lookup'"
        )).fetchone()
        if not result:
            print("[MIGRATE] Adding index idx_interaction_pair_lookup...")
            conn.execute(db.text(
                "CREATE INDEX idx_interaction_pair_lookup ON interactions (protein_a_id, protein_b_id)"
            ))
            changes += 1
            print("[MIGRATE]   OK")
        else:
            print("[MIGRATE] Index idx_interaction_pair_lookup already exists — skipping")

        # --- G4: CHECK constraint for function_context ---
        result = conn.execute(db.text(
            "SELECT 1 FROM pg_constraint WHERE conname = 'valid_function_context'"
        )).fetchone()
        if not result:
            # First fix any invalid values
            fixed = conn.execute(db.text(
                "UPDATE interactions SET function_context = NULL "
                "WHERE function_context IS NOT NULL "
                "AND function_context NOT IN ('direct', 'net', 'chain_derived', 'mixed')"
            ))
            if fixed.rowcount:
                print(f"[MIGRATE] Fixed {fixed.rowcount} rows with invalid function_context")

            print("[MIGRATE] Adding CHECK constraint valid_function_context...")
            conn.execute(db.text(
                "ALTER TABLE interactions ADD CONSTRAINT valid_function_context "
                "CHECK (function_context IS NULL OR function_context IN ('direct', 'net', 'chain_derived', 'mixed'))"
            ))
            changes += 1
            print("[MIGRATE]   OK")
        else:
            print("[MIGRATE] Constraint valid_function_context already exists — skipping")

        # --- G4: CHECK constraint for interaction_type ---
        result = conn.execute(db.text(
            "SELECT 1 FROM pg_constraint WHERE conname = 'valid_interaction_type'"
        )).fetchone()
        if not result:
            # First fix any invalid values
            fixed = conn.execute(db.text(
                "UPDATE interactions SET interaction_type = 'direct' "
                "WHERE interaction_type IS NOT NULL "
                "AND interaction_type NOT IN ('direct', 'indirect')"
            ))
            if fixed.rowcount:
                print(f"[MIGRATE] Fixed {fixed.rowcount} rows with invalid interaction_type")

            print("[MIGRATE] Adding CHECK constraint valid_interaction_type...")
            conn.execute(db.text(
                "ALTER TABLE interactions ADD CONSTRAINT valid_interaction_type "
                "CHECK (interaction_type IS NULL OR interaction_type IN ('direct', 'indirect'))"
            ))
            changes += 1
            print("[MIGRATE]   OK")
        else:
            print("[MIGRATE] Constraint valid_interaction_type already exists — skipping")

        db.session.commit()

        # --- Summary ---
        print("=" * 60)
        if changes:
            print(f"[MIGRATE] Done — {changes} change(s) applied.")
        else:
            print("[MIGRATE] Done — database already up to date.")
        print("=" * 60)


if __name__ == "__main__":
    migrate()
