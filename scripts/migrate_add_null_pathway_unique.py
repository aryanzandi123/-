"""Add partial unique index to prevent duplicate claims when pathway_name IS NULL.

PostgreSQL treats NULL as distinct in UNIQUE constraints, so
(interaction_id=1, function_name='foo', pathway_name=NULL) can appear multiple times.
This partial index enforces uniqueness for the NULL pathway_name case.

Usage: cd /path/to/nEW && python3 scripts/migrate_add_null_pathway_unique.py
"""
import sys
import os

# Ensure project root is on sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)


def run_migration(app):
    """Run within Flask app context."""
    from models import db
    with app.app_context():
        db.session.execute(db.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS claim_unique_no_pathway "
            "ON interaction_claims (interaction_id, function_name) "
            "WHERE pathway_name IS NULL;"
        ))
        db.session.commit()
        print("[MIGRATION] Added partial unique index claim_unique_no_pathway")


if __name__ == "__main__":
    from app import app
    run_migration(app)
