#!/usr/bin/env python3
"""
Clear Pathway Tables Utility
============================

Clears pathway tables for fresh hierarchy rebuild while keeping
proteins and interactions intact.

Usage:
    python scripts/clear_pathway_tables.py           # Clear pathway tables only
    python scripts/clear_pathway_tables.py --all     # Clear ALL tables (nuclear)
    python scripts/clear_pathway_tables.py --dry-run # Preview what would be deleted
"""
import sys
import argparse
import shutil
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import app, db
from models import (
    PathwayParent,
    PathwayInteraction,
    Pathway,
    Interaction,
    Protein,
    InteractionClaim,
    IndirectChain,
    ProteinAlias,
)

# ALL pathway-related fields to clear from interaction.data
PATHWAY_DATA_FIELDS = [
    'step2_proposal',
    'step2_function_proposals',
    'step3_finalized_pathway',
    'step3_function_pathways',
]

# Fields to clear from each function in the functions array
FUNCTION_PATHWAY_FIELDS = ['step2_pathway', 'pathway']

# Cache files to delete
CACHE_FILES = [
    'cache/pathway_hierarchy_cache.json',
]

# Logs directories whose contents get wiped on --keep-pathways.
# Directory shells stay so step_logger / verify pipeline can keep writing
# on the next run without mkdir'ing. Matches the CACHE_DIRS pattern.
LOG_DIRS_CONTENTS = [
    'Logs/verification_reports',
    'Logs/cleanup_reports',
]

# Top-level Logs/ file globs to delete on --keep-pathways.
LOG_FILE_GLOBS = [
    'Logs/json_parse_failures.log*',
]

# Additional cache subdirs to empty on --keep-pathways (query-generated
# scratch space; the directories themselves are recreated lazily).
QUERY_CACHE_DIRS_CONTENTS = [
    'cache/pruned',
    'cache/hierarchy_checkpoints',
    'cache/hierarchy_reports',
]

# Cache directories to clear (contents deleted, directory kept)
CACHE_DIRS = [
    'cache/hierarchy_checkpoints',
    'cache/hierarchy_reports',
]


def _clear_cache_files(dry_run: bool = False) -> dict:
    """Clear pathway-related cache files and directories.

    Returns dict with counts of files/dirs cleared.
    """
    stats = {'files_deleted': 0, 'dirs_cleared': 0}

    # Delete individual cache files
    for rel_path in CACHE_FILES:
        cache_file = PROJECT_ROOT / rel_path
        if cache_file.exists():
            if dry_run:
                print(f"  [DRY RUN] Would delete: {rel_path}")
            else:
                cache_file.unlink()
                print(f"  Deleted: {rel_path}")
            stats['files_deleted'] += 1

    # Clear cache directories (delete contents, keep directory)
    for rel_path in CACHE_DIRS:
        cache_dir = PROJECT_ROOT / rel_path
        if cache_dir.exists() and cache_dir.is_dir():
            files_in_dir = list(cache_dir.glob('*'))
            if files_in_dir:
                if dry_run:
                    print(f"  [DRY RUN] Would clear {len(files_in_dir)} files in: {rel_path}/")
                else:
                    for f in files_in_dir:
                        if f.is_file():
                            f.unlink()
                        elif f.is_dir():
                            shutil.rmtree(f)
                    print(f"  Cleared {len(files_in_dir)} items from: {rel_path}/")
                stats['dirs_cleared'] += 1

    return stats


