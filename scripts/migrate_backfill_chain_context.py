#!/usr/bin/env python3
"""Backfill ``chain_context`` on legacy indirect Interaction rows.

Rows written before the ITER0 upstream-context iteration landed only have
the legacy ``mediator_chain`` / ``upstream_interactor`` / ``depth`` fields
populated. They never got ``chain_context.full_chain`` written, so the
frontend's ``buildFullChainPath`` and the backend's `db_sync` overlap-
detection fall through to the legacy reconstruction path on every read —
which always places the query at index 0, losing the biologically-correct
position for any chain where the query sits in the middle.

This script walks every indirect Interaction row, and for each one that
has a usable ``mediator_chain`` + ``discovered_in_query`` but no
``chain_context.full_chain``, rebuilds the chain view via
``ChainView.from_interaction_data`` and writes it back via
``ChainView.apply_to_interaction``. Both helpers already exist in
``utils/chain_view.py``; this script is just the iteration driver.

Safety posture:
  * Idempotent — skips rows that already have ``chain_context.full_chain``.
  * Dry-run by default; pass ``--apply`` to actually commit.
  * Commits in batches of ``--batch-size`` (default 200) so a crash
    mid-run still leaves earlier rows persisted.
  * Never creates new rows; only updates in-place.
  * Skips legacy rows that can't be reconstructed (missing
    ``discovered_in_query``, self-loops, etc.) and logs each skip.

Usage::

    # Preview (no writes)
    python scripts/migrate_backfill_chain_context.py

    # Actually apply
    python scripts/migrate_backfill_chain_context.py --apply

    # Limit scope for testing
    python scripts/migrate_backfill_chain_context.py --apply --limit 50
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repo root importable whether this script is invoked from the
# repo root or via scripts/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _count_indirect_rows(db, Interaction) -> int:
    return Interaction.query.filter(Interaction.interaction_type == "indirect").count()


def _iter_candidates(db, Interaction, limit: int | None):
    q = Interaction.query.filter(Interaction.interaction_type == "indirect")
    if limit:
        q = q.limit(limit)
    return q.yield_per(200)


def _row_already_backfilled(row) -> bool:
    """Has this row already been through the backfill (or was written fresh)?"""
    try:
        direct_ctx = getattr(row, "chain_context", None)
        if isinstance(direct_ctx, dict) and isinstance(direct_ctx.get("full_chain"), list):
            if len(direct_ctx["full_chain"]) >= 2:
                return True
        data = row.data if isinstance(row.data, dict) else {}
        ctx = data.get("chain_context") if isinstance(data, dict) else None
        if isinstance(ctx, dict) and isinstance(ctx.get("full_chain"), list):
            if len(ctx["full_chain"]) >= 2:
                return True
    except Exception:
        return False
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually commit; default is dry-run.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Optional cap on how many rows to process (for testing).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=200,
        help="Commit batch size (default 200).",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Log each row's decision.",
    )
    args = parser.parse_args()

    from app import app
    from models import db, Interaction
    from utils.chain_view import ChainView

    with app.app_context():
        total_indirect = _count_indirect_rows(db, Interaction)
        print(f"[BACKFILL] Indirect interaction rows in DB: {total_indirect}")

        stats = {
            "scanned": 0,
            "already_set": 0,
            "backfilled": 0,
            "skipped_no_query": 0,
            "skipped_no_mediator": 0,
            "skipped_self_loop": 0,
            "errors": 0,
        }

        pending = 0
        for row in _iter_candidates(db, Interaction, args.limit):
            stats["scanned"] += 1

            if _row_already_backfilled(row):
                stats["already_set"] += 1
                continue

            query_protein = (row.discovered_in_query or "").strip() or None
            if not query_protein:
                stats["skipped_no_query"] += 1
                if args.verbose:
                    print(f"  skip id={row.id}: no discovered_in_query")
                continue

            data = row.data if isinstance(row.data, dict) else {}
            mediator = data.get("mediator_chain") or row.mediator_chain
            if not isinstance(mediator, list) or not mediator:
                # No chain info to reconstruct from; leave alone.
                stats["skipped_no_mediator"] += 1
                if args.verbose:
                    print(f"  skip id={row.id}: no mediator_chain")
                continue

            try:
                # from_interaction_data handles the legacy reconstruction
                # (prepends discovered_in_query and appends target).
                view = ChainView.from_interaction_data(data, query_protein=query_protein)
                if view.is_empty:
                    # Fall back to a direct reconstruction using the row's
                    # own primary / target hint if the data dict didn't
                    # give from_interaction_data enough to work with.
                    target = data.get("primary") or data.get("target")
                    if not target:
                        # Try deriving from the canonical endpoints.
                        pa_sym = getattr(getattr(row, "protein_a_obj", None), "symbol", None)
                        pb_sym = getattr(getattr(row, "protein_b_obj", None), "symbol", None)
                        # Target is whichever endpoint is NOT the query.
                        if pa_sym and pa_sym.upper() != query_protein.upper():
                            target = pa_sym
                        elif pb_sym and pb_sym.upper() != query_protein.upper():
                            target = pb_sym
                    if target:
                        reconstructed = [query_protein] + [str(m) for m in mediator if m] + [str(target)]
                        view = ChainView.from_full_chain(
                            reconstructed,
                            query_protein=query_protein,
                            query_position=0,
                        )

                if view.is_empty:
                    stats["skipped_no_mediator"] += 1
                    continue

                chain_list = list(view.full_chain)
                if any(chain_list[i] == chain_list[i + 1] for i in range(len(chain_list) - 1)):
                    # Self-loop from bad data — skip.
                    stats["skipped_self_loop"] += 1
                    continue

                # apply_to_interaction writes chain_context column +
                # data["chain_context"] mirror + derived mediator_chain,
                # upstream_interactor, depth — all atomically.
                view.apply_to_interaction(row)
                stats["backfilled"] += 1
                pending += 1
                if args.verbose:
                    print(f"  backfilled id={row.id}: {' → '.join(chain_list)}")

            except Exception as exc:
                stats["errors"] += 1
                print(
                    f"  error id={row.id}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue

            if pending >= args.batch_size and args.apply:
                try:
                    db.session.commit()
                    pending = 0
                except Exception as commit_exc:
                    db.session.rollback()
                    stats["errors"] += args.batch_size
                    print(
                        f"[BACKFILL] Batch commit failed ({commit_exc}); "
                        f"rolled back and continuing.",
                        file=sys.stderr,
                    )
                    pending = 0

        if args.apply and pending:
            try:
                db.session.commit()
            except Exception as commit_exc:
                db.session.rollback()
                stats["errors"] += pending
                print(f"[BACKFILL] Final commit failed: {commit_exc}", file=sys.stderr)

        if not args.apply:
            db.session.rollback()

        print()
        print("[BACKFILL] Summary:")
        for k, v in stats.items():
            print(f"  {k:24s} {v}")
        print()
        print(
            f"[BACKFILL] {'APPLIED' if args.apply else 'DRY RUN'} — "
            f"{stats['backfilled']} rows " +
            ("committed." if args.apply else "would be updated. Re-run with --apply to commit.")
        )


if __name__ == "__main__":
    main()
