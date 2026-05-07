#!/usr/bin/env python3
"""One-shot cleanup: null pathway_id/pathway_name on legacy fallback claims.

The 2026-04-30 session (P1.9) stopped NEW ``__fallback__`` /
``pipeline_fallback`` claims from being written with a pathway. But rows
written BEFORE that fix still carry ``pathway_id`` / ``pathway_name``,
which means they're indexed in ``PathwayInteraction`` and contribute to
pathway badge counts even though they have no scientific content
(empty mechanism, no cascades, no evidence).

This script finds those legacy rows and clears their pathway columns
(without deleting the claim itself — fallback rows are diagnostic and
the discovery_method tag still tells operators why they exist). The
``PathwayInteraction`` junction is rebuilt downstream by quick_assign on
the next run, so this script does NOT need to touch that table.

Safe to re-run: idempotent.

Usage:
    python3 scripts/cleanup_fallback_pathways.py --dry-run     # preview
    python3 scripts/cleanup_fallback_pathways.py --apply       # commit
    python3 scripts/cleanup_fallback_pathways.py               # default = dry-run

Examples of rows touched:
    function_name='__fallback__' AND (pathway_id IS NOT NULL OR pathway_name IS NOT NULL)
    discovery_method='pipeline_fallback' AND (pathway_id IS NOT NULL OR pathway_name IS NOT NULL)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure repo root on sys.path so ``from app import ...`` resolves when
# this script is invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skip the db.create_all() that app.py runs at import — we just need the
# Flask app + SQLAlchemy session, not schema bootstrap.
os.environ.setdefault("SKIP_APP_BOOTSTRAP", "1")

from app import app, db  # noqa: E402
from models import InteractionClaim, PathwayInteraction  # noqa: E402
from sqlalchemy import or_  # noqa: E402


def _fallback_with_pathway_filter():
    """SQLAlchemy filter for fallback claims that still hold a pathway."""
    is_fallback = or_(
        InteractionClaim.function_name == "__fallback__",
        InteractionClaim.discovery_method == "pipeline_fallback",
    )
    has_pathway = or_(
        InteractionClaim.pathway_id.isnot(None),
        InteractionClaim.pathway_name.isnot(None),
    )
    return db.and_(is_fallback, has_pathway)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview only, don't modify the DB (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually clear pathway_id and pathway_name. Required to commit.",
    )
    args = parser.parse_args()

    apply = bool(args.apply)
    if apply:
        # An explicit --apply overrides --dry-run.
        args.dry_run = False

    with app.app_context():
        # ── Before counts ────────────────────────────────────────────
        total_fallback = (
            InteractionClaim.query
            .filter(or_(
                InteractionClaim.function_name == "__fallback__",
                InteractionClaim.discovery_method == "pipeline_fallback",
            ))
            .count()
        )
        polluted = InteractionClaim.query.filter(_fallback_with_pathway_filter()).count()
        # Per-pathway breakdown of where the pollution lives.
        from sqlalchemy import func as sa_func
        breakdown_rows = (
            db.session.query(
                InteractionClaim.pathway_name,
                sa_func.count(InteractionClaim.id).label("n"),
            )
            .filter(_fallback_with_pathway_filter())
            .group_by(InteractionClaim.pathway_name)
            .order_by(sa_func.count(InteractionClaim.id).desc())
            .all()
        )

        print("=" * 60)
        print("STALE FALLBACK PATHWAY CLEANUP")
        print("=" * 60)
        print(f"Total fallback claims in DB:           {total_fallback}")
        print(f"Fallback claims still bearing pathway: {polluted}")
        if breakdown_rows:
            print("\nBreakdown by current pathway_name:")
            for name, count in breakdown_rows[:20]:
                shown_name = name if name is not None else "<NULL — has pathway_id but no name>"
                print(f"  {count:4d}  {shown_name}")
            if len(breakdown_rows) > 20:
                print(f"  ... and {len(breakdown_rows) - 20} more pathway buckets")

        if polluted == 0:
            print("\nNothing to clean — all fallback claims already have NULL pathway columns.")
            return 0

        if args.dry_run and not apply:
            print(f"\n[DRY-RUN] Would clear pathway_id and pathway_name on {polluted} row(s).")
            print("Re-run with --apply to commit.")
            return 0

        # ── Apply ────────────────────────────────────────────────────
        print(f"\n[APPLY] Clearing pathway_id and pathway_name on {polluted} row(s)...")
        # Use a single UPDATE for atomicity. SQLAlchemy's bulk update
        # avoids the per-row ORM overhead which matters for thousands of
        # rows.
        affected = (
            InteractionClaim.query
            .filter(_fallback_with_pathway_filter())
            .update(
                {
                    InteractionClaim.pathway_id: None,
                    InteractionClaim.pathway_name: None,
                },
                synchronize_session=False,
            )
        )
        db.session.commit()

        # ── After counts ─────────────────────────────────────────────
        polluted_after = (
            InteractionClaim.query
            .filter(_fallback_with_pathway_filter())
            .count()
        )
        print(f"\nAffected rows: {affected}")
        print(f"Remaining polluted rows: {polluted_after} (should be 0)")

        # The PathwayInteraction junction is rebuilt by quick_assign next
        # run; we don't proactively delete junction rows that pointed at
        # these claims because the junction rebuild handles staleness via
        # `_sync_pathway_interactions`. But surface the current count so
        # the operator can sanity-check after the next quick-assign.
        pi_count = PathwayInteraction.query.count()
        print(f"\nFor reference: pathway_interactions junction has {pi_count} rows.")
        print("Run a quick-assign or repair-pathways for affected proteins to refresh that table.")

        return 0


if __name__ == "__main__":
    sys.exit(main())
