"""Audit chain completeness for queried proteins.

Reports on every indirect chain in the DB:
- expected hops (from chain_context.full_chain length)
- actual hop interaction rows (mediator-pair Interactions)
- claims per hop
- pseudo-mediator hops surfaced

With ``--repair``, re-runs ``DatabaseSyncLayer.sync_query_results`` from each
protein's file cache so previously-dropped hops (pre-L1 era) are now created
under the new pseudo-aware logic.

Usage:
    python3 scripts/audit_chain_completeness.py                 # all queried proteins
    python3 scripts/audit_chain_completeness.py TDP43           # single protein
    python3 scripts/audit_chain_completeness.py TDP43 --repair  # repair from cache
    python3 scripts/audit_chain_completeness.py --json          # machine-readable
"""
import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app import app  # noqa: E402  # module-level Flask app instance
from models import db, Protein, Interaction, InteractionClaim, IndirectChain  # noqa: E402
from utils.db_sync import classify_symbol  # noqa: E402


# --- helpers -----------------------------------------------------------------


def _full_chain_for(interaction: Interaction) -> List[str]:
    """Pull the authoritative full_chain for an indirect interaction."""
    ctx = (interaction.data or {}).get("chain_context") or {}
    fc = ctx.get("full_chain")
    if isinstance(fc, list) and len(fc) >= 2:
        return [str(s) for s in fc if s]
    # Fallback: try the IndirectChain row via chain_id
    if interaction.chain_id:
        chain = db.session.get(IndirectChain, interaction.chain_id)
        if chain and chain.chain_proteins:
            return list(chain.chain_proteins)
    # Last resort: use upstream_interactor + primary
    up = (interaction.data or {}).get("upstream_interactor")
    primary = (interaction.data or {}).get("primary")
    if up and primary:
        return [up, primary]
    return []


def _hop_rows_for(full_chain: List[str], protein_id_map: Dict[str, int]) -> List[Tuple[str, str, Optional[Interaction]]]:
    """For each hop in full_chain, look up the corresponding Interaction row.

    Returns list of (src, tgt, interaction-or-None). The protein_id_map is
    case-insensitive keyed (uppercase) because DB stores canonical
    uppercase symbols while chain metadata may use mixed-case forms like
    "Ubiquitin".
    """
    out = []
    for i in range(len(full_chain) - 1):
        src = full_chain[i]
        tgt = full_chain[i + 1]
        a_id = protein_id_map.get((src or "").upper())
        b_id = protein_id_map.get((tgt or "").upper())
        if a_id is None or b_id is None:
            out.append((src, tgt, None))
            continue
        # Canonical ordering: protein_a_id < protein_b_id
        lo, hi = (a_id, b_id) if a_id < b_id else (b_id, a_id)
        ix = (
            db.session.query(Interaction)
            .filter(Interaction.protein_a_id == lo, Interaction.protein_b_id == hi)
            .first()
        )
        out.append((src, tgt, ix))
    return out


def _claim_count(interaction: Interaction) -> int:
    if not interaction:
        return 0
    return InteractionClaim.query.filter_by(interaction_id=interaction.id).count()


# --- audit -------------------------------------------------------------------


