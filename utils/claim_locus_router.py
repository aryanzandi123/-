"""Claim locus router — assigns LLM-emitted claims to their natural DB row.

The LLM generates ``chain_link_functions[pair]`` as per-hop biology for the
(src, tgt) pair. The prompt says "do not mention the query protein", but LLMs
are inconsistent. When a claim's text actually describes the full cascade
(query → ... → target) rather than the pair in isolation, it gets stored on
the wrong Interaction row — the user sees ATXN3-about-VDAC1 text on the
PRKN↔VDAC1 modal, which is a category error.

The router fixes this at ingestion time. It reads every claim's text, detects
which chain-context proteins are mentioned, and routes the claim to its
natural home:

  • hop            — claim mentions only the src/tgt pair.
  • parent_indirect — claim mentions the query (and usually a mediator).
  • drop           — claim mentions none of the pair's proteins.

One deterministic pass over each hop's claims, independent of LLM slot choice.
Idempotent: re-running on already-routed data leaves it unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


# Claim text fields we inspect. Ordered by salience: function name is often
# the clearest signal, but the LLM sometimes writes a clean name and then
# pollutes the body paragraphs — so we scan every field.
_TEXT_FIELDS = (
    "function",
    "cellular_process",
    "effect_description",
    "biological_consequence",
    "specific_effects",
)


@dataclass
class LocusDecision:
    """Result of routing a single claim.

    ``locus`` is one of:
      ``"hop"``              keep on the (src, tgt) hop interaction row.
      ``"parent_indirect"``  move to the parent indirect (query↔target) row.
      ``"drop"``             claim discusses neither the hop nor the query.

    ``mentioned`` is the subset of known chain proteins found in the text.
    ``reason`` is a short human-readable tag for structured logging.
    """

    locus: str
    reason: str
    mentioned: frozenset[str] = field(default_factory=frozenset)


def _adjacent_chain_proteins(
    chain_proteins: Sequence[str],
    hop_src: str,
    hop_tgt: str,
) -> set[str]:
    """Return the set of proteins immediately adjacent to the hop in the chain.

    For chain ``[Q, A, B, C, D, T]`` and hop ``B->C``, this returns ``{A, D}``.
    These are the proteins immediately upstream and downstream of the hop —
    a hop's claim that mentions them is talking about its inputs/outputs and
    is valid hop biology, not scope creep.

    Symbols are upper-cased before lookup. If either endpoint isn't found in
    the chain (mismatched casing, alias) the function returns an empty set
    so the caller falls through to the conservative default (treat all
    other-mediator mentions as non-adjacent).
    """
    chain_upper = [str(p).upper() for p in chain_proteins if p]
    src = hop_src.upper()
    tgt = hop_tgt.upper()
    try:
        i_src = chain_upper.index(src)
    except ValueError:
        return set()
    try:
        i_tgt = chain_upper.index(tgt)
    except ValueError:
        return set()
    lo, hi = (i_src, i_tgt) if i_src < i_tgt else (i_tgt, i_src)
    adjacent: set[str] = set()
    if lo > 0:
        adjacent.add(chain_upper[lo - 1])
    if hi + 1 < len(chain_upper):
        adjacent.add(chain_upper[hi + 1])
    return adjacent


def _load_default_alias_map() -> dict[str, list[str]]:
    """Load the hardcoded alias seed map without triggering a DB connection.

    ``utils.protein_aliases.HARDCODED_ALIAS_SEEDS`` is a module-level dict;
    importing it doesn't open a Flask app context. If that module can't be
    imported (circular / missing), we silently fall back to an empty map so
    the router still runs — symbol-only detection is strictly less accurate
    but not wrong.
    """
    try:
        from utils.protein_aliases import HARDCODED_ALIAS_SEEDS
        return dict(HARDCODED_ALIAS_SEEDS)
    except Exception:
        return {}


def _compile_mention_patterns(
    symbols: Iterable[str],
    alias_map: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, re.Pattern]:
    """Build case-insensitive whole-word regex patterns per canonical symbol.

    Each canonical symbol gets ONE pattern that alternates between the
    canonical form and any curated aliases. So ``PRKN`` matches ``PRKN``,
    ``Parkin``, and anything else seeded for that gene. ``TP53`` matches
    ``TP53``, ``p53``, ``P53``, etc.

    Whole-word means "not preceded or followed by an alphanumeric character".
    Prevents false positives on substrings: the symbol ``ATM`` will NOT match
    inside ``atmosphere`` or ``ATMP``, but WILL match ``ATM-mediated``.
    """
    if alias_map is None:
        alias_map = _load_default_alias_map()

    patterns: dict[str, re.Pattern] = {}
    for sym in symbols:
        if not sym or not isinstance(sym, str):
            continue
        upper = sym.upper()
        if upper in patterns:
            continue
        forms = [upper]
        for alias in alias_map.get(upper, ()):
            alias = (alias or "").strip()
            if alias and alias.upper() not in (f.upper() for f in forms):
                forms.append(alias)
        alt = "|".join(re.escape(f) for f in forms)
        patterns[upper] = re.compile(
            rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
    return patterns


def _extract_text(value: object) -> str:
    """Flatten a claim field (str / list / dict / None) into one string.

    Lists and dicts are walked recursively; non-string leaves are stringified.
    Empty inputs yield an empty string — safe to pass to a regex search.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_extract_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_extract_text(v) for v in value.values())
    return str(value)


