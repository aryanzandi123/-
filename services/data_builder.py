"""Data builder functions for reconstructing JSON from PostgreSQL."""

import os
import re
import sys

from sqlalchemy.orm import joinedload
from models import (
    db,
    Protein,
    Interaction,
    InteractionClaim,
    Pathway,
    PathwayParent,
    PathwayInteraction,
    IndirectChain,
    ChainParticipant,
)
from services.state import arrow_to_effect
from utils.chain_resolution import is_valid_chain_protein_symbol
from utils.interaction_contract import (
    normalize_arrow,
    normalize_arrows_map,
    normalize_chain_arrows,
    semantic_claim_direction,
)

# Snapshot/ctx contract version. Frontend (`react-app/src/app/main.tsx`)
# reads `_schema_version` and console.warns when it differs from its own
# EXPECTED_SCHEMA_VERSION. Bump whenever the API payload shape changes in a
# way the frontend would care about.
SCHEMA_VERSION = "2026-05-07"

# Compiled regex for detecting direct-evidence keywords in support summaries.
# Optional negation prefix captures "no"/"not"/"lack of"/"without"/"absent"/"failed to".
# Group 1 = negation (or empty), Group 2 = the evidence keyword.
_DIRECT_EVIDENCE_RE = re.compile(
    r'(?:(?P<neg>no\s+|not\s+|lack\s+of\s+|without\s+|absent\s+|failed\s+to\s+))?'
    r'(?P<kw>co-ip|co-immunoprecipitat\w*|spr(?=[\s,])|surface\s+plasmon'
    r'|y2h|yeast\s+two-hybrid|direct\s+interaction|physical\s+interaction'
    r'|direct\s+binding|physically\s+interacts|pull-?down'
    r'|fret(?=[\s,])|cross-?link\w*|bimolecular\s+fluorescence)',
    re.IGNORECASE,
)


def _mentions_protein(text, symbol: str) -> bool:
    """Return True if ``text`` contains a whole-word mention of ``symbol``.

    Case-insensitive, word-boundary match to avoid false positives on
    substrings (e.g. "ATM" shouldn't match inside "ATMosphere"). Handles
    str, list, and dict-like text fields; nested lists are flattened.
    """
    if not symbol or not text:
        return False
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", re.IGNORECASE)
    if isinstance(text, str):
        return bool(pattern.search(text))
    if isinstance(text, list):
        return any(_mentions_protein(item, symbol) for item in text)
    if isinstance(text, dict):
        return any(_mentions_protein(v, symbol) for v in text.values())
    return False


def _filter_query_mentions(functions, main_symbol: str, src_sym: str, tgt_sym: str):
    """Drop per-hop functions whose text mentions the query protein.

    The per-hop slot (src_sym↔tgt_sym) must describe that pair's biology in
    isolation. If the LLM wrote text mentioning the query (ignoring its own
    prompt rule), that function actually belongs on the parent indirect
    interaction, not here — so we drop it and let the parent row carry it.

    If ``main_symbol`` appears in the hop pair itself (query is one endpoint
    of the hop), we skip the filter — the query is supposed to be mentioned.

    Returns ``(kept_functions, dropped_count)``.
    """
    if not functions or not main_symbol:
        return functions or [], 0
    main_upper = main_symbol.upper()
    if src_sym and src_sym.upper() == main_upper:
        return functions, 0
    if tgt_sym and tgt_sym.upper() == main_upper:
        return functions, 0

    kept = []
    dropped = 0
    text_fields = (
        "cellular_process",
        "effect_description",
        "biological_consequence",
        "specific_effects",
        "function",
    )
    for fn in functions:
        if not isinstance(fn, dict):
            kept.append(fn)
            continue
        mentions = any(_mentions_protein(fn.get(field), main_symbol) for field in text_fields)
        if mentions:
            dropped += 1
            continue
        kept.append(fn)
    return kept, dropped


def _normalize_function_payloads(functions):
    """Normalize per-function arrow/direction fields before the UI sees them."""

    if not isinstance(functions, list):
        return functions
    for func in functions:
        if not isinstance(func, dict):
            continue
        if func.get("arrow"):
            func["arrow"] = normalize_arrow(func.get("arrow"), default="regulates")
        if func.get("interaction_effect"):
            effect = str(func.get("interaction_effect") or "").strip().lower()
            if effect in {"activates", "inhibits", "binds", "regulates", "complex", "modulates"}:
                func["interaction_effect"] = normalize_arrow(effect, default="regulates")
        direction = func.get("interaction_direction") or func.get("direction")
        if direction:
            semantic = semantic_claim_direction(direction)
            func["interaction_direction"] = semantic
            func["direction"] = semantic
    return functions


def _chain_summaries_for_item(item: dict) -> list[dict]:
    summaries: list[dict] = []
    seen: set[int] = set()
    for summary in item.get("all_chains") or []:
        if not isinstance(summary, dict):
            continue
        chain_id = summary.get("chain_id")
        if isinstance(chain_id, int):
            seen.add(chain_id)
        summaries.append(summary)
    entity = item.get("_chain_entity")
    if isinstance(entity, dict):
        chain_id = entity.get("id") or entity.get("chain_id")
        if not isinstance(chain_id, int) or chain_id not in seen:
            summaries.append({
                "chain_id": chain_id,
                "chain_proteins": entity.get("chain_proteins"),
                "pathway_name": entity.get("pathway_name"),
            })
    return summaries


def _primary_chain_summary(item: dict) -> dict | None:
    summaries = _chain_summaries_for_item(item)
    chain_id = item.get("chain_id")
    if isinstance(chain_id, int):
        for summary in summaries:
            if summary.get("chain_id") == chain_id:
                return summary
    return summaries[0] if summaries else None


def _chain_members_for_item(item: dict) -> list[str]:
    summary = _primary_chain_summary(item)
    if isinstance(summary, dict):
        members = summary.get("chain_proteins")
        if isinstance(members, list):
            return [str(p) for p in members if p]
    entity = item.get("_chain_entity")
    if isinstance(entity, dict):
        members = entity.get("chain_proteins")
        if isinstance(members, list):
            return [str(p) for p in members if p]
    context = item.get("chain_context")
    if isinstance(context, dict):
        members = context.get("full_chain")
        if isinstance(members, list):
            return [str(p) for p in members if p]
    return []


def _chain_context_pathway_for_item(item: dict) -> str | None:
    summary = _primary_chain_summary(item)
    if isinstance(summary, dict):
        pathway = summary.get("pathway_name") or summary.get("pathway")
        if pathway:
            return str(pathway)
    entity = item.get("_chain_entity")
    if isinstance(entity, dict) and entity.get("pathway_name"):
        return str(entity["pathway_name"])
    pathway = item.get("step3_finalized_pathway")
    return str(pathway) if pathway else None


def _hop_index_for_item(item: dict) -> int | None:
    for key in ("hop_index", "_chain_position"):
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    context = item.get("chain_context")
    if isinstance(context, dict):
        value = context.get("link_position")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    source = str(item.get("source") or "").upper()
    target = str(item.get("target") or "").upper()
    if not source or not target:
        return None
    members = [str(member).upper() for member in _chain_members_for_item(item)]
    for idx, left in enumerate(members[:-1]):
        right = members[idx + 1]
        if {left, right} == {source, target}:
            return idx
    return None


def _hop_local_pathway_for_item(item: dict) -> str | None:
    for claim in item.get("claims") or []:
        if isinstance(claim, dict) and claim.get("pathway_name"):
            return str(claim["pathway_name"])
    for function in item.get("functions") or []:
        if isinstance(function, dict) and function.get("pathway"):
            return str(function["pathway"])
    pathway = item.get("step3_finalized_pathway")
    return str(pathway) if pathway else None


def _mediators_for_net_effect(item: dict, chain_members: list[str]) -> list[str]:
    if chain_members:
        source = str(item.get("source") or "")
        target = str(item.get("target") or "")
        lower_members = [member.lower() for member in chain_members]
        try:
            source_idx = lower_members.index(source.lower())
            target_idx = lower_members.index(target.lower())
        except ValueError:
            source_idx = target_idx = -1
        if source_idx >= 0 and target_idx >= 0:
            if source_idx < target_idx:
                return chain_members[source_idx + 1:target_idx]
            if target_idx < source_idx:
                return list(reversed(chain_members[target_idx + 1:source_idx]))
    context = item.get("chain_context")
    if isinstance(context, dict) and isinstance(context.get("mediator_chain"), list):
        return [str(p) for p in context["mediator_chain"] if p]
    return []


def _has_chain_hop_membership(item: dict) -> bool:
    if item.get("_is_chain_link"):
        return True
    for summary in item.get("all_chains") or []:
        if isinstance(summary, dict) and str(summary.get("role") or "").lower() == "hop":
            return True
    return False


def _apply_claim_contract_fields(item: dict) -> None:
    claims = item.get("claims")
    if not isinstance(claims, list):
        return
    item_locus = item.get("locus")
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_context = str(claim.get("function_context") or "").lower()
        if claim_context == "net" or item_locus == "net_effect_claim":
            claim["locus"] = "net_effect_claim"
        elif item_locus == "chain_hop_claim":
            claim["locus"] = "chain_hop_claim"
        else:
            claim["locus"] = "direct_claim"
        if claim.get("chain_id") is None and item.get("chain_id") is not None:
            claim["chain_id"] = item.get("chain_id")
        claim.setdefault("source", item.get("source"))
        claim.setdefault("target", item.get("target"))
        if item.get("hop_index") is not None:
            claim.setdefault("hop_index", item.get("hop_index"))


