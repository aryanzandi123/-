#!/usr/bin/env python3
"""One-shot cleanup for legacy placeholder-stub claims.

The 2026-04-21 session added a gate in ``utils.db_sync._save_claims`` that
refuses to write a claim row when its only source text is a known
placeholder fragment (``"Discovered via chain resolution"``,
``"Function data not generated"``, etc.). That's forward-only — rows
written BEFORE the gate still show up in the modal, rendering the same
stub string in Mechanism / Effect / Cascade / Specific Effects.

This script finds and deletes those legacy rows. Always confirm the
count first (``--dry-run``) before running for real.

Usage:
    python3 scripts/cleanup_placeholder_claims.py --dry-run   # preview
    python3 scripts/cleanup_placeholder_claims.py             # delete

Safe to re-run: idempotent, only deletes rows whose text matches the
placeholder fragments.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root on sys.path so ``from app import ...`` resolves when
# this script is invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app, db  # noqa: E402
from models import InteractionClaim  # noqa: E402
from sqlalchemy import or_, func  # noqa: E402

PLACEHOLDER_FRAGMENTS = (
    "discovered via chain resolution",
    "function data not generated",
    "data not generated",
    "uncharacterized interaction",
)


def _placeholder_filter():
    """SQLAlchemy filter matching any placeholder fragment in mechanism or function_name."""
    clauses = []
    for frag in PLACEHOLDER_FRAGMENTS:
        clauses.append(func.lower(InteractionClaim.mechanism).like(f"%{frag}%"))
        clauses.append(func.lower(InteractionClaim.function_name).like(f"%{frag}%"))
    return or_(*clauses)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview count only, do not delete.")
    args = parser.parse_args()

    with app.app_context():
        q = InteractionClaim.query.filter(_placeholder_filter())
        total = q.count()
        if total == 0:
            print("No placeholder-stub claims found. Nothing to clean up.")
            return 0

        print(f"Found {total} placeholder-stub claim(s) matching fragments:")
        for frag in PLACEHOLDER_FRAGMENTS:
            print(f"  - {frag!r}")

        if args.dry_run:
            print("\n(dry-run — no rows deleted. Re-run without --dry-run to delete.)")
            # Show a small sample for sanity
            sample = q.limit(5).all()
            for c in sample:
                print(f"  id={c.id} interaction_id={c.interaction_id} "
                      f"function_name={c.function_name!r}")
            return 0

        print(f"\nDeleting {total} row(s)...")
        q.delete(synchronize_session=False)
        db.session.commit()
        print(f"Done. Deleted {total} placeholder-stub claim(s).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
