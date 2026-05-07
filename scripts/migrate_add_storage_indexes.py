"""Add missing indexes for dual-track queries and discovery source.

Usage: cd /path/to/nEW && python3 scripts/migrate_add_storage_indexes.py
"""
import sys
import os

# Ensure project root is on sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)  # app.py expects to mkdir 'cache' relative to cwd


def run_migration(app):
    """Run within Flask app context."""
    from models import db
    with app.app_context():
        db.session.execute(db.text(
            "CREATE INDEX IF NOT EXISTS idx_interaction_pair_context "
            "ON interactions (protein_a_id, protein_b_id, function_context);"
        ))
        db.session.execute(db.text(
            "CREATE INDEX IF NOT EXISTS idx_interaction_discovered_in "
            "ON interactions (discovered_in_query);"
        ))
        db.session.commit()
        print("[MIGRATION] Added idx_interaction_pair_context and idx_interaction_discovered_in")


if __name__ == "__main__":
    from app import app
    run_migration(app)
