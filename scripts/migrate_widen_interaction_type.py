"""
One-time migration: Widen interaction_type column from VARCHAR(20) to VARCHAR(100).

The LLM pipeline now generates granular interaction types like
'parallel_regulation_of_common_target' that exceed the original 20-char limit.

Run once:  python scripts/migrate_widen_interaction_type.py
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
    "ALTER TABLE interactions ALTER COLUMN interaction_type TYPE VARCHAR(100)",
]

print(f"\nRunning {len(ALTERATIONS)} migration statements...\n")

ok_count = 0
skip_count = 0

with engine.connect() as conn:
    for sql in ALTERATIONS:
        try:
            conn.execute(text(sql))
            print(f"  [OK]   {sql}")
            ok_count += 1
        except Exception as e:
            err_msg = str(e).split('\n')[0][:80]
            print(f"  [SKIP] {sql}  -- {err_msg}")
            skip_count += 1
    conn.commit()

print(f"\n{'='*60}")
print(f"Migration complete!  {ok_count} OK, {skip_count} skipped")
print(f"{'='*60}")