def _clear_logs_tree(dry_run: bool = False) -> dict:
    """Wipe step-log / verify-report / cleanup-report contents for --keep-pathways.

    Mirrors the ``_clear_cache_files`` contract: deletes CONTENTS only so
    the directory shells stay present for the next run. Covers:

      1. Per-protein step-log timestamp dirs (``Logs/<protein>/YYYYMMDD_HHMMSS``)
         and the ``quality_report.json`` snapshots alongside them.
      2. Verification and cleanup report dirs (``Logs/verification_reports/*``,
         ``Logs/cleanup_reports/*``).
      3. Top-level Logs/ file globs (``Logs/json_parse_failures.log*``).

    The ``Logs/`` root, each ``Logs/<protein>/`` dir, and the report dirs
    themselves survive so the pruner (``utils/observability``) and the
    step_logger don't need to recreate them. Pruner owns ongoing
    rotation; this helper owns the one-shot "clean slate" wipe a user
    asks for via ``--keep-pathways``.
    """
    import re
    stats = {
        'timestamp_dirs_deleted': 0,
        'quality_reports_deleted': 0,
        'contents_dirs_cleared': 0,
        'top_level_files_deleted': 0,
    }
    logs_root = PROJECT_ROOT / 'Logs'
    if not logs_root.exists():
        return stats

    ts_pattern = re.compile(r'^\d{8}_\d{6}$')
    # 1. Per-protein timestamp dirs + quality_report.json snapshots.
    for per_protein in logs_root.iterdir():
        if not per_protein.is_dir():
            continue
        if per_protein.name in ('verification_reports', 'cleanup_reports'):
            continue
        for entry in per_protein.iterdir():
            if entry.is_dir() and ts_pattern.match(entry.name):
                if dry_run:
                    print(f"  [DRY RUN] Would delete step-log dir: {entry.relative_to(PROJECT_ROOT)}")
                else:
                    shutil.rmtree(entry)
                stats['timestamp_dirs_deleted'] += 1
            elif entry.is_file() and entry.name == 'quality_report.json':
                if dry_run:
                    print(f"  [DRY RUN] Would delete: {entry.relative_to(PROJECT_ROOT)}")
                else:
                    entry.unlink()
                stats['quality_reports_deleted'] += 1

    # 2. verification_reports + cleanup_reports — wipe contents, keep dir.
    for rel in LOG_DIRS_CONTENTS:
        d = PROJECT_ROOT / rel
        if d.exists() and d.is_dir():
            files = list(d.iterdir())
            if files:
                if dry_run:
                    print(f"  [DRY RUN] Would clear {len(files)} items in: {rel}/")
                else:
                    for f in files:
                        if f.is_file():
                            f.unlink()
                        elif f.is_dir():
                            shutil.rmtree(f)
                stats['contents_dirs_cleared'] += 1

    # 3. Top-level Logs/*.log* glob patterns.
    for pattern in LOG_FILE_GLOBS:
        for f in PROJECT_ROOT.glob(pattern):
            if f.is_file():
                if dry_run:
                    print(f"  [DRY RUN] Would delete: {f.relative_to(PROJECT_ROOT)}")
                else:
                    f.unlink()
                stats['top_level_files_deleted'] += 1

    return stats


def _clear_query_cache_dirs(dry_run: bool = False) -> int:
    """Wipe the contents of query-scratch cache dirs for --keep-pathways.

    Covers ``cache/pruned``, ``cache/hierarchy_checkpoints``,
    ``cache/hierarchy_reports``. Keeps ``cache/pathway_hierarchy_cache.json``
    and ``cache/ontology_hierarchies/*`` intact — those are the curated
    inputs quick_assign relies on.
    """
    cleared = 0
    for rel in QUERY_CACHE_DIRS_CONTENTS:
        d = PROJECT_ROOT / rel
        if d.exists() and d.is_dir():
            files = list(d.iterdir())
            if files:
                if dry_run:
                    print(f"  [DRY RUN] Would clear {len(files)} items in: {rel}/")
                else:
                    for f in files:
                        if f.is_file():
                            f.unlink()
                        elif f.is_dir():
                            shutil.rmtree(f)
                cleared += 1
    return cleared


def _clear_interaction_pathway_data(interactions, dry_run: bool = False) -> int:
    """Clear ALL pathway-related fields from interactions.

    Clears:
    - Top-level fields: step2_proposal, step2_function_proposals,
      step3_finalized_pathway, step3_function_pathways
    - Per-function fields: functions[].step2_pathway, functions[].pathway

    Returns count of interactions modified.
    """
    cleared_count = 0

    for ix in interactions:
        if not ix.data:
            continue

        modified = False
        new_data = dict(ix.data)

        # Clear top-level pathway fields
        for field in PATHWAY_DATA_FIELDS:
            if field in new_data:
                del new_data[field]
                modified = True

        # Clear pathway fields from each function in functions array
        if 'functions' in new_data and isinstance(new_data['functions'], list):
            for func in new_data['functions']:
                if isinstance(func, dict):
                    for field in FUNCTION_PATHWAY_FIELDS:
                        if field in func:
                            del func[field]
                            modified = True

        if modified:
            if not dry_run:
                ix.data = new_data
            cleared_count += 1

    return cleared_count


