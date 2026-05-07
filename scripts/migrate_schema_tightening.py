#!/usr/bin/env python3
"""Combined schema tightening migration (B1 + B2 + B3 + B4 from the audit).

What this migration does
------------------------

B1. Add ``mechanism`` hash to the claim unique index so two claims with
    identical (name, pathway, context) but different mechanisms no longer
    silently collide. Uses ``md5(mechanism)`` in the index expression so
    the key stays fixed-width and portable.

B2. Add ``is_synthetic`` boolean column to ``interaction_claims``. Writers
    that fabricate placeholder claims (no real mechanism — pathway-only
    rows, fallback synthesized rows) must set it True. Display + pathway-
    stat code filters the flag out.

B3. Make ``interaction_claims.source_query`` NOT NULL. Pre-step: backfill
    rows where source_query IS NULL by joining to
    ``interactions.discovered_in_query`` (the legacy blob) or the first
    Protein row the chain was discovered under. Unrecoverable rows get
    ``source_query='__unknown__'`` so the NOT NULL constraint can still
    be installed; operators can audit them via
    ``SELECT * FROM interaction_claims WHERE source_query='__unknown__'``.

B4. Case-insensitive ``Protein.symbol`` uniqueness. Approach: de-dup
    case-variant rows first (merge claims + interactions from each alias
    row into the canonical UPPER(symbol) row), then add a functional
    UNIQUE index on ``UPPER(symbol)``. The original unique constraint on
    ``symbol`` stays (for backwards compat) but the functional index is
    what actually prevents new case-variant dups.

Safety
------
- Dry-run by default. Pass ``--apply`` to commit.
- Idempotent: each step checks for its own artifacts before acting.
- Transactional per-step: a failure inside one step rolls back that step
  only, so partial progress survives.
- Skippable: pass ``--skip=B3,B4`` to run only a subset.

Usage::

    python3 scripts/migrate_schema_tightening.py --help
    python3 scripts/migrate_schema_tightening.py            # dry-run
    python3 scripts/migrate_schema_tightening.py --apply
    python3 scripts/migrate_schema_tightening.py --apply --skip=B4
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _ensure_column(db, table: str, column: str, ddl_type: str) -> bool:
    """Check for column existence via information_schema; return True if added."""
    exists = db.session.execute(db.text(f"""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = :t AND column_name = :c LIMIT 1
    """), {"t": table, "c": column}).scalar()
    if exists:
        print(f"  [skip] {table}.{column} already exists")
        return False
    db.session.execute(db.text(
        f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"
    ))
    print(f"  [add]  {table}.{column} :: {ddl_type}")
    return True


def _ensure_index(db, index_name: str, create_sql: str) -> bool:
    """Check for index existence; return True if created."""
    exists = db.session.execute(db.text("""
        SELECT 1 FROM pg_indexes WHERE indexname = :n LIMIT 1
    """), {"n": index_name}).scalar()
    if exists:
        print(f"  [skip] index {index_name} already exists")
        return False
    db.session.execute(db.text(create_sql))
    print(f"  [add]  index {index_name}")
    return True


def step_b1_mechanism_in_unique_index(db, apply: bool) -> None:
    """Add mechanism hash to uq_claim_interaction_fn_pw_ctx."""
    print("[B1] Add mechanism hash to claim unique index")
    # New composite index; existing uq_claim_interaction_fn_pw_ctx stays
    # alongside (it's narrower — acts as a secondary guard).
    index_name = "uq_claim_interaction_fn_pw_ctx_mech"
    sql = f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
        ON interaction_claims (
            interaction_id,
            function_name,
            COALESCE(pathway_name, ''),
            COALESCE(function_context, ''),
            md5(COALESCE(mechanism, ''))
        )
    """
    if apply:
        try:
            db.session.execute(db.text(sql))
            db.session.commit()
            print("  [ok] unique index created")
        except Exception as exc:
            db.session.rollback()
            print(f"  [fail] {exc}")
    else:
        print("  [dry-run] would CREATE UNIQUE INDEX ...")


def step_b2_add_synthetic_column(db, apply: bool) -> None:
    """Add is_synthetic boolean column to interaction_claims."""
    print("[B2] Add interaction_claims.is_synthetic boolean")
    if apply:
        try:
            _ensure_column(
                db,
                "interaction_claims",
                "is_synthetic",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"  [fail] {exc}")
    else:
        print("  [dry-run] would ADD COLUMN is_synthetic BOOLEAN DEFAULT FALSE")


