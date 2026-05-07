#!/usr/bin/env python3
"""
Migration: Extract interaction claims from JSONB blobs + backfill from cache.

Step A: Iterates all Interaction rows, extracts functions[] from data JSONB,
        creates one InteractionClaim per function.
Step B: Backfills from file cache to close the DB vs cache data gap.

Usage:
    python scripts/migrate_claims.py              # Run full migration
    python scripts/migrate_claims.py --db-only    # Only extract from DB
    python scripts/migrate_claims.py --cache-only # Only backfill from cache
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not os.environ.get("DATABASE_URL"):
    print("ERROR: Set DATABASE_URL environment variable.", file=sys.stderr)
    sys.exit(1)

from pathlib import Path
from datetime import datetime


def extract_pathway_string(raw):
    """Extract pathway name from str or dict format."""
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        return raw.get("canonical_name") or raw.get("name")
    return None


def resolve_pathway_id(db, pathway_name):
    """Resolve pathway name to DB id."""
    if not pathway_name:
        return None
    from models import Pathway
    pw = Pathway.query.filter_by(name=pathway_name).first()
    return pw.id if pw else None


def step_a_extract_from_db(app):
    """Extract claims from existing Interaction.data JSONB blobs."""
    from models import db, Interaction, InteractionClaim

    with app.app_context():
        interactions = Interaction.query.all()
        total_claims = 0
        total_interactions = len(interactions)

        print(f"[Step A] Processing {total_interactions} interactions...")

        for i, interaction in enumerate(interactions):
            data = interaction.data or {}
            functions = data.get("functions", [])
            discovered_in = interaction.discovered_in_query or ""
            fallback_pathway = data.get("step3_finalized_pathway")

            if not functions:
                # Synthetic claim from interaction-level data
                summary = data.get("support_summary") or data.get("summary") or ""
                mechanism = data.get("mechanism") or data.get("effect") or ""
                if summary or mechanism:
                    func_name = summary[:200] if summary else "Uncharacterized interaction"
                    existing = InteractionClaim.query.filter_by(
                        interaction_id=interaction.id,
                        function_name=func_name,
                        pathway_name=fallback_pathway,
                    ).first()
                    if not existing:
                        claim = InteractionClaim(
                            interaction_id=interaction.id,
                            function_name=func_name,
                            arrow=data.get("arrow"),
                            interaction_effect=data.get("interaction_effect"),
                            direction=data.get("direction"),
                            mechanism=mechanism,
                            effect_description=data.get("effect"),
                            biological_consequences=[],
                            specific_effects=[],
                            evidence=data.get("evidence", []),
                            pmids=data.get("pmids", []),
                            pathway_name=fallback_pathway,
                            pathway_id=resolve_pathway_id(db, fallback_pathway),
                            confidence=data.get("confidence"),
                            source_query=discovered_in,
                            discovery_method="migration",
                            raw_function_data=None,
                        )
                        db.session.add(claim)
                        total_claims += 1
                else:
                    # Last resort: minimal claim from arrow
                    arrow_val = data.get("arrow") or interaction.arrow or "interacts"
                    func_name = f"{arrow_val} interaction"
                    existing = InteractionClaim.query.filter_by(
                        interaction_id=interaction.id,
                        function_name=func_name,
                        pathway_name=fallback_pathway,
                    ).first()
                    if not existing:
                        claim = InteractionClaim(
                            interaction_id=interaction.id,
                            function_name=func_name,
                            arrow=arrow_val,
                            interaction_effect=data.get("interaction_effect"),
                            direction=data.get("direction"),
                            pathway_name=fallback_pathway,
                            pathway_id=resolve_pathway_id(db, fallback_pathway),
                            source_query=discovered_in,
                            discovery_method="migration",
                        )
                        db.session.add(claim)
                        total_claims += 1
                continue

            for func in functions:
                func_name = (func.get("function") or "").strip()
                if not func_name:
                    continue

                pathway_name = extract_pathway_string(func.get("pathway")) or fallback_pathway

                existing = InteractionClaim.query.filter_by(
                    interaction_id=interaction.id,
                    function_name=func_name,
                    pathway_name=pathway_name,
                ).first()

                if existing:
                    continue  # Skip duplicates

                claim = InteractionClaim(
                    interaction_id=interaction.id,
                    function_name=func_name,
                    arrow=func.get("arrow"),
                    interaction_effect=func.get("interaction_effect") or func.get("function_effect"),
                    direction=func.get("interaction_direction") or func.get("likely_direction"),
                    mechanism=func.get("cellular_process"),
                    effect_description=func.get("effect_description"),
                    biological_consequences=func.get("biological_consequence", []),
                    specific_effects=func.get("specific_effects", []),
                    evidence=func.get("evidence", []),
                    pmids=func.get("pmids", []),
                    pathway_name=pathway_name,
                    pathway_id=resolve_pathway_id(db, pathway_name),
                    confidence=func.get("confidence"),
                    function_context=func.get("function_context"),
                    context_data=func.get("_context"),
                    source_query=discovered_in,
                    discovery_method="migration",
                    raw_function_data=func,
                )
                db.session.add(claim)
                total_claims += 1

            # Batch commit every 50 interactions
            if (i + 1) % 50 == 0:
                db.session.commit()
                print(f"  [{i+1}/{total_interactions}] Committed {total_claims} claims so far...")

        db.session.commit()
        print(f"[Step A] Done: {total_claims} claims created from {total_interactions} interactions")
        return total_claims


def step_b_backfill_from_cache(app):
    """Backfill missing interactions from file cache, then extract their claims."""
    from models import db, Protein, Interaction
    from utils.db_sync import DatabaseSyncLayer

    cache_dir = Path("cache/proteins")
    if not cache_dir.exists():
        print("[Step B] No cache/proteins directory found, skipping")
        return 0

    sync = DatabaseSyncLayer()
    total_synced = 0
    total_claims = 0

    with app.app_context():
        protein_dirs = sorted(cache_dir.iterdir())
        print(f"[Step B] Found {len(protein_dirs)} cached proteins")

        for protein_dir in protein_dirs:
            if not protein_dir.is_dir():
                continue
            protein_symbol = protein_dir.name
            interactions_dir = protein_dir / "interactions"
            if not interactions_dir.exists():
                continue

            cache_files = list(interactions_dir.glob("*.json"))
            if not cache_files:
                continue

            # Check how many DB rows exist for this protein
            protein = Protein.query.filter_by(symbol=protein_symbol).first()
            if not protein:
                continue

            db_count = Interaction.query.filter(
                (Interaction.protein_a_id == protein.id) |
                (Interaction.protein_b_id == protein.id)
            ).count()

            if db_count >= len(cache_files):
                continue  # DB already has all cached interactions

            print(f"  {protein_symbol}: {len(cache_files)} cached, {db_count} in DB — backfilling {len(cache_files) - db_count} missing")

            # Build snapshot from cache files for sync
            interactors = []
            for json_file in sorted(cache_files):
                try:
                    with open(json_file) as f:
                        data = json.load(f)
                    interactors.append(data)
                except (json.JSONDecodeError, IOError) as e:
                    print(f"    WARN: Failed to read {json_file}: {e}", file=sys.stderr)

            if not interactors:
                continue

            # Sync via the standard pipeline
            snapshot = {
                "snapshot_json": {"interactors": interactors},
                "ctx_json": {"main": protein_symbol, "interactors": interactors},
            }

            try:
                stats = sync.sync_query_results(protein_symbol, snapshot)
                synced = stats.get("created", 0) + stats.get("updated", 0)
                total_synced += synced
                if synced > 0:
                    print(f"    Synced {synced} interactions")
            except Exception as e:
                print(f"    ERROR syncing {protein_symbol}: {e}", file=sys.stderr)
                db.session.rollback()

        db.session.commit()
        print(f"[Step B] Done: {total_synced} interactions backfilled from cache")
        return total_synced


def cleanup_duplicate_claims(app):
    """Remove duplicate claims (same interaction_id + function_name, different pathway_name)."""
    from models import db, InteractionClaim
    from sqlalchemy import func as sqlfunc

    with app.app_context():
        dupes = db.session.query(
            InteractionClaim.interaction_id,
            InteractionClaim.function_name,
        ).group_by(
            InteractionClaim.interaction_id,
            InteractionClaim.function_name,
        ).having(sqlfunc.count(InteractionClaim.id) > 1).all()

        deleted = 0
        for interaction_id, function_name in dupes:
            claims = InteractionClaim.query.filter_by(
                interaction_id=interaction_id,
                function_name=function_name,
            ).order_by(InteractionClaim.id).all()

            # Keep the one with pathway_name set (prefer richer data)
            keep = None
            for c in claims:
                if c.pathway_name:
                    keep = c
                    break
            if not keep:
                keep = claims[0]

            for c in claims:
                if c.id != keep.id:
                    db.session.delete(c)
                    deleted += 1

        db.session.commit()
        print(f"[Cleanup] Deleted {deleted} duplicate claims")
        return deleted


def main():
    from app import app

    mode = sys.argv[1] if len(sys.argv) > 1 else None

    if mode == "--cleanup":
        with app.app_context():
            from models import InteractionClaim
            before = InteractionClaim.query.count()
            print(f"Claims before cleanup: {before}")
        cleanup_duplicate_claims(app)
        with app.app_context():
            from models import InteractionClaim
            after = InteractionClaim.query.count()
            print(f"Claims after cleanup: {after} (-{before - after})")
        return

    with app.app_context():
        from models import InteractionClaim
        before = InteractionClaim.query.count()
        print(f"Claims before migration: {before}")

    if mode != "--cache-only":
        step_a_extract_from_db(app)

    if mode != "--db-only":
        step_b_backfill_from_cache(app)
        # Re-run Step A to extract claims from newly backfilled interactions
        if mode != "--db-only":
            print("\n[Re-running Step A for newly backfilled interactions...]")
            step_a_extract_from_db(app)

    with app.app_context():
        from models import InteractionClaim
        after = InteractionClaim.query.count()
        print(f"\nClaims after migration: {after} (+{after - before})")


if __name__ == "__main__":
    main()