def clear_pathway_tables(dry_run: bool = False, clear_interaction_data: bool = True):
    """Clear pathway-related tables and optionally all pathway data from interactions.

    Args:
        dry_run: Preview changes without making them
        clear_interaction_data: Also clear pathway fields from interaction.data
    """
    with app.app_context():
        # Count rows first
        parents_count = db.session.query(PathwayParent).count()
        pi_count = db.session.query(PathwayInteraction).count()
        pathways_count = db.session.query(Pathway).count()

        # Count interactions with ANY pathway data
        interactions_with_pathway_data = db.session.query(Interaction).filter(
            db.or_(
                Interaction.data.has_key('step2_proposal'),
                Interaction.data.has_key('step2_function_proposals'),
                Interaction.data.has_key('step3_finalized_pathway'),
                Interaction.data.has_key('step3_function_pathways'),
            )
        ).count()

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Pathway Tables Status:")
        print(f"  pathway_parents: {parents_count} rows")
        print(f"  pathway_interactions: {pi_count} rows")
        print(f"  pathways: {pathways_count} rows")
        print(f"\nInteraction Pathway Data:")
        print(f"  interactions with pathway data: {interactions_with_pathway_data}")
        print(f"\nCache Files:")

        # Check cache files (preview mode)
        _clear_cache_files(dry_run=True)
        print()

        if dry_run:
            print("[DRY RUN] No changes made. Run without --dry-run to delete.")
            return

        # Order matters due to foreign keys!
        deleted_parents = db.session.query(PathwayParent).delete()
        deleted_pi = db.session.query(PathwayInteraction).delete()
        deleted_pathways = db.session.query(Pathway).delete()

        # Clear ALL pathway data from interactions
        cleared_interactions = 0
        if clear_interaction_data:
            interactions = db.session.query(Interaction).filter(
                db.or_(
                    Interaction.data.has_key('step2_proposal'),
                    Interaction.data.has_key('step2_function_proposals'),
                    Interaction.data.has_key('step3_finalized_pathway'),
                    Interaction.data.has_key('step3_function_pathways'),
                )
            ).all()

            cleared_interactions = _clear_interaction_pathway_data(interactions, dry_run=False)

        db.session.commit()

        # Clear cache files
        print("\nClearing cache files...")
        _clear_cache_files(dry_run=False)

        print(f"\n{'='*50}")
        print("Summary:")
        print(f"  Deleted {deleted_parents} pathway_parents rows")
        print(f"  Deleted {deleted_pi} pathway_interactions rows")
        print(f"  Deleted {deleted_pathways} pathways rows")
        if clear_interaction_data:
            print(f"  Cleared pathway data from {cleared_interactions} interactions")
            print(f"    (fields: {', '.join(PATHWAY_DATA_FIELDS)})")
            print(f"    (per-function: {', '.join(FUNCTION_PATHWAY_FIELDS)})")
        print(f"{'='*50}")
        print("\nPathway tables cleared - ready for rebuild")
        print("\nNext steps:")
        print("  python scripts/pathway_v2/run_all_v2.py")


