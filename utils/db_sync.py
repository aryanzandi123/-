#!/usr/bin/env python3
"""
Database Sync Layer

Extracts pipeline JSON output and syncs to PostgreSQL.

Design Philosophy:
- Non-invasive: Pipeline code unchanged
- Idempotent: Safe to run multiple times
- Transactional: All-or-nothing updates
- Backward Compatible: Maintains file cache

Strategy:
- Store FULL interactor JSON in interactions.data (JSONB)
- Preserves all fields: evidence[], functions[], pmids[], etc.
- No data loss from pipeline output
"""

from typing import Dict, List, Any, Tuple, Optional
from datetime import datetime, timezone
from copy import deepcopy
import os
import re
import sys

# Fix Windows console encoding for Greek letters and special characters
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from sqlalchemy.exc import IntegrityError
from models import Protein, Interaction, Pathway, PathwayInteraction, IndirectChain, db
from utils.interaction_contract import (
    CANONICAL_ARROW_SET,
    normalize_arrow,
    normalize_arrows_map,
    normalize_chain_arrows,
    semantic_claim_direction,
)


def _primary_arrow_map(direction: Optional[str], arrow: Optional[str]) -> dict:
    """Return a one-direction arrows JSONB map matching the scalar arrow."""

    clean_arrow = normalize_arrow(arrow)
    key = "b_to_a" if direction == "b_to_a" else "a_to_b"
    return {key: [clean_arrow]}


