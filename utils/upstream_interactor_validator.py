"""Validation guards for LLM-emitted upstream hints and mediator chains.

Two orthogonal checks over every indirect-interactor structure:

1. **upstream-hint validation** — the LLM can tag an indirect interactor
   with ``upstream_interactor="SOME_PROTEIN"``. If that protein isn't in
   the known interactor set for this query, promoting it creates an
   orphan chain (the mediator never lands as its own row; the chain
   dangles). This module cross-checks the hint against a known-set.

2. **cycle + self-loop detection** — an LLM-emitted
   ``mediator_chain=[PRKN, VDAC1, PRKN]`` has a cycle. The current
   pipeline accepts it verbatim. Cycles break chain rendering and
   corrupt the chain-link edge set. This module detects them.

Both checks return verdict dataclasses so callers can choose to log,
reject, or auto-repair. Observe-only by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping


@dataclass
class UpstreamVerdict:
    """Verdict for one indirect-interactor's upstream hint.

    ``reason`` is one of:
      • ``"valid"``               — upstream is in the known set
      • ``"self-reference"``       — upstream == interactor (self-loop hint)
      • ``"orphan"``               — upstream not in known set (would dangle)
      • ``"no-upstream"``          — field is empty; nothing to check
      • ``"query-as-upstream"``    — upstream == query (degenerate; query-as-endpoint chain is fine but the flag is redundant)
    """

    interactor: str
    upstream: str
    reason: str
    known_aliases: frozenset[str] = field(default_factory=frozenset)


@dataclass
class ChainShapeVerdict:
    """Verdict for one indirect-interactor's mediator_chain shape.

    ``cycles`` is a list of the repeat-proteins found (each appears >1
    times in the chain). ``self_loops`` is the list of adjacent-pair
    self-loops (``[..., X, X, ...]``). ``reason`` is ``"valid"`` when
    both lists are empty.
    """

    interactor: str
    chain: list[str]
    cycles: list[str]
    self_loops: list[str]
    reason: str


def _normalize(symbol: str) -> str:
    return (symbol or "").strip().upper()


def validate_upstream_hint(
    interactor: Mapping,
    *,
    main_symbol: str,
    known_interactors: Iterable[str],
    aliases: Mapping[str, Iterable[str]] | None = None,
) -> UpstreamVerdict:
    """Check that an indirect interactor's upstream is a real interactor.

    ``aliases`` maps canonical symbol → list of aliases. The check passes
    when the upstream hint matches ANY known interactor or any of that
    interactor's aliases. Case-insensitive.
    """
    primary = _normalize(str(interactor.get("primary") or ""))
    upstream = _normalize(str(interactor.get("upstream_interactor") or ""))

    if not upstream:
        return UpstreamVerdict(primary, upstream, "no-upstream")

    if upstream == primary:
        return UpstreamVerdict(primary, upstream, "self-reference")

    if upstream == _normalize(main_symbol):
        # Query as upstream is legitimate when the chain is
        # [query, interactor] — mediator_chain=[query]. We flag it as
        # informational only; caller can decide whether to accept.
        return UpstreamVerdict(primary, upstream, "query-as-upstream")

    # Build the full match set: known symbols + their aliases.
    match_set: set[str] = set()
    for sym in known_interactors:
        sym_norm = _normalize(sym)
        if not sym_norm:
            continue
        match_set.add(sym_norm)
        if aliases and sym in aliases:
            for alias in aliases[sym]:
                match_set.add(_normalize(alias))

    if upstream in match_set:
        return UpstreamVerdict(
            primary, upstream, "valid", frozenset(match_set),
        )
    return UpstreamVerdict(
        primary, upstream, "orphan", frozenset(match_set),
    )


def validate_chain_shape(interactor: Mapping) -> ChainShapeVerdict:
    """Detect cycles, self-loops, and repeat-proteins in a mediator chain.

    Does NOT auto-fix — cycles in biological cascades are almost never
    legitimate, but some feedback loops are described that way. Caller
    decides based on context.
    """
    primary = _normalize(str(interactor.get("primary") or ""))
    raw_chain = interactor.get("mediator_chain") or []
    chain = [_normalize(str(x)) for x in raw_chain if isinstance(x, (str, bytes))]

    # Repeated proteins anywhere in the chain.
    seen: dict[str, int] = {}
    for sym in chain:
        seen[sym] = seen.get(sym, 0) + 1
    cycles = sorted([s for s, n in seen.items() if n > 1 and s])

    # Adjacent self-loops: [..., X, X, ...]
    self_loops = []
    for i in range(len(chain) - 1):
        if chain[i] and chain[i] == chain[i + 1]:
            self_loops.append(chain[i])
    self_loops = sorted(set(self_loops))

    # Also: primary itself appearing inside its own mediator_chain.
    if primary and primary in chain:
        if primary not in cycles:
            cycles.append(primary)

    if cycles or self_loops:
        return ChainShapeVerdict(
            primary, chain, cycles, self_loops,
            "cycle" if cycles else "self-loop",
        )
    return ChainShapeVerdict(primary, chain, cycles, self_loops, "valid")


def validate_all_indirect_interactors(
    interactors: list[dict],
    *,
    main_symbol: str,
    aliases: Mapping[str, Iterable[str]] | None = None,
) -> tuple[list[UpstreamVerdict], list[ChainShapeVerdict]]:
    """Run both validations over an interactor list.

    Returns ``(upstream_verdicts, shape_verdicts)``. Caller iterates and
    handles each verdict — log orphans, drop cycles, etc.

    The "known set" for upstream validation is computed from the
    ``primary`` field of every passed interactor — so an interactor
    whose upstream hint points at another interactor in the same batch
    counts as valid. This matches the runner's promotion semantics.
    """
    known = [
        str(i.get("primary") or "")
        for i in interactors
        if isinstance(i, dict) and i.get("primary")
    ]

    upstream_verdicts: list[UpstreamVerdict] = []
    shape_verdicts: list[ChainShapeVerdict] = []

    for interactor in interactors:
        if not isinstance(interactor, dict):
            continue
        if interactor.get("interaction_type") != "indirect":
            continue
        upstream_verdicts.append(
            validate_upstream_hint(
                interactor,
                main_symbol=main_symbol,
                known_interactors=known,
                aliases=aliases,
            )
        )
        shape_verdicts.append(validate_chain_shape(interactor))

    return upstream_verdicts, shape_verdicts