def clear_query_data(dry_run: bool = False):
    """Clear all query data but KEEP the pathway hierarchy.

    FK-safe delete order (child → parent):
      interaction_claims → pathway_interactions → indirect_chains →
      protein_aliases → interactions → proteins

    BUG FIX: previous version missed indirect_chains (44+ rows persisted
    across "clears" like an unwanted memory) and protein_aliases (new
    table as of the alias-resolver refactor). Both now cleared.

    Also truncates the 3 dead/empty ancillary tables that are schema-
    present but unused, so running this gives you a genuinely clean
    query-data slate — nothing left behind except the curated pathway
    hierarchy.
    """
    with app.app_context():
        counts = {
            'interaction_claims': db.session.query(InteractionClaim).count(),
            'pathway_interactions': db.session.query(PathwayInteraction).count(),
            'indirect_chains': db.session.query(IndirectChain).count(),
            'protein_aliases': db.session.query(ProteinAlias).count(),
            'interactions': db.session.query(Interaction).count(),
            'proteins': db.session.query(Protein).count(),
        }
        preserved = {
            'pathways': db.session.query(Pathway).count(),
            'pathway_parents': db.session.query(PathwayParent).count(),
        }

        # Ancillary/dead tables — included defensively in case any path
        # writes to them. These SHOULD all be 0 rows on a clean system;
        # surfaced here so the operator sees if any legacy path is
        # accidentally populating them.
        ancillary_tables = (
            'interaction_chains',
            'interaction_pathways',
            'interaction_query_hits',
            'pathway_canonical_names',
            'pathway_hierarchy',
            'pathway_hierarchy_history',
            'pathway_initial_assignments',
        )
        ancillary_counts = {}
        for tname in ancillary_tables:
            # Each count runs in its own SAVEPOINT so a missing-table
            # error (migration 20260420_0001 dropped all 7 of these)
            # doesn't abort the outer transaction — which would then
            # crash the subsequent DELETE statements with
            # ``InFailedSqlTransaction``. begin_nested() is Postgres's
            # ``SAVEPOINT``; rolling it back unwinds ONLY the failed
            # count, leaving the main transaction usable.
            savepoint = db.session.begin_nested()
            try:
                ancillary_counts[tname] = db.session.execute(
                    db.text(f'SELECT COUNT(*) FROM "{tname}"')
                ).scalar() or 0
                savepoint.commit()
            except Exception:
                savepoint.rollback()
                ancillary_counts[tname] = 0

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Tables to CLEAR:")
        for table, count in counts.items():
            marker = '  ← used to be missed!' if table in (
                'indirect_chains', 'protein_aliases'
            ) else ''
            print(f"  {table}: {count} rows{marker}")
        if any(v > 0 for v in ancillary_counts.values()):
            print(f"\nAncillary tables with stray data (also cleared):")
            for t, c in ancillary_counts.items():
                if c > 0:
                    print(f"  {t}: {c} rows")
        print(f"\nTables PRESERVED (pathway hierarchy):")
        for table, count in preserved.items():
            print(f"  {table}: {count} rows")

        # Check cache
        proteins_cache = PROJECT_ROOT / 'cache' / 'proteins'
        cache_count = 0
        if proteins_cache.exists():
            cache_count = len(list(proteins_cache.iterdir()))
            print(f"\nCache: cache/proteins/ ({cache_count} protein directories)")

        # Preview query-scratch cache dirs + Logs/ wipe when dry-running
        if dry_run:
            print(f"\nQuery-scratch cache dirs to wipe:")
            _clear_query_cache_dirs(dry_run=True)
            print(f"\nLogs/ artifacts to wipe:")
            _clear_logs_tree(dry_run=True)

        if dry_run:
            print("\n[DRY RUN] No changes made. Run without --dry-run to delete.")
            return

        total = sum(counts.values())
        print(f"\nWARNING: This will delete {total} rows but KEEP "
              f"{sum(preserved.values())} pathway rows!")
        response = input("Type 'yes' to confirm: ")
        if response.lower() != 'yes':
            print("Aborted.")
            return

        # FK-safe delete order. InteractionClaim and PathwayInteraction
        # refer to Interaction; IndirectChain refers to Interaction (via
        # origin_interaction_id) AND Pathway (SET NULL) AND is referred
        # to by InteractionClaim + Interaction (SET NULL). ProteinAlias
        # refers to Protein. Interaction refers to Protein. So:
        #   1. claims      (leaf — child of interaction, chain, pathway)
        #   2. PI          (leaf — child of interaction, pathway)
        #   3. chains      (child of interaction; will cascade on its own
        #                    via origin_interaction FK, but we delete
        #                    explicitly so the counts are meaningful)
        #   4. aliases     (leaf — child of protein)
        #   5. interactions (child of protein)
        #   6. proteins    (leaf after all above)
        d_claims = db.session.query(InteractionClaim).delete()
        d_pi = db.session.query(PathwayInteraction).delete()
        d_chains = db.session.query(IndirectChain).delete()
        d_aliases = db.session.query(ProteinAlias).delete()
        d_ix = db.session.query(Interaction).delete()
        d_proteins = db.session.query(Protein).delete()

        # Ancillary tables — raw SQL so we don't need an ORM class.
        # Each DELETE runs inside a SAVEPOINT so a missing-table error
        # (migration 20260420_0001 dropped all 7) can't abort the outer
        # transaction and leave the real deletes above un-committable.
        ancillary_deleted = {}
        for tname in ancillary_tables:
            savepoint = db.session.begin_nested()
            try:
                res = db.session.execute(db.text(f'DELETE FROM "{tname}"'))
                ancillary_deleted[tname] = res.rowcount or 0
                savepoint.commit()
            except Exception as e:
                savepoint.rollback()
                ancillary_deleted[tname] = f'err: {type(e).__name__}'

        db.session.commit()

        # Clear protein cache directories
        if proteins_cache.exists() and cache_count > 0:
            shutil.rmtree(proteins_cache)
            proteins_cache.mkdir(exist_ok=True)
            print(f"\n  Cleared cache/proteins/ ({cache_count} directories)")

        # Per-protein cache JSONs
        cache_dir = PROJECT_ROOT / 'cache'
        json_cleared = 0
        if cache_dir.exists():
            for f in cache_dir.glob('*.json'):
                if f.name != 'pathway_hierarchy_cache.json':
                    f.unlink()
                    json_cleared += 1
            if json_cleared:
                print(f"  Cleared {json_cleared} cache/*.json files "
                      "(kept pathway_hierarchy_cache.json)")

        # Wipe query-scratch cache subdirs (cache/pruned, hierarchy_checkpoints,
        # hierarchy_reports). Keep the curated pathway_hierarchy_cache.json
        # and cache/ontology_hierarchies/* — those are inputs quick_assign
        # relies on.
        scratch_cleared = _clear_query_cache_dirs(dry_run=False)
        if scratch_cleared:
            print(f"  Cleared {scratch_cleared} query-scratch cache dir(s)")

        # Wipe Logs/ legacy so --keep-pathways leaves a truly pristine slate.
        log_stats = _clear_logs_tree(dry_run=False)
        if sum(log_stats.values()):
            print(
                f"  Cleared Logs/: "
                f"{log_stats['timestamp_dirs_deleted']} step-log run dir(s), "
                f"{log_stats['quality_reports_deleted']} quality_report.json, "
                f"{log_stats['contents_dirs_cleared']} verify/cleanup report dir(s), "
                f"{log_stats['top_level_files_deleted']} top-level .log file(s)"
            )

        print(f"\n{'='*50}")
        print("Summary:")
        print(f"  Deleted {d_claims:>6} interaction_claims")
        print(f"  Deleted {d_pi:>6} pathway_interactions")
        print(f"  Deleted {d_chains:>6} indirect_chains         ← previously missed")
        print(f"  Deleted {d_aliases:>6} protein_aliases         ← previously missed")
        print(f"  Deleted {d_ix:>6} interactions")
        print(f"  Deleted {d_proteins:>6} proteins")
        if any(v for v in ancillary_deleted.values() if isinstance(v, int) and v > 0):
            print(f"\n  Ancillary cleanup:")
            for t, n in ancillary_deleted.items():
                if isinstance(n, int) and n > 0:
                    print(f"    {t}: {n}")
        print(f"\n  PRESERVED {preserved['pathways']} pathways")
        print(f"  PRESERVED {preserved['pathway_parents']} pathway_parents")
        print(f"{'='*50}")
        print("\nQuery data cleared. Pathway hierarchy intact.")
        print("Re-query any protein — the pipeline will reuse existing pathways.")


