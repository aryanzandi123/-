#!/usr/bin/env python3
"""S1 Migration: Convert every direction='bidirectional' row to asymmetric.

For Interaction rows:
  - If the primary arrow is asymmetric (activates, inhibits, regulates, …),
    the direction becomes 'a_to_b' (the canonical "query acts on partner").
  - If the arrow is symmetric (binds, complex, interacts), the direction
    also becomes 'a_to_b' by convention — in the pipeline's query-centric
    model, the query protein is always the canonical subject.

For InteractionClaim rows:
  - Same logic using the claim's arrow field.

Idempotent: running twice is a no-op (no bidirectional rows remain).
Safe to run on an empty database.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def run_migration():
    from app import app
    from models import db, Interaction, InteractionClaim

    with app.app_context():
        # --- Interactions ---
        bidir_interactions = Interaction.query.filter_by(direction="bidirectional").all()
        fixed_interactions = 0

        for inter in bidir_interactions:
            inter.direction = "a_to_b"
            fixed_interactions += 1

        if fixed_interactions:
            db.session.commit()
            print(f"[MIGRATE] Fixed {fixed_interactions} Interaction rows: direction 'bidirectional' → 'a_to_b'")
        else:
            print("[MIGRATE] No Interaction rows with direction='bidirectional' found.")

        # --- InteractionClaims ---
        bidir_claims = InteractionClaim.query.filter_by(direction="bidirectional").all()
        fixed_claims = 0

        for claim in bidir_claims:
            claim.direction = "main_to_primary"
            fixed_claims += 1

        if fixed_claims:
            db.session.commit()
            print(f"[MIGRATE] Fixed {fixed_claims} InteractionClaim rows: direction 'bidirectional' → 'main_to_primary'")
        else:
            print("[MIGRATE] No InteractionClaim rows with direction='bidirectional' found.")

        # --- Interaction.arrows JSONB: fold 'bidirectional' key into 'a_to_b' ---
        interactions_with_bidir_arrows = Interaction.query.filter(
            Interaction.arrows.isnot(None)
        ).all()
        fixed_arrows = 0

        for inter in interactions_with_bidir_arrows:
            if not isinstance(inter.arrows, dict):
                continue
            if "bidirectional" not in inter.arrows:
                continue
            bidir_vals = inter.arrows.pop("bidirectional")
            existing = inter.arrows.get("a_to_b", [])
            for val in (bidir_vals or []):
                if val not in existing:
                    existing.append(val)
            inter.arrows["a_to_b"] = existing
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(inter, "arrows")
            fixed_arrows += 1

        if fixed_arrows:
            db.session.commit()
            print(f"[MIGRATE] Folded 'bidirectional' arrow key into 'a_to_b' on {fixed_arrows} Interaction rows")
        else:
            print("[MIGRATE] No Interaction.arrows dicts with 'bidirectional' key found.")

        print(f"[MIGRATE] Done. Total: {fixed_interactions} interactions, {fixed_claims} claims, {fixed_arrows} arrow dicts fixed.")


if __name__ == "__main__":
    run_migration()
