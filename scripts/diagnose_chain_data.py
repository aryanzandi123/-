#!/usr/bin/env python3
"""
Diagnostic: Check chain data integrity for specific protein interactions.

Queries the database to verify that indirect interaction chains are properly
stored with mediator_chain, upstream_interactor, and that mediator-target
interactions exist.

Usage:
    python scripts/diagnose_chain_data.py                           # Default: UBQLN2/TDP43/SQSTM1
    python scripts/diagnose_chain_data.py ATXN3 RHEB MTOR          # Custom proteins
    python scripts/diagnose_chain_data.py --fix                     # Auto-fix missing chain data
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not os.environ.get("DATABASE_URL"):
    print("ERROR: Set DATABASE_URL environment variable.", file=sys.stderr)
    sys.exit(1)

from app import app
from models import db, Protein, Interaction, InteractionClaim


def find_interaction(sym_a, sym_b):
    """Find interaction between two proteins (canonical ordering)."""
    pa = Protein.query.filter_by(symbol=sym_a).first()
    pb = Protein.query.filter_by(symbol=sym_b).first()
    if not pa or not pb:
        return None, pa, pb

    a_id, b_id = min(pa.id, pb.id), max(pa.id, pb.id)
    ix = Interaction.query.filter_by(protein_a_id=a_id, protein_b_id=b_id).first()
    return ix, pa, pb


def diagnose(query_protein, mediator, target, fix=False):
    with app.app_context():
        print(f"=== Chain Diagnosis: {query_protein} → {mediator} → {target} ===\n")

        # 1. Check proteins exist
        for sym in [query_protein, mediator, target]:
            p = Protein.query.filter_by(symbol=sym).first()
            if p:
                print(f"  [OK] Protein '{sym}' exists (id={p.id}, interactions={p.total_interactions})")
            else:
                print(f"  [MISSING] Protein '{sym}' NOT in database")

        print()

        # 2. Check query_protein ↔ target interaction (expected: indirect via mediator)
        ix_qt, p_q, p_t = find_interaction(query_protein, target)
        if ix_qt:
            print(f"  [OK] Interaction {query_protein} ↔ {target} exists (id={ix_qt.id})")
            print(f"       type={ix_qt.interaction_type}, depth={ix_qt.depth}")
            print(f"       upstream_interactor={ix_qt.upstream_interactor}")
            print(f"       mediator_chain={ix_qt.mediator_chain}")
            print(f"       direction={ix_qt.direction}, arrow={ix_qt.arrow}")
            print(f"       function_context={ix_qt.function_context}")
            print(f"       chain_with_arrows={ix_qt.chain_with_arrows}")

            # Check claims
            claims = InteractionClaim.query.filter_by(interaction_id=ix_qt.id).all()
            print(f"       claims={len(claims)}: {[c.function_name[:50] for c in claims]}")

            # Check if mediator is in the chain
            chain = ix_qt.mediator_chain or []
            if mediator in chain:
                print(f"  [OK] Mediator '{mediator}' is in mediator_chain")
            else:
                print(f"  [ISSUE] Mediator '{mediator}' NOT in mediator_chain: {chain}")
                if fix and p_q and p_t:
                    new_chain = chain + [mediator] if chain else [mediator]
                    ix_qt.mediator_chain = new_chain
                    if not ix_qt.upstream_interactor:
                        ix_qt.upstream_interactor = mediator
                    if ix_qt.interaction_type != 'indirect':
                        ix_qt.interaction_type = 'indirect'
                    if not ix_qt.depth or ix_qt.depth < 2:
                        ix_qt.depth = 2
                    db.session.commit()
                    print(f"  [FIXED] Set mediator_chain={new_chain}, upstream={mediator}")
        else:
            if p_q and p_t:
                print(f"  [MISSING] No interaction between {query_protein} and {target}")
            else:
                missing = [s for s, p in [(query_protein, p_q), (target, p_t)] if not p]
                print(f"  [MISSING] Cannot check — proteins missing: {missing}")

        print()

        # 3. Check query_protein ↔ mediator interaction (expected: direct)
        ix_qm, _, _ = find_interaction(query_protein, mediator)
        if ix_qm:
            print(f"  [OK] Interaction {query_protein} ↔ {mediator} exists (id={ix_qm.id})")
            print(f"       type={ix_qm.interaction_type}, arrow={ix_qm.arrow}")
        else:
            print(f"  [MISSING] No interaction between {query_protein} and {mediator}")

        print()

        # 4. Check mediator ↔ target interaction (chain link)
        ix_mt, _, _ = find_interaction(mediator, target)
        if ix_mt:
            print(f"  [OK] Interaction {mediator} ↔ {target} exists (id={ix_mt.id})")
            print(f"       type={ix_mt.interaction_type}, arrow={ix_mt.arrow}")
            claims = InteractionClaim.query.filter_by(interaction_id=ix_mt.id).all()
            print(f"       claims={len(claims)}: {[c.function_name[:50] for c in claims]}")
        else:
            print(f"  [MISSING] No interaction between {mediator} and {target}")
            print(f"       This chain link is needed for reconstruction.")
            print(f"       Consider running: python -c \"from runner import run_full_job; ...\" for {mediator}")

        print()

        # 5. Summary
        issues = []
        if not ix_qt:
            issues.append(f"No {query_protein}↔{target} interaction")
        elif mediator not in (ix_qt.mediator_chain or []):
            issues.append(f"mediator_chain missing '{mediator}'")
        if not ix_qm:
            issues.append(f"No {query_protein}↔{mediator} interaction")
        if not ix_mt:
            issues.append(f"No {mediator}↔{target} chain link")

        if issues:
            print(f"  ISSUES FOUND ({len(issues)}):")
            for i in issues:
                print(f"    - {i}")
        else:
            print(f"  ALL CHECKS PASSED — chain should reconstruct correctly.")


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    fix = '--fix' in sys.argv

    if len(args) >= 3:
        diagnose(args[0], args[1], args[2], fix=fix)
    else:
        # Default: UBQLN2 → TDP43 → SQSTM1
        diagnose('UBQLN2', 'TDP43', 'SQSTM1', fix=fix)
