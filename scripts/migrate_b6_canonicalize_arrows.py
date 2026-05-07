#!/usr/bin/env python3
"""B6 — canonicalize ``Interaction.arrows`` (JSONB) as the single source of
truth for arrow data.

Background
----------
Three places store arrow information today:
  • ``Interaction.arrow``       — legacy VARCHAR(50) scalar column
  • ``Interaction.arrows``      — newer JSONB dict keyed by direction
  • ``Interaction.data.arrows`` — legacy JSONB blob sub-field

Readers use ``Interaction.primary_arrow`` which walks all three. Writers
update different subsets, creating drift where the three sources disagree.

Long-term the plan is to delete both legacy sources. That's a bigger
surgery; this migration is the first safe step:

1. **Backfill** — for every row where ``arrows`` is NULL but ``arrow`` is
   set (legacy-only writes), populate ``arrows`` from ``arrow``.
2. **Preserve** — do NOT drop the ``arrow`` column yet; too many readers
   still reference it. Once the code-side audit finishes eliminating
   ``.arrow`` column reads, a follow-up migration drops it.
3. **Validate** — for every row with BOTH arrow and arrows set, check
   they agree. Log disagreements as a structured event so operators can
   resolve manually before the follow-up drops the column.

Safety
------
- Dry-run by default; ``--apply`` commits.
- Batched commits (default 500 rows).
- Idempotent: running twice is a no-op after the first successful run.
- Does NOT touch rows where both columns are set; only reports them.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="Commit. Default is dry-run.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Commit batch size (default 500).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    from app import app
    from models import db, Interaction

    with app.app_context():
        # Phase 1: backfill rows where arrows IS NULL but arrow IS NOT NULL.
        candidates = (
            Interaction.query
            .filter(Interaction.arrows.is_(None))
            .filter(Interaction.arrow.isnot(None))
            .all()
        )
        print(f"[B6] Backfill candidates (arrow set, arrows NULL): {len(candidates)}",
              file=sys.stderr)

        updated = 0
        for row in candidates:
            arrow_val = (row.arrow or "").strip()
            if not arrow_val:
                continue
            # Default direction pairing — legacy arrow was always
            # applied in the canonical a_to_b direction.
            new_arrows = {"a_to_b": [arrow_val]}
            if args.verbose:
                print(f"  row {row.id}: arrows := {new_arrows!r}")
            if args.apply:
                row.arrows = new_arrows
                updated += 1
                if updated % args.batch_size == 0:
                    db.session.commit()
                    print(f"[B6] committed batch ({updated})", file=sys.stderr)

        if args.apply and updated % args.batch_size:
            db.session.commit()
            print(f"[B6] committed final batch ({updated})", file=sys.stderr)

        if not args.apply:
            print(f"[B6] dry-run: would backfill arrows on {len(candidates)} rows",
                  file=sys.stderr)

        # Phase 2: disagreement audit. Report rows where both arrow and
        # arrows are set but disagree on the primary arrow.
        both = (
            Interaction.query
            .filter(Interaction.arrows.isnot(None))
            .filter(Interaction.arrow.isnot(None))
            .all()
        )
        print(f"[B6] rows with BOTH columns set: {len(both)}", file=sys.stderr)
        disagreements = []
        for row in both:
            primary_from_arrows = None
            if isinstance(row.arrows, dict):
                for key in ("a_to_b", "b_to_a"):
                    vals = row.arrows.get(key)
                    if vals:
                        primary_from_arrows = vals[0]
                        break
            if primary_from_arrows and primary_from_arrows != row.arrow:
                disagreements.append({
                    "id": row.id,
                    "arrow_col": row.arrow,
                    "arrows_primary": primary_from_arrows,
                })
        if disagreements:
            print(f"[B6] {len(disagreements)} disagreement(s):",
                  file=sys.stderr)
            for d in disagreements[:50]:
                print(f"  {json.dumps(d)}", file=sys.stderr)
            if len(disagreements) > 50:
                print(f"  ... and {len(disagreements) - 50} more", file=sys.stderr)
        else:
            print("[B6] no disagreements — arrow and arrows are consistent",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
