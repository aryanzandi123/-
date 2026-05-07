#!/usr/bin/env python3
"""
Cleanup: Remove redundant safety-net claims from interaction_claims table.

Deletes auto-generated garbage claims like "activates interaction" when the
same interaction has real scientific claims.  Leaves safety-net claims in place
if they are the ONLY claim for an interaction (legitimate fallback).

Usage:
    python scripts/cleanup_safety_net_claims.py           # Dry-run (report only)
    python scripts/cleanup_safety_net_claims.py --apply   # Actually delete
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not os.environ.get("DATABASE_URL"):
    sys.exit("ERROR: DATABASE_URL environment variable is required. Set it before running this script.")

from app import app
from models import db, InteractionClaim

# Pattern matching auto-generated claim names
GARBAGE_PATTERN = re.compile(
    r'^(activates?|inhibits?|binds?|regulates?|interacts?) interaction$', re.IGNORECASE
)


def run(apply=False):
    with app.app_context():
        # Step 1: Find all safety-net claims (by discovery_method)
        safety_net_claims = InteractionClaim.query.filter_by(
            discovery_method='auto_safety_net'
        ).all()
        print(f"Found {len(safety_net_claims)} claims with discovery_method='auto_safety_net'")

        # Step 2: Find all claims matching the garbage name pattern
        all_claims = InteractionClaim.query.all()
        garbage_name_claims = [c for c in all_claims if GARBAGE_PATTERN.match(c.function_name or '')]
        print(f"Found {len(garbage_name_claims)} claims matching garbage name pattern")

        # Combine candidates (deduplicate by ID)
        candidates = {c.id: c for c in safety_net_claims}
        for c in garbage_name_claims:
            candidates[c.id] = c
        print(f"Total unique candidate claims: {len(candidates)}")

        # Step 3: For each candidate, check if the interaction has other real claims
        to_delete = []
        to_keep = []

        # Batch-load claim counts per interaction
        interaction_ids = {c.interaction_id for c in candidates.values()}
        claims_by_interaction = {}
        for c in all_claims:
            claims_by_interaction.setdefault(c.interaction_id, []).append(c)

        for claim_id, claim in candidates.items():
            sibling_claims = claims_by_interaction.get(claim.interaction_id, [])
            other_real_claims = [
                c for c in sibling_claims
                if c.id != claim_id
                and c.discovery_method != 'auto_safety_net'
                and not GARBAGE_PATTERN.match(c.function_name or '')
            ]

            if other_real_claims:
                to_delete.append(claim)
            else:
                to_keep.append(claim)

        print(f"\nResults:")
        print(f"  Will DELETE: {len(to_delete)} redundant claims (interaction has real claims)")
        print(f"  Will KEEP:   {len(to_keep)} claims (only claim for their interaction)")

        if to_delete and not apply:
            print(f"\nSample deletions (first 10):")
            for c in to_delete[:10]:
                ix_claims = claims_by_interaction.get(c.interaction_id, [])
                real = [x for x in ix_claims if x.id != c.id]
                print(f"  Claim #{c.id}: '{c.function_name}' (interaction #{c.interaction_id}, {len(real)} real claims remain)")

        if apply and to_delete:
            delete_ids = [c.id for c in to_delete]
            # Batch delete
            InteractionClaim.query.filter(InteractionClaim.id.in_(delete_ids)).delete(
                synchronize_session=False
            )
            db.session.commit()
            print(f"\nDeleted {len(delete_ids)} redundant claims.")
        elif not apply:
            print(f"\nDry run — use --apply to delete.")


if __name__ == '__main__':
    apply = '--apply' in sys.argv
    run(apply=apply)
