#!/usr/bin/env python3
"""Drop the 7 empty / superseded tables from the public schema.

These tables are present in production but have ZERO rows and no active
writer. They're legacy design residue — some were replaced by a newer
table with a different name, some were scaffolded for features that
never shipped. Leaving them increases schema noise and makes reasoning
about the system harder.

Dropped tables:

  • interaction_chains          — parallel to indirect_chains, unused
  • interaction_pathways        — parallel to pathway_interactions, unused
  • interaction_query_hits      — tracking table never wired up
  • pathway_canonical_names     — pre-quick-assign normalization, dead
  • pathway_hierarchy           — parallel to pathway_parents, unused
  • pathway_hierarchy_history   — audit log scaffold, never populated
  • pathway_initial_assignments — pre-quick-assign assignment log, dead

The script refuses to drop any table that has rows — if a future writer
starts populating one of these, you'll be warned instead of losing data.

Safety
------
- Dry-run by default. Pass ``--apply`` to commit.
- Per-table row check before drop; non-empty tables are skipped + logged.
- Uses ``DROP TABLE IF EXISTS ... CASCADE`` so FK references (there are
  none expected, but belt-and-suspenders) are handled.

Usage::

    python3 scripts/migrate_drop_dead_tables.py              # preview
    python3 scripts/migrate_drop_dead_tables.py --apply      # drop them

After running, re-introspect with::

    python3 scripts/db_health_check.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


DEAD_TABLES = (
    "interaction_chains",
    "interaction_pathways",
    "interaction_query_hits",
    "pathway_canonical_names",
    "pathway_hierarchy",
    "pathway_hierarchy_history",
    "pathway_initial_assignments",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="Commit the drops. Default is dry-run.")
    parser.add_argument("--force", action="store_true",
                        help="Drop non-empty tables too. Use with care.")
    args = parser.parse_args()

    from app import app
    from models import db
    from sqlalchemy import text

    with app.app_context():
        for tname in DEAD_TABLES:
            # Does the table exist?
            exists = db.session.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=:t"
            ), {"t": tname}).scalar()
            if not exists:
                print(f"  [skip] {tname}: does not exist")
                continue

            try:
                n = db.session.execute(text(
                    f'SELECT COUNT(*) FROM "{tname}"'
                )).scalar() or 0
            except Exception as e:
                print(f"  [fail ] {tname}: count failed ({e})")
                continue

            if n > 0 and not args.force:
                print(f"  [SKIP] {tname}: {n} rows — refusing to drop without --force")
                continue

            if n > 0:
                print(f"  [WARN] {tname}: {n} rows — dropping anyway (--force)")

            if not args.apply:
                print(f"  [dry-run] would DROP TABLE {tname} (rows={n})")
                continue

            try:
                db.session.execute(text(f'DROP TABLE IF EXISTS "{tname}" CASCADE'))
                db.session.commit()
                print(f"  [drop ] {tname} (had {n} rows)")
            except Exception as e:
                db.session.rollback()
                print(f"  [FAIL ] {tname}: {type(e).__name__}: {e}")

        if not args.apply:
            print("\n[dry-run] No changes. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
