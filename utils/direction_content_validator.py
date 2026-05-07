"""Direction-vs-text consistency validator.

Each function has an ``interaction_direction`` field: either
``main_to_primary`` (query acts on interactor) or ``primary_to_main``
(interactor acts on query). The prompt asks the LLM to set this based on
"who acts on whom" in the described mechanism. In practice the LLM
regularly sets it randomly or flips it — especially for stabilizing /
protecting / chaperoning interactions where the biological "agent" is
the partner protein, not the query.

This validator extracts the subject → verb → object pattern from each
claim's prose and cross-checks it against the declared direction. Shape
matches ``arrow_content_validator`` and ``claim_locus_router``: LLM
structure is advisory, prose is authoritative.

Observe-only by default. Flip via ``DIRECTION_AUTO_CORRECT=true``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping


# Verbs that imply "X acts on Y" where the subject is the agent.
# Used to score each direction hypothesis.
_AGENT_VERBS: tuple[re.Pattern, ...] = (
    re.compile(
        r"\b(phosphorylat\w*|ubiquitinat\w*|deubiquitinat\w*|acetylat\w*|"
        r"deacetylat\w*|methylat\w*|demethylat\w*|sumoylat\w*|"
        r"cleav\w*|processes?|processed|processes|"
        r"stabiliz\w*|destabiliz\w*|protect\w*|shield\w*|chaperones?|chaperoned|"
        r"activat\w*|inhibit\w*|suppress\w*|induc\w*|trigger\w*|"
        r"recruit\w*|scaffold\w*|degrad\w*|targets?|targeted|"
        r"binds?|binding|bound|sequester\w*|relocat\w*|translocat\w*|"
        r"regulat\w*|modulat\w*|controls?|controlled|modif\w+|modif\w+s)\b",
        re.IGNORECASE,
    ),
)


@dataclass
class DirectionVerdict:
    """Outcome of inspecting one function's direction vs its prose.

    ``implied`` is ``"main_to_primary"`` / ``"primary_to_main"`` / ``""``
    when no direction could be derived. ``agree`` is True when they match
    or no implication was derivable.
    """

    declared: str
    implied: str
    agree: bool
    reason: str
    main_as_subject_hits: int = 0
    primary_as_subject_hits: int = 0


def _compile_subject_patterns(symbol: str) -> tuple[re.Pattern, re.Pattern]:
    """Return (subject_pattern, object_pattern) for a protein symbol.

    ``subject_pattern`` matches ``<symbol> <verb>`` constructions — the
    symbol appears before an agent verb within a short window.
    ``object_pattern`` matches ``<verb> ... <symbol>`` — the symbol is the
    object of an action. Whole-word on both sides to prevent substring
    false positives.
    """
    escaped = re.escape(symbol)
    # Symbol as subject: symbol followed within ~40 chars by an agent verb.
    subject = re.compile(
        rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
        r"[^.!?]{0,60}?"
        r"\b(?:phosphorylat\w*|ubiquitinat\w*|deubiquitinat\w*|acetylat\w*|"
        r"deacetylat\w*|cleav\w*|processes?|stabiliz\w*|destabiliz\w*|"
        r"protect\w*|shield\w*|chaperones?|activat\w*|inhibit\w*|"
        r"suppress\w*|induc\w*|trigger\w*|recruit\w*|scaffold\w*|"
        r"degrad\w*|binds?|binding|bound|sequester\w*|relocat\w*|"
        r"translocat\w*|regulat\w*|modulat\w*|controls?|modif\w+|targets?)\b",
        re.IGNORECASE,
    )
    # Symbol as object: agent verb followed within ~40 chars by the symbol.
    obj = re.compile(
        r"\b(?:phosphorylat\w*|ubiquitinat\w*|deubiquitinat\w*|acetylat\w*|"
        r"deacetylat\w*|cleav\w*|processes?|stabiliz\w*|destabiliz\w*|"
        r"protect\w*|shield\w*|chaperones?|activat\w*|inhibit\w*|"
        r"suppress\w*|induc\w*|trigger\w*|recruit\w*|scaffold\w*|"
        r"degrad\w*|binds?|binding|bound|sequester\w*|relocat\w*|"
        r"translocat\w*|regulat\w*|modulat\w*|controls?|modif\w+|targets?)\b"
        r"[^.!?]{0,60}?"
        rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    return subject, obj


def _flatten_text(claim: Mapping) -> str:
    chunks: list[str] = []
    for field_name in ("cellular_process", "effect_description",
                       "biological_consequence", "specific_effects"):
        value = claim.get(field_name)
        if value is None:
            continue
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, (list, tuple)):
            chunks.extend(str(v) for v in value if v)
        elif isinstance(value, dict):
            chunks.extend(str(v) for v in value.values() if v)
    return " ".join(chunks)


def classify_direction(
    claim: Mapping,
    *,
    main_symbol: str,
    partner_symbol: str,
    aliases_for_main: Iterable[str] = (),
    aliases_for_partner: Iterable[str] = (),
) -> DirectionVerdict:
    """Produce a direction verdict by counting who-acts-on-whom patterns.

    Strategy: for each protein, count how often it appears as the SUBJECT
    of an agent verb vs as the OBJECT. The one with more subject-hits is
    "the agent". Direction follows:
      • main as agent  → ``main_to_primary``
      • partner as agent → ``primary_to_main``
    If both are zero or tied, verdict is "no-implied" (agree=True, can't
    refute the LLM).
    """
    declared = str(claim.get("interaction_direction") or "").strip()

    if not main_symbol or not partner_symbol:
        return DirectionVerdict(declared, "", True, "missing-context")

    text = _flatten_text(claim)
    if not text:
        return DirectionVerdict(declared, "", True, "no-text")

    # Count subject vs object hits for main, using aliases.
    main_subject_hits = 0
    main_object_hits = 0
    for sym in (main_symbol, *aliases_for_main):
        sub_pat, obj_pat = _compile_subject_patterns(sym)
        main_subject_hits += len(sub_pat.findall(text))
        main_object_hits += len(obj_pat.findall(text))

    partner_subject_hits = 0
    partner_object_hits = 0
    for sym in (partner_symbol, *aliases_for_partner):
        sub_pat, obj_pat = _compile_subject_patterns(sym)
        partner_subject_hits += len(sub_pat.findall(text))
        partner_object_hits += len(obj_pat.findall(text))

    # Can't decide if neither protein is an obvious agent.
    if main_subject_hits == 0 and partner_subject_hits == 0:
        return DirectionVerdict(
            declared, "", True, "no-subject-verb-pattern",
            main_as_subject_hits=main_subject_hits,
            primary_as_subject_hits=partner_subject_hits,
        )

    # The one with more subject-hits is the agent.
    if main_subject_hits > partner_subject_hits:
        implied = "main_to_primary"
    elif partner_subject_hits > main_subject_hits:
        implied = "primary_to_main"
    else:
        return DirectionVerdict(
            declared, "", True, "tied-subject-hits",
            main_as_subject_hits=main_subject_hits,
            primary_as_subject_hits=partner_subject_hits,
        )

    if not declared:
        return DirectionVerdict(
            declared, implied, implied == "", "no-declared",
            main_as_subject_hits=main_subject_hits,
            primary_as_subject_hits=partner_subject_hits,
        )

    if declared == implied:
        return DirectionVerdict(
            declared, implied, True, "agree",
            main_as_subject_hits=main_subject_hits,
            primary_as_subject_hits=partner_subject_hits,
        )
    return DirectionVerdict(
        declared, implied, False, "mismatch",
        main_as_subject_hits=main_subject_hits,
        primary_as_subject_hits=partner_subject_hits,
    )


def validate_directions(
    claims: list[dict],
    *,
    main_symbol: str,
    partner_symbol: str,
    aliases_for_main: Iterable[str] = (),
    aliases_for_partner: Iterable[str] = (),
    auto_correct: bool = False,
) -> list[DirectionVerdict]:
    """Inspect every claim's direction vs prose. Optionally overwrite."""
    verdicts: list[DirectionVerdict] = []
    for claim in claims:
        verdict = classify_direction(
            claim,
            main_symbol=main_symbol,
            partner_symbol=partner_symbol,
            aliases_for_main=aliases_for_main,
            aliases_for_partner=aliases_for_partner,
        )
        verdicts.append(verdict)
        if auto_correct and verdict.reason == "mismatch":
            original = claim.get("interaction_direction")
            claim["interaction_direction"] = verdict.implied
            claim["_direction_corrected_from"] = original
            claim["_direction_correction_evidence"] = {
                "main_subject_hits": verdict.main_as_subject_hits,
                "partner_subject_hits": verdict.primary_as_subject_hits,
            }
    return verdicts