def audit(protein_symbol: Optional[str] = None) -> Dict:
    """Return audit results for one protein or all queried proteins."""
    if protein_symbol:
        targets = [protein_symbol]
    else:
        targets = [
            p.symbol for p in Protein.query.filter(Protein.query_count > 0).all()
        ]

    report = {"by_protein": {}, "totals": {
        "chains": 0, "complete_chains": 0,
        "hops_total": 0, "hops_with_claims": 0,
        "missing_hops": 0, "missing_proteins": 0,
        "pseudo_hops": 0,
    }}

    # Pre-build a case-insensitive protein-id map (cheaper than per-hop
    # lookup; DB stores uppercase canonical, chain metadata uses raw form).
    protein_id_map = {p.symbol.upper(): p.id for p in Protein.query.all()}

    for sym in targets:
        protein = Protein.query.filter_by(symbol=sym).first()
        if not protein:
            report["by_protein"][sym] = {"status": "not_in_db"}
            continue

        # All indirect interactions discovered with this protein as the query
        indirects = (
            Interaction.query.filter_by(
                discovered_in_query=sym, interaction_type="indirect"
            ).all()
        )

        per_protein = {
            "indirect_count": len(indirects),
            "complete": 0,
            "incomplete": 0,
            "missing_hops_examples": [],
            "pseudo_hops": 0,
            "hops_total": 0,
            "hops_with_claims": 0,
        }

        for ix in indirects:
            full_chain = _full_chain_for(ix)
            if len(full_chain) < 2:
                continue
            hop_rows = _hop_rows_for(full_chain, protein_id_map)

            missing = [(s, t) for (s, t, ixn) in hop_rows if ixn is None]
            with_claims = [(s, t, ixn) for (s, t, ixn) in hop_rows if ixn and _claim_count(ixn) > 0]
            pseudo_hops_in_chain = sum(
                1 for (s, t, _) in hop_rows
                if classify_symbol(s) == "pseudo" or classify_symbol(t) == "pseudo"
            )

            per_protein["hops_total"] += len(hop_rows)
            per_protein["hops_with_claims"] += len(with_claims)
            per_protein["pseudo_hops"] += pseudo_hops_in_chain
            if missing:
                per_protein["incomplete"] += 1
                if len(per_protein["missing_hops_examples"]) < 5:
                    per_protein["missing_hops_examples"].append({
                        "chain": " → ".join(full_chain),
                        "missing_pairs": [f"{s}→{t}" for (s, t) in missing],
                    })
            else:
                per_protein["complete"] += 1

        report["by_protein"][sym] = per_protein
        report["totals"]["chains"] += per_protein["indirect_count"]
        report["totals"]["complete_chains"] += per_protein["complete"]
        report["totals"]["hops_total"] += per_protein["hops_total"]
        report["totals"]["hops_with_claims"] += per_protein["hops_with_claims"]
        report["totals"]["missing_hops"] += (
            per_protein["hops_total"] - per_protein["hops_with_claims"]
        )
        report["totals"]["pseudo_hops"] += per_protein["pseudo_hops"]

    return report


def print_human_report(report: Dict) -> None:
    t = report["totals"]
    print("=" * 70)
    print("CHAIN COMPLETENESS AUDIT")
    print("=" * 70)
    print(f"  Chains total      : {t['chains']}")
    print(f"  Complete chains   : {t['complete_chains']} / {t['chains']}")
    print(f"  Hops total        : {t['hops_total']}")
    print(f"  Hops with claims  : {t['hops_with_claims']} / {t['hops_total']}")
    print(f"  Missing hops      : {t['missing_hops']}")
    print(f"  Pseudo-mediator hops surfaced: {t['pseudo_hops']}")
    print()
    for sym, data in sorted(report["by_protein"].items()):
        if data.get("status") == "not_in_db":
            print(f"[{sym}] NOT IN DB")
            continue
        print(f"[{sym}]")
        print(f"  Indirect chains: {data['indirect_count']}  (complete={data['complete']}, incomplete={data['incomplete']})")
        print(f"  Hops with claims: {data['hops_with_claims']} / {data['hops_total']}  pseudo={data['pseudo_hops']}")
        for ex in data.get("missing_hops_examples", []):
            print(f"    ✗ {ex['chain']}")
            for mp in ex["missing_pairs"]:
                print(f"        missing: {mp}")
        print()


# --- repair ------------------------------------------------------------------


def repair_from_cache(protein_symbol: str) -> Dict:
    """Re-run DatabaseSyncLayer.sync_query_results from the file cache."""
    from utils.storage import StorageLayer  # noqa
    from utils.db_sync import DatabaseSyncLayer

    cache_path = os.path.join(_PROJECT_ROOT, "cache", f"{protein_symbol}.json")
    if not os.path.exists(cache_path):
        return {"ok": False, "error": f"No cache file at {cache_path}"}

    with open(cache_path) as fh:
        snapshot = json.load(fh)

    layer = DatabaseSyncLayer()
    stats = layer.sync_query_results(protein_symbol, snapshot)
    return {"ok": True, "stats": stats}


# --- main --------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("protein", nargs="?", help="Single protein symbol (default: all)")
    ap.add_argument("--repair", action="store_true",
                    help="Re-run db_sync from file cache for the named protein")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON instead of human-readable report")
    args = ap.parse_args()

    with app.app_context():
        if args.repair:
            if not args.protein:
                print("--repair requires a protein symbol", file=sys.stderr)
                return 2
            print(f"[REPAIR] Re-running db_sync for {args.protein} from cache...")
            result = repair_from_cache(args.protein)
            print(json.dumps(result, indent=2, default=str))
            print()

        report = audit(args.protein)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_human_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
