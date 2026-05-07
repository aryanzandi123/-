"""Dedupe existing interaction_claims rows and add composite unique constraint.

Background:
    Commit a945a8c removed the earlier unique constraints from interaction_claims
    because they collided with legitimate variation (function_context=direct vs
    net for the same function name). The replacement application-level dedup
    leaves the table open to drift under concurrency. This migration installs
    the correct composite key (interaction_id, function_name, pathway_name,
    function_context) — but first it must remove any existing rows that would
    violate the new constraint, otherwise the ALTER TABLE will abort.

Run:
    cd /path/to/project && python3 scripts/migrate_add_claim_dedup_constraint.py

Safe to re-run: idempotent checks are performed before each operation.
"""
import os
import sys

# Ensure project root is on sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

# The constraint and partial index names defined in models.py __table_args__
FULL_CONSTRAINT = "uq_claim_interaction_fn_pw_ctx"
PARTIAL_INDEX = "uq_claim_fn_null_pw_ctx"

# Old artifacts from pre-a945a8c schema that may still exist in the DB
LEGACY_CONSTRAINTS = [
    "claim_unique_per_function_pathway",
]
LEGACY_INDEXES = [
    "claim_unique_no_pathway",
]


def run_migration(app):
    """Run the migration inside a Flask app context."""
    from models import db

    with app.app_context():
        engine = db.session.get_bind()
        print(f"[MIGRATION] Connected to: {engine.url.render_as_string(hide_password=True)}")

        # --- STEP 1: drop any stale legacy constraints/indexes ---
        for name in LEGACY_CONSTRAINTS:
            db.session.execute(db.text(
                f"ALTER TABLE interaction_claims DROP CONSTRAINT IF EXISTS {name}"
            ))
            print(f"[MIGRATION] Dropped legacy constraint if present: {name}")
        for name in LEGACY_INDEXES:
            db.session.execute(db.text(f"DROP INDEX IF EXISTS {name}"))
            print(f"[MIGRATION] Dropped legacy index if present: {name}")
        db.session.commit()

        # --- STEP 2: count duplicates that would violate the new constraint ---
        dup_result = db.session.execute(db.text(
            """
            SELECT COUNT(*) FROM (
                SELECT 1
                FROM interaction_claims
                GROUP BY interaction_id, function_name, pathway_name, function_context
                HAVING COUNT(*) > 1
            ) AS dup_groups
            """
        )).scalar()
        print(f"[MIGRATION] Duplicate groups found: {dup_result}")

        if dup_result and dup_result > 0:
            # --- STEP 3: dedupe by keeping the richest row per group ---
            # "Richest" = row whose text fields have the greatest total length
            # (proxy for most populated evidence/mechanism/etc.)
            print("[MIGRATION] Deduping — keeping row with most populated fields per group")
            deleted = db.session.execute(db.text(
                """
                WITH ranked AS (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY interaction_id, function_name, pathway_name, function_context
                            ORDER BY
                                COALESCE(jsonb_array_length(evidence), 0) DESC,
                                COALESCE(LENGTH(mechanism), 0) DESC,
                                COALESCE(LENGTH(effect_description), 0) DESC,
                                updated_at DESC,
                                id ASC
                        ) AS rn
                    FROM interaction_claims
                )
                DELETE FROM interaction_claims
                WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
                RETURNING id
                """
            ))
            deleted_count = len(list(deleted))
            db.session.commit()
            print(f"[MIGRATION] Deleted {deleted_count} duplicate claim rows")
        else:
            print("[MIGRATION] No duplicates to clean up")

        # --- STEP 4: install the composite unique constraint ---
        # Skip if already present
        exists = db.session.execute(db.text(
            """
            SELECT 1 FROM pg_constraint
            WHERE conname = :name AND conrelid = 'interaction_claims'::regclass
            """
        ), {"name": FULL_CONSTRAINT}).first()
        if exists:
            print(f"[MIGRATION] Constraint {FULL_CONSTRAINT} already exists")
        else:
            db.session.execute(db.text(
                f"""
                ALTER TABLE interaction_claims
                ADD CONSTRAINT {FULL_CONSTRAINT}
                UNIQUE (interaction_id, function_name, pathway_name, function_context)
                """
            ))
            db.session.commit()
            print(f"[MIGRATION] Added constraint {FULL_CONSTRAINT}")

        # --- STEP 5: install the partial unique index for NULL pathway_name ---
        # This handles rows where pathway_name IS NULL (which PostgreSQL otherwise
        # treats as distinct from each other in the main unique constraint).
        db.session.execute(db.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {PARTIAL_INDEX}
            ON interaction_claims (interaction_id, function_name, function_context)
            WHERE pathway_name IS NULL
            """
        ))
        db.session.commit()
        print(f"[MIGRATION] Ensured partial index {PARTIAL_INDEX}")

        # --- STEP 6: verification — the new constraint should hold ---
        check = db.session.execute(db.text(
            """
            SELECT COUNT(*) FROM (
                SELECT 1
                FROM interaction_claims
                GROUP BY interaction_id, function_name, pathway_name, function_context
                HAVING COUNT(*) > 1
            ) AS dup_groups
            """
        )).scalar()
        if check == 0:
            print("[MIGRATION] ✓ Verification passed: no duplicates remain")
        else:
            print(f"[MIGRATION] ✗ Verification failed: {check} duplicate groups still present")
            sys.exit(1)


if __name__ == "__main__":
    from app import app
    run_migration(app)
