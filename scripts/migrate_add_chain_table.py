#!/usr/bin/env python3
"""
Migration: Create indirect_chains table and add chain_id column to interaction_claims.

Creates:
  - indirect_chains table (full indirect chain entity grouping claims)
  - chain_id FK column on interaction_claims
  - Associated indexes and constraints

Idempotent: checks for existence before creating table/column.

Usage: python scripts/migrate_add_chain_table.py
"""
import os
import sys

# Ensure project root is on sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

from dotenv import load_dotenv

load_dotenv()

database_url = os.getenv('DATABASE_PUBLIC_URL') or os.getenv('DATABASE_URL')
if not database_url:
    print("ERROR: No DATABASE_URL found in environment. Set DATABASE_URL or DATABASE_PUBLIC_URL.")
    sys.exit(1)

if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

print(f"Connecting to: {database_url[:30]}...")

from sqlalchemy import create_engine, text

engine = create_engine(database_url)


def table_exists(conn, table_name):
    """Check if a table exists in the public schema."""
    result = conn.execute(text(
        "SELECT EXISTS ("
        "  SELECT 1 FROM information_schema.tables "
        "  WHERE table_schema = 'public' AND table_name = :tbl"
        ")"
    ), {"tbl": table_name})
    return result.scalar()


def column_exists(conn, table_name, column_name):
    """Check if a column exists on a table."""
    result = conn.execute(text(
        "SELECT EXISTS ("
        "  SELECT 1 FROM information_schema.columns "
        "  WHERE table_schema = 'public' AND table_name = :tbl AND column_name = :col"
        ")"
    ), {"tbl": table_name, "col": column_name})
    return result.scalar()


def run_migration():
    """Create indirect_chains table and add chain_id to interaction_claims."""
    ok_count = 0
    skip_count = 0

    with engine.connect() as conn:
        # ------------------------------------------------------------------
        # Step 1: Create indirect_chains table
        # ------------------------------------------------------------------
        if table_exists(conn, 'indirect_chains'):
            print("  [SKIP] indirect_chains table already exists")
            skip_count += 1
        else:
            conn.execute(text("""
                CREATE TABLE indirect_chains (
                    id SERIAL PRIMARY KEY,
                    chain_proteins JSONB NOT NULL,
                    origin_interaction_id INTEGER NOT NULL
                        REFERENCES interactions(id) ON DELETE CASCADE,
                    pathway_name VARCHAR(200),
                    pathway_id INTEGER REFERENCES pathways(id) ON DELETE SET NULL,
                    chain_with_arrows JSONB,
                    discovered_in_query VARCHAR(50),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),

                    CONSTRAINT chain_origin_unique UNIQUE (origin_interaction_id)
                )
            """))
            print("  [OK]   Created indirect_chains table")
            ok_count += 1

            # Index on origin_interaction_id (for FK lookups)
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_indirect_chains_origin "
                "ON indirect_chains (origin_interaction_id)"
            ))
            print("  [OK]   Created index idx_indirect_chains_origin")
            ok_count += 1

            # Index on pathway_id
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_indirect_chains_pathway "
                "ON indirect_chains (pathway_id)"
            ))
            print("  [OK]   Created index idx_indirect_chains_pathway")
            ok_count += 1

            # Index on discovered_in_query
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_indirect_chains_query "
                "ON indirect_chains (discovered_in_query)"
            ))
            print("  [OK]   Created index idx_indirect_chains_query")
            ok_count += 1

        # ------------------------------------------------------------------
        # Step 2: Add chain_id column to interaction_claims
        # ------------------------------------------------------------------
        if column_exists(conn, 'interaction_claims', 'chain_id'):
            print("  [SKIP] interaction_claims.chain_id column already exists")
            skip_count += 1
        else:
            conn.execute(text(
                "ALTER TABLE interaction_claims "
                "ADD COLUMN chain_id INTEGER REFERENCES indirect_chains(id) ON DELETE SET NULL"
            ))
            print("  [OK]   Added chain_id column to interaction_claims")
            ok_count += 1

            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_claims_chain "
                "ON interaction_claims (chain_id)"
            ))
            print("  [OK]   Created index idx_claims_chain")
            ok_count += 1

        conn.commit()

    print(f"\n{'='*60}")
    print(f"Migration complete!  {ok_count} OK, {skip_count} skipped")
    print(f"{'='*60}")


if __name__ == "__main__":
    run_migration()
