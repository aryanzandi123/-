"""Replace the two interaction_claims unique constraints with a single
COALESCE-based functional unique index.

Prior state (as dropped/renamed by earlier migrations):
  - UNIQUE (interaction_id, function_name, pathway_name, function_context)
    named `uq_claim_interaction_fn_pw_ctx`
  - partial UNIQUE INDEX on (interaction_id, function_name, function_context)
    WHERE pathway_name IS NULL, named `uq_claim_fn_null_pw_ctx`

These leave duplicates through when (pathway_name, function_context) is
(NULL, NULL), (value, NULL), or (NULL, value). This migration:

  1. Deletes duplicate rows by the new semantic key, keeping min(id).
  2. Drops the old constraint and the old partial index.
  3. Creates a new UNIQUE INDEX with COALESCE-wrapped columns, so NULL and
     '' collapse to the same key and duplicates are blocked in every NULL
     combination.

Idempotent — safe to run repeatedly.

Usage: cd /path/to/project && python3 scripts/migrate_claims_null_unique_fix.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)


def run_migration(app) -> None:
    from models import db

    with app.app_context():
        # Step 1: delete duplicates, keeping the lowest id per semantic key.
        # Using a CTE with row_number over the COALESCE key ensures correctness
        # under the new uniqueness rule.
        db.session.execute(db.text("""
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            interaction_id,
                            function_name,
                            COALESCE(pathway_name, ''),
                            COALESCE(function_context, '')
                        ORDER BY id
                    ) AS rn
                FROM interaction_claims
            )
            DELETE FROM interaction_claims
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """))
        deleted = db.session.execute(db.text(
            "SELECT COUNT(*) FROM interaction_claims"
        )).scalar()
        print(f"[MIGRATION] Duplicate cleanup done (remaining rows: {deleted})")

        # Step 2: drop old constraint + old partial index. IF EXISTS keeps this
        # idempotent and safe on fresh DBs.
        db.session.execute(db.text(
            "ALTER TABLE interaction_claims "
            "DROP CONSTRAINT IF EXISTS uq_claim_interaction_fn_pw_ctx"
        ))
        db.session.execute(db.text(
            "DROP INDEX IF EXISTS uq_claim_fn_null_pw_ctx"
        ))
        db.session.execute(db.text(
            "DROP INDEX IF EXISTS claim_unique_no_pathway"
        ))
        # If a prior run of this migration created the new index with the
        # same name, drop it so we recreate cleanly.
        db.session.execute(db.text(
            "DROP INDEX IF EXISTS uq_claim_interaction_fn_pw_ctx"
        ))
        print("[MIGRATION] Old constraints dropped")

        # Step 3: create the new COALESCE-based functional unique index.
        db.session.execute(db.text("""
            CREATE UNIQUE INDEX uq_claim_interaction_fn_pw_ctx
            ON interaction_claims (
                interaction_id,
                function_name,
                COALESCE(pathway_name, ''),
                COALESCE(function_context, '')
            )
        """))
        db.session.commit()
        print(
            "[MIGRATION] Created uq_claim_interaction_fn_pw_ctx "
            "(COALESCE-based, NULL-safe)"
        )


if __name__ == "__main__":
    from app import app
    run_migration(app)
