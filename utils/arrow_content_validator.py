"""Arrow-vs-text consistency validator.

The LLM assigns each function an ``arrow`` (activates / inhibits / binds /
regulates). The prompt asks for the arrow to reflect the function's
biological effect. But the LLM regularly writes prose that says "X inhibits Y"
and then emits ``arrow: "activates"`` on the same function, or vice versa.
Nothing downstream re-reads the prose to verify.

This validator does. It extracts the dominant action verb from the function's
text fields, maps each verb family to its implied arrow, and flags (or
auto-corrects) mismatches. Shape matches the Claim Locus Router: LLM
structural claims are advisory, text content is authoritative.

Auto-correction is OPT-IN via ``auto_correct=True``. Default is observe +
log so operators can review mismatch frequency before flipping the switch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping


# Verb-family → arrow. Each tuple is (regex, implied_arrow). Ordered so
# that more specific families match first (activates/inhibits before the
# binding and "regulates" families). Word boundaries prevent substring
# matches.
_VERB_FAMILIES: tuple[tuple[re.Pattern, str], ...] = (
    # Activating verbs: phosphorylates, ubiquitinates (K48 → degradation is
    # still a positive activation OF the ubiquitination function), stimulates,
    # promotes, enhances, upregulates, induces, drives, triggers, recruits.
    (re.compile(
        r"\b(activat\w*|stimulat\w*|promot\w*|enhanc\w*|upregulat\w*|"
        r"induc\w*|triggers?|triggered|recruit\w*|phosphorylat\w*|"
        r"deubiquitinat\w*|acetylat\w*|deacetylat\w*|stabiliz\w*|"
        r"protect\w*|shield\w*|chaperones?|chaperoned|drives?|driven|"
        r"agonizes?|agonized)\b",
        re.IGNORECASE,
    ), "activates"),
    # Inhibiting verbs: inhibits, suppresses, blocks, antagonizes, represses,
    # downregulates, degrades, cleaves, destabilizes, prevents.
    (re.compile(
        r"\b(inhibit\w*|suppress\w*|block\w*|antagoniz\w*|repress\w*|"
        r"downregulat\w*|degrad\w*|cleav\w*|destabiliz\w*|prevent\w*|"
        r"abrogat\w*|abolish\w*|attenuat\w*|disrupt\w*)\b",
        re.IGNORECASE,
    ), "inhibits"),
    # Binding verbs (no directional sign): binds, interacts, associates,
    # forms complex, docks, scaffolds.
    (re.compile(
        r"\b(binds?|binding|bound|interact\w*|associat\w*|forms?\s+complex|"
        r"docks?|scaffolds?|scaffolded)\b",
        re.IGNORECASE,
    ), "binds"),
    # CD1: noncommittal regulatory verbs. Only matches when no directional
    # verb was found first (the ordering above ensures that). These verbs
    # indicate "regulates" — the LLM's hedge label, now actually derivable
    # from prose.
    (re.compile(
        r"\b(regulat\w*|modulat\w*|affect\w*|influenc\w*|controls?|controlled|"
        r"governs?|governed|fine[- ]tun\w*|tunes?|tuned)\b",
        re.IGNORECASE,
    ), "regulates"),
)


_TEXT_FIELDS = (
    "function",
    "cellular_process",
    "effect_description",
    "biological_consequence",
    "specific_effects",
)


@dataclass
class ArrowVerdict:
    """Outcome of inspecting one function's arrow vs its text.

    ``implied`` is the arrow extracted from the claim's prose (may be ``""``
    if no verb matched). ``declared`` is whatever the LLM emitted. ``agree``
    is True when they match or when no implication could be derived.
    ``reason`` is a short tag for logging.
    """

    declared: str
    implied: str
    agree: bool
    reason: str
    verb_evidence: list[str] = field(default_factory=list)


def _flatten_text(claim: Mapping) -> str:
    """Gather the inspectable text fields of a claim into one string."""
    chunks: list[str] = []
    for field_name in _TEXT_FIELDS:
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


def _first_verb_match(blob: str) -> tuple[str, list[str]]:
    """Return the dominant (first-matching) verb-family arrow + verb evidence.

    Scans the prose for each verb family in priority order. Returns ``("", [])``
    when no family matches. The priority order (activate / inhibit / bind)
    biases toward directional verbs — we only fall back to 'binds' when no
    directional language is present, because most prose includes a binding
    verb incidentally ("X binds Y AND inhibits...").
    """
    for pattern, arrow in _VERB_FAMILIES:
        hits = pattern.findall(blob)
        if hits:
            return arrow, hits[:3]  # cap evidence list for logging
    return "", []


def classify_arrow(claim: Mapping) -> ArrowVerdict:
    """Produce an arrow verdict for one claim dict.

    Comparison is permissive on the canonical hedge label:
      • 'regulates' is accepted as matching anything (the LLM's hedge label;
        we don't flag it as a disagreement).
      • Empty / missing declared arrow is flagged as ``reason='no-declared'``.
    """
    declared = str(claim.get("arrow") or "").strip().lower()
    blob = _flatten_text(claim)
    implied, verb_evidence = _first_verb_match(blob)

    if not declared:
        return ArrowVerdict("", implied, implied == "", "no-declared", verb_evidence)
    if not implied:
        return ArrowVerdict(declared, "", True, "no-implied", verb_evidence)
    if declared == "regulates":
        return ArrowVerdict(declared, implied, True, "hedged-declared", verb_evidence)
    if declared == implied:
        return ArrowVerdict(declared, implied, True, "agree", verb_evidence)
    return ArrowVerdict(declared, implied, False, "mismatch", verb_evidence)


def validate_arrows(
    claims: list[dict],
    *,
    auto_correct: bool = False,
) -> list[ArrowVerdict]:
    """Inspect every claim's arrow vs its text.

    Returns a list of ``ArrowVerdict`` aligned with the input list. When
    ``auto_correct=True``, mismatches have their ``arrow`` field overwritten
    with the implied arrow IN-PLACE, and a ``_arrow_corrected_from`` marker
    is added so downstream renderers can surface the change.
    """
    verdicts: list[ArrowVerdict] = []
    for claim in claims:
        verdict = classify_arrow(claim)
        verdicts.append(verdict)
        if auto_correct and verdict.reason == "mismatch":
            original = claim.get("arrow")
            claim["arrow"] = verdict.implied
            claim["_arrow_corrected_from"] = original
            claim["_arrow_corrected_verbs"] = verdict.verb_evidence
    return verdicts