def _normalize_payload_contract(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize mutable payload fields before they are persisted."""

    clean = deepcopy(data or {})
    if clean.get("arrow"):
        clean["arrow"] = normalize_arrow(clean.get("arrow"))
    if clean.get("arrows"):
        clean["arrows"] = normalize_arrows_map(clean.get("arrows"))
    if clean.get("chain_with_arrows"):
        clean["chain_with_arrows"] = normalize_chain_arrows(clean.get("chain_with_arrows"))

    funcs = clean.get("functions")
    if isinstance(funcs, list):
        for fn in funcs:
            if not isinstance(fn, dict):
                continue
            if fn.get("arrow"):
                fn["arrow"] = normalize_arrow(fn.get("arrow"))
            if fn.get("interaction_effect") in CANONICAL_ARROW_SET or fn.get("interaction_effect") == "complex":
                fn["interaction_effect"] = normalize_arrow(fn.get("interaction_effect"))
            idir = fn.get("interaction_direction") or fn.get("direction")
            if idir:
                semantic = semantic_claim_direction(idir)
                fn["interaction_direction"] = semantic
                fn["direction"] = semantic

    clf = clean.get("chain_link_functions")
    if isinstance(clf, dict):
        for entries in clf.values():
            if not isinstance(entries, list):
                continue
            for fn in entries:
                if not isinstance(fn, dict):
                    continue
                if fn.get("arrow"):
                    fn["arrow"] = normalize_arrow(fn.get("arrow"))
                idir = fn.get("interaction_direction") or fn.get("direction")
                if idir:
                    semantic = semantic_claim_direction(idir)
                    fn["interaction_direction"] = semantic
                    fn["direction"] = semantic
    return clean


def _detect_and_log_chain_overlaps(
    new_chain_proteins: List[str],
    new_query: str,
) -> None:
    """Log IndirectChain rows whose proteins overlap with ``new_chain_proteins``.

    Detection-only; never mutates data. Goal is to give operators visibility
    into cross-query redundancy (query A writes ``A→B→C→D``, later query B
    writes ``B→C→D→E`` — both are slices of the same cascade). A proper
    merge would have to repoint InteractionClaim FKs and reconcile pathway
    assignments, which is a bigger operation we defer.

    Overlap rule: two chains overlap if one's chain_proteins sequence
    appears as a contiguous (order-preserving) substring of the other's.
    Case-insensitive comparison. Minimum shared length = 3 to avoid
    flagging trivial pair overlaps that every indirect chain shares.

    Scale protection: loading every IndirectChain row on every chain
    write is O(total chains) per write. At ~100 rows that's fine; at
    1000+ it becomes a latency tax on ingestion. Two-part bound:

      1. If total chain count exceeds ``CHAIN_OVERLAP_SCAN_CAP``
         (default 500), skip detection entirely and log why.
      2. Below the cap, narrow candidates via JSONB containment on
         PostgreSQL — load only chains that share at least one protein
         with the new chain. Falls back to full scan on SQLite (where
         ?| isn't supported) because small DBs don't hit the cap
         anyway.
    """
    if not new_chain_proteins or len(new_chain_proteins) < 3:
        return

    try:
        scan_cap = int(os.getenv("CHAIN_OVERLAP_SCAN_CAP", "500"))
    except (TypeError, ValueError):
        scan_cap = 500

    try:
        total_chain_count = IndirectChain.query.count()
    except Exception:
        return

    if total_chain_count > scan_cap:
        try:
            from utils.observability import log_event
            log_event(
                "chain_overlap_scan_skipped",
                level="debug",
                tag="CHAIN OVERLAP",
                new_query=new_query,
                total_chains=total_chain_count,
                cap=scan_cap,
                note="Set CHAIN_OVERLAP_SCAN_CAP higher to re-enable detection at this scale.",
            )
        except Exception:
            pass
        return

    try:
        new_upper = [str(p).upper() for p in new_chain_proteins if p]
        new_set = set(new_upper)

        # Prefer a JSONB containment filter on PostgreSQL so we only
        # pull rows that share a protein with the new chain. Fall
        # through to full scan on SQLite / any dialect that rejects
        # the ``?|`` operator — those deployments are small enough
        # that the cap above protects them.
        candidates = None
        try:
            dialect = (db.session.bind.dialect.name or "").lower()
        except Exception:
            dialect = ""

        if dialect == "postgresql" and new_upper:
            try:
                candidates = IndirectChain.query.filter(
                    IndirectChain.chain_proteins.op("?|")(list(new_set))
                ).all()
            except Exception:
                candidates = None  # Fall back below.

        if candidates is None:
            candidates = IndirectChain.query.all()
    except Exception:
        return

    def _is_contiguous_subseq(short: List[str], long: List[str]) -> bool:
        if not short or len(short) > len(long):
            return False
        for i in range(len(long) - len(short) + 1):
            if long[i : i + len(short)] == short:
                return True
        return False

    overlaps = []
    for cand in candidates:
        cand_proteins = cand.chain_proteins or []
        cand_upper = [str(p).upper() for p in cand_proteins if p]
        if len(cand_upper) < 3 or not (new_set & set(cand_upper)):
            continue
        if _is_contiguous_subseq(cand_upper, new_upper):
            overlaps.append(("new_superset", cand.id, cand_proteins))
        elif _is_contiguous_subseq(new_upper, cand_upper):
            overlaps.append(("existing_superset", cand.id, cand_proteins))

    if overlaps:
        try:
            from utils.observability import log_event
            log_event(
                "chain_overlap_detected",
                level="info",
                tag="CHAIN OVERLAP",
                new_query=new_query,
                new_chain=new_chain_proteins,
                overlap_count=len(overlaps),
                overlaps=[
                    {"kind": k, "existing_chain_id": cid, "existing_proteins": cp}
                    for k, cid, cp in overlaps[:10]
                ],
                note="See utils/db_sync.py:_merge_subset_chains_into for the merge path.",
            )
        except Exception:
            pass


def _merge_subset_chains_into(
    superset_chain: "IndirectChain",
    superset_proteins: List[str],
    new_query: str,
) -> int:
    """Fold strict-subset IndirectChain rows into ``superset_chain``.

    When a newly-written chain contains existing chains' proteins as a
    contiguous substring (``[A,B,C,D,E]`` contains the earlier
    ``[A,B,C,D]``), we want one canonical chain row per biological
    cascade — not two partial ones. This function:

      1. Finds every IndirectChain whose ``chain_proteins`` appears as
         a contiguous (order-preserving) substring of
         ``superset_proteins`` and is NOT the superset itself.
      2. Repoints every child claim's ``chain_id`` and every
         participating Interaction's ``chain_id`` from the subset row
         to the superset row via bulk UPDATE.
      3. Unions the subset's ``discovered_in_query`` into the
         superset's (comma-separated, dedup).
      4. Deletes the now-empty subset IndirectChain row.

    Runs inside a SAVEPOINT so any failure rolls back the merge
    cleanly without poisoning the outer sync transaction. Gated on
    ``ENABLE_CHAIN_MERGE`` (default ``true``) so operators can disable
    the auto-merge without losing the detection log line.

    Returns the number of subsets merged (0 if none or disabled).
    """
    if os.getenv("ENABLE_CHAIN_MERGE", "true").strip().lower() in (
        "0", "false", "no", "off"
    ):
        return 0
    if not superset_chain or not superset_proteins or len(superset_proteins) < 3:
        return 0

    from sqlalchemy import update as _sa_update

    superset_upper = [str(p).upper() for p in superset_proteins if p]

    try:
        from utils.observability import log_event
    except Exception:
        log_event = None  # type: ignore

    # Candidate pool: share at least one protein and shorter than us.
    try:
        scan_cap = int(os.getenv("CHAIN_OVERLAP_SCAN_CAP", "500"))
    except (TypeError, ValueError):
        scan_cap = 500
    try:
        total = IndirectChain.query.count()
    except Exception:
        return 0
    if total > scan_cap:
        return 0

    try:
        candidates = IndirectChain.query.filter(
            IndirectChain.id != superset_chain.id
        ).all()
    except Exception:
        return 0

    def _is_contiguous_subseq(short, long_):
        if not short or len(short) > len(long_):
            return False
        for i in range(len(long_) - len(short) + 1):
            if long_[i : i + len(short)] == short:
                return True
        return False

    merged_count = 0
    merged_ids: List[int] = []
    for cand in candidates:
        cand_proteins = cand.chain_proteins or []
        cand_upper = [str(p).upper() for p in cand_proteins if p]
        if len(cand_upper) < 2 or len(cand_upper) >= len(superset_upper):
            continue
        if not _is_contiguous_subseq(cand_upper, superset_upper):
            continue

        try:
            # Savepoint so a failed merge doesn't poison the outer
            # sync transaction — we either succeed or no-op on this
            # particular subset.
            with db.session.begin_nested():
                # Import lazily to avoid circular imports at module load.
                from models import InteractionClaim as _Claim

                # B5 — surface any claims that would collide with the
                # uq_claim_interaction_fn_pw_ctx unique index after the
                # subset→superset repoint. Previously the bulk UPDATE
                # could silently swallow an IntegrityError on the second
                # subset and leave the merge partially complete. We now
                # pre-detect the collisions and log them before the
                # UPDATE so operators can see exactly which claims won't
                # carry over cleanly.
                _subset_claims = (
                    _Claim.query.filter_by(chain_id=cand.id).all()
                )
                if _subset_claims and superset_chain.id:
                    _subset_keys = {
                        (
                            c.interaction_id,
                            c.function_name,
                            c.pathway_name or "",
                            c.function_context or "",
                        )
                        for c in _subset_claims
                    }
                    _superset_keys = {
                        (
                            c.interaction_id,
                            c.function_name,
                            c.pathway_name or "",
                            c.function_context or "",
                        )
                        for c in _Claim.query.filter_by(
                            chain_id=superset_chain.id
                        ).all()
                    }
                    _collisions = _subset_keys & _superset_keys
                    if _collisions and log_event:
                        log_event(
                            "chain_merge_claim_collision",
                            level="warn",
                            tag="CHAIN MERGE",
                            superset_chain_id=superset_chain.id,
                            subset_chain_id=cand.id,
                            collision_count=len(_collisions),
                        )

                # 1. Claims: InteractionClaim.chain_id subset → superset.
                db.session.execute(
                    _sa_update(_Claim)
                    .where(_Claim.chain_id == cand.id)
                    .values(chain_id=superset_chain.id)
                )

                # 2. Interaction.chain_id subset → superset (if the column exists).
                try:
                    db.session.execute(
                        _sa_update(Interaction)
                        .where(Interaction.chain_id == cand.id)
                        .values(chain_id=superset_chain.id)
                    )
                except Exception:
                    # Older schemas may not have Interaction.chain_id;
                    # claims repointing alone is the primary invariant.
                    pass

                # 3. Union discovered_in_query. Preserves the cross-
                #    query provenance that "discovered_in_query only
                #    updated when new" already gives us for identical
                #    chains; here we carry it across subset → superset.
                existing = [
                    s.strip()
                    for s in (superset_chain.discovered_in_query or "").split(",")
                    if s.strip()
                ]
                for donor in (cand.discovered_in_query or "").split(","):
                    donor = donor.strip()
                    if donor and donor not in existing:
                        existing.append(donor)
                if new_query and new_query not in existing:
                    existing.append(new_query)
                superset_chain.discovered_in_query = ",".join(existing) or None

                # 4. Drop the subset row.
                db.session.delete(cand)
                db.session.flush()

            merged_count += 1
            merged_ids.append(cand.id)
        except Exception as exc:
            if log_event:
                log_event(
                    "chain_merge_subset_failed",
                    level="warn",
                    tag="CHAIN MERGE",
                    superset_chain_id=superset_chain.id,
                    subset_chain_id=getattr(cand, "id", None),
                    error=f"{type(exc).__name__}: {exc}",
                )

    if merged_count and log_event:
        log_event(
            "chain_merge_completed",
            level="info",
            tag="CHAIN MERGE",
            superset_chain_id=superset_chain.id,
            superset_chain=superset_proteins,
            merged_subsets=merged_ids,
            merged_count=merged_count,
        )

    return merged_count


# ---------------------------------------------------------------------------
# H1: Shared COALESCE-aware dedup key for InteractionClaim
# ---------------------------------------------------------------------------
# The schema's unique index ``uq_claim_interaction_fn_pw_ctx`` collapses
# NULLs: ``(interaction_id, function_name, COALESCE(pathway_name, ''),
# COALESCE(function_context, ''))``. Python dict keys must match that
# semantics or in-memory dedup lookups will miss rows that the DB index
# considers duplicates, causing the next INSERT to race into an
# IntegrityError. Every callsite that builds a dedup key goes through
# this helper so the Python and SQL views agree.
def _claim_dedup_key(
    function_name: Optional[str],
    pathway_name: Optional[str],
    function_context: Optional[str],
) -> Tuple[str, str, str]:
    """Return the COALESCE-normalised key for a claim row."""
    return (
        function_name or "",
        pathway_name or "",
        function_context or "",
    )


# --- Protein symbol classification (3-state: protein|pseudo|invalid) ---
_GENE_SYMBOL_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]{0,14}(?:[-/][A-Za-z0-9]+)?$')

_ALLOWED_COMPLEXES = {'mTORC1', 'mTORC2', 'NF-kB', 'NF-κB'}

# Generic biomolecule classes that legitimately appear as chain mediators
# but are NOT valid stand-alone protein interactors. Stored as Protein rows
# with extra_data["is_pseudo"]=True so chain hops can reference them.
_PSEUDO_WHITELIST = {
    # RNA species
    'RNA', 'mRNA', 'pre-mRNA', 'tRNA', 'rRNA', 'lncRNA', 'miRNA', 'snRNA', 'snoRNA',
    # DNA species
    'DNA', 'ssDNA', 'dsDNA',
    # Post-translational modifiers
    'Ubiquitin', 'SUMO', 'NEDD8',
    # Macromolecular complexes used as mediators
    'Proteasome', 'Ribosome', 'Spliceosome',
    # Cytoskeleton-as-mediator
    'Actin', 'Tubulin',
    # Compartments-as-mediator (rare but valid in cascades)
    'Stress Granules', 'P-bodies',
}

# Backwards-compat alias kept for any external callers; new code should use
# classify_symbol() which returns the 3-state result.
_BLOCKED_ENTITIES = _PSEUDO_WHITELIST

# Case-insensitive lookup table — _get_or_create_protein normalises symbols
# to uppercase, so a case-sensitive whitelist would miss "UBIQUITIN" (the
# stored form) while matching "Ubiquitin" (the LLM-emitted form). We
# normalise to lowercase and check both directions.
_PSEUDO_LOWER = frozenset(s.lower() for s in _PSEUDO_WHITELIST)


def classify_symbol(symbol: str) -> str:
    """Classify a symbol as 'protein' | 'pseudo' | 'invalid'.

    - 'protein': real gene symbol, valid both as direct interactor and chain hop.
    - 'pseudo':  generic biomolecule class, valid as chain mediator only.
    - 'invalid': malformed, multi-word, or otherwise unparseable; rejected.

    Pseudo classification is CASE-INSENSITIVE because the alias normaliser
    in ``_get_or_create_protein`` uppercases symbols before storage. So
    "Ubiquitin", "ubiquitin", and "UBIQUITIN" all classify as pseudo.
    """
    if not symbol or len(symbol) > 25:
        return "invalid"
    if symbol in _ALLOWED_COMPLEXES:
        return "protein"
    if symbol.lower() in _PSEUDO_LOWER:
        return "pseudo"
    if ' ' in symbol:
        return "invalid"
    # ``XYZ-mRNA`` and similar transcript suffixes are pseudo (was: invalid),
    # case-insensitive: ``foo-MRNA`` etc. match too.
    _suffix_lower = symbol.lower()
    if _suffix_lower.endswith(('mrna', 'pre-mrna', 'rna')):
        return "pseudo"
    return "protein" if _GENE_SYMBOL_RE.match(symbol) else "invalid"


def _is_valid_protein_symbol(symbol: str) -> bool:
    """Legacy boolean wrapper. True only for 'protein' classification.

    Pseudo entities now classify as valid mediators (see classify_symbol);
    callers that need the 3-state answer should use classify_symbol directly.
    """
    return classify_symbol(symbol) == "protein"


def is_pseudo_symbol(symbol: str) -> bool:
    """True if symbol is a generic biomolecule class (RNA, Ubiquitin, ...)."""
    return classify_symbol(symbol) == "pseudo"


def deduplicate_functions(functions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Context-aware in-process dedup for the db_sync merge path.

    Dedup key is ``(function_name_lc, pathway_lc, function_context)``. The
    ``function_context`` segment is REQUIRED — without it, a ``direct``
    claim and a ``chain_derived`` claim describing the same biology would
    collapse to a single row, the loser's context label would be lost,
    and the parent interaction's function_context rollup would compute
    wrong (``direct`` instead of ``mixed``).

    The 2026-04-29 audit (the ATXN3 → MTOR via VCP / RHEB / TSC2 chains)
    proved this happens in practice when the post-processor's LLM dedup
    correctly partitions by context but the db_sync merge call here
    re-collapses across partitions on write.

    A higher-quality LLM-driven dedup runs in
    ``utils.deduplicate_functions.deduplicate_payload`` as part of
    post-processing. This in-process pass is a fast safety net for
    obvious duplicates that survive into the merge step (e.g., the
    same claim emitted twice across a recovery retry).

    Returns the deduplicated list; preference within a key bucket goes
    to validated entries first, then to entries with more populated
    fields.
    """
    if not functions:
        return []

    seen: Dict[tuple, Dict[str, Any]] = {}

    for func in functions:
        func_name = (func.get("function", "") or "").strip().lower()
        if not func_name:
            continue

        func_pathway = (func.get("pathway", "") or "").strip().lower()
        # Coerce missing/blank context to a literal sentinel so claims
        # without an explicit context don't accidentally collapse with
        # claims that have one. The post-processor's
        # normalize_function_contexts stage stamps a default before this
        # path runs, but a pre-Phase-C write or a legacy cached payload
        # may still arrive here without a context — keep them separate.
        func_context = (func.get("function_context", "") or "__unset__").strip().lower()

        dedup_key = (func_name, func_pathway, func_context)

        if dedup_key in seen:
            existing = seen[dedup_key]
            existing_fields = sum(1 for v in existing.values() if v not in [None, "", []])
            current_fields = sum(1 for v in func.values() if v not in [None, "", []])

            is_validated = func.get("_arrow_validated") or func.get("direct_arrow")
            existing_validated = existing.get("_arrow_validated") or existing.get("direct_arrow")

            if is_validated and not existing_validated:
                seen[dedup_key] = func
            elif existing_validated and not is_validated:
                pass
            elif current_fields > existing_fields:
                seen[dedup_key] = func
        else:
            seen[dedup_key] = func

    return list(seen.values())


def _clamp(val: Optional[str], limit: int) -> Optional[str]:
    """Truncate a string to fit its VARCHAR column limit, preventing StringDataRightTruncation."""
    return val[:limit] if val and len(val) > limit else val


# Known placeholder fragments that mean "no real claim was generated".
# Claims whose text matches any of these are useless to the user — the UI
# ends up rendering the same stub string in every field. Returning None
# from ``_strip_placeholder`` lets callers detect this and skip the write
# entirely (see stub-claim gate below).
_PLACEHOLDER_FRAGMENTS = (
    "not fully characterized",
    "discovered via chain resolution",
    "function data not generated",
    "data not generated",
    "uncharacterized interaction",
)


def _is_placeholder_text(val: Optional[str]) -> bool:
    """True if val is a known useless placeholder string."""
    if not val:
        return False
    lower = val.lower()
    return any(frag in lower for frag in _PLACEHOLDER_FRAGMENTS)


def _strip_placeholder(val: Optional[str]) -> Optional[str]:
    """Return None if val is a useless placeholder string."""
    return None if _is_placeholder_text(val) else val


class DatabaseSyncLayer:
    """Syncs pipeline output to PostgreSQL."""

    @staticmethod
    def _validate_and_fix_chain(interactor_data: Dict[str, Any], protein_symbol: str) -> Dict[str, Any]:
        """
        Validate and fix false chain assignments before database write.

        Detects and corrects false chains created by old schema_validator Strategy 3
        that blindly assigned first direct interactor as upstream for all proteins.

        Args:
            interactor_data: Interactor dict with potential false chain
            protein_symbol: Query protein symbol for logging

        Returns:
            Fixed interactor_data (modified in-place, but returned for chaining)
        """
        # Detect false chain from old Strategy 3
        if interactor_data.get('_chain_inferred_strategy') == 'first_direct_interactor':
            partner_symbol = interactor_data.get('primary', 'UNKNOWN')
            false_upstream = interactor_data.get('upstream_interactor')

            print(f"[DB_SYNC] ⚠️  Clearing false chain: {protein_symbol} → {partner_symbol} (false upstream: {false_upstream})")

            # Clear false chain data
            interactor_data['upstream_interactor'] = None
            interactor_data['mediator_chain'] = []

            # Add correction markers
            interactor_data['_chain_inference_corrected'] = True
            interactor_data['_correction_timestamp'] = datetime.now().isoformat()
            interactor_data['_false_upstream_removed'] = false_upstream

            # Add missing chain markers for transparency
            interactor_data['_chain_missing'] = True
            interactor_data['_inference_failed'] = 'no_biological_hints'

            # Remove the problematic strategy marker
            del interactor_data['_chain_inferred_strategy']

        return interactor_data

    def _resolve_pw(self, name: Optional[str]) -> Optional[int]:
        """Resolve pathway name to ID, with caching."""
        if not name:
            return None
        if not hasattr(self, '_pw_cache'):
            self._pw_cache = {}
        if name not in self._pw_cache:
            pw = Pathway.query.filter_by(name=name).first()
            self._pw_cache[name] = pw.id if pw else None
        return self._pw_cache[name]

    def sync_query_results(
        self,
        protein_symbol: str,
        snapshot_json: Dict[str, Any],
        ctx_json: Optional[Dict[str, Any]] = None
    ) -> Dict[str, int]:
        """
        Sync pipeline results to database.

        Args:
            protein_symbol: Query protein (e.g., "ATXN3")
            snapshot_json: Output from pipeline (format: {snapshot_json: {...}})
            ctx_json: Rich metadata (optional, from metadata file)

        Returns:
            Stats: {
                "proteins_created": int,
                "interactions_created": int,
                "interactions_updated": int
            }

        Raises:
            ValueError: If snapshot_json format is invalid
            Exception: If database transaction fails (rolled back automatically)
        """
        # Validate input
        if not protein_symbol:
            raise ValueError("protein_symbol cannot be empty")

        if not isinstance(snapshot_json, dict):
            raise ValueError("snapshot_json must be a dict")

        # Handle both formats: {"snapshot_json": {...}} and direct {...}
        snapshot_data = snapshot_json.get("snapshot_json", snapshot_json)

        if not isinstance(snapshot_data, dict):
            raise ValueError("snapshot_data must be a dict")

        # Reset per-sync pathway lookup cache. Scoped to one sync
        # transaction — if left bleeding across syncs it would cache
        # stale pathway ids on the next query.
        self._pw_cache = {}

        # Atom E: capture whether 2ax/2az ran in this pipeline run.
        # When set, ``sync_chain_relationships`` refuses to rehydrate
        # prior claims as a shortcut — missing per-hop claims are a
        # real contract violation, not a recoverable condition.
        self._chain_claim_phase_ran = bool(
            (ctx_json or {}).get("_chain_claim_phase_ran")
            or (snapshot_data or {}).get("_chain_claim_phase_ran")
        )

        stats = {
            "proteins_created": 0,
            "interactions_created": 0,
            "interactions_updated": 0
        }

        try:
            # Transaction wrapper (all-or-nothing)
            with db.session.begin_nested():
                # Step 1: Get or create main protein
                main_protein = self._get_or_create_protein(protein_symbol)
                if main_protein.query_count == 0:
                    stats["proteins_created"] += 1

                # Persist ``upstream_of_main`` on the main protein's
                # extra_data so it survives across page reloads.
                # Populated by the ITER0 upstream-context iteration at
                # ctx_json top level; without this, the list was only
                # available in the transient run payload and
                # disappeared after the pipeline finished.
                try:
                    ctx_upstream = None
                    if isinstance(ctx_json, dict):
                        ctx_upstream = ctx_json.get("upstream_of_main")
                    if ctx_upstream is None and isinstance(snapshot_data, dict):
                        ctx_upstream = snapshot_data.get("upstream_of_main")
                    if isinstance(ctx_upstream, list) and ctx_upstream:
                        normalized = sorted({
                            str(p).strip().upper()
                            for p in ctx_upstream
                            if isinstance(p, str) and p.strip()
                        })
                        if normalized:
                            meta = dict(main_protein.extra_data or {})
                            meta["upstream_of_main"] = normalized
                            main_protein.extra_data = meta
                except Exception as _exc:
                    # Persistence of this metadata must never block the
                    # rest of the sync — it's additive visibility, not
                    # load-bearing correctness.
                    print(
                        f"[DB SYNC] Failed to persist upstream_of_main for "
                        f"{protein_symbol}: {type(_exc).__name__}: {_exc}",
                        file=sys.stderr,
                    )

                # Step 2: Extract interactors for persistence.
                #
                # snapshot_json is intentionally compact for frontend/cache
                # use, while ctx_json is the rich source of truth. Chain
                # fields such as chain_context.full_chain and
                # chain_link_functions are load-bearing for db_sync; if we
                # persist snapshot-shaped interactors alone, fresh runs hit
                # the [CHAIN RECONSTRUCT] fallback and lose per-hop claims.
                snapshot_interactors = snapshot_data.get("interactors", [])
                interactors = snapshot_interactors

                if isinstance(ctx_json, dict):
                    ctx_interactors = ctx_json.get("interactors", [])
                    if isinstance(ctx_interactors, list) and ctx_interactors:
                        def _primary_key(item: Dict[str, Any]) -> str:
                            return str((item or {}).get("primary") or "").strip().upper()

                        rich_by_key = {
                            _primary_key(item): item
                            for item in ctx_interactors
                            if isinstance(item, dict) and _primary_key(item)
                        }
                        merged_interactors: List[Dict[str, Any]] = []
                        seen_keys: set[str] = set()

                        if isinstance(snapshot_interactors, list):
                            for snap_item in snapshot_interactors:
                                if not isinstance(snap_item, dict):
                                    merged_interactors.append(snap_item)
                                    continue
                                key = _primary_key(snap_item)
                                rich_item = rich_by_key.get(key)
                                if rich_item:
                                    merged = deepcopy(rich_item)
                                    # Keep snapshot-only additions, but never
                                    # overwrite populated rich fields with the
                                    # compact snapshot view.
                                    for field, value in snap_item.items():
                                        if merged.get(field) in (None, "", [], {}):
                                            merged[field] = deepcopy(value)
                                    merged_interactors.append(merged)
                                    seen_keys.add(key)
                                else:
                                    merged_interactors.append(snap_item)
                                    if key:
                                        seen_keys.add(key)

                        for key, rich_item in rich_by_key.items():
                            if key not in seen_keys:
                                merged_interactors.append(deepcopy(rich_item))

                        interactors = merged_interactors

                if not isinstance(interactors, list):
                    print(f"[WARN]WARNING: interactors is not a list, got {type(interactors)}", file=sys.stderr)
                    interactors = []

                # Reset pathway cache per sync run (shared across _save_claims calls)
                self._pw_cache: Dict[str, Optional[int]] = {}

                # Step 3: Process each interactor
                _touched_proteins: set = set()
                for interactor_data in interactors:
                    if not isinstance(interactor_data, dict):
                        print(f"[WARN]WARNING: Skipping invalid interactor data: {type(interactor_data)}", file=sys.stderr)
                        continue

                    # Skip rejected interactions (evidence validator flagged as invalid)
                    if interactor_data.get('_validation_status') == 'rejected':
                        print(f"[DB SYNC] Skipping rejected: {interactor_data.get('primary')}", file=sys.stderr)
                        continue

                    partner_symbol = interactor_data.get("primary")
                    if not partner_symbol:
                        print(f"[WARN]WARNING: Skipping interactor with no 'primary' field", file=sys.stderr)
                        continue

                    # Validate protein symbol. 3-state classification:
                    #   protein → proceed
                    #   pseudo  → refuse as a stand-alone DIRECT interactor
                    #             of the query, but allow as the primary of
                    #             an INDIRECT interactor so the chain's
                    #             real-protein hops (mid-chain biology) are
                    #             still preserved. The parent indirect row
                    #             pointing query↔pseudo is biologically thin
                    #             but better than losing every hop.
                    #   invalid → drop with structured log
                    _cls = classify_symbol(partner_symbol)
                    if _cls == "invalid":
                        print(f"[DB SYNC] Skipping invalid symbol: {partner_symbol}", file=sys.stderr)
                        continue
                    if _cls == "pseudo":
                        _itype = (interactor_data.get("interaction_type") or "").lower()
                        if _itype != "indirect":
                            print(
                                f"[DB SYNC] Refusing pseudo entity as direct interactor: "
                                f"{partner_symbol} (only valid as chain mediator or indirect endpoint)",
                                file=sys.stderr,
                            )
                            continue
                        # Pseudo as INDIRECT primary: log and proceed so the
                        # chain hops downstream still get created.
                        print(
                            f"[DB SYNC] Pseudo entity as indirect primary: "
                            f"{partner_symbol} — proceeding so chain hops survive",
                            file=sys.stderr,
                        )

                    # Skip self-interactions (query protein appearing as its own interactor)
                    if partner_symbol.upper() == protein_symbol.upper():
                        print(f"[DB SYNC] Skipping self-interaction: {partner_symbol}", file=sys.stderr)
                        continue

                    # Validate and fix false chains BEFORE database write
                    interactor_data = self._validate_and_fix_chain(interactor_data, protein_symbol)

                    # Get or create partner protein
                    partner_protein = self._get_or_create_protein(partner_symbol)
                    if partner_protein.query_count == 0:
                        stats["proteins_created"] += 1

                    # Save interaction (stores ENTIRE interactor_data in JSONB)
                    created = self._save_interaction(
                        protein_a=main_protein,
                        protein_b=partner_protein,
                        data=interactor_data,
                        discovered_in=protein_symbol
                    )

                    if created:
                        stats["interactions_created"] += 1
                    else:
                        stats["interactions_updated"] += 1

                    # Process chain relationships for indirect interactions
                    if interactor_data.get("interaction_type") == "indirect":
                        chain_stats = self.sync_chain_relationships(
                            query_protein=protein_symbol,
                            interactor_data=interactor_data
                        )
                        stats["interactions_created"] += chain_stats["chain_links_created"]
                        stats["interactions_updated"] += chain_stats["chain_links_updated"]

                    # Track partner for batch count update
                    _touched_proteins.add(partner_protein)

                # Step 4: Update main protein metadata
                main_protein.last_queried = datetime.now(timezone.utc).replace(tzinfo=None)
                main_protein.query_count += 1

                # Batch-update interaction counts for ALL touched proteins
                _touched_proteins.add(main_protein)
                if _touched_proteins:
                    from sqlalchemy import func, union_all, literal_column
                    _touched_ids = [p.id for p in _touched_proteins]
                    # Single aggregation query instead of N separate count queries
                    _counts_q = db.session.query(
                        literal_column('pid'), func.count()
                    ).select_from(
                        union_all(
                            db.session.query(
                                Interaction.protein_a_id.label('pid')
                            ).filter(Interaction.protein_a_id.in_(_touched_ids)),
                            db.session.query(
                                Interaction.protein_b_id.label('pid')
                            ).filter(Interaction.protein_b_id.in_(_touched_ids)),
                        ).subquery()
                    ).group_by('pid').all()
                    _count_map = dict(_counts_q)
                    for _protein in _touched_proteins:
                        _protein.total_interactions = _count_map.get(_protein.id, 0)

            # Commit transaction
            db.session.commit()

        except Exception as e:
            # Rollback on any error
            db.session.rollback()
            print(f"[ERROR]Database sync failed: {e}", file=sys.stderr)
            raise

        return stats

    def _get_or_create_protein(self, symbol: str) -> Protein:
        """
        Get existing protein or create new one.

        Args:
            symbol: Protein symbol (e.g., "ATXN3")

        Returns:
            Protein instance

        Side effects:
            - Creates new protein in database if not exists
            - Flushes to get ID (does not commit)
        """
        if not symbol:
            raise ValueError("symbol cannot be empty")

        # Unified normalization: uppercase, trim whitespace, strip decorative
        # punctuation, translate Greek letters → Latin, drop zero-width chars.
        # Canonical HGNC symbols arrive in many guises from the LLM and from
        # literature copy-paste ("Ataxin-3", "ATXN3", "α-synuclein", "SNCA"
        # with a trailing period, etc.); we fold them all to one form so
        # two call sites never produce two canonical rows for the same
        # biology.
        from utils.protein_aliases import (
            normalize_symbol,
            lookup_by_alias,
            ensure_seed_aliases_registered,
            record_alias,
        )
        raw_input = symbol
        symbol = normalize_symbol(symbol)
        if not symbol:
            raise ValueError("symbol cannot be empty")

        # Single resolver covers: (1) direct canonical lookup, (2) the
        # ``ProteinAlias`` table, (3) hard-coded seed synonyms. Returns
        # the canonical row regardless of how the caller spelled it.
        protein = lookup_by_alias(raw_input)

        if not protein:
            # Create the canonical row (symbol already normalized above)
            # and register any hard-coded seed aliases so next time the
            # same protein arrives under a synonym we resolve it via the
            # alias table instead of creating yet another duplicate.
            #
            # Pseudo flag: generic biomolecule classes (RNA, Ubiquitin, ...)
            # are stored as Protein rows so chain hops can reference them,
            # but flagged ``is_pseudo`` so the frontend renders them as a
            # class and ``/api/search`` refuses them as stand-alone queries.
            _initial_extra: dict = {}
            if classify_symbol(symbol) == "pseudo":
                _initial_extra["is_pseudo"] = True
            protein = Protein(
                symbol=symbol,
                first_queried=datetime.now(timezone.utc).replace(tzinfo=None),
                last_queried=datetime.now(timezone.utc).replace(tzinfo=None),
                query_count=0,
                total_interactions=0,
                extra_data=_initial_extra,
            )
            db.session.add(protein)
            db.session.flush()  # Get ID without committing
            ensure_seed_aliases_registered(protein)
            if _initial_extra.get("is_pseudo"):
                print(
                    f"[DB SYNC] Created pseudo Protein row: {symbol} (is_pseudo=True)",
                    file=sys.stderr,
                )
        else:
            # Backfill the pseudo flag on existing rows whose symbol is now
            # whitelisted but which were created before pseudo support landed.
            if classify_symbol(symbol) == "pseudo":
                _ed = dict(protein.extra_data or {})
                if not _ed.get("is_pseudo"):
                    _ed["is_pseudo"] = True
                    protein.extra_data = _ed

        # If the caller's raw_input differed from the canonical symbol
        # (e.g. arrived as "Ataxin-3" or "atxn3"), record the alias so
        # subsequent lookups take the faster alias-table path instead
        # of re-doing the seed-map scan. Silent no-op when raw matches.
        try:
            raw_norm = normalize_symbol(raw_input)
            if raw_norm and raw_norm != protein.symbol.upper():
                record_alias(protein, raw_input, source="observed")
        except Exception:
            # Alias bookkeeping is additive — a flush failure here must
            # not prevent the caller from getting back their Protein row.
            pass

        return protein

    def _lookup_arrow_for_pair(self, from_protein_symbol: str, to_protein_symbol: str) -> str:
        """
        Look up the arrow type for an interaction between two proteins.

        Used to build chain_with_arrows for indirect interactions.

        Args:
            from_protein_symbol: Source protein symbol
            to_protein_symbol: Target protein symbol

        Returns:
            Arrow type ('activates', 'inhibits', 'binds', 'regulates')
            Returns 'binds' as default if interaction not found
        """
        # Get protein objects (normalize case so lookups match the
        # canonical uppercase rows created by _get_or_create_protein).
        from_protein = Protein.query.filter_by(symbol=(from_protein_symbol or "").strip().upper()).first()
        to_protein = Protein.query.filter_by(symbol=(to_protein_symbol or "").strip().upper()).first()

        if not from_protein or not to_protein:
            return 'binds'  # Default if proteins don't exist

        # Query interaction with canonical ordering
        protein_a_id = min(from_protein.id, to_protein.id)
        protein_b_id = max(from_protein.id, to_protein.id)

        interaction = Interaction.query.filter_by(
            protein_a_id=protein_a_id,
            protein_b_id=protein_b_id
        ).first()

        if not interaction:
            return 'binds'  # Default if interaction doesn't exist

        # Get arrow type
        # Priority: arrows JSONB field > arrow field (backward compat)
        if interaction.arrows:
            # Determine direction based on canonical ordering
            if from_protein.id < to_protein.id:
                # Natural order: from=a, to=b
                # Check a_to_b direction
                if 'a_to_b' in interaction.arrows and interaction.arrows['a_to_b']:
                    return interaction.arrows['a_to_b'][0]  # Primary arrow
            else:
                # Reversed order: from=b, to=a
                # Check b_to_a direction
                if 'b_to_a' in interaction.arrows and interaction.arrows['b_to_a']:
                    return interaction.arrows['b_to_a'][0]  # Primary arrow

        # Fallback to legacy arrow field
        return interaction.arrow or 'binds'

    def _save_interaction(
        self,
        protein_a: Protein,
        protein_b: Protein,
        data: Dict[str, Any],
        discovered_in: str
    ) -> bool:
        """
        Save or update interaction with CANONICAL ORDERING.

        Strategy:
        - Enforces protein_a_id < protein_b_id to prevent duplicates
        - Stores FULL interactor data in JSONB
        - Preserves original direction in data JSONB
        - Transforms direction when storing in reversed order

        Args:
            protein_a: Main protein (queried protein)
            protein_b: Partner protein (interactor)
            data: Complete interactor dict from pipeline
            discovered_in: Which protein query found this interaction

        Returns:
            True if created new interaction, False if updated existing

        Side effects:
            - Creates or updates interaction in database
            - Flushes to database (does not commit)
        """
        if not protein_a or not protein_b:
            raise ValueError("protein_a and protein_b cannot be None")

        if protein_a.id == protein_b.id:
            raise ValueError("Cannot create self-interaction")

        if not isinstance(data, dict):
            raise ValueError("data must be a dict")

        data = _normalize_payload_contract(data)

        # CANONICAL ORDERING: Always store with lower ID as protein_a
        # This prevents (A,B) and (B,A) from both existing in database
        original_direction = data.get("direction")

        # S1: kill incoming bidirectional at the gate. The LLM or prior
        # code may still emit it; replace with the best asymmetric guess
        # from the arrow semantics.
        if original_direction == "bidirectional":
            from utils.direction import infer_direction_from_arrow
            original_direction = infer_direction_from_arrow(data.get("arrow"))

        # Convert query-relative direction to protein-absolute direction
        # protein_a (arg) = query protein (main)
        # protein_b (arg) = partner protein (primary)
        # S1: directions are always asymmetric: "a_to_b" or "b_to_a"
        _direction_fallback_used = False
        if protein_a.id < protein_b.id:
            # Natural canonical order: query protein has lower ID
            canonical_a = protein_a
            canonical_b = protein_b
            # Convert query-relative → absolute:
            # main_to_primary (query → partner) = a_to_b (protein_a → protein_b)
            # primary_to_main (partner → query) = b_to_a (protein_b → protein_a)
            if original_direction == "main_to_primary":
                stored_direction = "a_to_b"
            elif original_direction == "primary_to_main":
                stored_direction = "b_to_a"
            else:
                # Unknown direction, default to a_to_b
                stored_direction = original_direction or "a_to_b"
                if not original_direction:
                    _direction_fallback_used = True
        else:
            # Reversed canonical order: partner protein becomes protein_a in storage
            canonical_a = protein_b  # partner (was arg protein_b, now stored protein_a)
            canonical_b = protein_a  # query (was arg protein_a, now stored protein_b)
            # After swap: canonical_a (partner) < canonical_b (query)
            # Convert query-relative → absolute:
            # main_to_primary (query → partner) = b_to_a (after swap: protein_b → protein_a)
            # primary_to_main (partner → query) = a_to_b (after swap: protein_a → protein_b)
            if original_direction == "main_to_primary":
                stored_direction = "b_to_a"
            elif original_direction == "primary_to_main":
                stored_direction = "a_to_b"
            else:
                # Unknown direction, default to a_to_b
                stored_direction = original_direction or "a_to_b"
                if not original_direction:
                    _direction_fallback_used = True

        # D.4: surface the implicit fallback so a payload missing
        # ``direction`` doesn't silently pick "a_to_b" — the silent
        # default is how query-relative ↔ canonical conversions drift.
        if _direction_fallback_used:
            try:
                from utils.observability import log_event
                log_event(
                    "direction_fallback",
                    level="warn",
                    tag="DIRECTION FALLBACK",
                    protein_a=protein_a.symbol,
                    protein_b=protein_b.symbol,
                    arrow=data.get("arrow"),
                    fallback_to=stored_direction,
                    reason="payload missing 'direction' field",
                )
            except Exception:
                # Logging must never break a sync write.
                pass

        # Store original direction in data for retrieval
        data_copy = deepcopy(data)
        data_copy["_original_direction"] = original_direction
        data_copy["_query_context"] = discovered_in

        # Check if interaction exists (using canonical IDs)
        interaction = Interaction.query.filter_by(
            protein_a_id=canonical_a.id,
            protein_b_id=canonical_b.id
        ).first()

        # Extract denormalized fields (for fast queries)
        confidence = data.get("confidence")
        arrow = _clamp(normalize_arrow(data.get("arrow"), default="regulates"), 50)
        interaction_type = _clamp(data.get("interaction_type", "direct"), 100)
        upstream_interactor = _clamp(data.get("upstream_interactor"), 50)

        # NOTE: upstream_interactor CAN be one of the proteins in the interaction pair
        # This is VALID when querying the regulating protein
        # Example: Query VCP → finds IκBα with upstream=VCP
        # Meaning: VCP regulates IκBα (VCP→IκBα directed edge)
        # This is NOT a self-interaction (VCP→VCP would be invalid)
        # The self-link will be filtered out in sync_chain_relationships (line 446)

        # Extract chain metadata for indirect interactions
        mediator_chain = data.get("mediator_chain")  # e.g., ["VCP", "LAMP2"] for multi-hop paths
        depth = data.get("depth", 1)  # Default to 1 (direct) if not specified
        chain_context = data.get("chain_context")  # Full chain context from all perspectives

        # NEW (Issue #4): Extract arrows field (multiple arrow types per direction)
        arrows_raw = normalize_arrows_map(data.get("arrows", {}))
        if not arrows_raw and arrow:
            # Backward compatibility: convert single arrow to arrows dict
            arrows_raw = {"main_to_primary": [arrow]}

        # CRITICAL: Convert semantic keys → canonical keys for BOTH orderings.
        # Natural order: main_to_primary → a_to_b, primary_to_main → b_to_a
        # Reversed order: main_to_primary → b_to_a (flipped), primary_to_main → a_to_b
        if protein_a.id < protein_b.id:
            # Natural canonical order: convert semantic → canonical (no direction flip)
            arrows = {}
            if "main_to_primary" in arrows_raw:
                arrows["a_to_b"] = arrows_raw["main_to_primary"]
            if "primary_to_main" in arrows_raw:
                arrows["b_to_a"] = arrows_raw["primary_to_main"]
            # Passthrough already-canonical keys
            if "a_to_b" in arrows_raw and "a_to_b" not in arrows:
                arrows["a_to_b"] = arrows_raw["a_to_b"]
            if "b_to_a" in arrows_raw and "b_to_a" not in arrows:
                arrows["b_to_a"] = arrows_raw["b_to_a"]
            if not arrows:
                arrows = arrows_raw  # fallback for unknown key schemes
        else:
            # Reversed canonical order: flip arrow directions
            # main_to_primary becomes b_to_a (after swap)
            # primary_to_main becomes a_to_b (after swap)
            arrows = {}
            if "main_to_primary" in arrows_raw:
                arrows["b_to_a"] = arrows_raw["main_to_primary"]
            if "primary_to_main" in arrows_raw:
                arrows["a_to_b"] = arrows_raw["primary_to_main"]
            # Legacy defense: fold any residual bidirectional keys (from pre-S1 data) into a_to_b
            if "bidirectional" in arrows_raw:
                arrows.setdefault("a_to_b", []).extend(arrows_raw["bidirectional"])

        # S1b: collapse to a single rational direction per user intent.
        # If stored_direction is asymmetric (a_to_b / b_to_a / main_to_primary
        # / primary_to_main) AND the incoming arrows dict has populated BOTH
        # canonical keys, keep only the side matching the direction. Without
        # this, each chain-hop writer that touches the pair unions arrows
        # into both sides, and the frontend renders ↔ for a pair that the
        # pipeline actually resolved as unidirectional.
        if arrows and arrows.get("a_to_b") and arrows.get("b_to_a"):
            _sd = (stored_direction or "").lower()
            if _sd in ("a_to_b", "main_to_primary"):
                # Only keep a_to_b iff we're in natural canonical order,
                # otherwise main_to_primary already mapped to b_to_a above.
                if protein_a.id < protein_b.id or _sd == "a_to_b":
                    arrows.pop("b_to_a", None)
                else:
                    arrows.pop("a_to_b", None)
            elif _sd in ("b_to_a", "primary_to_main"):
                if protein_a.id < protein_b.id or _sd == "b_to_a":
                    arrows.pop("a_to_b", None)
                else:
                    arrows.pop("b_to_a", None)

        if arrow:
            arrows = _primary_arrow_map(stored_direction, arrow)
            data_copy["arrows"] = arrows
            data_copy["arrow"] = arrow

        # Extract function_context from tagged functions.
        # Honor the top-level ``function_context`` field first (this is
        # the shape the LLM emits for chain-link / net / direct claims
        # per FUNCTION_CONTEXT_LABELING) and fall back to the legacy
        # ``_context.type`` shape for older callers. Without the
        # top-level read, chain-link functions whose context is
        # 'chain_derived' silently failed the lookup and the parent
        # row defaulted to 'direct' — producing the
        # function_context_drift verification failure where the
        # parent label disagreed with every child claim.
        functions = data.get("functions", [])
        function_contexts = set()
        for fn in functions:
            if not isinstance(fn, dict):
                continue
            ctx = (fn.get("function_context") or "").strip().lower()
            if not ctx and isinstance(fn.get("_context"), dict):
                ctx = (fn["_context"].get("type") or "").strip().lower()
            if ctx:
                function_contexts.add(ctx)

        # Determine overall function_context for this interaction
        if not function_contexts:
            function_context = "direct"  # Default
        elif len(function_contexts) == 1:
            function_context = next(iter(function_contexts))  # Single type
        else:
            function_context = "mixed"  # Multiple types
        function_context = _clamp(function_context, 20)
        stored_direction = _clamp(stored_direction, 20)
        discovered_in = _clamp(discovered_in, 50)

        if interaction:
            # UPDATE existing interaction (merge data intelligently)
            # CRITICAL FIX: Merge functions and evidence instead of choosing one or the other
            existing_evidence = interaction.data.get("evidence", [])
            new_evidence = data_copy.get("evidence", [])
            existing_functions = interaction.data.get("functions", [])
            new_functions = data_copy.get("functions", [])

            # Merge evidence arrays (deduplicate by PMID + content-hash fallback)
            from utils.json_helpers import _evidence_dedup_key
            merged_evidence = existing_evidence.copy()
            existing_ev_keys = {_evidence_dedup_key(ev) for ev in existing_evidence}
            for ev in new_evidence:
                if _evidence_dedup_key(ev) not in existing_ev_keys:
                    merged_evidence.append(ev)
                    existing_ev_keys.add(_evidence_dedup_key(ev))

            # Merge function arrays. Key must include function_context to
            # match the DB's uq_claim_interaction_fn_pw_ctx unique index —
            # otherwise a 'direct' and 'chain_derived' pair sharing
            # name/pathway/arrow collide here and the second one gets
            # silently dropped before it ever reaches the DB.
            def _fn_key(fn):
                name = (fn.get("function", "") or "").strip().lower()
                pw = (fn.get("pathway", "") or "").strip().lower()
                arrow = (fn.get("arrow", "") or "").strip().lower()
                ctx = (fn.get("function_context", "") or "").strip().lower()
                return f"{name}||{pw}||{arrow}||{ctx}"

            merged_functions = existing_functions.copy()
            existing_fn_keys = {_fn_key(fn) for fn in existing_functions if fn.get("function")}
            for fn in new_functions:
                if fn.get("function") and _fn_key(fn) not in existing_fn_keys:
                    merged_functions.append(fn)

            # Deduplicate merged functions (case-insensitive, prefer validated)
            merged_functions = deduplicate_functions(merged_functions)

            # Determine which data is richer by total content length (not just count)
            existing_richness = sum(len(str(e)) for e in existing_evidence)
            new_richness = sum(len(str(e)) for e in new_evidence)
            # Direction-update rule: only overwrite the existing direction
            # when the incoming one is strictly more specific (a real
            # asymmetric direction vs. None or the "bidirectional"
            # placeholder). This used to be hand-rolled in two nearly
            # identical if-blocks — now it's a single pure helper.
            from utils.direction import is_more_specific_direction
            if new_richness > existing_richness:
                # New data is richer, use it as base and merge in old functions
                interaction.data = data_copy
                interaction.data["functions"] = merged_functions
                interaction.data["evidence"] = merged_evidence
                if is_more_specific_direction(stored_direction, interaction.direction):
                    interaction.direction = stored_direction
            else:
                # Existing data is richer or equal, merge in new functions/evidence
                interaction.data["functions"] = merged_functions
                interaction.data["evidence"] = merged_evidence
                interaction.data["_last_seen_in"] = discovered_in
                interaction.data["_last_updated"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                if is_more_specific_direction(stored_direction, interaction.direction):
                    interaction.direction = stored_direction

            interaction.confidence = confidence
            interaction.arrow = arrow
            # Keep scalar arrow and arrows JSONB atomically aligned. Older
            # code unioned every observed arrow into the JSONB map, which left
            # rows like ATXN3↔KEAP1 with activates+inhibits in one direction.
            # The UI needs one predominant edge label per stored direction.
            interaction.arrows = _primary_arrow_map(interaction.direction, interaction.arrow)

            # CRITICAL: Never downgrade direct→indirect
            # This prevents chain processing from corrupting direct interactions
            if interaction.interaction_type == "direct" and interaction_type == "indirect":
                # Existing direct interaction takes precedence
                # Don't overwrite with chain-derived metadata from mediator role.
                # Dedup the log across repeat hops — the same protein pair
                # fires this guard once per chain hop it participates in
                # (e.g. TDP43↔ATG7 hits it for →MAP1LC3B, →ATG12, →ATG3).
                # Lazy-init the dedup set per-instance (matches the _pw_cache
                # pattern used elsewhere in this class).
                if not hasattr(self, "_preserving_direct_logged"):
                    self._preserving_direct_logged = set()
                _pair_key = (canonical_a.id, canonical_b.id)
                if _pair_key not in self._preserving_direct_logged:
                    print(
                        f"[DB SYNC] Preserving direct interaction: "
                        f"{canonical_a.symbol}↔{canonical_b.symbol} "
                        "(refusing downgrade to indirect)",
                        file=sys.stderr,
                    )
                    self._preserving_direct_logged.add(_pair_key)
                # Keep existing direct metadata — but if chain-derived functions
                # were merged above, update function_context to reflect the mix
                if function_context == "mixed" or (
                    function_context != "direct" and interaction.function_context == "direct"
                ):
                    interaction.function_context = "mixed"
            else:
                # Safe to update (creating new, upgrading indirect, or updating direct with direct)
                interaction.interaction_type = interaction_type
                # C0: Single write surface for chain state. Derives
                # mediator_chain / upstream_interactor / depth /
                # chain_context from one ``full_chain`` via ChainView so
                # the four legacy fields never drift. ``chain_id`` is
                # populated separately by ``_tag_claims_with_chain``
                # once the IndirectChain row exists.
                from utils.chain_view import ChainView
                ChainView.from_interaction_data(
                    data, query_protein=discovered_in
                ).apply_to_interaction(interaction)
                interaction.chain_with_arrows = data.get("chain_with_arrows")
                interaction.function_context = function_context

            interaction.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            created = False
        else:
            # CREATE new interaction. Chain fields (mediator_chain /
            # upstream_interactor / depth / chain_context) are applied
            # via the single ChainView write surface after construction
            # (C0), so they're always derived from ``full_chain`` by one
            # code path instead of set field-by-field here.
            interaction = Interaction(
                protein_a_id=canonical_a.id,
                protein_b_id=canonical_b.id,
                data=data_copy,
                confidence=confidence,
                direction=stored_direction,
                arrow=arrow,
                arrows=arrows,
                interaction_type=interaction_type,
                chain_with_arrows=data.get("chain_with_arrows"),
                function_context=function_context,
                discovered_in_query=discovered_in,
                discovery_method='pipeline'
            )
            from utils.chain_view import ChainView
            ChainView.from_interaction_data(
                data, query_protein=discovered_in
            ).apply_to_interaction(interaction)
            db.session.add(interaction)
            created = True

        db.session.flush()  # Persist to DB (does not commit)

        # Extract atomic claims from functions[]
        # Use interaction.data (final merged state) not data_copy (incoming only)
        self._save_claims(interaction, interaction.data, discovered_in)

        # Store pathway links (if pathways were assigned by PathwayAssigner)
        pathways_data = data.get("pathways", [])
        if pathways_data and interaction:
            # Batch-load pathways and existing links (avoids N+1 queries)
            pw_names = [pw_info.get("canonical_name") or pw_info.get("name") for pw_info in pathways_data]
            pw_names = [n for n in pw_names if n]
            pw_map = {pw.name.lower(): pw for pw in Pathway.query.filter(Pathway.name.in_(pw_names)).all()} if pw_names else {}
            existing_link_set = {
                pi.pathway_id for pi in PathwayInteraction.query.filter_by(interaction_id=interaction.id).all()
            }

            for pw_info in pathways_data:
                pw_name = pw_info.get("canonical_name") or pw_info.get("name")
                if not pw_name:
                    continue

                # Get or create Pathway record (using batch-loaded map)
                pathway = pw_map.get(pw_name.lower())
                if not pathway:
                    pathway = Pathway(
                        name=_clamp(pw_name, 200),
                        ontology_id=_clamp(pw_info.get("ontology_id"), 50),
                        ontology_source=_clamp(pw_info.get("ontology_source"), 20),
                        ai_generated=not bool(pw_info.get("ontology_id")),
                        usage_count=0
                    )
                    db.session.add(pathway)
                    db.session.flush()
                    pw_map[pw_name.lower()] = pathway

                # Check if link already exists (using batch-loaded set)
                if pathway.id not in existing_link_set:
                    link = PathwayInteraction(
                        pathway_id=pathway.id,
                        interaction_id=interaction.id,
                        assignment_confidence=pw_info.get("confidence", 0.8),
                        assignment_method="ai_pipeline"
                    )
                    db.session.add(link)
                    existing_link_set.add(pathway.id)
                    pathway.usage_count = (pathway.usage_count or 0) + 1

            db.session.flush()

        return created

    def _save_claims(self, interaction, data: Dict[str, Any], discovered_in: str) -> int:
        """Extract functions from data and save as individual InteractionClaim rows.

        Multiple claims per interaction are allowed — no unique constraint on
        (function_name, pathway_name).  Dedup is best-effort by in-memory key.
        Returns number of new claims created.
        """
        from models import InteractionClaim, Pathway

        functions = data.get("functions", [])
        claims_created = 0

        # Batch-load all existing claims for this interaction (avoids N+1).
        # H1: key the dedup dict with the same COALESCE semantics as the
        # uq_claim_interaction_fn_pw_ctx index so Python lookups never
        # disagree with the DB about whether a row already exists.
        existing_claims = InteractionClaim.query.filter_by(
            interaction_id=interaction.id
        ).all()
        _existing_by_key = {
            _claim_dedup_key(c.function_name, c.pathway_name, c.function_context): c
            for c in existing_claims
        }

        # ── Per-interaction claims ceiling ──────────────────────────
        _MAX_CLAIMS = int(os.environ.get("MAX_CLAIMS_PER_INTERACTION", "30"))
        if len(existing_claims) >= _MAX_CLAIMS:
            print(
                f"  [CLAIMS CAP] interaction {interaction.id} already has "
                f"{len(existing_claims)} claims (cap={_MAX_CLAIMS}); skipping new claims"
            )
            # Tag the interaction so downstream knows data was truncated
            if interaction.data is None:
                interaction.data = {}
            interaction.data["_claims_capped"] = True
            interaction.data["_claims_cap_limit"] = _MAX_CLAIMS
            return 0

        # Use class-level pathway cache (shared across _save_claims calls within one sync run)
        if not hasattr(self, '_pw_cache'):
            self._pw_cache = {}

        def _resolve_pw(name: Optional[str]) -> Optional[int]:
            if not name:
                return None
            if name not in self._pw_cache:
                pw = Pathway.query.filter_by(name=name).first()
                self._pw_cache[name] = pw.id if pw else None
            return self._pw_cache[name]

        def _claim_arrow_from(*values) -> str:
            for value in values:
                if value:
                    return normalize_arrow(value)
            return "regulates"

        def _claim_direction_from(func: Optional[Dict[str, Any]] = None, fallback=None) -> str:
            func = func or {}
            return semantic_claim_direction(
                func.get("interaction_direction")
                or func.get("direction")
                or func.get("likely_direction")
                or fallback
                or interaction.direction
            )

        if not functions:
            # Create synthetic claim from interaction-level summary if available.
            # Use support_summary to populate structured fields so the UI can
            # render Mechanism / Effect / Cascade even without full function data.
            summary = data.get("support_summary") or data.get("summary") or ""
            mechanism_text = data.get("mechanism") or data.get("effect") or summary or ""
            effect_text = data.get("effect") or summary or ""
            pathway_name = data.get("step3_finalized_pathway")

            # Gate: if every non-empty source is a known placeholder like
            # "Discovered via chain resolution", writing a claim means the
            # modal will render that same stub in Mechanism, Effect, Cascade
            # and Specific Effects — five copies of a useless string. Skip
            # creation entirely; let the read-time parent-fallback handle
            # this hop, or let the UI render an empty-state for the row.
            non_empty = [s for s in (summary, mechanism_text, effect_text) if s]
            if non_empty and all(_is_placeholder_text(s) for s in non_empty):
                return claims_created

            bio_consequences = (
                [summary] if summary and not _is_placeholder_text(summary) else []
            )
            specific_eff = (
                [summary] if summary and not _is_placeholder_text(summary) else []
            )
            if summary or mechanism_text:
                # Per-hop signature (set by ``sync_chain_relationships``)
                # makes the synthetic claim's function_name distinct per
                # hop direction, so cyclic chains can have one claim per
                # visit without colliding on uq_claim_fn_null_pw_ctx.
                # Direct interactions (no chain context) skip the prefix
                # and keep their existing function_name shape.
                hop_sig = data.get("_hop_signature")
                if hop_sig:
                    prefix = f"[{hop_sig}] "
                    body_budget = max(0, 200 - len(prefix))
                    func_name = (
                        prefix + summary[:body_budget]
                        if summary
                        else f"{prefix}Uncharacterized interaction"[:200]
                    )
                else:
                    func_name = summary[:200] if summary else "Uncharacterized interaction"
                # Use the SAME function_context that the INSERT below will
                # stamp (``interaction.function_context``). Previously this
                # looked up with ``None`` while the INSERT used the actual
                # context, so dedup silently missed and the DB's
                # ``uq_claim_fn_null_pw_ctx`` index caught the duplicate
                # as a UniqueViolation at flush time. Same mistake on the
                # fallback branch below.
                lookup_ctx = _clamp(interaction.function_context, 20)
                existing = _existing_by_key.get(_claim_dedup_key(func_name, pathway_name, lookup_ctx))
                if existing and pathway_name and not existing.pathway_name:
                    target_exists = _existing_by_key.get(_claim_dedup_key(func_name, pathway_name, lookup_ctx))
                    if not target_exists:
                        existing.pathway_name = pathway_name
                        existing.pathway_id = _resolve_pw(pathway_name)
                if not existing:
                    claim = InteractionClaim(
                        interaction_id=interaction.id,
                        function_name=func_name,
                        arrow=_clamp(_claim_arrow_from(data.get("arrow"), interaction.arrow), 50),
                        interaction_effect=_clamp(data.get("interaction_effect"), 50),
                        direction=_clamp(_claim_direction_from(fallback=data.get("direction")), 30),
                        mechanism=_strip_placeholder(mechanism_text),
                        effect_description=_strip_placeholder(effect_text),
                        biological_consequences=bio_consequences,
                        specific_effects=specific_eff,
                        evidence=data.get("evidence", []),
                        pmids=data.get("pmids", []),
                        pathway_name=_clamp(pathway_name, 200),
                        pathway_id=_resolve_pw(pathway_name),
                        confidence=data.get("confidence"),
                        # Synthetic claim inherits from its parent interaction
                        # because there's no source function dict to read. The
                        # claim *is* the interaction's own summary, so carrying
                        # the interaction's function_context is architecturally
                        # correct (the interaction row already has this field
                        # set by _save_interaction above).
                        function_context=_clamp(interaction.function_context, 20),
                        source_query=_clamp(discovered_in, 50),
                        discovery_method="pipeline_summary",
                        raw_function_data=None,
                    )
                    db.session.add(claim)
                    claims_created += 1
            else:
                # Last resort: create minimal claim with sentinel name.
                # C2: fall back through primary_arrow (which reads the
                # arrows JSONB first) before the hard-coded "interacts".
                #
                # P1.9 quarantine: fallback claims are diagnostic, not
                # scientific. They have no mechanism, no cascades, no
                # evidence. Strip the pathway so they don't pollute
                # PathwayInteraction counts or appear under any pathway
                # in the UI. data_builder filters them from
                # _interaction_pathways and modal lists at read time.
                arrow_val = normalize_arrow(data.get("arrow") or interaction.primary_arrow, default="binds")
                func_name = "__fallback__"
                lookup_ctx_fb = _clamp(interaction.function_context, 20)
                # Use a stable dedup key with pathway=None so re-syncs
                # don't create duplicates; the existing pathway-bearing
                # fallback (if any from a prior run) is matched too.
                existing = (
                    _existing_by_key.get(_claim_dedup_key(func_name, None, lookup_ctx_fb))
                    or _existing_by_key.get(_claim_dedup_key(func_name, pathway_name, lookup_ctx_fb))
                )
                if not existing:
                    claim = InteractionClaim(
                        interaction_id=interaction.id,
                        function_name=func_name,
                        arrow=_clamp(_claim_arrow_from(arrow_val), 50),
                        interaction_effect=_clamp(data.get("interaction_effect"), 50),
                        direction=_clamp(_claim_direction_from(fallback=data.get("direction")), 30),
                        # NO pathway for fallback claims — they're
                        # diagnostic, not science.
                        pathway_name=None,
                        pathway_id=None,
                        function_context=_clamp(interaction.function_context, 20),
                        source_query=_clamp(discovered_in, 50),
                        discovery_method="pipeline_fallback",
                    )
                    db.session.add(claim)
                    claims_created += 1
            db.session.flush()
            return claims_created

        for func in functions:
            func_name = (func.get("function") or "").strip()
            if not func_name:
                continue

            # Extract pathway string (handle both str and dict formats)
            pathway_raw = func.get("pathway")
            if isinstance(pathway_raw, str) and pathway_raw:
                pathway_name = pathway_raw
            elif isinstance(pathway_raw, dict):
                pathway_name = pathway_raw.get("canonical_name") or pathway_raw.get("name")
            else:
                pathway_name = data.get("step3_finalized_pathway")

            # H1: dedup by the COALESCE-normalised key so Python lookups
            # match the uq_claim_interaction_fn_pw_ctx index exactly.
            # Fall back to the parent interaction's function_context when
            # the function dict doesn't carry one — this prevents claims
            # from being saved with NULL context, which was the source of
            # the function_context_drift verification failure.
            #
            # L4.5 — function_context enum validation at ingest. The schema
            # CHECK enforces {direct, net, chain_derived, mixed}; any other
            # value (or null) was previously silently coerced by the
            # downstream normalize_function_contexts post-processor, hiding
            # LLM enum violations. Now: log explicitly so we see what the
            # model is producing, then fall back to the parent's context.
            _ALLOWED_FN_CTX = {"direct", "net", "chain_derived", "mixed"}
            _raw_fn_ctx = func.get("function_context")
            if _raw_fn_ctx and _raw_fn_ctx not in _ALLOWED_FN_CTX:
                print(
                    f"[INGEST GATE] function_context out-of-enum on claim "
                    f"{func.get('function', '?')!r}: got {_raw_fn_ctx!r} — "
                    f"falling back to parent context "
                    f"{interaction.function_context!r}",
                    file=sys.stderr,
                )
                _raw_fn_ctx = None
            fn_ctx = _clamp(
                _raw_fn_ctx or interaction.function_context,
                20,
            )
            claim_arrow = _claim_arrow_from(func.get("arrow"), data.get("arrow"), interaction.arrow)
            claim_direction = _claim_direction_from(func, fallback=data.get("direction"))
            dedup_key = _claim_dedup_key(func_name, pathway_name, fn_ctx)
            existing = _existing_by_key.get(dedup_key)

            if existing:
                # Update pathway_name if we now have one and existing doesn't
                # BUT only if the target combo doesn't already exist
                if pathway_name and not existing.pathway_name:
                    target_exists = _existing_by_key.get(dedup_key)
                    if not target_exists:
                        existing.pathway_name = pathway_name
                        existing.pathway_id = _resolve_pw(pathway_name)
                        # Update the dedup dict key
                        _existing_by_key.pop(_claim_dedup_key(func_name, None, fn_ctx), None)
                        _existing_by_key[dedup_key] = existing
                # Merge evidence (deduplicate by PMID + content-hash fallback)
                from utils.json_helpers import _evidence_dedup_key
                old_pmids = set(p for p in (existing.pmids or []))
                new_evidence = func.get("evidence", [])
                merged_evidence = list(existing.evidence or [])
                existing_ev_keys = {_evidence_dedup_key(ev) for ev in merged_evidence}
                for ev in new_evidence:
                    key = _evidence_dedup_key(ev)
                    if key not in existing_ev_keys:
                        merged_evidence.append(ev)
                        existing_ev_keys.add(key)
                        if ev.get("pmid"):
                            old_pmids.add(ev["pmid"])
                existing.evidence = merged_evidence
                existing.pmids = list(old_pmids | set(func.get("pmids", [])))
                existing.raw_function_data = func
                existing.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                # R2: propagate function_context from the (post-processed)
                # function dict to the existing claim if the claim's context
                # is still NULL. This is NOT inheritance — the normalize
                # stage already stamped the correct value on the function
                # dict; we're just writing it through to the DB row.
                if fn_ctx and not existing.function_context:
                    existing.function_context = fn_ctx
                if not existing.direction or existing.direction not in ("main_to_primary", "primary_to_main"):
                    existing.direction = _clamp(claim_direction, 30)
                if not existing.arrow or existing.arrow not in CANONICAL_ARROW_SET:
                    existing.arrow = _clamp(claim_arrow, 50)
            else:
                claim = InteractionClaim(
                    interaction_id=interaction.id,
                    function_name=func_name,
                    arrow=_clamp(claim_arrow, 50),
                    interaction_effect=_clamp(func.get("interaction_effect") or func.get("function_effect"), 50),
                    direction=_clamp(claim_direction, 30),
                    mechanism=_strip_placeholder(func.get("cellular_process")),
                    # _strip_placeholder now returns None for a wider set
                    # of placeholder fragments (e.g. "Discovered via chain
                    # resolution"), so chained slicing like `.[:500]` would
                    # crash on NoneType. Bind the stripped value first and
                    # guard before slicing.
                    effect_description=(
                        _strip_placeholder(func.get("effect_description"))
                        or (
                            (_strip_placeholder(func.get("cellular_process", "")) or "")[:500]
                            if func.get("cellular_process") and "pending" not in func.get("cellular_process", "").lower()
                            else ""
                        )
                    ),
                    biological_consequences=(
                        func.get("biological_consequence", [])
                        or (
                            [(_strip_placeholder(func.get("cellular_process", "")) or "")[:200]]
                            if func.get("cellular_process")
                               and "pending" not in func.get("cellular_process", "").lower()
                               and _strip_placeholder(func.get("cellular_process", ""))
                            else []
                        )
                    ),
                    specific_effects=func.get("specific_effects", []),
                    evidence=func.get("evidence", []),
                    pmids=func.get("pmids", []),
                    pathway_name=_clamp(pathway_name, 200),
                    pathway_id=_resolve_pw(pathway_name),
                    confidence=func.get("confidence"),
                    # Inherit from the parent interaction when the function
                    # dict lacks an explicit context, so claims never land
                    # with NULL function_context (which causes the
                    # function_context_drift verification check to flag
                    # the parent ↔ child mismatch).
                    function_context=fn_ctx,
                    context_data=func.get("_context"),
                    source_query=_clamp(discovered_in, 50),
                    discovery_method="pipeline_auto_generated" if func.get("_auto_generated") else "pipeline",
                    raw_function_data=func,
                )
                # H1: wrap the insert in a SAVEPOINT so a UniqueViolation
                # from a concurrent writer rolls back only this one claim
                # instead of blowing away every unrelated pending mutation
                # in the outer session.
                try:
                    with db.session.begin_nested():
                        db.session.add(claim)
                    _existing_by_key[dedup_key] = claim
                    claims_created += 1
                except IntegrityError:
                    # Duplicate hit the DB constraint (race with another
                    # writer). The savepoint already rolled back the INSERT
                    # attempt, but subsequent DB queries in this except
                    # block need their own savepoint — if the re-fetch or
                    # the evidence merge itself raises, the outer session
                    # is left in an aborted transaction state and the next
                    # flush will crash. Wrap the recovery path in its own
                    # nested savepoint so any follow-on error is contained.
                    try:
                        with db.session.begin_nested():
                            dup = (
                                InteractionClaim.query
                                .filter(InteractionClaim.interaction_id == interaction.id)
                                .filter(InteractionClaim.function_name == func_name)
                                .filter(db.func.coalesce(InteractionClaim.pathway_name, "") == (pathway_name or ""))
                                .filter(db.func.coalesce(InteractionClaim.function_context, "") == (fn_ctx or ""))
                                .first()
                            )
                            if dup:
                                from utils.json_helpers import _evidence_dedup_key
                                old_ev_keys = {_evidence_dedup_key(ev) for ev in (dup.evidence or [])}
                                for ev in func.get("evidence", []):
                                    if _evidence_dedup_key(ev) not in old_ev_keys:
                                        dup.evidence = list(dup.evidence or []) + [ev]
                                if not dup.direction or dup.direction not in ("main_to_primary", "primary_to_main"):
                                    dup.direction = _clamp(claim_direction, 30)
                                if not dup.arrow or dup.arrow not in CANONICAL_ARROW_SET:
                                    dup.arrow = _clamp(claim_arrow, 50)
                                dup.raw_function_data = func
                                dup.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                                _existing_by_key[dedup_key] = dup
                    except Exception as _recovery_exc:
                        print(
                            f"[DB SYNC] Savepoint recovery after "
                            f"IntegrityError failed for claim "
                            f"({interaction.id}, {func_name!r}): "
                            f"{type(_recovery_exc).__name__}: "
                            f"{_recovery_exc}. Session rolled back; "
                            "claim skipped.",
                            file=sys.stderr,
                        )

        # Clean up stale claims for functions that no longer exist.
        # H1: use the same COALESCE-normalised key as _existing_by_key so
        # stale-check membership tests can't be fooled by NULL/'' drift.
        # The fn_ctx derivation MUST match the creation loop above
        # (parent-fallback) — otherwise newly-inserted claims with
        # fn_ctx='direct' (inherited from interaction.function_context,
        # now NOT NULL) get keyed under '' here and look "stale" against
        # _existing_by_key entries keyed under 'direct'.
        if functions:
            current_fn_keys = set()
            for fn in functions:
                if isinstance(fn, dict) and fn.get("function"):
                    fn_name = (fn.get("function") or "").strip()
                    pw_raw = fn.get("pathway")
                    if isinstance(pw_raw, str) and pw_raw:
                        pw_name = pw_raw
                    elif isinstance(pw_raw, dict):
                        pw_name = pw_raw.get("canonical_name") or pw_raw.get("name")
                    else:
                        pw_name = data.get("step3_finalized_pathway")
                    fn_ctx = _clamp(
                        fn.get("function_context") or interaction.function_context,
                        20,
                    )
                    current_fn_keys.add(_claim_dedup_key(fn_name, pw_name, fn_ctx))
            for existing_key, existing_claim in _existing_by_key.items():
                if existing_key not in current_fn_keys:
                    # Only remove pipeline-generated claims, not manually curated ones
                    if existing_claim.discovery_method in ("pipeline", "pipeline_fallback", "pipeline_summary", "pipeline_auto_generated"):
                        db.session.delete(existing_claim)

        db.session.flush()
        return claims_created

    def sync_chain_relationships(
        self,
        query_protein: str,
        interactor_data: Dict[str, Any]
    ) -> Dict[str, int]:
        """
        Store chain relationships from ALL protein perspectives.

        For a chain like ATXN3 → VCP → LAMP2:
        - ATXN3-LAMP2: indirect (depth=2, mediator_chain=["VCP"])
        - VCP-LAMP2: direct (depth=1, mediator_chain=[])

        This ensures bidirectional queries work:
        - Query ATXN3 → sees LAMP2 as indirect via VCP
        - Query VCP → sees LAMP2 as direct
        - Query LAMP2 → sees VCP as direct, ATXN3 as indirect

        Args:
            query_protein: Main query protein symbol
            interactor_data: Interactor dict with chain metadata

        Returns:
            Stats: {
                "chain_links_created": int,
                "chain_links_updated": int
            }
        """
        stats = {
            "chain_links_created": 0,
            "chain_links_updated": 0
        }

        # Extract chain information
        target_protein = interactor_data.get("primary")
        interaction_type = interactor_data.get("interaction_type", "direct")
        upstream_interactor = interactor_data.get("upstream_interactor")
        mediator_chain = interactor_data.get("mediator_chain", [])

        if not target_protein:
            return stats

        # Case 1: Direct interaction (no chain)
        if interaction_type == "direct" and not mediator_chain:
            # Already handled by sync_query_results
            return stats

        # Case 2: Indirect interaction with chain
        # Example: ATXN3 → VCP → LAMP2
        # mediator_chain = ["VCP"]
        # We need to store:
        # 1. ATXN3-LAMP2 (indirect, depth=2)
        # 2. VCP-LAMP2 (direct, depth=1)

        if not mediator_chain or not isinstance(mediator_chain, list):
            # If no proper chain but marked as indirect, use upstream_interactor
            # as a single-element fallback (covers the common depth=2 case
            # where only the immediate upstream was recorded on the interactor
            # and the mediator_chain list was never populated).
            if interaction_type == "indirect" and upstream_interactor:
                mediator_chain = [upstream_interactor]
            else:
                return stats

        # Build full chain. ``chain_context.full_chain`` is the authoritative
        # source — it carries the LLM's intended biological direction (query
        # at head, middle, or tail depending on the cascade). We use it
        # verbatim and NEVER overwrite it with a query-at-head reconstruction,
        # which is what the previous code did when the explicit chain was
        # missing query or target. That overwrite inverted query-at-tail
        # chains (AKT1 → TSC2 → RHEB → MTOR → RPTOR → ULK1 became
        # ULK1 → TSC2 → RHEB → MTOR → RPTOR → AKT1) and caused the 2ax
        # enumerator and db_sync chain storage to disagree on hop direction,
        # which is how [CHAIN HOP CLAIM MISSING] floods happened.
        _existing_ctx = interactor_data.get("chain_context") or {}
        explicit_full_chain = None
        if isinstance(_existing_ctx, dict):
            _explicit = _existing_ctx.get("full_chain")
            if isinstance(_explicit, list) and len(_explicit) >= 2:
                explicit_full_chain = [str(p) for p in _explicit if p]

        if explicit_full_chain:
            full_chain = explicit_full_chain
            # If the LLM's chain doesn't cover both endpoints, warn but do
            # NOT override — the LLM's biology beats our endpoint-padding
            # guess. Downstream readers will see exactly what the LLM
            # emitted, matching what 2ax/2az enumerated.
            _upper_chain = {str(p).upper() for p in full_chain}
            _missing: List[str] = []
            if (query_protein or "").upper() not in _upper_chain:
                _missing.append(f"query={query_protein}")
            if (target_protein or "").upper() not in _upper_chain:
                _missing.append(f"target={target_protein}")
            if _missing:
                print(
                    f"[CHAIN] Using LLM-emitted chain as-is "
                    f"({' → '.join(full_chain)}) even though it lacks "
                    f"{', '.join(_missing)} — no query-at-head override.",
                    file=sys.stderr, flush=True,
                )
        else:
            # Last-resort reconstruction: no explicit full_chain AND no
            # chain_context. This happens only for legacy data written
            # before chain_context existed. The query-at-head assumption
            # is flagged so we can see when this path fires.
            full_chain = [query_protein] + list(mediator_chain) + [target_protein]
            print(
                f"[CHAIN RECONSTRUCT] Assumed query-at-head direction for "
                f"{query_protein}→...→{target_protein} (no chain_context on "
                f"interactor). If this fires on a fresh run, the pipeline "
                f"write path has drifted — chain_context should always be "
                f"populated for indirect interactors.",
                file=sys.stderr, flush=True,
            )

        # --- IndirectChain entity creation ---
        chain_pathway = interactor_data.get("_chain_pathway") or interactor_data.get("step3_finalized_pathway")
        if not chain_pathway:
            # Pick the dominant (most-common) pathway across all chain functions
            # instead of just functions[0]. A 4+ protein chain whose first link
            # has an atypical pathway would otherwise mis-tag the whole chain.
            from collections import Counter
            chain_funcs = interactor_data.get("functions", [])
            _pw_votes = Counter()
            for _fn in chain_funcs:
                if isinstance(_fn, dict):
                    _pw = _fn.get("pathway")
                    if isinstance(_pw, dict):
                        _pw = _pw.get("canonical_name") or _pw.get("name")
                    if isinstance(_pw, str) and _pw.strip():
                        _pw_votes[_pw.strip()] += 1
            if _pw_votes:
                chain_pathway = _pw_votes.most_common(1)[0][0]

        # Find parent indirect interaction (query<->target, already saved by sync_query_results)
        source_obj = Protein.query.filter_by(symbol=(query_protein or "").strip().upper()).first()
        target_obj = Protein.query.filter_by(symbol=(target_protein or "").strip().upper()).first()
        parent_interaction = None
        chain_record = None
        if source_obj and target_obj:
            canon_a = min(source_obj.id, target_obj.id)
            canon_b = max(source_obj.id, target_obj.id)
            parent_interaction = Interaction.query.filter_by(
                protein_a_id=canon_a, protein_b_id=canon_b
            ).first()

        if parent_interaction:
            # Compute the directional signature of this chain so we can
            # tell apart distinct cascades that share the same origin
            # interaction (ATXN3↔MTOR via VCP→RHEB vs via TSC2→TSC1 are
            # the SAME endpoints but DIFFERENT biology). Pre-#12 we only
            # filtered by origin_interaction_id — the second cascade got
            # silently merged into the first. Now the unique constraint
            # is (origin_interaction_id, chain_signature) and we look up
            # by both.
            from models import _compute_chain_signature
            new_chain_signature = _compute_chain_signature(full_chain)

            chain_record = IndirectChain.query.filter_by(
                origin_interaction_id=parent_interaction.id,
                chain_signature=new_chain_signature,
            ).first()
            # Layer 1 of CLAUDE_DOCS/11_CHAIN_TOPOLOGY.md gate: only
            # canonicalize chain direction on freshly-discovered chains.
            # Existing chains are left in their stored order ("going
            # forward only" — no retroactive backfill of inverted rows).
            chain_just_created = chain_record is None
            if not chain_record:
                # Detection pass: look for any existing IndirectChain whose
                # chain_proteins is a contiguous substring of the new one,
                # or vice versa. We only LOG the overlap.
                _detect_and_log_chain_overlaps(full_chain, query_protein)
                chain_record = IndirectChain(
                    chain_proteins=full_chain,
                    chain_signature=new_chain_signature,
                    origin_interaction_id=parent_interaction.id,
                    pathway_name=chain_pathway,
                    pathway_id=self._resolve_pw(chain_pathway) if chain_pathway else None,
                    chain_with_arrows=None,  # Will be populated later in the loop
                    discovered_in_query=query_protein,
                )
                db.session.add(chain_record)
                db.session.flush()

                # Merge any existing chains that are strict substrings of
                # this longer one. Runs in a savepoint so a failure here
                # can't corrupt the overall sync transaction.
                _merge_subset_chains_into(
                    chain_record, full_chain, query_protein,
                )
            else:
                # Same origin AND same signature → identical chain. Append
                # the current query to discovered_in_query so cross-query
                # provenance isn't lost.
                existing = [
                    s.strip()
                    for s in (chain_record.discovered_in_query or "").split(",")
                    if s.strip()
                ]
                if query_protein and query_protein not in existing:
                    existing.append(query_protein)
                    chain_record.discovered_in_query = ",".join(existing)

            # Membership: register the parent interaction as participating
            # in this chain. ``role='origin'`` because its (protein_a,
            # protein_b) pair is the chain's owner. Idempotent — UPSERT
            # via the SQLAlchemy session merge so re-syncs don't duplicate
            # rows. The composite primary key (chain_id, interaction_id)
            # enforces this at the DB level too.
            try:
                from models import ChainParticipant
                _existing_membership = ChainParticipant.query.filter_by(
                    chain_id=chain_record.id,
                    interaction_id=parent_interaction.id,
                ).first()
                if not _existing_membership:
                    db.session.add(ChainParticipant(
                        chain_id=chain_record.id,
                        interaction_id=parent_interaction.id,
                        role='origin',
                    ))
            except Exception as _mem_exc:
                # Membership write is non-critical — the legacy
                # ``Interaction.chain_id`` link still works as primary
                # pointer. Log so we can spot drift.
                print(
                    f"[CHAIN MEMBERSHIP] Failed to register origin "
                    f"participant for chain {chain_record.id}: {_mem_exc}",
                    file=sys.stderr,
                )

        # Pre-build arrow lookup map for all chain proteins (avoids N+1 queries)
        _chain_protein_objs = {p.symbol: p for p in Protein.query.filter(Protein.symbol.in_(full_chain)).all()}
        _chain_protein_ids = {sym: p.id for sym, p in _chain_protein_objs.items()}
        if len(_chain_protein_ids) >= 2:
            from sqlalchemy import or_, and_
            _all_ids = list(_chain_protein_ids.values())
            _chain_interactions = Interaction.query.filter(
                Interaction.protein_a_id.in_(_all_ids),
                Interaction.protein_b_id.in_(_all_ids),
            ).all()
            _arrow_map = {}
            for _ci in _chain_interactions:
                _arrow_map[(_ci.protein_a_id, _ci.protein_b_id)] = _ci
        else:
            _arrow_map = {}

        # Process each link in the chain
        chain_with_arrows = []
        # Pre-import the chain-resolution helpers once for the whole loop —
        # we now call ``canonical_pair_key`` and ``canonicalize_chain_link_functions``
        # *before* symbol validation so claims for an invalid-symbol hop still
        # get routed (rescued to parent or explicitly dropped) instead of
        # silently lost.
        from utils.chain_resolution import (
            canonical_pair_key as _canon_pair_key,
            canonicalize_chain_direction as _canon_chain_dir,
            canonicalize_chain_link_functions as _canon_clf,
        )
        from utils.claim_locus_router import route_chain_link_claims as _route_chain
        _chain_link_map = _canon_clf(
            interactor_data.get("chain_link_functions") or {}
        )
        for i in range(len(full_chain) - 1):
            source_symbol = full_chain[i]
            target_symbol = full_chain[i + 1]

            # === LOCUS ROUTER FIRST (before any symbol-validation drop) ===
            # Pull this hop's claims and route them to their natural locus
            # NOW. If the hop endpoints turn out to be invalid below, the
            # claims will already have been rerouted to parent-indirect or
            # explicitly dropped — they will not be silently lost.
            chain_link_funcs = _chain_link_map.get(
                _canon_pair_key(source_symbol, target_symbol), []
            )
            if chain_link_funcs:
                routing = _route_chain(
                    chain_link_funcs,
                    main_symbol=query_protein,
                    hop_src=source_symbol,
                    hop_tgt=target_symbol,
                    chain_proteins=list(full_chain),
                )
                if routing.rerouted:
                    # Merge into the parent indirect's net-effect ``functions``
                    # immediately so the rescue is locked in regardless of
                    # what happens to this hop below.
                    parent_funcs = list(interactor_data.get("functions") or [])
                    rerouted_keys = {
                        (fn.get("function"), fn.get("cellular_process") or "")
                        for fn in parent_funcs
                        if isinstance(fn, dict)
                    }
                    for fn in routing.rerouted:
                        key = (fn.get("function"), fn.get("cellular_process") or "")
                        if key in rerouted_keys:
                            continue
                        parent_funcs.append(fn)
                        rerouted_keys.add(key)
                    interactor_data["functions"] = parent_funcs
                if routing.dropped or routing.rerouted:
                    print(
                        f"[LOCUS ROUTER] {source_symbol}->{target_symbol} "
                        f"(chain {' → '.join(full_chain)}): "
                        f"kept={len(routing.kept)} "
                        f"rerouted={len(routing.rerouted)} "
                        f"dropped={len(routing.dropped)}",
                        file=sys.stderr,
                    )
                    for fn in routing.rerouted:
                        print(
                            f"  [REROUTED → parent] {fn.get('function', '?')} "
                            f"({fn.get('_router_reason')}, "
                            f"mentioned={fn.get('_router_mentioned')})",
                            file=sys.stderr,
                        )
                    for fn in routing.dropped:
                        print(
                            f"  [DROPPED]            {fn.get('function', '?')} "
                            f"({fn.get('_router_reason')})",
                            file=sys.stderr,
                        )
                chain_link_funcs = routing.kept

            # === NOW classify symbols (3-state). Claims for this hop are
            #     already rescued/dropped above, so a drop here is safe. ===
            _src_cls = classify_symbol(source_symbol)
            _tgt_cls = classify_symbol(target_symbol)
            if _src_cls == "invalid" or _tgt_cls == "invalid":
                _bad = source_symbol if _src_cls == "invalid" else target_symbol
                _kept_n = len(chain_link_funcs) if chain_link_funcs else 0
                print(
                    f"[DB SYNC] Hop dropped (invalid endpoint {_bad!r}); "
                    f"locus router already handled hop claims (kept={_kept_n} now stranded). "
                    f"Pair: {source_symbol}->{target_symbol}",
                    file=sys.stderr,
                )
                continue
            # Pseudo classifies as a valid mediator — proceed. The Protein
            # row will be created/flagged in ``_get_or_create_protein``.

            # Get or create proteins (pseudo entities now flagged automatically)
            source_protein = self._get_or_create_protein(source_symbol)
            target_protein_obj = self._get_or_create_protein(target_symbol)

            # CRITICAL FIX: Skip self-links (source == target)
            # This happens when upstream_interactor == query_protein
            # Example: VCP query with IκBα having upstream=VCP
            # Creates chain ["VCP", "VCP", "IκBα"] which has VCP→VCP self-link
            # This is VALID metadata (VCP regulates IκBα) but should not create VCP→VCP interaction
            if source_symbol == target_symbol:
                print(f"[DB SYNC] Skipping self-link in chain: {source_symbol}→{source_symbol} (valid upstream=query case)", file=sys.stderr)
                continue  # Skip to next link in chain

            # B1/S4e: The first-hop special branch that used to live here
            # (``if i == 0 and len(full_chain) > 2:``) is gone. Every hop
            # — including the first — now goes through the normal
            # link-creation path below. This fixes:
            #   - Wrong-row claim attribution (claims landed on whichever
            #     pre-existing pair matched the canonical key instead of
            #     the correct per-hop direct row).
            #   - chain_with_arrows skipping the first hop (the branch
            #     ``continue``d before the builder ran).
            # Parent-interaction tagging happens AFTER the loop (see
            # ``_tag_claims_with_chain(parent_interaction, ...)`` below).

            # Calculate depth: direct links (i → i+1) have depth=1
            link_depth = 1

            # Build chain context for this specific link
            link_data = interactor_data.copy()
            link_data["primary"] = target_symbol

            # Per-hop signature consumed by ``_save_claims`` to make
            # synthetic claim function_names distinct per hop direction.
            # Cyclic chains (e.g. ``A → B → C → B``) revisit a canonical
            # pair, and without this both visits would land identical
            # ``(interaction_id, function_name, function_context)`` rows
            # and trip ``uq_claim_fn_null_pw_ctx``. With it, each visit's
            # synthetic claim gets a distinct title (``[B->C] ...`` vs
            # ``[C->B] ...``) so each hop is its own modal — matching the
            # biology (different position = different mechanism).
            link_data["_hop_signature"] = f"{source_symbol}->{target_symbol}"

            # Ensure chain links inherit pathway metadata for PathwayInteraction creation
            if not link_data.get("pathways"):
                pw_name = link_data.get("step3_finalized_pathway")
                if pw_name:
                    link_data["pathways"] = [{"canonical_name": pw_name, "name": pw_name, "confidence": 0.8}]

            # Last-resort: if still empty, try to reuse any existing
            # InteractionClaim rows on the pair (irrespective of arrow
            # direction, since claims describe the pair's biology and we
            # don't care which side "originated" them). This covers the
            # bidirectional-arrow case where a chain link's data was
            # generated earlier as a direct-interaction claim and the
            # chain lookup key never matched because arrows were flipped.
            #
            # Hop-signature guard: stub claims created by the ``if not
            # functions`` branch above carry a ``[X->Y]`` prefix on
            # function_name. A claim with prefix ``[ATXN3->LIG3]`` must
            # NOT be rehydrated onto a ``XRCC1->LIG3`` chain link — that's
            # how wrong-pair claims leak across hops (user-reported bug).
            # Also reject the known-placeholder fragments entirely so stubs
            # from prior queries don't resurface as "reused" claims.
            # PostgreSQL is a first-class memory layer, not just a final
            # persistence target. Even when 2ax/2az ran in this session, a
            # valid prior pair claim is better than writing an empty hop and
            # relying on read-time parent fallback. Operators can restore the
            # old "fresh LLM claim required" behavior with
            # STRICT_CHAIN_CLAIM_ZERO_SKIP=true.
            _strict_zero_skip = (
                bool(getattr(self, "_chain_claim_phase_ran", False))
                and os.getenv("STRICT_CHAIN_CLAIM_ZERO_SKIP", "false").strip().lower()
                in ("1", "true", "yes", "on")
            )
            if not chain_link_funcs and not _strict_zero_skip:
                try:
                    from models import InteractionClaim as _Claim
                    canonical_a_id = min(source_protein.id, target_protein_obj.id)
                    canonical_b_id = max(source_protein.id, target_protein_obj.id)
                    # Use pre-built _arrow_map instead of per-link DB query
                    existing_pair = _arrow_map.get((canonical_a_id, canonical_b_id))
                    # Canonical hop signatures for this pair, used to keep
                    # rehydration scoped to the CURRENT edge's biology.
                    _hop_sig_forward = f"{source_symbol}->{target_symbol}"
                    _hop_sig_reverse = f"{target_symbol}->{source_symbol}"
                    _allowed_sigs = {_hop_sig_forward, _hop_sig_reverse}
                    if existing_pair:
                        existing_claims = _Claim.query.filter_by(
                            interaction_id=existing_pair.id,
                        ).all()
                        # Rehydrate claims back into function-dict shape so
                        # downstream code can treat them uniformly. Prefer
                        # direct-context claims (they describe the pair's
                        # biology); fall back to any context if that's all
                        # we have.
                        rehydrated = []
                        for c in existing_claims:
                            if not c.function_name:
                                continue
                            fn_lower = c.function_name.lower()
                            # Reject placeholder stubs from prior queries
                            # (these surface "Discovered via chain resolution"
                            # in the UI if rehydrated).
                            if _is_placeholder_text(c.function_name) or _is_placeholder_text(c.mechanism):
                                continue
                            # If function_name carries a [X->Y] hop prefix,
                            # require it to match THIS hop's pair. Prefix
                            # from a different hop means the claim is not
                            # about this edge's biology.
                            import re as _re
                            m = _re.match(r"^\[([^\]]+)\]", c.function_name)
                            if m:
                                claim_sig = m.group(1).strip()
                                if claim_sig not in _allowed_sigs:
                                    continue
                            # Resolve pathway name: prefer stored name, fall back to ID lookup
                            _pw_name = c.pathway_name
                            if not _pw_name and c.pathway_id:
                                _pw_obj = db.session.get(Pathway, c.pathway_id)
                                _pw_name = _pw_obj.name if _pw_obj else None
                            rehydrated.append({
                                "function": c.function_name,
                                "arrow": normalize_arrow(c.arrow, default="regulates"),
                                "cellular_process": c.mechanism or "",
                                "effect_description": c.effect_description or "",
                                "biological_consequence": c.biological_consequences or [],
                                "specific_effects": c.specific_effects or [],
                                "evidence": c.evidence or [],
                                "pmids": c.pmids or [],
                                "pathway": _pw_name,
                                "confidence": float(c.confidence) if c.confidence is not None else None,
                                "direction": semantic_claim_direction(c.direction),
                                "_rehydrated_from_existing_claim": True,
                                "_source_claim_id": c.id,
                            })
                        if rehydrated:
                            chain_link_funcs = rehydrated
                            if not hasattr(self, "_rehydrated_chain_pairs_logged"):
                                self._rehydrated_chain_pairs_logged = set()
                            _log_key = (canonical_a_id, canonical_b_id)
                            if _log_key not in self._rehydrated_chain_pairs_logged:
                                print(
                                    f"[DB SYNC] Reused {len(rehydrated)} existing "
                                    f"claim(s) for chain link {source_symbol}↔"
                                    f"{target_symbol} (hop-signature filtered) "
                                    f"— avoids 'Function data not generated' stub.",
                                    file=sys.stderr,
                                )
                                self._rehydrated_chain_pairs_logged.add(_log_key)
                except Exception as _reuse_exc:
                    # Rehydration is a best-effort optimization — if it
                    # fails for any reason, fall through to the existing
                    # stub-generation path so pipeline correctness isn't
                    # blocked on a lookup failure.
                    print(
                        f"[DB SYNC] Failed to rehydrate existing claims for "
                        f"{source_symbol}↔{target_symbol}: "
                        f"{type(_reuse_exc).__name__}: {_reuse_exc}",
                        file=sys.stderr,
                    )

            if chain_link_funcs:
                link_data["functions"] = chain_link_funcs
                link_data["function_context"] = "direct"
                link_data["_independent_chain_content"] = True
                # FORCE chain pathway on all functions AND retag each
                # function's function_context to 'direct'. The LLM
                # emits chain_link_functions with function_context=
                # 'chain_derived' (per FUNCTION_CONTEXT_LABELING in
                # shared_blocks.py) because they live inside
                # chain_link_functions. Downstream of that split,
                # though, each hop becomes a stand-alone direct
                # interaction row — see the schema notes that say
                # "'direct' downstream, tagged '_inferred_from_chain'".
                # Without retagging here the parent ends up labeled
                # 'direct' while every child claim is still
                # 'chain_derived' (or NULL), which is exactly the
                # function_context_drift the verification reports.
                for func in link_data["functions"]:
                    if isinstance(func, dict):
                        func["pathway"] = chain_pathway or func.get("pathway")
                        func["function_context"] = "direct"
                        func["_inferred_from_chain"] = True
            elif i > 0:
                # Atom E — pipeline contract violation.
                #
                # Under the zero-skip invariant, every hop of every
                # resolved chain is supposed to receive a fresh
                # LLM-generated claim from 2ax/2az (or, when genuine
                # pair biology is undocumented, an honest thin claim
                # with ``_thin_claim=true``). Hitting this branch
                # means the claim-generation phase silently omitted
                # this hop — a bug the pipeline must surface, not
                # paper over.
                #
                # We still write ``functions=[]`` so the row is
                # valid, and tag ``_chain_hop_claim_missing`` so QC
                # reports can enumerate violations. The read-time
                # parent-fallback in services/data_builder.py remains
                # as a final safety net, but should essentially
                # never fire once Atom E is fully deployed.
                link_data["functions"] = []
                link_data["_chain_link_missing_functions"] = True
                link_data["_chain_hop_claim_missing"] = True
                _level = (
                    "ERROR" if getattr(self, "_chain_claim_phase_ran", False)
                    else "INFO"
                )
                print(
                    f"[CHAIN HOP CLAIM MISSING] ({_level}) "
                    f"{source_symbol}->{target_symbol} in "
                    f"{' → '.join(full_chain)} (length={len(full_chain)}): "
                    "no LLM functions + no DB rehydration. "
                    + (
                        "Pipeline contract violated — 2ax/2az ran but "
                        "skipped this hop. Read-time FILTERED parent-"
                        "fallback will paper over in the UI."
                        if _level == "ERROR"
                        else "2ax/2az did not run in this session; read-"
                        "time FILTERED parent-fallback will surface "
                        "query-free parent functions if any exist."
                    ),
                    file=sys.stderr,
                )

            # Set correct interaction_type for this link
            if i == 0 and len(full_chain) > 2:
                # First link of multi-hop chain: could be direct or indirect
                # Keep original type
                pass
            elif i > 0:
                # Intermediate and final links in chain: always direct
                link_data["interaction_type"] = "direct"

            # Apply chain state via the single ChainView write surface so
            # the four legacy fields (mediator_chain, upstream_interactor,
            # depth, chain_context) are always derived from one source.
            # ``link_data`` is an in-memory dict at this point — db_sync
            # writes it onto the Interaction row farther down via
            # ``_save_interaction``. The ChainView constructed from
            # ``full_chain`` carries every field downstream readers need;
            # we just augment ``chain_context`` with the per-link
            # ``link_position`` so the row knows which hop it represents.
            from utils.chain_view import ChainView
            chain_view_for_link = ChainView.from_full_chain(
                full_chain, query_protein=query_protein,
            )
            chain_view_for_link.apply_to_dict(link_data)
            # Augment with link_position (the single-write helper doesn't
            # know which hop in the chain this is — that's a per-link
            # detail, not part of the chain itself).
            if isinstance(link_data.get("chain_context"), dict):
                link_data["chain_context"]["link_position"] = i

            # A5 — chain_with_arrows consistency. Prefer the hop's own
            # per-hop function arrows (what the modal actually renders) over
            # the interaction-level _arrow_map. Only fall back to the map
            # when a hop has no functions, and log any disagreement.
            chain_with_arrows = []
            for j in range(len(full_chain) - 1):
                from_sym = full_chain[j]
                to_sym = full_chain[j + 1]

                # Map-derived arrow (legacy source)
                from_id = _chain_protein_ids.get(from_sym)
                to_id = _chain_protein_ids.get(to_sym)
                map_arrow = 'binds'
                if from_id and to_id:
                    a_id, b_id = min(from_id, to_id), max(from_id, to_id)
                    ci = _arrow_map.get((a_id, b_id))
                    if ci:
                        if ci.arrows:
                            if from_id < to_id:
                                map_arrow = normalize_arrow((ci.arrows.get('a_to_b') or ['binds'])[0], default="binds")
                            else:
                                map_arrow = normalize_arrow((ci.arrows.get('b_to_a') or ['binds'])[0], default="binds")
                        else:
                            map_arrow = normalize_arrow(ci.arrow, default="binds")

                # Hop-derived arrow: pull from the router-kept functions on
                # THIS hop if this is the current segment we're writing.
                hop_arrow = None
                if (from_sym, to_sym) == (source_symbol, target_symbol):
                    hop_fns = link_data.get("functions") or []
                    hop_arrows = [
                        normalize_arrow(fn.get("arrow"), default="binds")
                        for fn in hop_fns
                        if isinstance(fn, dict) and fn.get("arrow")
                    ]
                    hop_arrows = [a for a in hop_arrows if a]
                    if hop_arrows:
                        # Dominant arrow across the hop's kept functions
                        from collections import Counter
                        hop_arrow = Counter(hop_arrows).most_common(1)[0][0]

                # [CHAIN ARROW DRIFT] log suppressed per user request
                # (2026-04-21). Behavior unchanged — hop function arrow
                # still wins over the interaction-level map arrow; we
                # just don't print the disagreement any more.
                arrow_type = normalize_arrow(hop_arrow or map_arrow, default="binds")

                chain_with_arrows.append({
                    "from": from_sym,
                    "to": to_sym,
                    "arrow": arrow_type,
                })

            link_data["chain_with_arrows"] = chain_with_arrows

            # Save interaction
            created = self._save_interaction(
                protein_a=source_protein,
                protein_b=target_protein_obj,
                data=link_data,
                discovered_in=query_protein
            )

            if created:
                stats["chain_links_created"] += 1
            else:
                stats["chain_links_updated"] += 1

            # Tag chain link claims with chain_id
            if chain_record:
                link_interaction = Interaction.query.filter_by(
                    protein_a_id=min(source_protein.id, target_protein_obj.id),
                    protein_b_id=max(source_protein.id, target_protein_obj.id)
                ).first()
                if link_interaction:
                    self._tag_claims_with_chain(link_interaction, chain_record)

        # Update chain_record with arrows now that they're computed.
        # Layer 1 of CLAUDE_DOCS/11_CHAIN_TOPOLOGY.md: when this chain
        # was just created in THIS sync call AND its arrows are
        # dominated by reverse-direction verbs (is_substrate_of,
        # is_phosphorylated_by, …), reverse BOTH the protein order AND
        # the arrow list so chain[i]→chain[i+1] is biological cause →
        # effect. Existing rows are left alone — "going forward only."
        if chain_record and chain_with_arrows:
            if chain_just_created:
                canonical_proteins, canonical_arrows, was_reversed = _canon_chain_dir(
                    chain_record.chain_proteins or full_chain,
                    chain_with_arrows,
                )
                if was_reversed:
                    chain_record.set_chain_proteins(canonical_proteins)
                    chain_record.chain_with_arrows = canonical_arrows
                    print(
                        f"[CHAIN CANONICAL] Reversed new chain "
                        f"{chain_record.id} from query-centric to causal "
                        f"direction: {' → '.join(canonical_proteins)}",
                        file=sys.stderr,
                    )
                else:
                    chain_record.chain_with_arrows = chain_with_arrows
            else:
                chain_record.chain_with_arrows = chain_with_arrows

        # B1/S4e: Tag the PARENT (net-effect) interaction with the chain_id.
        # Previously this was buried inside the deleted first-hop branch;
        # now it runs unconditionally after the loop so the parent is always
        # tagged regardless of which hops were processed.
        if parent_interaction and chain_record:
            self._tag_claims_with_chain(parent_interaction, chain_record)

        # Chain pathway unification is NOT done here. At this point in the
        # flow, ``step3_finalized_pathway`` has not yet been assigned
        # (that happens later in ``scripts/pathway_v2/quick_assign.py``),
        # so any "unify chain claims to the chain pathway" pass running
        # now would almost always short-circuit on a missing pathway. The
        # authoritative unification lives in ``quick_assign``'s
        # ``_unify_all_chain_claims`` pass, which runs *after* every
        # pathway assignment is final and scopes to ALL chain claims
        # (not just unassigned). See the plan note in
        # ``docs/SESSION_HANDOFF_*`` for the ordering rationale.

        return stats

    def _tag_claims_with_chain(self, interaction, chain_record, *, role: str = 'hop'):
        """Tag an interaction and all its claims with the chain_id, AND
        register a ChainParticipant membership row for the M2M view.

        Pre-#12, ``Interaction.chain_id`` was a 1:1 FK so an interaction
        could only belong to ONE chain. With #12 it's still kept as a
        denormalized "primary chain" pointer for backward compat, but
        the canonical M2M relationship now lives in the
        ``chain_participants`` table — letting the same interaction
        participate in multiple distinct cascades.

        The ``role`` parameter says HOW this interaction participates:
          * ``'hop'``        — single mediator-pair edge (default)
          * ``'origin'``     — owns the chain (matches origin_interaction_id)
          * ``'net_effect'`` — query→target indirect summary row
        """
        from models import InteractionClaim, ChainParticipant
        if interaction is None or chain_record is None:
            return

        # 1. Denormalized "primary chain" pointer (#6 refactor). Pre-#12
        #    this was the single source of truth; now it's a fast-path
        #    cache for readers that only want the most recent chain.
        if interaction.chain_id != chain_record.id:
            interaction.chain_id = chain_record.id

        # 2. M2M participant row (#12). Allows the interaction to
        #    belong to multiple chains over its lifetime. Idempotent —
        #    composite PK (chain_id, interaction_id) blocks duplicates.
        try:
            existing = ChainParticipant.query.filter_by(
                chain_id=chain_record.id,
                interaction_id=interaction.id,
            ).first()
            if existing:
                # Promote role if a stronger label arrives later
                # (e.g., a hop is later confirmed as the origin).
                _ROLE_RANK = {'hop': 0, 'net_effect': 1, 'origin': 2}
                if _ROLE_RANK.get(role, 0) > _ROLE_RANK.get(existing.role, 0):
                    existing.role = role
            else:
                db.session.add(ChainParticipant(
                    chain_id=chain_record.id,
                    interaction_id=interaction.id,
                    role=role,
                ))
        except Exception as _mem_exc:
            print(
                f"[CHAIN MEMBERSHIP] Failed to register {role} participant "
                f"for chain {chain_record.id} interaction "
                f"{interaction.id}: {_mem_exc}",
                file=sys.stderr,
            )

        # 3. Tag every claim on the interaction (the pre-existing
        #    per-claim link, still used for pathway consistency passes).
        claims = InteractionClaim.query.filter_by(interaction_id=interaction.id).all()
        for claim in claims:
            if not claim.chain_id:
                claim.chain_id = chain_record.id
        db.session.flush()


def backfill_missing_claims():
    """Backfill InteractionClaim rows for interactions that have none.

    Finds all interactions with zero claims and runs _save_claims()
    to generate synthetic claims from interaction-level data.
    Safe to run multiple times (idempotent).
    """
    from models import InteractionClaim
    from sqlalchemy import text

    syncer = DatabaseSyncLayer()

    # Find interactions missing claims via LEFT JOIN
    rows = db.session.execute(text("""
        SELECT i.id FROM interactions i
        LEFT JOIN interaction_claims ic ON i.id = ic.interaction_id
        WHERE ic.id IS NULL
    """)).fetchall()

    orphan_ids = [row[0] for row in rows]
    if not orphan_ids:
        print(f"[BACKFILL] All interactions have claims — nothing to do.")
        return {"backfilled": 0, "total_checked": 0}

    print(f"[BACKFILL] Found {len(orphan_ids)} interactions missing claims")

    backfilled = 0
    for interaction in Interaction.query.filter(Interaction.id.in_(orphan_ids)).all():
        data = interaction.data or {}
        discovered_in = interaction.discovered_in_query or "unknown"
        count = syncer._save_claims(interaction, data, discovered_in)
        if count > 0:
            backfilled += 1

    db.session.commit()
    print(f"[BACKFILL] Created claims for {backfilled}/{len(orphan_ids)} interactions")
    return {"backfilled": backfilled, "total_checked": len(orphan_ids)}
