#!/usr/bin/env python3
"""Retag legacy ``function_context='chain_derived'`` claims on stand-alone
direct Interaction rows.

Background
----------
Before the Claim Locus Router, some pipeline paths wrote
``function_context='chain_derived'`` claims to plain direct Interaction
rows that have no ``chain_id`` — the claim conceptually belonged to a
chain hop but structurally landed on a direct row without any chain
linkage. The new read-time filter at
``services/data_builder.py:_build_chain_link_data`` only surfaces
``chain_derived`` claims joined via ``chain_id`` — legacy rows where
``chain_id IS NULL`` are silently skipped and never render.

Two repair options, we ship the safer one
-----------------------------------------
(a) Re-parent each legacy ``chain_derived`` claim onto the proper
    indirect row — requires reconstructing which chain it belonged to;
    often impossible after the fact.
(b) Retag these legacy claims as ``'direct'`` so they render as plain
    per-pair claims again.

This script ships option (b). Rationale: these claims describe a pair's
binary biology (that's why they landed on a direct row). Re-labelling as
'direct' makes them render on the direct modal for that pair, which is
a strictly-better outcome than silent data loss. If the claim actually
describes cascade biology, the Locus Router will catch it on the next
pipeline run and reroute it correctly; at worst it stays visible where
it currently is.

Safety posture
--------------
- Dry-run by default; pass ``--apply`` to actually commit.
- Only touches rows where:
  * ``function_context == 'chain_derived'``
  * The parent interaction has NO ``chain_id`` AND
    ``interaction_type != 'indirect'``
  This filter ensures we don't disturb legitimate chain-derived claims
  on actual indirect parent rows.
- Idempotent: the filter excludes already-retagged rows by definition.
- Emits one JSON line per retagged row for audit.

Usage
-----

::

    python3 scripts/migrate_retag_legacy_chain_derived.py         # dry run
    python3 scripts/migrate_retag_legacy_chain_derived.py --apply # commit
    python3 scripts/migrate_retag_legacy_chain_derived.py --limit 50
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="Commit the retagging; default is dry-run.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap rows processed (for testing).")
    parser.add_argument("--verbose", action="store_true",
                        help="Print one JSON line per candidate row.")
    args = parser.parse_args()

    from app import app
    from models import db, InteractionClaim, Interaction

    with app.app_context():
        # Build the candidate query: chain_derived claims whose parent
        # Interaction has no chain_id AND is not indirect. Joining to
        # Interaction gives us the parent's chain_id and type in one
        # query so we don't need a per-claim lookup.
        q = (
            db.session.query(InteractionClaim, Interaction)
            .join(Interaction, InteractionClaim.interaction_id == Interaction.id)
            .filter(InteractionClaim.function_context == "chain_derived")
            .filter(Interaction.chain_id.is_(None))
            .filter(Interaction.interaction_type != "indirect")
        )
        if args.limit:
            q = q.limit(args.limit)

        rows = q.all()
        print(f"[RETAG] Candidate claims: {len(rows)}", file=sys.stderr)

        if not rows:
            print("[RETAG] No legacy chain_derived claims found on direct rows.",
                  file=sys.stderr)
            return

        retagged = 0
        for claim, interaction in rows:
            record = {
                "claim_id": claim.id,
                "interaction_id": interaction.id,
                "function_name": claim.function_name,
                "interaction_type": interaction.interaction_type,
                "pathway_name": claim.pathway_name,
            }
            if args.verbose:
                print(json.dumps(record))
            if args.apply:
                claim.function_context = "direct"
                retagged += 1

        if args.apply:
            db.session.commit()
            print(f"[RETAG] Committed: retagged {retagged} claims.",
                  file=sys.stderr)
        else:
            print(
                f"[RETAG] Dry-run: would retag {len(rows)} claim(s). "
                "Pass --apply to commit.",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