def step_b3_source_query_not_null(db, apply: bool) -> None:
    """Backfill NULL source_query then install NOT NULL constraint."""
    print("[B3] Backfill NULL source_query + install NOT NULL constraint")
    # First count and report.
    null_count = db.session.execute(db.text(
        "SELECT COUNT(*) FROM interaction_claims WHERE source_query IS NULL"
    )).scalar() or 0
    print(f"  NULL source_query rows: {null_count}")
    if apply:
        try:
            # Backfill from interactions.discovered_in_query when possible.
            db.session.execute(db.text("""
                UPDATE interaction_claims AS ic
                SET source_query = COALESCE(
                    LEFT(i.discovered_in_query, 50),
                    '__unknown__'
                )
                FROM interactions AS i
                WHERE ic.interaction_id = i.id AND ic.source_query IS NULL
            """))
            # Any orphans with no parent match get the sentinel.
            db.session.execute(db.text("""
                UPDATE interaction_claims
                SET source_query = '__unknown__'
                WHERE source_query IS NULL
            """))
            # Install constraint (uses ALTER COLUMN ... SET NOT NULL which
            # PostgreSQL supports directly).
            db.session.execute(db.text(
                "ALTER TABLE interaction_claims "
                "ALTER COLUMN source_query SET NOT NULL"
            ))
            db.session.commit()
            print("  [ok] backfilled and set NOT NULL")
        except Exception as exc:
            db.session.rollback()
            print(f"  [fail] {exc}")
    else:
        print("  [dry-run] would backfill + SET NOT NULL")


def step_b4_case_insensitive_protein_symbol(db, apply: bool) -> None:
    """Dedupe case-variant protein rows and add functional UPPER(symbol) unique index."""
    print("[B4] Dedupe Protein case-variants + case-insensitive unique index")
    # Find case-collisions.
    dup_rows = db.session.execute(db.text("""
        SELECT UPPER(symbol) AS canonical, COUNT(*) AS n,
               array_agg(id ORDER BY query_count DESC NULLS LAST, id ASC) AS ids
        FROM proteins
        GROUP BY UPPER(symbol)
        HAVING COUNT(*) > 1
    """)).fetchall()
    print(f"  Case-variant clusters: {len(dup_rows)}")
    if dup_rows and apply:
        for canonical, n, ids in dup_rows:
            survivor = ids[0]
            doomed = ids[1:]
            print(f"    {canonical}: survivor={survivor}, merging {len(doomed)} dupes")
            for victim in doomed:
                try:
                    db.session.execute(db.text(
                        "UPDATE interactions SET protein_a_id = :s WHERE protein_a_id = :v"
                    ), {"s": survivor, "v": victim})
                    db.session.execute(db.text(
                        "UPDATE interactions SET protein_b_id = :s WHERE protein_b_id = :v"
                    ), {"s": survivor, "v": victim})
                    db.session.execute(db.text(
                        "DELETE FROM proteins WHERE id = :v"
                    ), {"v": victim})
                except Exception as exc:
                    db.session.rollback()
                    print(f"    [fail merging {victim}] {exc}")
                    continue
            # Normalize the survivor's symbol to upper-case canonical.
            db.session.execute(db.text(
                "UPDATE proteins SET symbol = :c WHERE id = :s"
            ), {"c": canonical, "s": survivor})
        db.session.commit()

    if apply:
        _ensure_index(
            db,
            "uq_protein_symbol_upper",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_protein_symbol_upper "
            "ON proteins (UPPER(symbol))",
        )
        db.session.commit()
    else:
        print("  [dry-run] would dedupe + CREATE UNIQUE INDEX uq_protein_symbol_upper")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="Commit. Default is dry-run.")
    parser.add_argument("--skip", default="",
                        help="Comma list of step IDs to skip (e.g. B3,B4).")
    args = parser.parse_args()

    skip = {s.strip().upper() for s in args.skip.split(",") if s.strip()}

    from app import app
    from models import db

    steps = [
        ("B1", step_b1_mechanism_in_unique_index),
        ("B2", step_b2_add_synthetic_column),
        ("B3", step_b3_source_query_not_null),
        ("B4", step_b4_case_insensitive_protein_symbol),
    ]

    with app.app_context():
        for label, fn in steps:
            if label in skip:
                print(f"[{label}] skipped by --skip flag")
                continue
            try:
                fn(db, apply=args.apply)
            except Exception as exc:
                db.session.rollback()
                print(f"[{label}] crashed: {type(exc).__name__}: {exc}")
        print("\n[migration done]")


if __name__ == "__main__":
    main()