def clear_all_tables(dry_run: bool = False):
    """Clear ALL tables (nuclear option)."""
    with app.app_context():
        # Count rows first
        counts = {
            'interaction_claims': db.session.query(InteractionClaim).count(),
            'pathway_parents': db.session.query(PathwayParent).count(),
            'pathway_interactions': db.session.query(PathwayInteraction).count(),
            'pathways': db.session.query(Pathway).count(),
            'interactions': db.session.query(Interaction).count(),
            'proteins': db.session.query(Protein).count(),
        }

        print(f"\n{'[DRY RUN] ' if dry_run else ''}ALL Tables Status:")
        for table, count in counts.items():
            print(f"  {table}: {count} rows")
        print()

        if dry_run:
            print("[DRY RUN] No changes made. Run without --dry-run to delete.")
            return

        # Confirmation for destructive operation
        total = sum(counts.values())
        print(f"WARNING: This will delete {total} rows across ALL tables!")
        response = input("Type 'yes' to confirm: ")
        if response.lower() != 'yes':
            print("Aborted.")
            return

        # FK-safe order: every child before its parent.
        db.session.query(InteractionClaim).delete()
        db.session.query(PathwayParent).delete()
        db.session.query(PathwayInteraction).delete()
        db.session.query(IndirectChain).delete()        # was missed
        db.session.query(ProteinAlias).delete()         # was missed
        db.session.query(Pathway).delete()
        db.session.query(Interaction).delete()
        db.session.query(Protein).delete()

        # Ancillary / dead tables.
        for tname in (
            'interaction_chains',
            'interaction_pathways',
            'interaction_query_hits',
            'pathway_canonical_names',
            'pathway_hierarchy',
            'pathway_hierarchy_history',
            'pathway_initial_assignments',
        ):
            try:
                db.session.execute(db.text(f'DELETE FROM "{tname}"'))
            except Exception:
                pass

        db.session.commit()

        # Clear ALL cache
        print("\nClearing cache files...")
        _clear_cache_files(dry_run=False)

        print("\nALL tables cleared")
        print("\nNext steps:")
        print("  1. Re-query a protein: curl -X POST localhost:5000/api/query -d '{\"protein\":\"ATXN3\"}'")
        print("  2. Run hierarchy pipeline: python scripts/pathway_v2/run_all_v2.py")