def _coerce_int_id(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _claim_chain_scope_for_item(item: dict) -> set[int]:
    """Return chain ids whose claims belong on this emitted interaction row."""
    if not isinstance(item, dict):
        return set()

    # If this payload row has a scalar chain_id, that is the visible row scope.
    # Sibling chains are emitted as their own rows. Letting all_chains widen a
    # scalar row leaks claims into aggregate node modals for the wrong terminal.
    chain_id = _coerce_int_id(item.get("chain_id"))
    if chain_id is not None:
        return {chain_id}

    function_context = str(item.get("function_context") or "").lower()
    role_filter = None
    if _has_chain_hop_membership(item):
        role_filter = "hop"
    elif function_context == "net" or item.get("_net_effect") is True:
        role_filter = "net_effect"

    # Legacy fallback for rows that have only the multi-chain shape.
    if role_filter and not item.get("_parent_chain"):
        scoped = {
            cid
            for summary in (item.get("all_chains") or [])
            if isinstance(summary, dict)
            and str(summary.get("role") or "").lower() == role_filter
            for cid in [_coerce_int_id(summary.get("chain_id"))]
            if cid is not None
        }
        if scoped:
            return scoped

    return set()


def _claims_scoped_to_item(claims, item: dict):
    """Filter InteractionClaim rows so chain-specific payload rows keep their own evidence."""
    scope = _claim_chain_scope_for_item(item)
    if not scope:
        return list(claims or [])

    chain_specific = [
        claim for claim in (claims or [])
        if _coerce_int_id(getattr(claim, "chain_id", None)) in scope
    ]
    if chain_specific:
        return chain_specific

    # Legacy fallback: older rows may have hop claims without chain_id. Keep
    # only unscoped claims when no scoped claims exist for this emitted row.
    return [
        claim for claim in (claims or [])
        if _coerce_int_id(getattr(claim, "chain_id", None)) is None
    ]


def _apply_contract_fields(item: dict, query_symbol: str | None = None) -> None:
    function_context = str(item.get("function_context") or "").lower()
    is_chain_hop = _has_chain_hop_membership(item)
    is_net_effect = (
        function_context == "net"
        or item.get("_net_effect") is True
        or item.get("_display_badge") == "NET EFFECT"
    )

    if is_net_effect:
        locus = "net_effect_claim"
        item["type"] = "indirect"
        item["interaction_type"] = "indirect"
    elif is_chain_hop:
        locus = "chain_hop_claim"
        item["_is_chain_link"] = True
    else:
        locus = "direct_claim"

    chain_members = _chain_members_for_item(item)
    mediators = _mediators_for_net_effect(item, chain_members) if is_net_effect else []

    item["locus"] = locus
    item["is_net_effect"] = bool(is_net_effect)
    item["chain_members"] = chain_members or None
    item["chain_context_pathway"] = _chain_context_pathway_for_item(item)
    item["hop_index"] = _hop_index_for_item(item) if is_chain_hop else None
    item["hop_local_pathway"] = _hop_local_pathway_for_item(item) if is_chain_hop else None
    item["via"] = mediators
    item["mediators"] = mediators
    item["source"] = str(item.get("source") or query_symbol or "")
    item["target"] = str(item.get("target") or item.get("protein_b") or item.get("protein_a") or "")
    _apply_claim_contract_fields(item)


def _build_chain_membership_index(interactions) -> dict:
    """Batch-load ChainParticipant rows for interactions.

    ``Interaction.chain_memberships`` is a dynamic relationship, so iterating
    it per interaction issues one SELECT per row. The PERK snapshot has enough
    chain-bearing rows that this alone dominates route latency over the remote
    Postgres connection.
    """

    interaction_ids = [ix.id for ix in interactions or [] if getattr(ix, "id", None)]
    if not interaction_ids:
        return {}
    rows = (
        ChainParticipant.query
        .options(joinedload(ChainParticipant.chain))
        .filter(ChainParticipant.interaction_id.in_(interaction_ids))
        .all()
    )
    by_interaction: dict = {}
    for row in rows:
        by_interaction.setdefault(row.interaction_id, []).append(row)
    return by_interaction


def _memberships_for(interaction, chain_memberships_index: dict | None = None):
    if chain_memberships_index is not None:
        return chain_memberships_index.get(interaction.id, [])
    try:
        return list(interaction.chain_memberships)
    except Exception:
        return []


def _chain_fields_for(
    interaction,
    chain_pathways_index: dict | None = None,
    chain_memberships_index: dict | None = None,
) -> dict:
    """Return the canonical chain payload fields for an Interaction row.

    This is the one production code path that reads chain state off an
    Interaction. Every payload builder routes through here so the four
    legacy fields (``mediator_chain``, ``upstream_interactor``, ``depth``,
    ``chain_context``) never drift apart: they're all derived from one
    ``ChainView``, which itself prefers the linked ``IndirectChain`` row
    (via ``chain_id``) and falls back to the JSONB blob for rows written
    before refactor #6.

    ``chain_id`` is always emitted (``None`` for direct interactions) so
    the frontend can decide whether to call ``GET /api/chain/<id>``. The
    chain-shape fields are only emitted when the view is non-empty; for
    direct rows the caller's ``dict.update`` is a no-op for them, which
    preserves anything the pipeline may have tucked into the JSONB blob.

    ``chain_pathways_index`` (optional): a precomputed
    ``Map[chain_id -> Set[pathway_name]]`` to avoid the per-row N+1
    InteractionClaim query. ``build_full_json_from_db`` hoists one query
    for the whole snapshot and passes the index in. When not provided we
    fall back to the per-row query so direct callers (e.g. ``build_protein_detail_json``)
    still work without coordination.
    """
    result = {"chain_id": interaction.chain_id}
    view = interaction.chain_view
    if not view.is_empty:
        result["mediator_chain"] = view.mediator_chain
        result["upstream_interactor"] = view.upstream_interactor
        result["depth"] = view.depth
        result["chain_context"] = view.to_chain_context()

    # #12: surface ALL chains this interaction participates in.
    # Pre-#12 the schema enforced one chain per origin, so a pair like
    # ATXN3↔MTOR could only show one of {via VCP→RHEB, via TSC2→TSC1}.
    # With ChainParticipant in place, we emit ``chain_ids`` (an array)
    # plus a parallel ``all_chains`` list so the modal can render
    # multiple cascade banners. The legacy ``chain_id`` stays as the
    # "primary" pointer (== chain_memberships[0].chain_id) for readers
    # that don't yet handle the multi-chain shape.
    try:
        memberships = _memberships_for(interaction, chain_memberships_index)
        if memberships:
            chain_ids = [m.chain_id for m in memberships]
            # Layer 2 of CLAUDE_DOCS/11_CHAIN_TOPOLOGY.md: collect every
            # distinct pathway any chain-derived claim landed in, per
            # chain. Frontend gate widens on these so HDAC6's chain
            # appears under HDAC6's direct-claim pathway when at least
            # one of the chain's claims also landed there — fixes the
            # silent-drop case where ``chain.pathway_name`` points
            # elsewhere even though the chain has biology in this
            # pathway.
            per_chain_pathways: dict = {}
            if chain_ids:
                if chain_pathways_index is not None:
                    # Hoisted path: caller precomputed the index across the
                    # full snapshot in ONE query. Just look up.
                    for cid in chain_ids:
                        pws = chain_pathways_index.get(cid)
                        if pws:
                            per_chain_pathways[cid] = pws
                else:
                    rows = (
                        InteractionClaim.query
                        .filter(InteractionClaim.chain_id.in_(chain_ids))
                        .with_entities(InteractionClaim.chain_id, InteractionClaim.pathway_name)
                        .distinct()
                        .all()
                    )
                    for cid, pw in rows:
                        if cid is None or not pw:
                            continue
                        per_chain_pathways.setdefault(cid, set()).add(pw)

            chain_summaries = []
            for m in memberships:
                ch = m.chain
                if not ch:
                    continue
                summary = {
                    "chain_id": ch.id,
                    "role": m.role,
                    "chain_proteins": list(ch.chain_proteins or []),
                    "chain_with_arrows": normalize_chain_arrows(ch.chain_with_arrows),
                    "pathway_name": ch.pathway_name,
                    "discovered_in_query": ch.discovered_in_query,
                }
                pathways_for_chain = per_chain_pathways.get(ch.id)
                if pathways_for_chain:
                    summary["chain_pathways"] = sorted(pathways_for_chain)
                chain_summaries.append(summary)

            if chain_summaries:
                result["chain_ids"] = chain_ids
                result["all_chains"] = chain_summaries
                # Promote primary chain_id from membership when the
                # legacy ``Interaction.chain_id`` cache is stale or
                # missing — readers should never see a populated
                # ``all_chains`` with a NULL ``chain_id``.
                if not result.get("chain_id"):
                    result["chain_id"] = chain_ids[0]

                # Top-level union — used by the parent gate in
                # static/card_view.js:groupChainsByChainId so chains
                # whose `chain.pathway_name` doesn't match the expanded
                # pathway still pass the parent filter when ANY claim
                # in any of their chains landed in this pathway.
                union_pathways = set()
                for s in chain_summaries:
                    for pw in s.get("chain_pathways", []) or []:
                        union_pathways.add(pw)
                if union_pathways:
                    result["chain_pathways"] = sorted(union_pathways)
    except Exception:
        # Membership read is best-effort; never break the response over it.
        pass

    return result


def _reconstruct_chain_links(
    main_protein_id,
    db_interactions,
    interactions_list,
    protein_set,
    interactor_proteins,
    chain_memberships_index: dict | None = None,
):
    """Extract chain link interactions for indirect mediator chains.

    Collects mediator symbols, batch-queries mediator proteins and their
    interactions, builds chain link data dicts (appended to *interactions_list*),
    and updates *protein_set* / *interactor_proteins* in-place.

    Returns ``(all_chain_links, chain_link_ids, synthesized_chain_keys)`` so
    callers can feed chain-link DB objects into pathway and shared-link queries.
    """
    chain_link_ids = set()
    all_chain_links = []

    # BATCH: Collect every valid symbol in each chain, then query once.
    # The terminal protein is required for final-hop lookup. Loading only
    # mediators forces terminal hops like GLE1->DDX3X to be synthesized from
    # parent JSONB even when a real DB row exists.
    all_mediator_symbols = set()
    all_chain_symbols = set()
    for interaction in db_interactions:
        if interaction.interaction_type != "indirect":
            continue
        view = interaction.chain_view
        if not view.is_empty:
            all_mediator_symbols.update(view.mediator_chain)
            all_chain_symbols.update(
                symbol for symbol in view.full_chain
                if is_valid_chain_protein_symbol(symbol)
            )

    # Single batch query for all proteins that can participate in chain hops.
    # Chain text preserves LLM/source casing (for example C9orf72), while the
    # Protein row may use canonical gene casing (C9ORF72).
    mediator_protein_map = {}
    mediator_protein_map_ci = {}
    if all_chain_symbols:
        _chain_symbol_keys = {str(symbol).upper() for symbol in all_chain_symbols if symbol}
        mediator_proteins_list = Protein.query.filter(
            db.func.upper(Protein.symbol).in_(_chain_symbol_keys)
        ).all()
        mediator_protein_map = {p.symbol: p for p in mediator_proteins_list}
        mediator_protein_map_ci = {p.symbol.upper(): p for p in mediator_proteins_list}

    # Audit: any mediator declared in chain metadata but not present as a
    # Protein row is a structural hole — most commonly a pseudo-entity that
    # was rejected by the pre-L1 db_sync logic. After L1 (pseudo-protein
    # storage), this set should be empty for newly-synced queries; for
    # historically-synced queries, ``scripts/audit_chain_completeness.py
    # --repair`` re-runs db_sync from the file cache to backfill them.
    _resolved_symbol_keys = set(mediator_protein_map_ci.keys())
    _unresolved_mediators = sorted(
        symbol for symbol in all_mediator_symbols
        if str(symbol).upper() not in _resolved_symbol_keys
    )
    if _unresolved_mediators:
        print(
            f"[CHAIN AUDIT] Unresolved mediators (not in DB, frontend will "
            f"render as placeholders): {_unresolved_mediators}",
            file=sys.stderr,
        )

    # Batch-load all possible chain link interactions (avoids N+1 queries per mediator)
    _chain_link_map = {}
    _chain_mediator_ids = {protein.id for protein in mediator_protein_map.values()}

    if _chain_mediator_ids:
        _all_possible_chain_links = db.session.query(Interaction).options(
            joinedload(Interaction.protein_a),
            joinedload(Interaction.protein_b)
        ).filter(
            db.or_(
                Interaction.protein_a_id.in_(_chain_mediator_ids),
                Interaction.protein_b_id.in_(_chain_mediator_ids)
            )
        ).all()
        for cl in _all_possible_chain_links:
            _chain_link_map[(cl.protein_a_id, cl.protein_b_id)] = cl

    # Batch-load chain-derived claims for ALL chain link interactions to avoid
    # N+1 queries inside _build_chain_link_data (was 1 query per segment).
    _chain_claims_by_interaction: dict = {}
    _chain_link_ids = [cl.id for cl in _chain_link_map.values()]
    if _chain_link_ids:
        _all_chain_claims = InteractionClaim.query.filter(
            InteractionClaim.interaction_id.in_(_chain_link_ids),
            InteractionClaim.function_context == "chain_derived",
        ).all()
        for c in _all_chain_claims:
            _chain_claims_by_interaction.setdefault(c.interaction_id, []).append(c)

    synthesized_chain_keys = set()
    # Track chain-context link keys to avoid duplicates across indirect interactions
    _added_chain_context_keys = set()

    # Resolve main protein symbol once
    _main_protein_obj = db.session.get(Protein, main_protein_id) if main_protein_id else None
    _main_symbol = _main_protein_obj.symbol if _main_protein_obj else None

    def _build_chain_link_data(chain_link, src_sym, tgt_sym, chain_funcs, parent_interaction, segment_arrow=None):
        """Build a chain-link display dict from a DB interaction row, overlaying chain-specific functions.

        Per-hop modals must show biology about the SRC↔TGT pair in isolation,
        not the full-cascade net-effect (which mentions the query protein).
        Two guards enforce this:
          1. Any overlay function whose text references ``_main_symbol`` is
             dropped here (LLM violating the "no query mention" prompt rule
             would otherwise pollute the direct slot with query-biased text).
          2. We do NOT fall back to ``parent_interaction.data.functions`` when
             a hop has no pair-specific claims. Parent functions describe the
             query↔target net effect; rendering them on a mid-chain hop is a
             category error. Empty hops render as stubs; the parent cascade
             lives on its own indirect row.
        """
        chain_data = (chain_link.data or {}).copy()
        chain_data["_db_id"] = chain_link.id
        chain_data["source"] = src_sym
        chain_data["target"] = tgt_sym
        # Surface pseudo-entity flags so the frontend can italicize generic
        # biomolecule classes (RNA, Ubiquitin, ...) in chain hop cards.
        try:
            chain_data["_source_is_pseudo"] = bool(
                getattr(chain_link.protein_a, "is_pseudo", False)
                if chain_link.protein_a and chain_link.protein_a.symbol == src_sym
                else getattr(chain_link.protein_b, "is_pseudo", False)
            )
            chain_data["_target_is_pseudo"] = bool(
                getattr(chain_link.protein_b, "is_pseudo", False)
                if chain_link.protein_b and chain_link.protein_b.symbol == tgt_sym
                else getattr(chain_link.protein_a, "is_pseudo", False)
            )
        except Exception:
            chain_data["_source_is_pseudo"] = False
            chain_data["_target_is_pseudo"] = False

        existing_functions = chain_data.get("functions") or []
        existing_real_functions = any(
            isinstance(f, dict) and not f.get("_auto_generated") and not f.get("_synthetic")
            for f in existing_functions
        )
        if chain_funcs and not existing_real_functions:
            filtered, dropped = _filter_query_mentions(chain_funcs, _main_symbol, src_sym, tgt_sym)
            if dropped:
                print(
                    f"[CHAIN LINK] Dropped {dropped} function(s) from "
                    f"{src_sym}->{tgt_sym} that mention the query {_main_symbol!r} "
                    "(per-hop claims must stand alone).",
                    file=sys.stderr,
                )
            chain_data["functions"] = filtered

        chain_interaction_type = chain_link.interaction_type or "direct"
        chain_data["type"] = chain_interaction_type
        chain_data["interaction_type"] = chain_interaction_type
        chain_data["function_context"] = (
            getattr(chain_link, "function_context", None)
            or chain_data.get("function_context")
            or "direct"
        )
        # Chain-link payloads are emitted in source->target segment order.
        # The DB row's direction is protein-id absolute (a_to_b/b_to_a), so
        # do not leak that to claim/modal renderers that expect semantic
        # pair-local direction.
        chain_data["_stored_direction"] = chain_link.direction
        chain_data["direction"] = "main_to_primary"
        chain_data["_direction_is_link_absolute"] = True

        if chain_data.get("confidence") is None:
            chain_data["confidence"] = 0.5
        if chain_data.get("functions") is None:
            chain_data["functions"] = []
        else:
            chain_data["functions"] = _normalize_function_payloads(chain_data["functions"])
        if chain_data.get("evidence") is None:
            chain_data["evidence"] = []
        if chain_data.get("pmids") is None:
            chain_data["pmids"] = []

        _has_real_functions = any(
            f for f in chain_data.get("functions", [])
            if isinstance(f, dict) and not f.get("_auto_generated")
        )
        if not _has_real_functions:
            chain_claims = _chain_claims_by_interaction.get(chain_link.id, [])
            if chain_claims:
                rehydrated = []
                for c in chain_claims:
                    claim_dict = {
                        "function": c.function_name,
                            "arrow": normalize_arrow(c.arrow, default="binds"),
                            "direction": semantic_claim_direction(c.direction),
                        "cellular_process": c.mechanism,
                        "effect_description": c.effect_description,
                        "biological_consequence": c.biological_consequences or [],
                        "specific_effects": c.specific_effects or [],
                        "evidence": c.evidence or [],
                        "pathway": c.pathway_name,
                        "function_context": "chain_derived",
                    }
                    # CD5 — surface router metadata persisted to raw_function_data
                    # so the modal can explain "this claim was rerouted from the
                    # PRKN→MFN1 hop because its text mentioned ATXN3". Without
                    # this, router decisions were invisible past the log.
                    raw = c.raw_function_data or {}
                    if isinstance(raw, dict):
                        # L4.4 — surface quality validator's _depth_issues so
                        # the modal can render a "shallow" badge on claims
                        # that failed the PhD-depth gate (6-10 sentences,
                        # 3-5 cascades).
                        for mk in ("_router_reason", "_router_mentioned",
                                   "_rerouted_from_hop", "_depth_issues",
                                   "_arrow_source"):
                            if mk in raw:
                                claim_dict[mk] = raw[mk]
                    rehydrated.append(claim_dict)
                filtered, dropped = _filter_query_mentions(rehydrated, _main_symbol, src_sym, tgt_sym)
                if dropped:
                    print(
                        f"[CHAIN LINK] Dropped {dropped} rehydrated claim(s) from "
                        f"{src_sym}->{tgt_sym} that mention the query {_main_symbol!r}.",
                        file=sys.stderr,
                    )
                chain_data["functions"] = filtered
            elif parent_interaction:
                # FILTERED parent-fallback: when the hop has NO claims of
                # its own, surface parent-interaction functions ONLY when
                # they pass the same Locus Router filter — i.e., they
                # describe pair-specific biology without mentioning the
                # query protein. This recovers the "modal shows something"
                # experience from before I killed the broken fallback,
                # without reintroducing the VDAC1 bug (where query-
                # cascade text polluted a mid-chain hop).
                parent_funcs = (parent_interaction.data or {}).get("functions", []) or []
                real_parent = [
                    f for f in parent_funcs
                    if isinstance(f, dict) and not f.get("_auto_generated")
                ]
                if real_parent:
                    filtered, _dropped = _filter_query_mentions(
                        real_parent, _main_symbol, src_sym, tgt_sym
                    )
                    if filtered:
                        chain_data["functions"] = [
                            {**fn, "_from_parent_chain_filtered": True}
                            for fn in filtered
                        ]
                        chain_data["functions"] = _normalize_function_payloads(chain_data["functions"])
                        # [CHAIN LINK] Parent-fallback log suppressed per
                        # user request (2026-04-21). Behavior unchanged —
                        # parent-fallback still runs and still surfaces
                        # query-free parent functions on empty hops.
            # If nothing survived, empty hops stay empty and render as
            # stubs. Parent net-effect biology belongs on the parent's own
            # indirect interaction row, not on a mid-chain hop.

        # Chain link arrow resolution
        resolved_arrow = None
        arrow_inferred = False
        for func in chain_data.get("functions", []):
            if func.get("arrow_context") and func["arrow_context"].get("direct_arrow"):
                resolved_arrow = normalize_arrow(func["arrow_context"]["direct_arrow"], default="binds")
                break
        if not resolved_arrow:
            for func in chain_data.get("functions", []):
                if isinstance(func, dict) and func.get("arrow"):
                    resolved_arrow = normalize_arrow(func.get("arrow"), default="binds")
                    break
        if not resolved_arrow and segment_arrow:
            resolved_arrow = normalize_arrow(segment_arrow, default="binds")
        if not resolved_arrow:
            if chain_link.arrow:
                resolved_arrow = normalize_arrow(chain_link.arrow, default="binds")
        if not resolved_arrow:
            # Nothing recorded on either the function's arrow_context or
            # the chain_link row. Fall back to the chain payload's arrow
            # if present; otherwise mark as inferred so downstream QC /
            # future UI can flag the "binds" display as not literature-backed.
            payload_arrow = chain_data.get("arrow")
            if payload_arrow:
                resolved_arrow = normalize_arrow(payload_arrow, default="binds")
            else:
                resolved_arrow = "binds"
                arrow_inferred = True
        chain_data["arrow"] = resolved_arrow
        if arrow_inferred:
            chain_data["_arrow_inferred"] = True

        chain_data["interaction_effect"] = arrow_to_effect(chain_data.get("arrow", "binds"))

        for func in chain_data.get("functions", []):
            if not func.get("function_effect"):
                func_arrow = func.get("arrow", "")
                if func_arrow:
                    func["function_effect"] = arrow_to_effect(func_arrow)

        chain_function_context = chain_data["function_context"]
        if chain_function_context == "direct" and chain_data.get("_inferred_from_chain"):
            chain_data["_direct_mediator_link"] = True
            chain_data["_display_badge"] = "DIRECT LINK"

        # Tag with parent chain context for pathway coherence
        chain_data["_parent_chain"] = {
            "indirect_db_id": parent_interaction.id if parent_interaction else None,
            "pathway": (parent_interaction.data or {}).get("step3_finalized_pathway") if parent_interaction else None,
        }

        # V2 PATHWAY INJECTION FOR CHAIN LINKS — inherit parent chain pathway
        parent_pathway = chain_data["_parent_chain"].get("pathway")
        if parent_pathway:
            chain_data["step3_finalized_pathway"] = parent_pathway
            for func in chain_data.get("functions", []):
                if isinstance(func, dict):
                    current_pw = func.get("pathway")
                    if not current_pw or current_pw == "Uncategorized":
                        func["pathway"] = parent_pathway

        # Suppress stub functions from UI output — don't show
        # "Function data not generated for this specific pair" to users
        chain_data["functions"] = [
            f for f in chain_data.get("functions", [])
            if isinstance(f, dict) and not f.get("_auto_generated")
        ]
        chain_data["functions"] = _normalize_function_payloads(chain_data["functions"])

        return chain_data

    def _lookup_chain_link(sym_a, sym_b):
        """Look up a chain link interaction by protein symbol pair."""
        prot_a = mediator_protein_map.get(sym_a) or mediator_protein_map_ci.get(str(sym_a).upper())
        prot_b = mediator_protein_map.get(sym_b) or mediator_protein_map_ci.get(str(sym_b).upper())
        if not prot_a or not prot_b:
            return None
        a_id, b_id = min(prot_a.id, prot_b.id), max(prot_a.id, prot_b.id)
        return _chain_link_map.get((a_id, b_id))

    for interaction in db_interactions:
        if interaction.interaction_type == "indirect":
            # Read chain state through the canonical ChainView. The view
            # prefers the linked ``IndirectChain`` row (when ``chain_id``
            # is set), then the JSONB ``chain_context.full_chain``, then
            # the legacy ``mediator_chain`` column. By going through one
            # accessor, every reader sees the same chain state regardless
            # of which storage layer was populated for the row.
            chain_view = interaction.chain_view
            if not chain_view.is_empty:
                if interaction.protein_a_id == main_protein_id:
                    target_protein = interaction.protein_b
                else:
                    target_protein = interaction.protein_a

                # Get chain-specific functions from parent indirect interaction.
                # Canonicalize keys so we can look up any (src, tgt) pair
                # regardless of which direction the LLM used when generating
                # the link claims.
                from utils.chain_resolution import (
                    canonical_pair_key as _canon_pair_key,
                    canonicalize_chain_link_functions as _canon_clf,
                )
                parent_chain_funcs = _canon_clf(
                    (interaction.data or {}).get("chain_link_functions") or {}
                )

                # Authoritative full chain comes from the ChainView. The
                # legacy "[_main_symbol, ...mediator_chain, target]"
                # reconstruction is only used when chain_view itself was
                # empty (handled above by the is_empty short-circuit).
                full_chain = list(chain_view.full_chain)

                def _valid_component_for_segment(seg_index: int) -> tuple[list[str], list[dict]]:
                    """Contiguous valid-protein slice around one hop.

                    Historical rows may contain generic nodes such as RNA or
                    Ubiquitin. Do not render those as graph proteins, and do
                    not simply drop them from the full chain because that would
                    create false adjacency across the gap.
                    """
                    start = seg_index
                    while start > 0 and is_valid_chain_protein_symbol(full_chain[start - 1]):
                        start -= 1
                    end = seg_index + 1
                    while end < len(full_chain) - 1 and is_valid_chain_protein_symbol(full_chain[end + 1]):
                        end += 1
                    linked_chain = getattr(interaction, "linked_chain", None)
                    if linked_chain is not None and getattr(linked_chain, "chain_with_arrows", None):
                        all_arrows = normalize_chain_arrows(linked_chain.chain_with_arrows)
                    else:
                        all_arrows = normalize_chain_arrows(
                            (interaction.data or {}).get("chain_with_arrows", []) or []
                        )
                    return list(full_chain[start:end + 1]), list(all_arrows[start:end])

                def _arrow_for_segment(seg_index: int):
                    linked_chain = getattr(interaction, "linked_chain", None)
                    if linked_chain is not None and getattr(linked_chain, "chain_with_arrows", None):
                        all_arrows = normalize_chain_arrows(linked_chain.chain_with_arrows)
                    else:
                        all_arrows = normalize_chain_arrows(
                            (interaction.data or {}).get("chain_with_arrows", []) or []
                        )
                    if 0 <= seg_index < len(all_arrows):
                        return all_arrows[seg_index].get("arrow")
                    return None

                # Ensure main protein is in mediator_protein_map for lookups
                if _main_symbol and _main_symbol not in mediator_protein_map and _main_protein_obj:
                    mediator_protein_map[_main_symbol] = _main_protein_obj

                # Process each segment of the chain
                for seg_idx in range(len(full_chain) - 1):
                    src_sym = full_chain[seg_idx]
                    tgt_sym = full_chain[seg_idx + 1]

                    # Skip self-links
                    if src_sym == tgt_sym:
                        continue
                    if not is_valid_chain_protein_symbol(src_sym) or not is_valid_chain_protein_symbol(tgt_sym):
                        continue

                    # Deduplicate: use CANONICAL pair key so the same DB edge
                    # doesn't render twice when two chains traverse it in
                    # opposite directions (e.g. ATXN3→VCP→TDP43 and
                    # ATXN3→TDP43→VCP both produce a VCP↔TDP43 segment
                    # pointing at the same interaction.id). The directional
                    # key used to let both pass, which showed up in the
                    # modal as duplicate VCP→TDP43 and TDP43→VCP rows with
                    # identical claim text. One entry per (pair, id).
                    context_key = (frozenset((src_sym, tgt_sym)), interaction.id)
                    if context_key in _added_chain_context_keys:
                        continue
                    _added_chain_context_keys.add(context_key)

                    # Get chain-specific functions for this segment (canonical lookup).
                    chain_funcs = parent_chain_funcs.get(
                        _canon_pair_key(src_sym, tgt_sym), []
                    )

                    # Look up the DB row for this pair
                    chain_link = _lookup_chain_link(src_sym, tgt_sym)

                    # S2: Stamp chain-link metadata on every segment so the
                    # frontend's card view can build hierarchies for 4+
                    # protein chains. Previously chain links had NO
                    # upstream_interactor, so findUpstreamParent() Strategy 1
                    # failed and chains broke at 3+ hops.
                    def _stamp_chain_metadata(d):
                        display_chain, display_arrows = _valid_component_for_segment(seg_idx)
                        d["upstream_interactor"] = src_sym
                        d["_is_chain_link"] = True
                        d["_chain_position"] = seg_idx
                        d["_chain_length"] = len(full_chain)
                        d["chain_id"] = interaction.chain_id
                        d["mediator_chain"] = chain_view.mediator_chain
                        d.setdefault("depth", chain_view.depth)
                        # Attach full chain entity for frontend linear rendering
                        d["_chain_entity"] = {
                            "chain_proteins": display_chain,
                            "chain_with_arrows": display_arrows,
                            "pathway_name": (interaction.data or {}).get("step3_finalized_pathway"),
                        }
                        # R3: also stamp ALL chain memberships so the card
                        # view can place this hop into each chain instance
                        # it belongs to instead of only the parent's
                        # primary chain. Without this, an interaction
                        # that participates in two chains via the M2M
                        # ChainParticipant table only renders under the
                        # primary chain_id, and the second instance is
                        # invisible. Frontend can fall back to scalar
                        # `chain_id` when `all_chains` is empty/absent.
                        try:
                            memberships = _memberships_for(interaction, chain_memberships_index)
                            if memberships:
                                summaries = []
                                for m in memberships:
                                    ch = m.chain
                                    if not ch:
                                        continue
                                    summaries.append({
                                        "chain_id": ch.id,
                                        "role": m.role,
                                        "chain_proteins": list(ch.chain_proteins or []),
                                        "chain_with_arrows": normalize_chain_arrows(ch.chain_with_arrows),
                                        "pathway_name": ch.pathway_name,
                                    })
                                if summaries:
                                    d["chain_ids"] = [s["chain_id"] for s in summaries]
                                    d["all_chains"] = summaries
                        except Exception:
                            # Multi-chain enrichment is best-effort;
                            # never let it break a hop emission.
                            pass
                        return d

                    if chain_link and chain_link.id not in chain_link_ids:
                        chain_link_ids.add(chain_link.id)
                        all_chain_links.append(chain_link)

                        if chain_link.protein_a not in interactor_proteins:
                            interactor_proteins.append(chain_link.protein_a)
                        if chain_link.protein_b not in interactor_proteins:
                            interactor_proteins.append(chain_link.protein_b)

                        protein_set.add(chain_link.protein_a.symbol)
                        protein_set.add(chain_link.protein_b.symbol)

                        chain_data = _build_chain_link_data(
                            chain_link, src_sym, tgt_sym, chain_funcs, interaction,
                            segment_arrow=_arrow_for_segment(seg_idx),
                        )
                        _stamp_chain_metadata(chain_data)
                        interactions_list.append(chain_data)

                    elif chain_link and chain_link.id in chain_link_ids and chain_funcs:
                        chain_data = _build_chain_link_data(
                            chain_link, src_sym, tgt_sym, chain_funcs, interaction,
                            segment_arrow=_arrow_for_segment(seg_idx),
                        )
                        _stamp_chain_metadata(chain_data)
                        interactions_list.append(chain_data)

                    elif chain_link is None:
                        synth_key = tuple(sorted([src_sym, tgt_sym]))
                        if synth_key not in synthesized_chain_keys:
                            synthesized_chain_keys.add(synth_key)
                            protein_set.add(src_sym)
                            protein_set.add(tgt_sym)
                            synth_arrow = normalize_arrow(_arrow_for_segment(seg_idx) or interaction.primary_arrow, default="binds")
                            synth_data = {
                                "source": src_sym,
                                "target": tgt_sym,
                                "arrow": synth_arrow,
                                "arrows": normalize_arrows_map(interaction.arrows),
                                "interaction_type": "direct",
                                "type": "direct",
                                "function_context": "chain_derived",
                                "direction": "main_to_primary",
                                "confidence": 0.5,
                                "functions": chain_funcs if chain_funcs else [],
                                "evidence": [],
                                "pmids": [],
                                "interaction_effect": arrow_to_effect(synth_arrow),
                                "_synthesized_from_chain": True,
                            }
                            _stamp_chain_metadata(synth_data)

                            # Parent chain function fallback for synthesized links
                            if not synth_data["functions"]:
                                # First try chain-derived claims from DB
                                _synth_chain_claims = InteractionClaim.query.filter_by(
                                    interaction_id=interaction.id,
                                    function_context="chain_derived"
                                ).all()
                                if _synth_chain_claims:
                                    _synth_rows = [{
                                        "function": c.function_name,
                                        "arrow": normalize_arrow(c.arrow, default="regulates"),
                                        "cellular_process": c.mechanism,
                                        "effect_description": c.effect_description,
                                        "biological_consequence": c.biological_consequences or [],
                                        "specific_effects": c.specific_effects or [],
                                        "evidence": c.evidence or [],
                                        "pathway": c.pathway_name,
                                        "function_context": "chain_derived",
                                        "direction": semantic_claim_direction(c.direction),
                                    } for c in _synth_chain_claims]
                                    # Defense-in-depth: filter out any claim whose
                                    # text mentions the query protein — even though
                                    # this is the chain-derived slot, stale rows
                                    # from before the Locus Router can still leak.
                                    _synth_src, _synth_tgt = src_sym, tgt_sym
                                    _filtered, _dropped = _filter_query_mentions(
                                        _synth_rows, _main_symbol, _synth_src, _synth_tgt
                                    )
                                    if _dropped:
                                        print(
                                            f"[CHAIN LINK synth] Dropped {_dropped} "
                                            f"claim(s) from {_synth_src}->{_synth_tgt} "
                                            f"that mention query {_main_symbol!r}.",
                                            file=sys.stderr,
                                        )
                                    synth_data["functions"] = _filtered
                                # No parent-functions fallback: synthetic chain
                                # stubs with no real claims render as empty.
                                # Parent net-effect biology lives on its own
                                # indirect row — rendering it here is a category
                                # error (the VDAC1 bug class).

                            # Pathway injection from parent chain
                            parent_pathway = (interaction.data or {}).get("step3_finalized_pathway")
                            if parent_pathway:
                                synth_data["step3_finalized_pathway"] = parent_pathway
                                for func in synth_data.get("functions", []):
                                    if isinstance(func, dict):
                                        current_pw = func.get("pathway")
                                        if not current_pw or current_pw == "Uncategorized":
                                            func["pathway"] = parent_pathway

                            interactions_list.append(synth_data)

    return all_chain_links, chain_link_ids, synthesized_chain_keys


def build_full_json_from_db(protein_symbol: str) -> dict:
    """Reconstruct complete JSON from PostgreSQL database.

    Returns restructured format with proteins array and interactions array.
    """
    # Query main protein
    main_protein = Protein.query.filter_by(symbol=protein_symbol).first()
    if not main_protein:
        return None

    # Hide partial data while pipeline is actively running —
    # but only if there's an actual active job (prevents stale "running" from blocking forever)
    if main_protein.pipeline_status == "running":
        from services.state import jobs, jobs_lock
        with jobs_lock:
            active_job = jobs.get(protein_symbol, {})
            is_actually_running = active_job.get("status") == "processing"
        if is_actually_running:
            return None

    # Query all interactions using CANONICAL ORDERING (eager-load proteins
    # to avoid N+1). ``Interaction.claims`` is a ``lazy='dynamic'``
    # relationship and cannot be eager-loaded — SQLAlchemy reraises on any
    # selectinload/joinedload attempt against it. Claims are batch-loaded
    # separately below via ``InteractionClaim.query.filter(...)`` which
    # avoids the dynamic-loader restriction.
    db_interactions = db.session.query(Interaction).options(
        joinedload(Interaction.protein_a),
        joinedload(Interaction.protein_b),
        joinedload(Interaction.linked_chain),
    ).filter(
        (Interaction.protein_a_id == main_protein.id) |
        (Interaction.protein_b_id == main_protein.id)
    ).all()

    chain_memberships_index = _build_chain_membership_index(db_interactions)

    # Claims will be batch-loaded after all interaction IDs are known
    # (direct + chain links + shared links)
    claims_by_interaction = {}

    # Hoist the chain→pathways query that _chain_fields_for would otherwise
    # run per-interaction. Gather every chain_id touched by any of these
    # interactions (via ChainParticipant), then run ONE distinct query
    # against InteractionClaim. Without this, large queries pay an N
    # query overhead on every payload build.
    _all_chain_ids: set = set()
    for _memberships in chain_memberships_index.values():
        for _m in _memberships:
            if _m.chain_id is not None:
                _all_chain_ids.add(_m.chain_id)
    chain_pathways_index: dict = {}
    if _all_chain_ids:
        try:
            _rows = (
                InteractionClaim.query
                .filter(InteractionClaim.chain_id.in_(_all_chain_ids))
                .with_entities(InteractionClaim.chain_id, InteractionClaim.pathway_name)
                .distinct()
                .all()
            )
            for _cid, _pw in _rows:
                if _cid is None or not _pw:
                    continue
                chain_pathways_index.setdefault(_cid, set()).add(_pw)
        except Exception:
            # Best-effort hoist; per-row fallback in _chain_fields_for handles
            # the unlikely case of a transient query failure.
            chain_pathways_index = {}

    # Build interactions list with explicit source/target/type
    interactions_list = []
    protein_set = {protein_symbol}
    interactor_proteins = []

    # Process direct interactions (main protein <-> interactor)
    # (Auto-correct flag removed — corrections are payload-level only)
    for interaction in db_interactions:
        if interaction.protein_a_id == main_protein.id:
            partner = interaction.protein_b
            needs_flip = False
        else:
            partner = interaction.protein_a
            needs_flip = True

        interactor_proteins.append(partner)
        protein_set.add(partner.symbol)

        interaction_data = interaction.data.copy()
        # Surface partner pseudo flag so the frontend can italicize generic
        # biomolecule classes (RNA, Ubiquitin, ...) when they appear as
        # interactors. After L1, pseudo entities cannot be direct interactors
        # of a query (refused at db_sync.py), but they CAN appear as the
        # ``primary`` of a chain-link row that lands here.
        try:
            interaction_data["_partner_is_pseudo"] = bool(getattr(partner, "is_pseudo", False))
        except Exception:
            interaction_data["_partner_is_pseudo"] = False

        # Convert absolute direction to query-relative direction
        stored_direction = interaction.direction

        if needs_flip:
            if stored_direction == "a_to_b":
                final_direction = "primary_to_main"
            elif stored_direction == "b_to_a":
                final_direction = "main_to_primary"
            else:
                final_direction = stored_direction or "main_to_primary"
        else:
            if stored_direction == "a_to_b":
                final_direction = "main_to_primary"
            elif stored_direction == "b_to_a":
                final_direction = "primary_to_main"
            else:
                final_direction = stored_direction or "main_to_primary"

        # Set source/target based on FINAL DIRECTION
        if final_direction == "main_to_primary":
            interaction_data["source"] = protein_symbol
            interaction_data["target"] = partner.symbol
        elif final_direction == "primary_to_main":
            interaction_data["source"] = partner.symbol
            interaction_data["target"] = protein_symbol
        else:
            if protein_symbol < partner.symbol:
                interaction_data["source"] = protein_symbol
                interaction_data["target"] = partner.symbol
            else:
                interaction_data["source"] = partner.symbol
                interaction_data["target"] = protein_symbol

        interaction_data["direction"] = final_direction

        interaction_type_value = interaction.interaction_type or "direct"
        function_context_value = (
            interaction.function_context
            or interaction_data.get("function_context")
            or "direct"
        )
        is_net_effect_row = str(function_context_value).lower() == "net"

        # A4 — interaction_type assay guard (content overrides LLM structure).
        #
        # If the claim's prose (support_summary OR any function's text) cites
        # a direct-binding assay (Co-IP, Y2H, BioID, SPR, pulldown, FRET, XL-MS,
        # structural data), the interaction must render as direct regardless
        # of what the LLM labeled it. A negated mention ("no Co-IP detected")
        # does not count.
        #
        # The prior gate required BOTH proteins to be named in the summary —
        # too strict. Real LLM outputs cite assays in function.cellular_process
        # or function.specific_effects far more often than in support_summary,
        # and frequently reference only one protein by symbol. We now scan:
        #   • support_summary
        #   • each function's cellular_process + effect_description +
        #     specific_effects + evidence[].assay
        # and trigger the flip when any positive assay mention co-occurs with
        # at least ONE of the pair's symbols somewhere in the scanned text.
        if interaction_type_value == "indirect" and not is_net_effect_row:
            scan_blob_parts = [interaction_data.get("support_summary") or ""]
            for func in interaction_data.get("functions", []) or []:
                if not isinstance(func, dict):
                    continue
                scan_blob_parts.append(str(func.get("cellular_process") or ""))
                scan_blob_parts.append(str(func.get("effect_description") or ""))
                for se in (func.get("specific_effects") or []):
                    scan_blob_parts.append(str(se))
                for ev in (func.get("evidence") or []):
                    if isinstance(ev, dict):
                        scan_blob_parts.append(str(ev.get("assay") or ""))
                        scan_blob_parts.append(str(ev.get("key_finding") or ""))
            blob = " ".join(scan_blob_parts)
            blob_lower = blob.lower()
            query_mentioned = protein_symbol.lower() in blob_lower
            partner_mentioned = partner.symbol.lower() in blob_lower
            has_any_pair_mention = query_mentioned or partner_mentioned
            has_positive_evidence = False
            assay_hit = None
            if has_any_pair_mention:
                for m in _DIRECT_EVIDENCE_RE.finditer(blob):
                    if m.group("neg"):
                        continue
                    has_positive_evidence = True
                    assay_hit = m.group("kw")
                    break
            if has_positive_evidence:
                # [AUTO-CORRECT] log suppressed per user request
                # (2026-04-21). Behavior unchanged — the indirect→direct
                # upgrade still happens in the payload; we just don't
                # print it any more. Flags below remain so QC tooling
                # can still see which payloads were corrected.
                interaction_type_value = "direct"
                interaction_data["_auto_corrected_to_direct"] = True
                interaction_data["_auto_correct_evidence"] = assay_hit
                # NOTE: Do NOT mutate interaction.interaction_type — this is a
                # read function. The correction is applied to the payload only.

        interaction_data["type"] = interaction_type_value
        interaction_data["interaction_type"] = interaction_type_value
        interaction_data["function_context"] = function_context_value

        # C1: every chain-related field on the payload comes through one
        # canonical reader. No code path below this line reads
        # ``interaction.mediator_chain`` / ``upstream_interactor`` /
        # ``depth`` / ``chain_context`` / ``chain_id`` directly.
        chain_fields = _chain_fields_for(
            interaction,
            chain_pathways_index=chain_pathways_index,
            chain_memberships_index=chain_memberships_index,
        )
        interaction_data.update(chain_fields)

        # For indirect interactions, the graph-view source node should
        # be the chain's upstream_interactor (so arrows render in the
        # chain direction, not the canonical protein-id order).
        upstream = chain_fields.get("upstream_interactor")
        if interaction_type_value == "indirect" and upstream and not is_net_effect_row:
            interaction_data["source"] = upstream
            interaction_data["_direction_is_link_absolute"] = True

        # C2: arrow comes from the canonical primary_arrow property
        # (reads the arrows JSONB dict first, then the legacy scalar
        # column, then 'binds'). Also emit the full arrows dict so
        # the frontend can render multi-type arrows per direction.
        interaction_data["confidence"] = interaction_data.get("confidence") or 0.5
        interaction_data["arrow"] = normalize_arrow(interaction.primary_arrow, default="binds")
        interaction_data["arrows"] = normalize_arrows_map(interaction.arrows)
        if interaction_data.get("functions") is None:
            interaction_data["functions"] = []
        else:
            interaction_data["functions"] = _normalize_function_payloads(interaction_data["functions"])
        if interaction_data.get("evidence") is None:
            interaction_data["evidence"] = []
        if interaction_data.get("pmids") is None:
            interaction_data["pmids"] = []

        # Auto-generate interaction_effect from arrow if not present
        if not interaction_data.get("interaction_effect"):
            interaction_data["interaction_effect"] = arrow_to_effect(interaction_data["arrow"])

        # A1 — arrow ↔ content consistency check (observe-only).
        _funcs_for_arrow_check = interaction_data.get("functions") or []
        # A1 — arrow ↔ content consistency check.
        # The env flag is the master switch. When `ARROW_AUTO_CORRECT=false`
        # the whole validator is inactive: no classification pass, no log,
        # no rewrite, nothing reaches the DB. Flip the flag to `true` (or
        # leave it unset — default is on) to enable it.
        _auto = (os.getenv("ARROW_AUTO_CORRECT", "true").lower() != "false")
        if _funcs_for_arrow_check and _auto:
            try:
                from utils.arrow_content_validator import validate_arrows

                # Default ON: the verb-family validator is high-precision
                # (ordered activate → inhibit → bind → regulate; word-
                # boundary matches only; skips hedge labels), and every
                # tested ATXN3 log shows it catching real LLM mislabels
                # at a sustained rate without false positives.
                # [ARROW DRIFT] logs suppressed per user request
                # (2026-04-21). Behavior unchanged — the validator
                # still runs with auto_correct=True, so mismatched
                # arrows are rewritten in place. Only the stderr
                # chatter is silenced.
                validate_arrows(_funcs_for_arrow_check, auto_correct=True)
            except Exception:
                # Validator failure is non-fatal; stay silent per
                # the same suppression request.
                pass

        # PR-3a — direction content validator.
        # Gated by DIRECTION_AUTO_CORRECT (default off). Off = inactive.
        _dir_auto = (os.getenv("DIRECTION_AUTO_CORRECT", "").lower() == "true")
        if _funcs_for_arrow_check and _dir_auto:
            try:
                from utils.direction_content_validator import validate_directions

                # [DIRECTION DRIFT] logs suppressed per user request
                # (2026-04-21). Behavior unchanged — directions are
                # still validated and rewritten when mismatched.
                validate_directions(
                    _funcs_for_arrow_check,
                    main_symbol=protein_symbol,
                    partner_symbol=partner.symbol,
                    auto_correct=True,
                )
            except Exception:
                # Validator failure is non-fatal; stay silent.
                pass

        # PR-3b — pathway ↔ mechanism keyword check.
        # P3.2: this used to flip the assigned pathway in-place at
        # read time when keywords disagreed (auto_correct=True). The
        # DB never received the correction, so every read recomputed
        # it and the user saw a heuristic-derived pathway instead of
        # whatever quick_assign actually committed. PATHWAY_AUTO_CORRECT
        # now defaults to false; we still RUN the validator to log
        # drift (so operators can see what would be corrected), but
        # we don't mutate the payload. The proper fix is in
        # quick_assign.py (P3.1) — drift is detected once at write
        # time and persisted to the DB.
        _pw_auto = (os.getenv("PATHWAY_AUTO_CORRECT", "false").lower() == "true")
        if _funcs_for_arrow_check:
            try:
                from utils.pathway_content_validator import validate_pathways

                _pw_verdicts = validate_pathways(_funcs_for_arrow_check, auto_correct=_pw_auto)
                _pw_drifts = [v for v in _pw_verdicts if v.reason == "drift"]
                if _pw_drifts:
                    _action_label = "rewritten" if _pw_auto else "report-only (PATHWAY_AUTO_CORRECT=false)"
                    print(
                        f"[PATHWAY DRIFT] {partner.symbol}: "
                        f"{len(_pw_drifts)}/{len(_pw_verdicts)} function(s) "
                        f"have pathway assignment disagreeing with prose keywords — {_action_label}.",
                        file=sys.stderr,
                    )
                    for v in _pw_drifts:
                        print(
                            f"  - assigned={v.assigned!r} "
                            f"(score={v.assigned_score}) "
                            f"top={v.implied!r} (score={v.top_alternative_score})",
                            file=sys.stderr,
                        )
            except Exception as _pw_exc:
                print(
                    f"[PATHWAY DRIFT] validator failed for {partner.symbol}: "
                    f"{type(_pw_exc).__name__}: {_pw_exc}",
                    file=sys.stderr,
                )

        # Auto-generate function_effect for each function if not present
        for func in interaction_data.get("functions", []):
            if not func.get("function_effect"):
                func_arrow = normalize_arrow(func.get("arrow", ""), default="")
                if func_arrow:
                    func["arrow"] = func_arrow
                    func["function_effect"] = arrow_to_effect(func_arrow)

            if func.get("arrow_context"):
                arrow_ctx = func["arrow_context"]
                net_arrow = arrow_ctx.get("net_arrow", func.get("arrow", "regulates"))
                direct_arrow = arrow_ctx.get("direct_arrow", net_arrow)
                func["net_effect"] = arrow_to_effect(net_arrow)
                func["direct_effect"] = arrow_to_effect(direct_arrow)

        # For direct mediator links, use direct_arrow from arrow_context
        function_context = interaction_data.get("function_context")
        if function_context == "direct":
            for func in interaction_data.get("functions", []):
                if func.get("arrow_context") and func["arrow_context"].get("direct_arrow"):
                    correct_arrow = func["arrow_context"]["direct_arrow"]
                    if interaction_data.get("arrow") != correct_arrow:
                        interaction_data["arrow"] = correct_arrow
                        interaction_data["interaction_effect"] = arrow_to_effect(correct_arrow)
                    break

        # Add differentiation flags for dual-track indirect/direct system
        if function_context == "net":
            interaction_data["_net_effect"] = True
            interaction_data["_display_badge"] = "NET EFFECT"
        elif function_context == "direct" and interaction_data.get("_inferred_from_chain"):
            interaction_data["_direct_mediator_link"] = True
            interaction_data["_display_badge"] = "DIRECT LINK"

        # V2 PATHWAY INJECTION
        v2_pathway = interaction.data.get('step3_finalized_pathway')

        if interaction_data.get("functions"):
            for func in interaction_data["functions"]:
                current_pw = func.get("pathway")
                if isinstance(current_pw, str) and current_pw and current_pw != "Uncategorized":
                    continue
                if isinstance(current_pw, dict) and current_pw.get("name"):
                    func["pathway"] = current_pw["name"]
                    continue
                if v2_pathway:
                    func["pathway"] = v2_pathway
        else:
            if v2_pathway:
                # PR-2 / C5: previously we fabricated a function whose NAME
                # was the pathway (e.g., "Apoptosis"), which rendered in the
                # modal as a real-looking scientific claim. That's a lie —
                # this interaction has no documented mechanism yet. Stamp
                # the synthetic function with _synthetic=True so the
                # frontend can suppress it or show a "No mechanism
                # documented" stub instead.
                interaction_data["functions"] = [{
                    "function": f"No mechanism documented — pathway: {v2_pathway}",
                    "pathway": v2_pathway,
                    "description": f"Interaction classified as {v2_pathway}. "
                                   "No pipeline-generated mechanism yet.",
                    "evidence": [],
                    "pmids": [],
                    "_synthetic": True,
                    "_synthetic_reason": "pathway_only_no_mechanism",
                }]

        if v2_pathway:
            interaction_data["step3_finalized_pathway"] = v2_pathway

        interaction_data["_db_id"] = interaction.id

        # Empty-hop badge: when a chain-derived interaction made it to the
        # payload but carries no real claims (e.g. VCP→UFD1 with no LLM
        # output and no rehydration match after the hop-signature filter),
        # tag it so the modal can render an explicit "No specific claim
        # generated for this hop" state rather than a blank row or a
        # placeholder-text stub. Only tags chain-derived rows; direct
        # interactions keep their legacy empty-functions rendering.
        _funcs = interaction_data.get("functions") or []
        _synthetic_only = all(isinstance(f, dict) and f.get("_synthetic") for f in _funcs)
        if (not _funcs or _synthetic_only) and (
            interaction.chain_id is not None
            or interaction_data.get("_is_chain_link")
            or interaction_data.get("_inferred_from_chain")
            or interaction_data.get("function_context") == "chain_derived"
        ):
            interaction_data["_is_stub_hop"] = True
            interaction_data["_stub_reason"] = "chain_hop_no_llm_claim"

        interactions_list.append(interaction_data)

    # Auto-corrections are payload-level only (no DB mutation in read path)

    # Retrieve chain links for indirect interactions
    all_chain_links, chain_link_ids, _synthesized_chain_keys = _reconstruct_chain_links(
        main_protein.id,
        db_interactions,
        interactions_list,
        protein_set,
        interactor_proteins,
        chain_memberships_index=chain_memberships_index,
    )

    # Query for shared interactions BETWEEN interactors
    all_shared_links = []
    if len(interactor_proteins) > 1:
        interactor_ids = [p.id for p in interactor_proteins]

        shared_interactions = db.session.query(Interaction).options(
            joinedload(Interaction.protein_a),
            joinedload(Interaction.protein_b)
        ).filter(
            Interaction.protein_a_id.in_(interactor_ids),
            Interaction.protein_b_id.in_(interactor_ids),
            ~((Interaction.protein_a_id == main_protein.id) | (Interaction.protein_b_id == main_protein.id))
        ).all()

        indirect_chain_pairs = set()
        for interaction in db_interactions:
            inter_data = interaction.data
            if interaction.interaction_type == 'indirect':
                upstream = inter_data.get('upstream_interactor')
                if upstream:
                    if interaction.protein_a_id == main_protein.id:
                        target = interaction.protein_b.symbol
                    else:
                        target = interaction.protein_a.symbol
                    indirect_chain_pairs.add((upstream, target))
                    indirect_chain_pairs.add((target, upstream))

        existing_db_ids = {item.get("_db_id") for item in interactions_list if item.get("_db_id")}
        for shared_ix in shared_interactions:
            if shared_ix.id in existing_db_ids:
                continue  # Already added as direct/chain interaction
            protein_a_sym = shared_ix.protein_a.symbol
            protein_b_sym = shared_ix.protein_b.symbol
            # Only skip chain-inferred interactions, not independently-discovered shared ones
            if shared_ix.data.get('_inferred_from_chain'):
                continue
            if (protein_a_sym, protein_b_sym) in indirect_chain_pairs or (protein_b_sym, protein_a_sym) in indirect_chain_pairs:
                # Check if this is a chain link already included — skip to avoid duplication
                pair_key = tuple(sorted([protein_a_sym, protein_b_sym]))
                if pair_key in _synthesized_chain_keys:
                    continue

            all_shared_links.append(shared_ix)
            protein_a = shared_ix.protein_a
            protein_b = shared_ix.protein_b
            protein_set.add(protein_a.symbol)
            protein_set.add(protein_b.symbol)

            shared_data = shared_ix.data.copy()
            shared_data["_db_id"] = shared_ix.id
            # Use stored absolute direction for correct arrow rendering
            if shared_ix.direction == "a_to_b":
                shared_data["source"] = protein_a.symbol
                shared_data["target"] = protein_b.symbol
            elif shared_ix.direction == "b_to_a":
                shared_data["source"] = protein_b.symbol
                shared_data["target"] = protein_a.symbol
            else:
                # Bidirectional or unknown — alphabetical for consistency
                if protein_a.symbol < protein_b.symbol:
                    shared_data["source"] = protein_a.symbol
                    shared_data["target"] = protein_b.symbol
                else:
                    shared_data["source"] = protein_b.symbol
                    shared_data["target"] = protein_a.symbol
            shared_data["type"] = "shared"
            shared_data["_is_shared_link"] = True
            shared_data["interaction_type"] = shared_ix.interaction_type or "direct"
            shared_data["function_context"] = (
                shared_ix.function_context
                or shared_data.get("function_context")
                or "direct"
            )
            if shared_ix.upstream_interactor:
                shared_data["upstream_interactor"] = shared_ix.upstream_interactor
            shared_data["direction"] = semantic_claim_direction(shared_ix.direction)

            if shared_data.get("confidence") is None:
                shared_data["confidence"] = 0.5
            if shared_data.get("arrow") is None:
                resolved_arrow = normalize_arrow(shared_ix.arrow, default="binds")
                if resolved_arrow:
                    shared_data["arrow"] = resolved_arrow
                else:
                    # No recorded arrow anywhere. Keep the visual "binds"
                    # default so the UI has something to render, but flag
                    # it as inferred so downstream QC / future UI can
                    # distinguish it from a real recorded binding.
                    shared_data["arrow"] = "binds"
                    shared_data["_arrow_inferred"] = True
            if shared_data.get("functions") is None:
                shared_data["functions"] = []
            if shared_data.get("evidence") is None:
                shared_data["evidence"] = []
            if shared_data.get("pmids") is None:
                shared_data["pmids"] = []
            if shared_data.get("intent") is None:
                shared_data["intent"] = "binding"

            if not shared_data.get("interaction_effect"):
                shared_data["interaction_effect"] = arrow_to_effect(shared_data.get("arrow", "binds"))

            for func in shared_data.get("functions", []):
                if not func.get("function_effect"):
                    func_arrow = func.get("arrow", "")
                    if func_arrow:
                        func["function_effect"] = arrow_to_effect(func_arrow)

            # V2 PATHWAY INJECTION FOR SHARED LINKS
            v2_pathway = shared_ix.data.get('step3_finalized_pathway')

            if shared_data.get("functions"):
                for func in shared_data["functions"]:
                    current_pw = func.get("pathway")
                    if isinstance(current_pw, str) and current_pw and current_pw != "Uncategorized":
                        continue
                    if isinstance(current_pw, dict) and current_pw.get("name"):
                        func["pathway"] = current_pw["name"]
                        continue
                    if v2_pathway:
                        func["pathway"] = v2_pathway
            else:
                if v2_pathway:
                    shared_data["functions"] = [{
                        "function": v2_pathway,
                        "pathway": v2_pathway,
                        "description": f"Interaction classified as {v2_pathway}",
                        "evidence": [],
                        "pmids": []
                    }]

            if v2_pathway:
                shared_data["step3_finalized_pathway"] = v2_pathway

            interactions_list.append(shared_data)

    # Batch-load claims for ALL interactions (direct + chain + shared)
    all_db_ids = set()
    for item in interactions_list:
        db_id = item.get("_db_id")
        if db_id:
            all_db_ids.add(db_id)

    # Build claim pathway_id → ancestry name list for hierarchy matching in modal
    _claim_pw_ancestry: dict[int, list[str]] = {}  # pathway_id → [root, ..., leaf]

    if all_db_ids:
        all_claims = InteractionClaim.query.filter(
            InteractionClaim.interaction_id.in_(list(all_db_ids))
        ).all()
        for claim in all_claims:
            claims_by_interaction.setdefault(claim.interaction_id, []).append(claim)

        # Batch-load Pathway objects referenced by claims
        claim_pw_ids = {c.pathway_id for c in all_claims if c.pathway_id}
        if claim_pw_ids:
            claim_pws = Pathway.query.filter(Pathway.id.in_(claim_pw_ids)).all()
            claim_pw_by_id = {pw.id: pw for pw in claim_pws}
            # Collect all ancestor IDs we need to resolve to names
            all_ancestor_ids = set()
            for pw in claim_pws:
                all_ancestor_ids.update(pw.ancestor_ids or [])
            all_ancestor_ids -= claim_pw_ids  # Don't re-load ones we already have
            if all_ancestor_ids:
                ancestor_pws = Pathway.query.filter(Pathway.id.in_(all_ancestor_ids)).all()
                for apw in ancestor_pws:
                    claim_pw_by_id[apw.id] = apw
            # Build ancestry name lists
            for pw in claim_pws:
                names = []
                for aid in (pw.ancestor_ids or []):
                    apw = claim_pw_by_id.get(aid)
                    if apw:
                        names.append(apw.name)
                names.append(pw.name)  # Include self at the end
                _claim_pw_ancestry[pw.id] = names

    # Build interaction → canonical pathway-name set from the pathway_interactions
    # junction so each claim can carry _interaction_pathways. This mirrors the
    # later hierarchy-building query but is computed up front for claim serialization.
    _interaction_pathway_names: dict[int, list[str]] = {}
    if all_db_ids:
        _pwi_rows = db.session.query(PathwayInteraction).filter(
            PathwayInteraction.interaction_id.in_(list(all_db_ids))
        ).all()
        _pwi_pw_ids = {row.pathway_id for row in _pwi_rows}
        _pwi_pw_names = {}
        if _pwi_pw_ids:
            for pw in Pathway.query.filter(Pathway.id.in_(_pwi_pw_ids)).all():
                _pwi_pw_names[pw.id] = pw.name
        _names_by_ix: dict[int, set[str]] = {}
        for row in _pwi_rows:
            name = _pwi_pw_names.get(row.pathway_id)
            if name:
                _names_by_ix.setdefault(row.interaction_id, set()).add(name)
        _interaction_pathway_names = {
            ix_id: sorted(names) for ix_id, names in _names_by_ix.items()
        }

    _VERB_MARKERS = (' is ', ' are ', ' was ', ' were ', ' has ', ' promotes ',
                     ' inhibits ', ' represents ', ' mediates ', ' regulates ',
                     ' involves ', ' facilitates ', ' promoted ', ' representing ')

    _GARBAGE_CLAIM_RE = re.compile(
        r'^__fallback__$|^(?:activates?|inhibits?|binds?|regulates?|interacts?)\s+interaction$',
        re.IGNORECASE,
    )

    def _repair_claim_fields(function_name, mechanism):
        """Fix claims where a description was placed in function_name."""
        if not function_name:
            return function_name, mechanism
        # Flag auto-generated garbage names so the frontend can filter them
        if _GARBAGE_CLAIM_RE.match(function_name):
            return '__fallback__', mechanism
        if len(function_name) <= 60:
            return function_name, mechanism
        is_sentence = any(v in function_name.lower() for v in _VERB_MARKERS)
        if is_sentence and not mechanism:
            short_name = ' '.join(function_name.split()[:4]).rstrip('.,;:')
            return short_name, function_name
        return function_name, mechanism

    def _serialize_claims(db_id, item=None):
        result = []
        for c in _claims_scoped_to_item(claims_by_interaction.get(db_id, []), item or {}):
            fn_name, mech = _repair_claim_fields(c.function_name, c.mechanism)
            # P1.9 quarantine: fallback claims are diagnostic, not
            # scientific. They have no mechanism, no cascades, no
            # evidence — surfacing them adds clutter and pollutes the
            # pathway/badge counts the user sees. Their existence is
            # already captured in the interaction row's discovery
            # method; the UI doesn't need a synthetic "no biology
            # found" stub claim.
            if fn_name == "__fallback__" or (
                c.discovery_method or ""
            ) == "pipeline_fallback":
                continue
            claim_dict = {
                "id": c.id,
                "function_name": fn_name,
                "arrow": normalize_arrow(c.arrow, default="regulates"),
                "interaction_effect": c.interaction_effect,
                "direction": semantic_claim_direction(c.direction),
                "mechanism": mech,
                "effect_description": c.effect_description,
                "biological_consequences": c.biological_consequences or [],
                "specific_effects": c.specific_effects or [],
                "evidence": c.evidence or [],
                "pmids": c.pmids or [],
                "pathway_name": c.pathway_name,
                "confidence": float(c.confidence) if c.confidence else None,
                "function_context": c.function_context,
                "context_data": c.context_data,
                "chain_id": c.chain_id,
            }
            # Attach ancestry so modal can match child claims to parent pathway context
            if c.pathway_id and c.pathway_id in _claim_pw_ancestry:
                claim_dict["_hierarchy"] = _claim_pw_ancestry[c.pathway_id]
            # P3.3: removed `_interaction_pathways` attachment. The field
            # used to carry the FULL set of pathways the parent
            # interaction belonged to, attached to every claim. The
            # modal then treated any-pathway-match as proof the claim
            # belonged in the current pathway view — exactly the leak
            # that crammed Apoptosis/Hippo/DNA-repair claims under
            # Protein Quality Control. Membership is now claim-scoped:
            # the claim's own `pathway_name` plus its `_hierarchy`
            # ancestor chain are the only valid membership signals.
            result.append(claim_dict)
        return result

    for item in interactions_list:
        db_id = item.get("_db_id")
        if db_id:
            item["claims"] = _serialize_claims(db_id, item)

    # Cross-protein chain claim injection
    _inject_cross_protein_chain_claims(
        protein_symbol,
        interactions_list,
        interaction_pathway_names=_interaction_pathway_names,
        claim_pw_ancestry=_claim_pw_ancestry,
    )

    # Attach IndirectChain entities to indirect interactions
    _indirect_db_ids = [
        item.get("_db_id")
        for item in interactions_list
        if item.get("_db_id")
        and (item.get("interaction_type") == "indirect" or item.get("type") == "indirect")
    ]
    _chain_entities_by_origin = {}
    if _indirect_db_ids:
        for _chain_entity in IndirectChain.query.filter(
            IndirectChain.origin_interaction_id.in_(_indirect_db_ids)
        ).all():
            _chain_entities_by_origin[_chain_entity.origin_interaction_id] = _chain_entity
    for interaction_data in interactions_list:
        if interaction_data.get("interaction_type") == "indirect" or interaction_data.get("type") == "indirect":
            db_id = interaction_data.get("_db_id")
            if db_id:
                chain_entity = _chain_entities_by_origin.get(db_id)
                if chain_entity:
                    interaction_data["_chain_entity"] = {
                        "id": chain_entity.id,
                        "chain_proteins": chain_entity.chain_proteins,
                        "pathway_name": chain_entity.pathway_name,
                        "chain_with_arrows": normalize_chain_arrows(chain_entity.chain_with_arrows),
                    }

    for interaction_data in interactions_list:
        _apply_contract_fields(interaction_data, protein_symbol)

    # BUILD PATHWAY HIERARCHY FROM DATABASE TABLES
    interaction_db_ids = [ix.id for ix in db_interactions] + \
                        [ix.id for ix in all_chain_links] + \
                        [ix.id for ix in all_shared_links]

    pathway_interactions = []
    pathway_ids_set = set()
    if interaction_db_ids:
        pathway_interactions = db.session.query(PathwayInteraction).filter(
            PathwayInteraction.interaction_id.in_(interaction_db_ids)
        ).all()
        for pwi in pathway_interactions:
            pathway_ids_set.add(pwi.pathway_id)

    pathway_by_id = {}
    if pathway_ids_set:
        pathway_objs = Pathway.query.filter(Pathway.id.in_(pathway_ids_set)).all()
        pathway_by_id = {p.id: p for p in pathway_objs}

    # Load full PathwayParent table for BFS traversal (hierarchy is small, typically < 1000 rows)
    # Cannot filter upfront because BFS discovers ancestor IDs incrementally
    all_parent_links = PathwayParent.query.all() if pathway_ids_set else []
    _parents_of = {}
    _children_of = {}
    for link in all_parent_links:
        _parents_of.setdefault(link.child_pathway_id, []).append(link.parent_pathway_id)
        _children_of.setdefault(link.parent_pathway_id, []).append(link.child_pathway_id)

    # BFS up to collect all ancestors
    current_layer_ids = set(pathway_by_id.keys())
    visited_ids = set(pathway_by_id.keys())

    while current_layer_ids:
        next_layer_ids = set()
        for cid in current_layer_ids:
            for pid in _parents_of.get(cid, []):
                if pid not in visited_ids:
                    next_layer_ids.add(pid)
                    visited_ids.add(pid)
        current_layer_ids = next_layer_ids

    missing_ids = visited_ids - set(pathway_by_id.keys())
    if missing_ids:
        ancestor_objs = Pathway.query.filter(Pathway.id.in_(missing_ids)).all()
        for a in ancestor_objs:
            pathway_by_id[a.id] = a

    # ALWAYS include all root pathways
    root_pathways = Pathway.query.filter(Pathway.hierarchy_level == 0).all()
    for rp in root_pathways:
        if rp.id not in pathway_by_id:
            pathway_by_id[rp.id] = rp

    # Include SIBLING pathways for complete taxonomy visualization
    current_pathway_ids = set(pathway_by_id.keys())
    sibling_ids_to_add = set()

    if current_pathway_ids:
        parent_ids = set()
        for cid in current_pathway_ids:
            parent_ids.update(_parents_of.get(cid, []))
        for pid in parent_ids:
            for child_id in _children_of.get(pid, []):
                if child_id not in pathway_by_id:
                    sibling_ids_to_add.add(child_id)

    if sibling_ids_to_add:
        sibling_objs = Pathway.query.filter(Pathway.id.in_(sibling_ids_to_add)).all()
        for sib in sibling_objs:
            pathway_by_id[sib.id] = sib

    # Build parent/child maps scoped to collected pathways
    all_pathway_ids_set = set(pathway_by_id.keys())
    parents_map = {}
    children_map = {}

    for link in all_parent_links:
        if link.child_pathway_id in all_pathway_ids_set or link.parent_pathway_id in all_pathway_ids_set:
            parents_map.setdefault(link.child_pathway_id, []).append(link.parent_pathway_id)
            children_map.setdefault(link.parent_pathway_id, []).append(link.child_pathway_id)

    # Build pathway_groups
    pathway_groups = {}
    for pw_id, pw_obj in pathway_by_id.items():
        pw_name = pw_obj.name

        parent_ids_list = parents_map.get(pw_id, [])
        child_ids_list = children_map.get(pw_id, [])
        parent_pathway_ids = []
        child_pathway_ids = []

        for pid in parent_ids_list:
            if pid in pathway_by_id:
                p_name = pathway_by_id[pid].name
                parent_pathway_ids.append(f"pathway_{p_name.replace(' ', '_').replace('-', '_')}")

        for cid in child_ids_list:
            if cid in pathway_by_id:
                c_name = pathway_by_id[cid].name
                child_pathway_ids.append(f"pathway_{c_name.replace(' ', '_').replace('-', '_')}")

        # Build ancestry dynamically
        ancestry = []
        curr_id = pw_id
        visited_anc = set()
        path_stack = [pw_obj.name]

        while True:
            pids = parents_map.get(curr_id, [])
            if not pids:
                break

            valid_parent = None
            for pid in pids:
                if pid in pathway_by_id and pid not in visited_anc:
                    valid_parent = pid
                    break

            if not valid_parent:
                break

            parent_obj = pathway_by_id[valid_parent]
            path_stack.insert(0, parent_obj.name)
            curr_id = valid_parent
            visited_anc.add(valid_parent)

            if parent_obj.hierarchy_level == 0:
                break

        ancestry = path_stack

        pathway_groups[pw_name] = {
            "id": f"pathway_{pw_name.replace(' ', '_').replace('-', '_')}",
            "_db_pathway_id": pw_obj.id,
            "name": pw_name,
            "ontology_id": pw_obj.ontology_id,
            "ontology_source": pw_obj.ontology_source,
            "hierarchy_level": pw_obj.hierarchy_level or 0,
            "is_leaf": pw_obj.is_leaf if pw_obj.is_leaf is not None else True,
            "interactor_ids": set(),
            "cross_query_interactor_ids": set(),
            "interactions": [],
            "cross_query_interactions": [],
            "interaction_count": 0,
            "parent_pathway_ids": parent_pathway_ids,
            "child_pathway_ids": child_pathway_ids,
            "ancestry": ancestry
        }

    # Build interaction_data lookup by DB ID
    interaction_data_by_id = {}
    for item in interactions_list:
        if item.get("_db_id"):
            interaction_data_by_id[item["_db_id"]] = item

    # ── Claim-based pathway population ────────────────────────────
    # Build interactor_ids from InteractionClaim.pathway_id, but ONLY
    # for proteins whose interactions are already loaded in SNAP.
    # This guarantees: (1) claims exist in the pathway, (2) interaction
    # data is available for getLocalRelationship() and the modal.

    # Set of protein symbols with loaded interaction data
    _loaded_proteins = {protein_symbol}
    for item in interactions_list:
        src = item.get("source")
        tgt = item.get("target")
        if src:
            _loaded_proteins.add(src)
        if tgt:
            _loaded_proteins.add(tgt)

    pw_dbid_to_name = {
        pw_data["_db_pathway_id"]: pw_name
        for pw_name, pw_data in pathway_groups.items()
        if pw_data.get("_db_pathway_id")
    }

    if pw_dbid_to_name:
        from sqlalchemy.orm import joinedload as _jl
        _pw_claims = InteractionClaim.query.filter(
            InteractionClaim.pathway_id.in_(pw_dbid_to_name.keys())
        ).options(
            _jl(InteractionClaim.interaction).joinedload(Interaction.protein_a),
            _jl(InteractionClaim.interaction).joinedload(Interaction.protein_b),
        ).all()
        _claims_by_interaction_for_pw = {}
        for _claim in _pw_claims:
            _claims_by_interaction_for_pw.setdefault(_claim.interaction_id, []).append(_claim)

        _seen_interactions = set()
        _cross_query_added_to_snap = set()  # track cross-query interactions added to SNAP

        for claim in _pw_claims:
            pw_name = pw_dbid_to_name.get(claim.pathway_id)
            if not pw_name or pw_name not in pathway_groups:
                continue
            inter = claim.interaction
            if not inter:
                continue
            pa_sym = inter.protein_a.symbol if inter.protein_a else None
            pb_sym = inter.protein_b.symbol if inter.protein_b else None
            if not pa_sym or not pb_sym:
                continue

            is_cross_query = pa_sym not in _loaded_proteins or pb_sym not in _loaded_proteins

            if is_cross_query:
                # Cross-query interaction: add to separate set
                if pa_sym != protein_symbol:
                    pathway_groups[pw_name]["cross_query_interactor_ids"].add(pa_sym)
                if pb_sym != protein_symbol:
                    pathway_groups[pw_name]["cross_query_interactor_ids"].add(pb_sym)
            else:
                # Query-related interaction: add to main set
                if pa_sym != protein_symbol:
                    pathway_groups[pw_name]["interactor_ids"].add(pa_sym)
                if pb_sym != protein_symbol:
                    pathway_groups[pw_name]["interactor_ids"].add(pb_sym)

            _ix_key = (pw_name, inter.id)
            if _ix_key not in _seen_interactions:
                _seen_interactions.add(_ix_key)
                pathway_groups[pw_name]["interaction_count"] += 1
                ix_data = interaction_data_by_id.get(inter.id, {})
                ix_chain_fields = _chain_fields_for(inter)
                ix_entry = {
                    "source": ix_data.get("source", pa_sym),
                    "target": ix_data.get("target", pb_sym),
                    "arrow": ix_data.get("arrow", inter.arrow or "binds"),
                    "direction": ix_data.get("direction", inter.direction or "main_to_primary"),
                    "confidence": ix_data.get("confidence", float(inter.confidence) if inter.confidence else 0.5),
                    "type": ix_data.get("type", inter.interaction_type or "direct"),
                    "interaction_type": ix_data.get("interaction_type") or inter.interaction_type or "direct",
                    "function_context": ix_data.get("function_context") or inter.function_context or "direct",
                    "interaction_effect": ix_data.get("interaction_effect", "binding"),
                    "functions": ix_data.get("functions", []),
                    "evidence": ix_data.get("evidence", []),
                    "pmids": ix_data.get("pmids", []),
                    "locus": ix_data.get("locus"),
                    "chain_id": ix_data.get("chain_id") or ix_chain_fields.get("chain_id"),
                    "chain_ids": ix_data.get("chain_ids") or ix_chain_fields.get("chain_ids"),
                    "all_chains": ix_data.get("all_chains") or ix_chain_fields.get("all_chains"),
                    "hop_index": ix_data.get("hop_index"),
                    "chain_members": ix_data.get("chain_members") or ix_chain_fields.get("chain_members"),
                    "chain_context_pathway": ix_data.get("chain_context_pathway"),
                    "hop_local_pathway": ix_data.get("hop_local_pathway"),
                    "is_net_effect": ix_data.get("is_net_effect"),
                    "via": ix_data.get("via", []),
                    "mediators": ix_data.get("mediators", []),
                    "_cross_query": is_cross_query,
                    "_is_chain_link": ix_data.get("_is_chain_link"),
                }
                _apply_contract_fields(ix_entry, protein_symbol)

                if is_cross_query:
                    pathway_groups[pw_name]["cross_query_interactions"].append(ix_entry)
                    # Also inject into interactions_list so SNAP.interactions
                    # has the data for getLocalRelationship() and the modal
                    if inter.id not in _cross_query_added_to_snap:
                        _cross_query_added_to_snap.add(inter.id)
                        # Build a full interaction entry for SNAP
                        cross_snap_entry = {
                            "_db_id": inter.id,
                            "source": pa_sym,
                            "target": pb_sym,
                            "id": pb_sym if pa_sym == protein_symbol else pa_sym,
                            "arrow": inter.arrow or "binds",
                            "direction": inter.direction or "main_to_primary",
                            "confidence": float(inter.confidence) if inter.confidence else 0.5,
                            "type": inter.interaction_type or "direct",
                            "interaction_type": inter.interaction_type or "direct",
                            "function_context": inter.function_context or "direct",
                            "interaction_effect": ix_data.get("interaction_effect", "binding"),
                            "functions": ix_data.get("functions", []),
                            "evidence": ix_data.get("evidence", []),
                            "pmids": ix_data.get("pmids", []),
                            "locus": ix_data.get("locus"),
                            "chain_id": ix_data.get("chain_id") or ix_chain_fields.get("chain_id"),
                            "chain_ids": ix_data.get("chain_ids") or ix_chain_fields.get("chain_ids"),
                            "all_chains": ix_data.get("all_chains") or ix_chain_fields.get("all_chains"),
                            "hop_index": ix_data.get("hop_index"),
                            "chain_members": ix_data.get("chain_members") or ix_chain_fields.get("chain_members"),
                            "chain_context_pathway": ix_data.get("chain_context_pathway"),
                            "hop_local_pathway": ix_data.get("hop_local_pathway"),
                            "is_net_effect": ix_data.get("is_net_effect"),
                            "via": ix_data.get("via", []),
                            "mediators": ix_data.get("mediators", []),
                            "_is_chain_link": ix_data.get("_is_chain_link"),
                            "_cross_query": True,
                            "claims": [],
                        }
                        # Claims were already loaded for pathway population.
                        # Reuse that batch instead of issuing one query per
                        # cross-query interaction.
                        _cq_claims = _claims_by_interaction_for_pw.get(inter.id, [])
                        for cqc in _claims_scoped_to_item(_cq_claims, cross_snap_entry):
                            cross_snap_entry["claims"].append({
                                "id": cqc.id,
                                "function_name": cqc.function_name,
                                "arrow": normalize_arrow(cqc.arrow, default="regulates"),
                                "mechanism": cqc.mechanism,
                                "effect_description": cqc.effect_description,
                                "biological_consequences": cqc.biological_consequences or [],
                                "specific_effects": cqc.specific_effects or [],
                                "evidence": cqc.evidence or [],
                                "pmids": cqc.pmids or [],
                                "pathway_name": cqc.pathway_name,
                                "pathway_id": cqc.pathway_id,
                                "interaction_effect": normalize_arrow(cqc.arrow, default="regulates"),
                                "direction": semantic_claim_direction(cqc.direction),
                                "function_context": cqc.function_context,
                                "confidence": float(cqc.confidence) if cqc.confidence else None,
                                "context_data": cqc.context_data,
                                "chain_id": cqc.chain_id,
                            })
                        _apply_contract_fields(cross_snap_entry, protein_symbol)
                        interactions_list.append(cross_snap_entry)
                else:
                    pathway_groups[pw_name]["interactions"].append(ix_entry)

    # S2: Inject chain-link mediator proteins into their pathway's
    # interactor_ids. Only inject if the chain link has at least 1
    # InteractionClaim with matching pathway (guarantees non-empty modal).
    # Build a set of (interaction_db_id, pathway_db_id) pairs that have claims.
    _claim_pw_pairs = set()
    if pw_dbid_to_name:
        for c in _pw_claims:
            _claim_pw_pairs.add((c.interaction_id, c.pathway_id))

    _name_to_dbid = {v: k for k, v in pw_dbid_to_name.items()}

    def _upsert_chain_pathway_interaction(pw_name, item):
        """Surface chain-link rows in pathway payloads with their hop functions.

        Synthetic chain links do not have a database id, so claim-based
        pathway population can add an empty same-pair placeholder while the
        real hop functions live only in ``snapshot_json.interactions``. Card
        view consumes the pathway-local ``interactions`` array for scoped
        edges, so keep the function-bearing chain entry there too.
        """
        if not pw_name or pw_name not in pathway_groups:
            return False
        src = item.get("source")
        tgt = item.get("target")
        if not src or not tgt:
            return False

        entry = {
            "source": src,
            "target": tgt,
            "arrow": item.get("arrow", "binds"),
            "direction": item.get("direction", "main_to_primary"),
            "confidence": item.get("confidence", 0.5),
            "type": item.get("type") or item.get("interaction_type") or "direct",
            "function_context": item.get("function_context") or "direct",
            "interaction_effect": item.get("interaction_effect", "binding"),
            "functions": item.get("functions", []),
            "evidence": item.get("evidence", []),
            "pmids": item.get("pmids", []),
            "locus": item.get("locus"),
            "hop_index": item.get("hop_index"),
            "chain_members": item.get("chain_members"),
            "chain_context_pathway": item.get("chain_context_pathway"),
            "hop_local_pathway": item.get("hop_local_pathway"),
            "is_net_effect": item.get("is_net_effect"),
            "via": item.get("via", []),
            "mediators": item.get("mediators", []),
            "_cross_query": False,
            "_is_chain_link": True,
            "_chain_position": item.get("_chain_position"),
            "_chain_length": item.get("_chain_length"),
            "chain_id": item.get("chain_id"),
            # R3: forward multi-chain memberships so the frontend can
            # render this hop under EACH chain instance it belongs to,
            # not only under the parent's primary chain_id.
            "chain_ids": item.get("chain_ids"),
            "all_chains": item.get("all_chains"),
            "_chain_entity": item.get("_chain_entity"),
            "_synthesized_from_chain": bool(item.get("_synthesized_from_chain")),
        }

        group = pathway_groups[pw_name]
        ordered_key = (
            src,
            tgt,
            item.get("chain_id"),
            item.get("_chain_position"),
        )
        unordered_pair = frozenset((src, tgt))
        item_func_count = len(item.get("functions") or [])

        for idx, existing in enumerate(group["interactions"]):
            existing_key = (
                existing.get("source"),
                existing.get("target"),
                existing.get("chain_id"),
                existing.get("_chain_position"),
            )
            existing_pair = frozenset((
                existing.get("source"),
                existing.get("target"),
            ))
            exact_same_chain = existing_key == ordered_key
            empty_same_pair = (
                existing_pair == unordered_pair
                and not existing.get("_is_chain_link")
                and not (existing.get("functions") or [])
                and item_func_count > 0
            )
            if exact_same_chain or empty_same_pair:
                merged = {**existing, **entry}
                group["interactions"][idx] = merged
                return False

        group["interactions"].append(entry)
        group["interaction_count"] += 1
        return True

    for item in interactions_list:
        if not item.get("_is_chain_link"):
            continue
        pw_name = item.get("step3_finalized_pathway")
        if not pw_name or pw_name not in pathway_groups:
            continue
        # Gate: only inject if this chain link has claims in the pathway
        db_id = item.get("_db_id")
        pw_db_id = _name_to_dbid.get(pw_name)
        if db_id and pw_db_id and (db_id, pw_db_id) not in _claim_pw_pairs:
            continue  # No claims for this chain link in this pathway
        src = item.get("source")
        tgt = item.get("target")
        if src and src != protein_symbol:
            pathway_groups[pw_name]["interactor_ids"].add(src)
        if tgt and tgt != protein_symbol:
            pathway_groups[pw_name]["interactor_ids"].add(tgt)
        _upsert_chain_pathway_interaction(pw_name, item)

    # ── S2b: chain links missing step3_finalized_pathway inherit from
    #    sibling indirect interactions sharing the same chain_id ────
    chain_id_to_pathway: dict = {}
    for item in interactions_list:
        cid = item.get("chain_id")
        pw = item.get("step3_finalized_pathway")
        if item.get("interaction_type") == "indirect" and cid and pw:
            chain_id_to_pathway.setdefault(cid, pw)

    for item in interactions_list:
        if not item.get("_is_chain_link"):
            continue
        if item.get("step3_finalized_pathway"):
            continue  # already handled in S2
        cid = item.get("chain_id")
        if not cid or cid not in chain_id_to_pathway:
            continue
        pw_name = chain_id_to_pathway[cid]
        if pw_name not in pathway_groups:
            continue
        # Gate: only inject if chain link has claims in the pathway
        db_id = item.get("_db_id")
        pw_db_id = _name_to_dbid.get(pw_name)
        if db_id and pw_db_id and (db_id, pw_db_id) not in _claim_pw_pairs:
            continue
        src = item.get("source")
        tgt = item.get("target")
        if src and src != protein_symbol:
            pathway_groups[pw_name]["interactor_ids"].add(src)
        if tgt and tgt != protein_symbol:
            pathway_groups[pw_name]["interactor_ids"].add(tgt)
        _upsert_chain_pathway_interaction(pw_name, item)

    # Convert sets to lists for JSON serialization
    pathways_list = []
    for pw_data in pathway_groups.values():
        pw_data["interactor_ids"] = sorted(list(pw_data["interactor_ids"]))
        pw_data["cross_query_interactor_ids"] = sorted(list(pw_data.get("cross_query_interactor_ids", set())))
        pathways_list.append(pw_data)

    pathways_list.sort(key=lambda p: (p["hierarchy_level"], -p["interaction_count"]))

    # Pull upstream regulators persisted by the ITER0 discovery pass
    # (stored on Protein.extra_data["upstream_of_main"]). Surfacing this
    # on snapshot_json lets the frontend render it in the query-node
    # modal so users can see what acts on their query protein without
    # having to crack the JSON payload. Silent when the list is absent
    # (older queries ran before ITER0 existed).
    _main_meta = (main_protein.extra_data or {}) if main_protein else {}
    _upstream_of_main = _main_meta.get("upstream_of_main")
    if not isinstance(_upstream_of_main, list):
        _upstream_of_main = []

    snapshot_json = {
        "main": protein_symbol,
        "proteins": sorted(list(protein_set)),
        "interactions": interactions_list,
        "pathways": pathways_list,
        "upstream_of_main": _upstream_of_main,
    }

    ctx_json = {
        "main": protein_symbol,
        "proteins": snapshot_json["proteins"],
        "interactions": interactions_list,
        "interactor_history": [p for p in snapshot_json["proteins"] if p != protein_symbol],
        "function_history": {},
        "function_batches": [],
        "upstream_of_main": _upstream_of_main,
    }

    result = {
        "snapshot_json": snapshot_json,
        "ctx_json": ctx_json
    }

    if main_protein.pipeline_status == "partial":
        result["_pipeline_status"] = "partial"
        result["_completed_phases"] = main_protein.last_pipeline_phase

    # Pipeline diagnostics — surface silent drops and depth pass-rate
    # to the frontend. Written by runner.py just before storage.save.
    # Best-effort read: missing/corrupt file is non-fatal.
    try:
        import json as _json
        import os as _os
        _diag_path = _os.path.join("Logs", protein_symbol, "pipeline_diagnostics.json")
        if _os.path.isfile(_diag_path):
            with open(_diag_path, "r", encoding="utf-8") as _diag_fp:
                _diag = _json.load(_diag_fp)
            result["_diagnostics"] = _diag
        # Quality report sits next to it; merge pass_rate into the
        # diagnostics block for one-stop frontend access.
        _qr_path = _os.path.join("Logs", protein_symbol, "quality_report.json")
        if _os.path.isfile(_qr_path):
            with open(_qr_path, "r", encoding="utf-8") as _qr_fp:
                _qr = _json.load(_qr_fp)
            result.setdefault("_diagnostics", {})["quality_report"] = _qr
    except Exception as _diag_exc:
        # Never block the API response over a diagnostics-read failure.
        import sys as _sys
        print(
            f"[WARN] data_builder: failed to attach diagnostics for "
            f"{protein_symbol}: {_diag_exc}",
            file=_sys.stderr,
        )

    # Schema version: SPA reads this and warns on mismatch with its own
    # EXPECTED_SCHEMA_VERSION constant. Bump whenever the snapshot/ctx
    # contract shifts in a way the frontend would care about.
    result["_schema_version"] = SCHEMA_VERSION

    return result


def _inject_cross_protein_chain_claims(
    main_symbol,
    interactions_list,
    interaction_pathway_names=None,
    claim_pw_ancestry=None,
):
    """Ensure all proteins in a chain see all chain-related claims.

    The injected claim dicts must match the shape produced by the main
    ``_serialize_claims`` closure — otherwise the frontend's pathway
    badge logic (which looks at ``_hierarchy`` and
    ``_interaction_pathways``) sees these injected claims as "other
    pathway" even when they belong to the current view.
    """
    main_protein = Protein.query.filter_by(symbol=main_symbol).first()
    if not main_protein:
        return

    # Find all chains involving main protein
    chains = IndirectChain.query.filter(
        IndirectChain.chain_proteins.contains([main_symbol])
    ).all()

    ix_pw_lookup = interaction_pathway_names or {}
    ancestry_lookup = claim_pw_ancestry or {}
    chain_claims_by_chain = {}
    chain_ids = [chain.id for chain in chains if chain.id is not None]
    if chain_ids:
        for claim in InteractionClaim.query.filter(InteractionClaim.chain_id.in_(chain_ids)).all():
            chain_claims_by_chain.setdefault(claim.chain_id, []).append(claim)

    for chain in chains:
        chain_claims = chain_claims_by_chain.get(chain.id, [])
        claims_by_interaction = {}
        for claim in chain_claims:
            claims_by_interaction.setdefault(claim.interaction_id, []).append(claim)

        for interaction_data in interactions_list:
            db_id = interaction_data.get("_db_id")
            if db_id and db_id in claims_by_interaction:
                scoped_claims = _claims_scoped_to_item(claims_by_interaction[db_id], interaction_data)
                existing_claim_names = {
                    c.get("function_name") or c.get("function")
                    for c in interaction_data.get("claims", [])
                }
                for claim in scoped_claims:
                    if claim.function_name in existing_claim_names:
                        continue
                    claim_dict = {
                        "id": claim.id,
                        "function_name": claim.function_name,
                        "arrow": normalize_arrow(claim.arrow, default="regulates"),
                        "interaction_effect": claim.interaction_effect,
                        "direction": semantic_claim_direction(claim.direction),
                        "mechanism": claim.mechanism,
                        "effect_description": claim.effect_description,
                        "biological_consequences": claim.biological_consequences or [],
                        "specific_effects": claim.specific_effects or [],
                        "evidence": claim.evidence or [],
                        "pmids": claim.pmids or [],
                        "pathway_name": claim.pathway_name,
                        "confidence": float(claim.confidence) if claim.confidence else None,
                        "function_context": claim.function_context,
                        "context_data": claim.context_data,
                        "chain_id": claim.chain_id,
                    }
                    if claim.pathway_id and claim.pathway_id in ancestry_lookup:
                        claim_dict["_hierarchy"] = ancestry_lookup[claim.pathway_id]
                    # P3.3: dropped `_interaction_pathways` attachment for
                    # the same reason as in `_serialize_claims` above —
                    # parent-interaction pathway set is not membership
                    # proof for an individual claim.
                    interaction_data.setdefault("claims", []).append(claim_dict)


def build_protein_detail_json(symbol: str) -> dict | None:
    """Get ALL interactions for a protein from the database (for modal detail view)."""
    protein = Protein.query.filter_by(symbol=symbol).first()
    if not protein:
        return None

    db_interactions = db.session.query(Interaction).options(
        joinedload(Interaction.protein_a),
        joinedload(Interaction.protein_b),
        joinedload(Interaction.linked_chain),
    ).filter(
        (Interaction.protein_a_id == protein.id) |
        (Interaction.protein_b_id == protein.id)
    ).all()

    if not db_interactions:
        return {"protein": symbol, "query_count": 0, "total_interactions": 0, "interactions": []}

    chain_memberships_index = _build_chain_membership_index(db_interactions)

    # Hoist chain→pathways query (mirrors build_full_json_from_db). Without
    # this, the per-row _chain_fields_for would run one InteractionClaim
    # query per interaction — N+1 across this protein's interactions.
    _all_chain_ids: set = set()
    for _memberships in chain_memberships_index.values():
        for _m in _memberships:
            if _m.chain_id is not None:
                _all_chain_ids.add(_m.chain_id)
    chain_pathways_index: dict = {}
    if _all_chain_ids:
        try:
            _rows = (
                InteractionClaim.query
                .filter(InteractionClaim.chain_id.in_(_all_chain_ids))
                .with_entities(InteractionClaim.chain_id, InteractionClaim.pathway_name)
                .distinct()
                .all()
            )
            for _cid, _pw in _rows:
                if _cid is None or not _pw:
                    continue
                chain_pathways_index.setdefault(_cid, set()).add(_pw)
        except Exception:
            chain_pathways_index = {}

    # Count distinct query contexts
    query_sources = set()
    interactions_list = []

    for interaction in db_interactions:
        if interaction.protein_a_id == protein.id:
            partner = interaction.protein_b
            needs_flip = False
        else:
            partner = interaction.protein_a
            needs_flip = True

        if interaction.discovered_in_query:
            query_sources.add(interaction.discovered_in_query)

        interaction_data = interaction.data.copy() if interaction.data else {}

        # Orient source/target relative to the clicked protein
        stored_direction = interaction.direction
        if needs_flip:
            if stored_direction == "a_to_b":
                final_direction = "primary_to_main"
            elif stored_direction == "b_to_a":
                final_direction = "main_to_primary"
            else:
                final_direction = stored_direction or "main_to_primary"
        else:
            if stored_direction == "a_to_b":
                final_direction = "main_to_primary"
            elif stored_direction == "b_to_a":
                final_direction = "primary_to_main"
            else:
                final_direction = stored_direction or "main_to_primary"

        if final_direction == "main_to_primary":
            interaction_data["source"] = symbol
            interaction_data["target"] = partner.symbol
        elif final_direction == "primary_to_main":
            interaction_data["source"] = partner.symbol
            interaction_data["target"] = symbol
        else:
            if symbol < partner.symbol:
                interaction_data["source"] = symbol
                interaction_data["target"] = partner.symbol
            else:
                interaction_data["source"] = partner.symbol
                interaction_data["target"] = symbol

        interaction_data["direction"] = final_direction

        # C2: arrow through the canonical primary_arrow property; emit
        # the full arrows dict so the modal can render multi-type arrows.
        if interaction_data.get("confidence") is None:
            interaction_data["confidence"] = float(interaction.confidence) if interaction.confidence else 0.5
        interaction_data["arrow"] = normalize_arrow(interaction.primary_arrow, default="binds")
        interaction_data["arrows"] = normalize_arrows_map(interaction.arrows)
        if interaction_data.get("functions") is None:
            interaction_data["functions"] = []
        else:
            interaction_data["functions"] = _normalize_function_payloads(interaction_data["functions"])
        if interaction_data.get("evidence") is None:
            interaction_data["evidence"] = []
        if interaction_data.get("pmids") is None:
            interaction_data["pmids"] = []

        if not interaction_data.get("interaction_effect"):
            interaction_data["interaction_effect"] = arrow_to_effect(interaction_data["arrow"])

        interaction_data["_db_id"] = interaction.id
        interaction_data["interaction_type"] = interaction.interaction_type or "direct"
        interaction_data["type"] = interaction_data["interaction_type"]
        # C1: all chain state through the canonical reader. No
        # interaction.mediator_chain / upstream_interactor / depth /
        # chain_context / chain_id reads past this line.
        interaction_data.update(_chain_fields_for(
            interaction,
            chain_pathways_index=chain_pathways_index,
            chain_memberships_index=chain_memberships_index,
        ))

        interactions_list.append(interaction_data)

    # Reconstruct chain link interactions for indirect mediator chains
    protein_set = {symbol}
    interactor_proteins = []
    for interaction in db_interactions:
        if interaction.protein_a_id == protein.id:
            partner = interaction.protein_b
        else:
            partner = interaction.protein_a
        protein_set.add(partner.symbol)
        if partner not in interactor_proteins:
            interactor_proteins.append(partner)

    _reconstruct_chain_links(
        protein.id,
        db_interactions,
        interactions_list,
        protein_set,
        interactor_proteins,
        chain_memberships_index=chain_memberships_index,
    )

    # Batch-load claims
    all_db_ids = [item["_db_id"] for item in interactions_list if item.get("_db_id")]
    claims_by_interaction = {}
    _detail_pw_ancestry: dict[int, list[str]] = {}  # pathway_id → [root, ..., leaf]

    if all_db_ids:
        all_claims = InteractionClaim.query.filter(
            InteractionClaim.interaction_id.in_(all_db_ids)
        ).all()
        for claim in all_claims:
            claims_by_interaction.setdefault(claim.interaction_id, []).append(claim)

        # Build pathway ancestry for hierarchy matching in modal
        claim_pw_ids = {c.pathway_id for c in all_claims if c.pathway_id}
        if claim_pw_ids:
            claim_pws = Pathway.query.filter(Pathway.id.in_(claim_pw_ids)).all()
            pw_by_id = {pw.id: pw for pw in claim_pws}
            ancestor_ids_needed = set()
            for pw in claim_pws:
                ancestor_ids_needed.update(pw.ancestor_ids or [])
            ancestor_ids_needed -= claim_pw_ids
            if ancestor_ids_needed:
                for apw in Pathway.query.filter(Pathway.id.in_(ancestor_ids_needed)).all():
                    pw_by_id[apw.id] = apw
            for pw in claim_pws:
                names = [pw_by_id[aid].name for aid in (pw.ancestor_ids or []) if aid in pw_by_id]
                names.append(pw.name)
                _detail_pw_ancestry[pw.id] = names

    for item in interactions_list:
        db_id = item.get("_db_id")
        if db_id:
            item["claims"] = [
                {
                    "id": c.id,
                    "function_name": c.function_name,
                    "arrow": normalize_arrow(c.arrow, default="regulates"),
                    "interaction_effect": c.interaction_effect,
                    "direction": semantic_claim_direction(c.direction),
                    "mechanism": c.mechanism,
                    "effect_description": c.effect_description,
                    "biological_consequences": c.biological_consequences or [],
                    "specific_effects": c.specific_effects or [],
                    "evidence": c.evidence or [],
                    "pmids": c.pmids or [],
                    "pathway_name": c.pathway_name,
                    "confidence": float(c.confidence) if c.confidence else None,
                    "function_context": c.function_context,
                    "context_data": c.context_data,
                    **( {"_hierarchy": _detail_pw_ancestry[c.pathway_id]}
                        if c.pathway_id and c.pathway_id in _detail_pw_ancestry else {}),
                }
                for c in _claims_scoped_to_item(claims_by_interaction.get(db_id, []), item)
            ]

    return {
        "protein": symbol,
        "query_count": len(query_sources),
        "total_interactions": len(interactions_list),
        "interactions": interactions_list,
    }


def build_expansion_json_from_db(protein_symbol: str, visible_proteins: list = None) -> dict:
    """Build expansion JSON with auto-cross-linking support."""
    result = build_full_json_from_db(protein_symbol)
    if not result:
        return None

    if not visible_proteins or not isinstance(visible_proteins, list):
        return result

    visible_proteins = [p for p in visible_proteins if p != protein_symbol]
    if not visible_proteins:
        return result

    snapshot = result["snapshot_json"]
    new_proteins = [
        p for p in snapshot["proteins"]
        if p != protein_symbol and p not in visible_proteins
    ]

    if not new_proteins:
        return result

    new_protein_objs = Protein.query.filter(Protein.symbol.in_(new_proteins)).all()
    visible_protein_objs = Protein.query.filter(Protein.symbol.in_(visible_proteins)).all()

    if not new_protein_objs or not visible_protein_objs:
        return result

    new_ids = [p.id for p in new_protein_objs]
    visible_ids = [p.id for p in visible_protein_objs]

    cross_link_interactions = db.session.query(Interaction).filter(
        db.or_(
            db.and_(
                Interaction.protein_a_id.in_(new_ids),
                Interaction.protein_b_id.in_(visible_ids)
            ),
            db.and_(
                Interaction.protein_a_id.in_(visible_ids),
                Interaction.protein_b_id.in_(new_ids)
            )
        )
    ).all()

    interactions_list = snapshot["interactions"]
    existing_ids = {
        f"{i.get('source', '')}-{i.get('target', '')}"
        for i in interactions_list
        if i.get('source') and i.get('target')
    }
    existing_ids.update({
        f"{i.get('target', '')}-{i.get('source', '')}"
        for i in interactions_list
        if i.get('source') and i.get('target')
    })

    for cross_ix in cross_link_interactions:
        cross_data = cross_ix.data.copy()
        protein_a = cross_ix.protein_a
        protein_b = cross_ix.protein_b

        cross_data["source"] = protein_a.symbol
        cross_data["target"] = protein_b.symbol
        cross_data["type"] = "cross_link"
        cross_data["direction"] = cross_ix.direction if cross_ix.direction else "main_to_primary"
        # H2: canonical IndirectChain FK for cross-link rows too.
        cross_data["chain_id"] = cross_ix.chain_id

        if cross_data.get("confidence") is None:
            cross_data["confidence"] = 0.5
        # C2: primary_arrow + arrows JSONB for cross-link rows too.
        cross_data["arrow"] = normalize_arrow(cross_ix.primary_arrow, default="binds")
        cross_data["arrows"] = normalize_arrows_map(cross_ix.arrows)
        if cross_data.get("functions") is None:
            cross_data["functions"] = []
        else:
            cross_data["functions"] = _normalize_function_payloads(cross_data["functions"])
        if cross_data.get("evidence") is None:
            cross_data["evidence"] = []
        if cross_data.get("pmids") is None:
            cross_data["pmids"] = []
        if cross_data.get("intent") is None:
            cross_data["intent"] = "binding"

        link_id = f"{cross_data['source']}-{cross_data['target']}"
        rev_link_id = f"{cross_data['target']}-{cross_data['source']}"

        if link_id not in existing_ids and rev_link_id not in existing_ids:
            interactions_list.append(cross_data)
            existing_ids.add(link_id)

            if protein_a.symbol not in snapshot["proteins"]:
                snapshot["proteins"].append(protein_a.symbol)
            if protein_b.symbol not in snapshot["proteins"]:
                snapshot["proteins"].append(protein_b.symbol)

    snapshot["proteins"] = sorted(snapshot["proteins"])
    result["ctx_json"]["proteins"] = snapshot["proteins"]
    result["ctx_json"]["interactions"] = interactions_list
    result["ctx_json"]["interactor_history"] = [p for p in snapshot["proteins"] if p != protein_symbol]

    return result
