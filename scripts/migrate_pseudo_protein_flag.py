"""Migration: stamp ``is_pseudo=True`` on existing Protein rows.

Idempotent. Scans the Protein table, flags any row whose symbol is in the
pseudo whitelist (RNA, Ubiquitin, Proteasome, ...), and writes
``extra_data["is_pseudo"]=True``.

Run: python3 scripts/migrate_pseudo_protein_flag.py
Optional: --dry-run to preview without writing.
"""
import argparse
import os
import sys

# Allow running from project root: ``python3 scripts/migrate_pseudo_protein_flag.py``
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app import app  # noqa: E402  # module-level Flask app instance
from models import db, Protein  # noqa: E402
from utils.db_sync import _PSEUDO_WHITELIST, classify_symbol  # noqa: E402


def run(dry_run: bool = False) -> int:
    with app.app_context():
        candidates = Protein.query.filter(Protein.symbol.in_(_PSEUDO_WHITELIST)).all()
        # Also catch any rows whose symbol classifies as pseudo by suffix
        # (e.g. transcript-specific entries like "FOOmRNA") that are not
        # in the explicit whitelist.
        all_proteins = Protein.query.all()
        suffix_candidates = [
            p for p in all_proteins
            if classify_symbol(p.symbol) == "pseudo" and p not in candidates
        ]
        candidates = list(candidates) + suffix_candidates

        if not candidates:
            print("[migrate] No pseudo-eligible Protein rows found. Nothing to do.")
            return 0

        flagged = 0
        already = 0
        for p in candidates:
            ed = dict(p.extra_data or {})
            if ed.get("is_pseudo") is True:
                already += 1
                continue
            ed["is_pseudo"] = True
            if dry_run:
                print(f"  [DRY] would flag id={p.id} symbol={p.symbol!r}")
            else:
                p.extra_data = ed
                flagged += 1

        if dry_run:
            print(f"[migrate] DRY RUN — would flag {len(candidates) - already} row(s); {already} already flagged.")
            return 0

        db.session.commit()
        print(f"[migrate] Flagged is_pseudo=True on {flagged} row(s); {already} already flagged.")
        # Show summary
        for p in candidates:
            ed = p.extra_data or {}
            mark = "✓" if ed.get("is_pseudo") else "✗"
            print(f"  {mark} id={p.id:<5} symbol={p.symbol!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    args = ap.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