def main():
    parser = argparse.ArgumentParser(
        description="Clear pathway tables for fresh hierarchy rebuild",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/clear_pathway_tables.py                    # Clear pathway tables + ALL interaction pathway data
  python scripts/clear_pathway_tables.py --keep-assignments # Clear tables but keep pathway data in interactions
  python scripts/clear_pathway_tables.py --keep-pathways    # Clear query data but KEEP pathway hierarchy
  python scripts/clear_pathway_tables.py --all              # Clear ALL tables (nuclear)
  python scripts/clear_pathway_tables.py --dry-run          # Preview what would be deleted

Fields cleared from interaction.data:
  - step2_proposal, step2_function_proposals
  - step3_finalized_pathway, step3_function_pathways
  - functions[].step2_pathway, functions[].pathway

Cache files cleared:
  - cache/pathway_hierarchy_cache.json
  - cache/hierarchy_checkpoints/*
  - cache/hierarchy_reports/*
        """
    )
    parser.add_argument(
        '--all',
        action='store_true',
        help='Clear ALL tables including proteins and interactions (nuclear option)'
    )
    parser.add_argument(
        '--keep-pathways',
        action='store_true',
        help='Clear proteins/interactions/claims but KEEP pathway hierarchy'
    )
    parser.add_argument(
        '--keep-assignments',
        action='store_true',
        help='Keep pathway assignments in interaction.data (only clear tables)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview what would be deleted without making changes'
    )

    args = parser.parse_args()

    if args.all:
        clear_all_tables(dry_run=args.dry_run)
    elif args.keep_pathways:
        clear_query_data(dry_run=args.dry_run)
    else:
        clear_pathway_tables(
            dry_run=args.dry_run,
            clear_interaction_data=not args.keep_assignments
        )


if __name__ == "__main__":
    main()
