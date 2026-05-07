#!/usr/bin/env python3
"""Backfill NULL function_context on InteractionClaim rows.

Handles COALESCE unique index collisions: if setting NULL→'direct' would
duplicate an existing claim, the NULL claim is merged into the existing one
(evidence + PMIDs unioned) and deleted.

Also repairs orphan chain_id, fragmented chains, stale JSONB, missing
PathwayInteraction records.

Idempotent: safe to run multiple times.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def run_migration():
    from app import app
    from models import db, Interaction, InteractionClaim, IndirectChain, Pathway, PathwayInteraction
    from sqlalchemy import text, func as sqla_func

    with app.app_context():
        # ── R1: Backfill NULL function_context ──
        null_ctx = InteractionClaim.query.filter(
            InteractionClaim.function_context.is_(None)
        ).all()
        print(f"[R1] Claims with NULL function_context: {len(null_ctx)}")

        fixed = 0
        merged = 0
        for claim in null_ctx:
            # Determine the target context
            if claim.chain_id is not None:
                target_ctx = "chain_derived"
            elif claim.interaction and claim.interaction.interaction_type == "indirect":
                target_ctx = "net"
            else:
                target_ctx = "direct"

            # Check for collision: another claim with the same
            # (interaction_id, function_name, pathway_name, target_ctx)
            collision = (
                InteractionClaim.query
                .filter(InteractionClaim.interaction_id == claim.interaction_id)
                .filter(InteractionClaim.function_name == claim.function_name)
                .filter(
                    sqla_func.coalesce(InteractionClaim.pathway_name, "")
                    == (claim.pathway_name or "")
                )
                .filter(InteractionClaim.function_context == target_ctx)
                .filter(InteractionClaim.id != claim.id)
                .first()
            )

            if collision:
                # Merge: union evidence + PMIDs onto the existing claim, delete this one
                existing_ev_keys = set()
                for ev in (collision.evidence or []):
                    if isinstance(ev, dict):
                        title = (ev.get("paper_title") or "").strip().lower()
                        existing_ev_keys.add(title)

                new_evidence = list(collision.evidence or [])
                for ev in (claim.evidence or []):
                    if isinstance(ev, dict):
                        title = (ev.get("paper_title") or "").strip().lower()
                        if title and title not in existing_ev_keys:
                            new_evidence.append(ev)
                            existing_ev_keys.add(title)

                collision.evidence = new_evidence
                collision.pmids = list(
                    set(collision.pmids or []) | set(claim.pmids or [])
                )

                # Prefer longer mechanism/effect_description
                for field in ("mechanism", "effect_description"):
                    existing_val = getattr(collision, field) or ""
                    claim_val = getattr(claim, field) or ""
                    if len(claim_val) > len(existing_val):
                        setattr(collision, field, claim_val)

                db.session.delete(claim)
                merged += 1
            else:
                claim.function_context = target_ctx
                fixed += 1

        db.session.commit()
        print(f"[R1] Fixed {fixed}, merged {merged} duplicate(s)")

        # ── R4: Backfill orphan chain_id claims ──
        orphan_result = db.session.execute(text("""
            UPDATE interaction_claims AS ic
            SET chain_id = i.chain_id
            FROM interactions AS i
            WHERE ic.interaction_id = i.id
              AND ic.chain_id IS NULL
              AND i.chain_id IS NOT NULL
        """))
        orphan_count = orphan_result.rowcount or 0
        db.session.commit()
        print(f"[R4] Backfilled chain_id on {orphan_count} orphan claims")

        # ── R5: Unify fragmented chains ──
        from scripts.pathway_v2.quick_assign import _unify_all_chain_claims
        _unify_all_chain_claims(db, IndirectChain, InteractionClaim, Pathway)
        db.session.commit()
        print("[R5] Unified all chain claims")

        # ── R3: Sync PathwayInteraction + JSONB ──
        from scripts.pathway_v2.quick_assign import (
            _sync_pathway_interactions,
            _sync_interaction_finalized_pathway,
        )
        _sync_pathway_interactions(db, InteractionClaim, PathwayInteraction)
        _sync_interaction_finalized_pathway(db, InteractionClaim)
        db.session.commit()
        print("[R3] Synced PathwayInteraction + JSONB")

        # ── Verify ──
        null_remaining = InteractionClaim.query.filter(
            InteractionClaim.function_context.is_(None)
        ).count()
        fragmented = 0
        for chain in IndirectChain.query.all():
            claims = InteractionClaim.query.filter_by(chain_id=chain.id).all()
            pw_set = set(c.pathway_name for c in claims if c.pathway_name)
            if len(pw_set) > 1:
                fragmented += 1
        missing_pi = db.session.execute(text('''
            SELECT COUNT(*) FROM interaction_claims ic
            LEFT JOIN pathway_interactions pi
                ON pi.interaction_id = ic.interaction_id AND pi.pathway_id = ic.pathway_id
            WHERE ic.pathway_id IS NOT NULL AND pi.id IS NULL
        ''')).scalar()

        print()
        print("=== VERIFICATION ===")
        print(f"  NULL function_context: {null_remaining} (target: 0)")
        print(f"  Fragmented chains: {fragmented} (target: 0)")
        print(f"  Missing PathwayInteraction: {missing_pi} (target: 0)")


if __name__ == "__main__":
    run_migration()