def _mentioned_symbols(
    claim: Mapping,
    patterns: Mapping[str, re.Pattern],
) -> frozenset[str]:
    """Return the set of symbols (by canonical upper-case) mentioned in claim text.

    Only scans the configured text fields — metadata like ``pathway`` or
    ``confidence`` is ignored so a pathway named after a protein doesn't
    trigger a false mention.
    """
    blob = " ".join(_extract_text(claim.get(field)) for field in _TEXT_FIELDS)
    if not blob:
        return frozenset()
    return frozenset(sym for sym, pat in patterns.items() if pat.search(blob))


def classify_claim_locus(
    claim: Mapping,
    *,
    main_symbol: str,
    hop_src: str,
    hop_tgt: str,
    chain_proteins: Sequence[str],
    alias_map: Mapping[str, Iterable[str]] | None = None,
) -> LocusDecision:
    """Decide which interaction row a per-hop-candidate claim belongs to.

    See module docstring for routing rules. Special cases:
      • If the hop itself contains the query (hop endpoint == main), the
        router is effectively a no-op — legitimate direct claim between the
        query and its partner. Returns ``locus="hop"``.
      • If ``main_symbol`` or ``chain_proteins`` is falsy, the router can't
        reason about the context and returns ``locus="hop"`` to preserve
        LLM intent.
    """
    if not main_symbol or not hop_src or not hop_tgt:
        return LocusDecision("hop", "missing-context")

    main_upper = main_symbol.upper()
    src_upper = hop_src.upper()
    tgt_upper = hop_tgt.upper()
    hop_pair = {src_upper, tgt_upper}

    if main_upper in hop_pair:
        return LocusDecision("hop", "query-is-endpoint")

    known = [s for s in chain_proteins if s]
    if not known:
        return LocusDecision("hop", "no-chain-context")

    symbols = set(known) | {main_upper, src_upper, tgt_upper}
    patterns = _compile_mention_patterns(symbols, alias_map)
    mentioned = _mentioned_symbols(claim, patterns)

    if not mentioned:
        return LocusDecision("hop", "no-mentions", mentioned)

    mentions_query = main_upper in mentioned
    mentions_hop = bool(mentioned & hop_pair)
    other_mediators = (mentioned - hop_pair) - {main_upper}

    # Cascade-level: claim mentions the query AND something else in the
    # chain (hop endpoints or other mediators). Belongs on the parent
    # indirect row, not on this hop.
    if mentions_query:
        if mentions_hop:
            return LocusDecision("parent_indirect", "mentions-query-and-hop", mentioned)
        if other_mediators:
            return LocusDecision("parent_indirect", "mentions-query-and-mediators", mentioned)
        # Query alone with no hop or mediator context — meaningless on
        # the hop row.
        return LocusDecision("drop", "mentions-query-only", mentioned)

    # No query mention. Must mention at least one hop endpoint to stay
    # on this hop row.
    if not mentions_hop:
        return LocusDecision("drop", "no-hop-mention", mentioned)

    # Adjacency-aware re-routing (P1.4). A hop's claim legitimately
    # mentions adjacent chain mediators — the protein immediately
    # upstream of the hop's source feeds into it, the protein immediately
    # downstream of the hop's target receives its output. Mentioning
    # those is valid hop biology, not scope creep.
    #
    # Reroute to parent only when ≥2 NON-adjacent mediators are mentioned,
    # which is the signature of cascade-level prose pretending to be
    # hop-level.
    if other_mediators:
        adjacent = _adjacent_chain_proteins(known, src_upper, tgt_upper)
        non_adjacent = other_mediators - adjacent
        if len(non_adjacent) >= 2:
            return LocusDecision("parent_indirect", "cross-hop-mediators", mentioned)
        if non_adjacent:
            return LocusDecision("hop", "peripheral-mediator-ok", mentioned)
        return LocusDecision("hop", "adjacent-mediators-ok", mentioned)

    return LocusDecision("hop", "pair-only", mentioned)


@dataclass
class RoutingResult:
    """Output of ``route_chain_link_claims`` — one bucket per locus.

    ``kept`` is the list of claims that should be written to the hop row.
    ``rerouted`` claims get merged into the parent indirect's functions.
    ``dropped`` claims are neither persisted on the hop nor the parent; they
    are returned for structured logging.
    """

    kept: list[dict] = field(default_factory=list)
    rerouted: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)


# Canonical function_context values expected by the DB (see models.py CHECK).
_FUNCTION_CONTEXT_DIRECT = "direct"
_FUNCTION_CONTEXT_NET = "net"
_FUNCTION_CONTEXT_CHAIN_DERIVED = "chain_derived"
_FUNCTION_CONTEXT_MIXED = "mixed"


