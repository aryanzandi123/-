#!/usr/bin/env python3
"""Health + sanity check for the ProPaths Postgres DB.

Prints:
  1. Row counts per table (grouped: active / ancillary-dead / system).
  2. FK integrity spot-checks (orphan claims, orphan chains, dangling PIs).
  3. Pathway-hierarchy shape checks (pathways without parents, cycle hints).
  4. Clear-script leftover detection — anything that SHOULD be empty after
     ``clear_pathway_tables.py --keep-pathways`` but isn't.
  5. Schema drift warnings — duplicate unique indexes, case-variant
     Protein.symbol collisions, stale Interaction.arrow vs arrows.

Runs read-only. Safe to execute against production.

Usage::

    python3 scripts/db_health_check.py
    python3 scripts/db_health_check.py --json  # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Tables the system actively uses. Rows here are expected after a query run.
ACTIVE_TABLES = (
    "proteins",
    "protein_aliases",
    "interactions",
    "interaction_claims",
    "indirect_chains",
    "pathway_interactions",
    "pathways",
    "pathway_parents",
)

# Tables present in the schema but not actively written to. Rows here are
# a red flag — either legacy migration residue or a forgotten writer.
DEAD_TABLES = (
    "interaction_chains",
    "interaction_pathways",
    "interaction_query_hits",
    "pathway_canonical_names",
    "pathway_hierarchy",
    "pathway_hierarchy_history",
    "pathway_initial_assignments",
)

# Tables that should be empty IMMEDIATELY after
# ``clear_pathway_tables.py --keep-pathways`` completes successfully.
SHOULD_BE_EMPTY_AFTER_CLEAR = (
    "proteins",
    "protein_aliases",
    "interactions",
    "interaction_claims",
    "indirect_chains",
    "pathway_interactions",
) + DEAD_TABLES


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of pretty text.")
    args = p.parse_args()

    from app import app
    from models import db
    from sqlalchemy import text

    report: dict = {}

    with app.app_context():
        # 1. Row counts.
        def count(t: str) -> int:
            try:
                return db.session.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0
            except Exception:
                return -1

        report["row_counts"] = {
            "active": {t: count(t) for t in ACTIVE_TABLES},
            "dead": {t: count(t) for t in DEAD_TABLES},
        }

        # 2. FK integrity.
        orphans: dict = {}

        orphans["claims_with_missing_interaction"] = db.session.execute(text(
            "SELECT COUNT(*) FROM interaction_claims c "
            "LEFT JOIN interactions i ON c.interaction_id = i.id "
            "WHERE i.id IS NULL"
        )).scalar() or 0

        orphans["claims_with_missing_chain"] = db.session.execute(text(
            "SELECT COUNT(*) FROM interaction_claims c "
            "WHERE c.chain_id IS NOT NULL "
            "AND c.chain_id NOT IN (SELECT id FROM indirect_chains)"
        )).scalar() or 0

        orphans["chains_with_missing_origin_interaction"] = db.session.execute(text(
            "SELECT COUNT(*) FROM indirect_chains c "
            "WHERE c.origin_interaction_id NOT IN (SELECT id FROM interactions)"
        )).scalar() or 0

        orphans["interactions_with_missing_chain_id"] = db.session.execute(text(
            "SELECT COUNT(*) FROM interactions i "
            "WHERE i.chain_id IS NOT NULL "
            "AND i.chain_id NOT IN (SELECT id FROM indirect_chains)"
        )).scalar() or 0

        orphans["pathway_interactions_missing_parent"] = db.session.execute(text(
            "SELECT COUNT(*) FROM pathway_interactions pi "
            "WHERE pi.pathway_id NOT IN (SELECT id FROM pathways) "
            "   OR pi.interaction_id NOT IN (SELECT id FROM interactions)"
        )).scalar() or 0

        orphans["aliases_with_missing_protein"] = db.session.execute(text(
            "SELECT COUNT(*) FROM protein_aliases a "
            "WHERE a.protein_id NOT IN (SELECT id FROM proteins)"
        )).scalar() or 0

        report["fk_orphans"] = orphans

        # 3. Pathway hierarchy shape.
        hierarchy: dict = {}
        hierarchy["orphan_pathways_no_parent_and_not_root"] = db.session.execute(text(
            "SELECT COUNT(*) FROM pathways p "
            "WHERE p.id NOT IN (SELECT child_pathway_id FROM pathway_parents) "
            "AND p.hierarchy_level > 0"
        )).scalar() or 0

        hierarchy["pathways_with_self_parent"] = db.session.execute(text(
            "SELECT COUNT(*) FROM pathway_parents WHERE child_pathway_id = parent_pathway_id"
        )).scalar() or 0

        hierarchy["pathways_total"] = count("pathways")
        hierarchy["pathway_parents_total"] = count("pathway_parents")

        report["hierarchy"] = hierarchy

        # 4. Schema drift.
        drift: dict = {}

        drift["protein_symbol_case_collisions"] = [
            dict(r._mapping)
            for r in db.session.execute(text(
                "SELECT UPPER(symbol) AS canonical, COUNT(*) AS n "
                "FROM proteins GROUP BY UPPER(symbol) HAVING COUNT(*) > 1 "
                "LIMIT 20"
            ))
        ]

        drift["interactions_with_arrow_but_no_arrows_jsonb"] = db.session.execute(text(
            "SELECT COUNT(*) FROM interactions "
            "WHERE arrow IS NOT NULL AND arrows IS NULL"
        )).scalar() or 0

        drift["interactions_with_arrows_disagreeing_with_arrow"] = db.session.execute(text(
            "SELECT COUNT(*) FROM interactions "
            "WHERE arrows IS NOT NULL AND arrow IS NOT NULL "
            "AND COALESCE(arrows->'a_to_b'->>0, arrows->'b_to_a'->>0) <> arrow"
        )).scalar() or 0

        drift["null_source_query_claims"] = db.session.execute(text(
            "SELECT COUNT(*) FROM interaction_claims WHERE source_query IS NULL"
        )).scalar() or 0

        drift["duplicate_unique_indexes_on_claims"] = [
            r[0] for r in db.session.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'interaction_claims' AND indexname LIKE 'uq_claim%'"
            ))
        ]

        report["drift"] = drift

        # 5. Leftover check — these tables SHOULD all be zero after a
        # successful --keep-pathways run. Highlight any that aren't.
        report["leftover_after_clear_keep_pathways"] = {
            t: count(t)
            for t in SHOULD_BE_EMPTY_AFTER_CLEAR
            if count(t) > 0
        }

    # Render.
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    print("\n" + "=" * 70)
    print("PROPATHS DB HEALTH REPORT")
    print("=" * 70)

    print("\n── row counts ──")
    print("  active tables:")
    for t, n in report["row_counts"]["active"].items():
        print(f"    {t:25} {n}")
    print("  dead / ancillary tables (SHOULD all be 0):")
    dead_hot = False
    for t, n in report["row_counts"]["dead"].items():
        marker = "  ← non-empty!" if n > 0 else ""
        if n > 0:
            dead_hot = True
        print(f"    {t:25} {n}{marker}")
    if not dead_hot:
        print("    (all clean)")

    print("\n── FK orphans (all should be 0) ──")
    any_orphans = False
    for k, v in report["fk_orphans"].items():
        marker = "  ← ORPHAN!" if v > 0 else ""
        if v > 0:
            any_orphans = True
        print(f"  {k:48} {v}{marker}")
    if not any_orphans:
        print("  (no orphans)")

    print("\n── hierarchy ──")
    for k, v in report["hierarchy"].items():
        print(f"  {k:48} {v}")

    print("\n── schema drift ──")
    if report["drift"]["protein_symbol_case_collisions"]:
        print("  CASE-VARIANT Protein.symbol collisions:")
        for c in report["drift"]["protein_symbol_case_collisions"]:
            print(f"    {c}")
    print(f"  interactions w/ arrow but NULL arrows: "
          f"{report['drift']['interactions_with_arrow_but_no_arrows_jsonb']}")
    print(f"  arrow ↔ arrows disagreements:         "
          f"{report['drift']['interactions_with_arrows_disagreeing_with_arrow']}")
    print(f"  NULL source_query claims:             "
          f"{report['drift']['null_source_query_claims']}")
    print(f"  unique indexes on claims:             "
          f"{report['drift']['duplicate_unique_indexes_on_claims']}")

    leftover = report["leftover_after_clear_keep_pathways"]
    if leftover:
        print("\n── LEFTOVER after --keep-pathways (these ARE the bug) ──")
        for t, n in leftover.items():
            print(f"    {t}: {n}")
    else:
        print("\n── leftover after --keep-pathways: clean ──")

    print()


if __name__ == "__main__":
    main()
