"""
One-time migration: Add DEFAULT NOW() to all timestamp columns in existing PostgreSQL tables.
Also adds missing indexes and fills any NULL timestamps.

Run once:  python migrate_add_defaults.py
"""
import os
import sys
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

ALTERATIONS = [
    # --- Add DEFAULT NOW() to all timestamp columns ---
    "ALTER TABLE proteins ALTER COLUMN created_at SET DEFAULT NOW()",
    "ALTER TABLE proteins ALTER COLUMN updated_at SET DEFAULT NOW()",
    "ALTER TABLE proteins ALTER COLUMN first_queried SET DEFAULT NOW()",
    "ALTER TABLE proteins ALTER COLUMN last_queried SET DEFAULT NOW()",
    "ALTER TABLE interactions ALTER COLUMN created_at SET DEFAULT NOW()",
    "ALTER TABLE interactions ALTER COLUMN updated_at SET DEFAULT NOW()",
    "ALTER TABLE pathways ALTER COLUMN created_at SET DEFAULT NOW()",
    "ALTER TABLE pathways ALTER COLUMN updated_at SET DEFAULT NOW()",
    "ALTER TABLE pathway_interactions ALTER COLUMN created_at SET DEFAULT NOW()",
    "ALTER TABLE pathway_parents ALTER COLUMN created_at SET DEFAULT NOW()",

    # --- Add missing indexes for query performance ---
    "CREATE INDEX IF NOT EXISTS idx_interactions_upstream ON interactions (upstream_interactor)",
    "CREATE INDEX IF NOT EXISTS idx_interactions_function_context ON interactions (function_context)",

    # --- Fill any existing NULL timestamps with sensible values ---
    "UPDATE proteins SET created_at = COALESCE(first_queried, NOW()) WHERE created_at IS NULL",
    "UPDATE proteins SET updated_at = COALESCE(created_at, NOW()) WHERE updated_at IS NULL",
    "UPDATE interactions SET created_at = NOW() WHERE created_at IS NULL",
    "UPDATE interactions SET updated_at = NOW() WHERE updated_at IS NULL",
    "UPDATE pathways SET created_at = NOW() WHERE created_at IS NULL",
    "UPDATE pathways SET updated_at = NOW() WHERE updated_at IS NULL",
    "UPDATE pathway_interactions SET created_at = NOW() WHERE created_at IS NULL",
    "UPDATE pathway_parents SET created_at = NOW() WHERE created_at IS NULL",
]

print(f"\nRunning {len(ALTERATIONS)} migration statements...\n")

ok_count = 0
skip_count = 0

with engine.connect() as conn:
    for sql in ALTERATIONS:
        try:
            result = conn.execute(text(sql))
            rowcount = getattr(result, 'rowcount', None)
            suffix = f" ({rowcount} rows)" if rowcount and rowcount > 0 else ""
            print(f"  [OK]   {sql[:75]}{suffix}")
            ok_count += 1
        except Exception as e:
            err_msg = str(e).split('\n')[0][:80]
            print(f"  [SKIP] {sql[:75]}  -- {err_msg}")
            skip_count += 1
    conn.commit()

print(f"\n{'='*60}")
print(f"Migration complete!  {ok_count} OK, {skip_count} skipped")
print(f"{'='*60}")
print("\nYou can now run: python app.py")