def derive_function_context(
    decision: LocusDecision,
    claim: Mapping,
) -> str:
    """Pick the correct function_context label for a routed claim.

    The LLM's own ``function_context`` (if any) is advisory — what the claim
    actually describes, per the router's decision, wins:

      • kept on hop         → 'direct'   (pair-specific binary interaction)
      • rerouted to parent  → 'net'      (cascade-level; query → ... → target)
      • dropped             → discarded; label irrelevant

    CD2: when the LLM previously stamped a claim as ``mixed`` (it carried
    both direct-pair AND cascade content), the router would drop that
    signal by overwriting with 'direct' or 'net'. Now: if the LLM already
    said 'mixed', preserve it — the schema explicitly supports mixed as
    the "cross-context conflict" label. We only override when the LLM
    said something other than 'mixed'.
    """
    llm_label = str(claim.get("function_context") or "").strip().lower()
    if llm_label == _FUNCTION_CONTEXT_MIXED:
        return _FUNCTION_CONTEXT_MIXED
    if decision.locus == "hop":
        return _FUNCTION_CONTEXT_DIRECT
    if decision.locus == "parent_indirect":
        return _FUNCTION_CONTEXT_NET
    return llm_label or _FUNCTION_CONTEXT_CHAIN_DERIVED


def route_chain_link_claims(
    chain_link_funcs: Sequence[Mapping],
    *,
    main_symbol: str,
    hop_src: str,
    hop_tgt: str,
    chain_proteins: Sequence[str],
    alias_map: Mapping[str, Iterable[str]] | None = None,
) -> RoutingResult:
    """Route every LLM-emitted per-hop claim to its natural locus.

    Annotates each claim with ``_router_reason`` and ``_router_mentioned``
    so downstream logging / display can explain why a decision was made.
    Rerouted claims also get ``_rerouted_from_hop = "<src>-><tgt>"`` so the
    parent indirect row's merge step can detect duplicates.

    Zero-skip invariant (Atom E): when the caller passed ≥1 claim in
    ``chain_link_funcs`` but classification would leave ``result.kept``
    empty (every claim rerouted or dropped), we synthesize a router-stub
    claim so the hop row is never empty. The synthetic claim carries
    ``_synthetic_from_router=True`` and explains the routing outcome so
    the modal can render a helpful explanation instead of falling
    through to parent-fallback.
    """
    result = RoutingResult()
    for raw in chain_link_funcs:
        if not isinstance(raw, dict):
            result.kept.append(raw)  # preserve unknown shapes untouched
            continue
        decision = classify_claim_locus(
            raw,
            main_symbol=main_symbol,
            hop_src=hop_src,
            hop_tgt=hop_tgt,
            chain_proteins=chain_proteins,
            alias_map=alias_map,
        )
        tagged = {
            **raw,
            "_router_reason": decision.reason,
            "_router_mentioned": sorted(decision.mentioned),
            "function_context": derive_function_context(decision, raw),
        }
        if decision.locus == "hop":
            result.kept.append(tagged)
        elif decision.locus == "parent_indirect":
            tagged["_rerouted_from_hop"] = f"{hop_src}->{hop_tgt}"
            result.rerouted.append(tagged)
        else:
            result.dropped.append(tagged)

    # Zero-skip invariant: if every claim got routed away from the hop,
    # synthesize a stub so the hop row still has something to display.
    if chain_link_funcs and not result.kept:
        rerouted_count = len(result.rerouted)
        dropped_count = len(result.dropped)
        outcome_bits = []
        if rerouted_count:
            outcome_bits.append(
                f"{rerouted_count} rerouted to parent indirect"
            )
        if dropped_count:
            outcome_bits.append(f"{dropped_count} dropped")
        outcome_text = "; ".join(outcome_bits) or "no hop-compatible claims"
        stub = {
            "function": (
                f"Pair-specific biology pending manual curation "
                f"({hop_src}->{hop_tgt})"
            ),
            "function_context": _FUNCTION_CONTEXT_CHAIN_DERIVED,
            "arrow": "regulates",
            "cellular_process": (
                f"All LLM-generated claims for the {hop_src}->{hop_tgt} hop "
                f"in this cascade were routed off the hop row ({outcome_text}) "
                "because their prose mentioned the query protein or did not "
                "reference the hop's endpoints. The hop is still present in "
                "the cascade topology; pair-specific mechanistic detail is "
                "pending manual curation."
            ),
            "effect_description": (
                f"Hop retained in cascade; pair-scoped biochemistry not "
                f"surfaced to the hop row by the Locus Router."
            ),
            "biological_consequence": [],
            "specific_effects": [],
            "evidence": [],
            "pathway": "",
            "_synthetic_from_router": True,
            "_router_reason": "zero-skip-stub",
            "_router_mentioned": [],
            "_router_outcome_summary": outcome_text,
        }
        result.kept.append(stub)
    return result
