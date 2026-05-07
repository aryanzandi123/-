#!/usr/bin/env python3
"""
Migration: Add Hierarchy Columns to Pathways Table

Adds the following columns to the pathways table:
- hierarchy_level (INTEGER NOT NULL DEFAULT 0)
- is_leaf (BOOLEAN NOT NULL DEFAULT TRUE)
- protein_count (INTEGER NOT NULL DEFAULT 0)
- ancestor_ids (JSONB NOT NULL DEFAULT '[]')

Also ensures pathway_parents and pathway_interactions tables exist.

Run: python scripts/migrate_add_hierarchy_columns.py
"""
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import app, db
from sqlalchemy import text


# Initial root categories to seed into database (RUN ONCE)
# These are the top-level pathway categories for the hierarchy
INITIAL_ROOT_CATEGORIES = [
    {
        "name": "Proteostasis",
        "go_id": "GO:0006457",
        "description": "Protein homeostasis mechanisms including folding, quality control, and degradation.",
    },
    {
        "name": "Metabolism & Bioenergetics",
        "go_id": "GO:0008152",
        "description": "Chemical reactions involved in energy production and maintaining cellular functions.",
    },
    {
        "name": "Membrane & Transport",
        "go_id": "GO:0016192",
        "description": "Membrane dynamics, vesicle trafficking, and cellular transport mechanisms.",
    },
    {
        "name": "Genome Maintenance",
        "go_id": "GO:0006281",
        "description": "DNA repair, replication, chromatin organization, and genome stability.",
    },
    {
        "name": "Gene Expression",
        "go_id": "GO:0010467",
        "description": "Transcription, translation, RNA processing, and gene regulation.",
    },
    {
        "name": "Signal Transduction",
        "go_id": "GO:0007165",
        "description": "Cellular signaling cascades and signal transmission mechanisms.",
    },
    {
        "name": "Cytoskeletal Dynamics",
        "go_id": "GO:0007010",
        "description": "Assembly, arrangement, and regulation of cytoskeletal structures.",
    },
]


def seed_root_categories(session):
    """
    Seed initial root categories into database.
    Only runs if no root pathways exist (hierarchy_level = 0).
    """
    from models import Pathway

    # Check if roots already exist
    existing_roots = session.query(Pathway).filter_by(hierarchy_level=0).count()
    if existing_roots > 0:
        print(f"   ✓ Root categories already exist ({existing_roots} found), skipping seed")
        return

    print("   Seeding initial root categories...")
    for root in INITIAL_ROOT_CATEGORIES:
        # Check if pathway already exists by name
        existing = session.query(Pathway).filter_by(name=root["name"]).first()
        if existing:
            # Update existing pathway to be a root
            existing.hierarchy_level = 0
            existing.ontology_id = root["go_id"]
            existing.ontology_source = "GO"
            existing.description = root["description"]
            existing.is_leaf = True  # Will be updated when children are added
            print(f"     Updated existing: {root['name']}")
        else:
            # Create new root pathway
            pathway = Pathway(
                name=root["name"],
                description=root["description"],
                ontology_id=root["go_id"],
                ontology_source="GO",
                hierarchy_level=0,
                is_leaf=True,  # Will be updated when children are added
                ai_generated=False,
            )
            session.add(pathway)
            print(f"     Created: {root['name']}")

    session.flush()
    print(f"   ✓ Seeded {len(INITIAL_ROOT_CATEGORIES)} root categories")


def migrate():
    """Add missing hierarchy columns to pathways table."""
    print("=" * 60)
    print("MIGRATION: Add Hierarchy Columns to Pathways Table")
    print("=" * 60)

    with app.app_context():
        conn = db.session.connection()

        # Check if pathways table exists
        result = conn.execute(text(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'pathways')"
        ))
        if not result.scalar():
            print("\n[INFO] pathways table doesn't exist yet, will be created by db.create_all()")
        else:
            print("\n[INFO] pathways table exists, checking for missing columns...")

            # Get existing columns
            result = conn.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'pathways'
            """))
            existing_columns = {row[0] for row in result}
            print(f"   Existing columns: {sorted(existing_columns)}")

            # Columns to add with their definitions
            columns_to_add = [
                ("hierarchy_level", "INTEGER NOT NULL DEFAULT 0"),
                ("is_leaf", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("protein_count", "INTEGER NOT NULL DEFAULT 0"),
                ("ancestor_ids", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
            ]

            for col_name, col_def in columns_to_add:
                if col_name in existing_columns:
                    print(f"   ✓ Column '{col_name}' already exists")
                else:
                    try:
                        conn.execute(text(f"ALTER TABLE pathways ADD COLUMN {col_name} {col_def}"))
                        print(f"   ✓ Added column: {col_name}")
                    except Exception as e:
                        print(f"   ✗ Failed to add {col_name}: {e}")
                        raise

        # Create any missing tables (pathway_parents, pathway_interactions)
        print("\n[INFO] Ensuring all model tables exist...")
        db.create_all()
        print("   ✓ db.create_all() completed")

        # Check which tables exist now
        result = conn.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """))
        tables = [row[0] for row in result]
        print(f"   Tables in database: {tables}")

        # Create indexes if they don't exist
        print("\n[INFO] Creating indexes...")
        indexes = [
            ("idx_pathways_hierarchy_level", "CREATE INDEX IF NOT EXISTS idx_pathways_hierarchy_level ON pathways(hierarchy_level)"),
            ("idx_pathways_is_leaf", "CREATE INDEX IF NOT EXISTS idx_pathways_is_leaf ON pathways(is_leaf)"),
            ("idx_pathways_ontology", "CREATE INDEX IF NOT EXISTS idx_pathways_ontology ON pathways(ontology_source, ontology_id)"),
        ]

        for idx_name, idx_sql in indexes:
            try:
                conn.execute(text(idx_sql))
                print(f"   ✓ Index: {idx_name}")
            except Exception as e:
                print(f"   ✗ Index {idx_name}: {e}")

        # Seed initial root categories (ONLY if database is empty)
        print("\n[INFO] Checking for root categories...")
        seed_root_categories(db.session)

        # Commit all changes
        db.session.commit()

        # Verify the migration with fresh connection
        print("\n[INFO] Verifying migration...")
        with db.engine.connect() as verify_conn:
            result = verify_conn.execute(text("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = 'pathways'
                ORDER BY ordinal_position
            """))
            print("   pathways table columns:")
            for row in result:
                print(f"     - {row[0]}: {row[1]} (nullable={row[2]}, default={row[3]})")

        print("\n" + "=" * 60)
        print("✅ MIGRATION COMPLETE")
        print("=" * 60)
        print("\nYou can now run:")
        print("  python scripts/pathway_hierarchy/run_all.py --from 2")


if __name__ == "__main__":
    migrate()
