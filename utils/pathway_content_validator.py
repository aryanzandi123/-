"""Pathway-vs-mechanism consistency validator.

Each claim carries a ``pathway`` / ``pathway_name`` / ``pathway_id`` set by
the pipeline's pathway-assignment step. In practice pathways drift: a
claim whose mechanism describes mitochondrial fission gets assigned to
"Apoptosis" because the terminal outcome mentions cell death, or a
kinase-signaling claim gets filed under "Protein Quality Control"
because a UPS keyword appeared once. No read-time sanity check catches
these.

This module scores each claim's prose against a curated keyword map
from pathway name → biological-mechanism keywords. When the assigned
pathway's score is dramatically lower than another pathway's score, we
flag drift. Observe-only by default.

Keyword map is SEED-level — it covers the common ProPaths pathways and
is extensible via ``add_pathway_keywords``. Not a replacement for the
LLM assignment; a safety net against obvious miscategorisation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping


# Seed keyword map. Case-insensitive whole-word matches. Add more via
# ``add_pathway_keywords(name, keywords)`` when operators spot drift.
# Entries are regex fragments combined with word boundaries; each hit
# adds 1 to the pathway's score.
_SEED_PATHWAY_KEYWORDS: dict[str, list[str]] = {
    "Apoptosis": [
        "apoptosis", "apoptotic", "caspase", "BAX", "BAK", "BCL-2", "BCL2",
        "cytochrome c", "programmed cell death", "pro-apoptotic",
        "anti-apoptotic", "TUNEL", "DISC",
    ],
    "Autophagy": [
        "autophagy", "autophagic", "autophagosome", "autolysosome",
        "LC3", "LC3B", "LC3A", "ATG5", "ATG7", "ATG12", "ATG16", "BECN1",
        "beclin", "SQSTM1", "p62", "NBR1", "OPTN",
    ],
    "Mitophagy": [
        "mitophagy", "PINK1", "PRKN", "Parkin", "mitochondrial clearance",
        "MFN1", "MFN2", "Mitofusin", "VDAC", "TOM70", "TOM20",
        "depolarization", "damaged mitochondria",
    ],
    "Mitochondrial Quality Control": [
        "mitochondrial quality control", "mitochondrial dynamics",
        "mitochondrial fission", "mitochondrial fusion", "DRP1", "FIS1",
        "OPA1", "mitochondrial membrane potential",
    ],
    "Protein Quality Control": [
        "protein quality control", "ubiquitin-proteasome", "UPS",
        "proteasome", "proteasomal degradation", "ubiquitin ligase",
        "E3 ligase", "K48-linked", "polyubiquitination",
        "proteasomal turnover", "misfolded protein", "aggresome",
    ],
    "ERAD": [
        "ERAD", "endoplasmic reticulum-associated degradation",
        "ER-associated degradation", "HRD1", "SEL1L", "DERL", "VCP",
        "p97", "UFD1", "NPLOC4",
    ],
    "DNA Damage Response": [
        "DNA damage", "DDR", "ATM", "ATR", "CHK1", "CHK2", "H2AX",
        "double-strand break", "DSB", "single-strand break",
        "BRCA1", "BRCA2", "53BP1", "MDC1", "RNF8", "RNF168",
    ],
    "Cell Cycle": [
        "cell cycle", "G1", "G2", "S phase", "M phase", "cyclin",
        "CDK1", "CDK2", "CDK4", "CDK6", "checkpoint", "mitosis",
    ],
    "mTOR Signaling": [
        "mTOR", "mTORC1", "mTORC2", "RAPTOR", "RICTOR", "4E-BP1",
        "S6K", "TSC1", "TSC2", "RHEB", "AKT", "PI3K",
    ],
    "Heat Shock Response": [
        "heat shock", "HSF1", "HSP70", "HSP90", "HSP40", "chaperone",
        "chaperonin", "TRiC", "CCT",
    ],
    "Integrated Stress Response (ISR)": [
        "integrated stress response", "ISR", "eIF2", "EIF2S1", "ATF4",
        "PERK", "GCN2", "PKR", "HRI", "CHOP", "DDIT3", "GADD34", "PPP1R15A",
    ],
    "Transcriptional Regulation": [
        "transcription factor", "promoter binding", "enhancer",
        "transcription", "RNA polymerase", "coactivator", "corepressor",
    ],
}


from collections import OrderedDict

# LRU-bounded compiled-pattern cache. Previously this was an unbounded
# dict; on long-running processes with many pathway-name additions via
# add_pathway_keywords() it would grow indefinitely. Cap matches the
# number of pathways the seed map + a generous headroom would ever need.
_COMPILED_CACHE_MAX = 256
_COMPILED_CACHE: "OrderedDict[str, re.Pattern]" = OrderedDict()


def _compiled_pattern(pathway: str) -> re.Pattern | None:
    """Return a compiled whole-word alternation regex for a pathway's keywords."""
    if pathway in _COMPILED_CACHE:
        # LRU bump.
        _COMPILED_CACHE.move_to_end(pathway)
        return _COMPILED_CACHE[pathway]
    keywords = _SEED_PATHWAY_KEYWORDS.get(pathway)
    if not keywords:
        return None
    alt = "|".join(re.escape(k) for k in keywords)
    pat = re.compile(
        rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    _COMPILED_CACHE[pathway] = pat
    if len(_COMPILED_CACHE) > _COMPILED_CACHE_MAX:
        _COMPILED_CACHE.popitem(last=False)  # evict oldest
    return pat


def add_pathway_keywords(pathway: str, keywords: list[str]) -> None:
    """Extend the keyword map at runtime. Invalidates the compiled cache."""
    existing = _SEED_PATHWAY_KEYWORDS.get(pathway, [])
    _SEED_PATHWAY_KEYWORDS[pathway] = list({*existing, *keywords})
    _COMPILED_CACHE.pop(pathway, None)


@dataclass
class PathwayVerdict:
    """Pathway alignment verdict for one claim.

    ``assigned_score`` is the keyword-hit count for the pathway the claim
    was assigned to. ``top_alternative_score`` and ``top_alternative`` are
    the best-scoring pathway from the keyword map. ``reason`` is one of:
      • ``"agree"``           — assigned pathway has the top score
      • ``"tied"``             — assigned pathway ties another
      • ``"unknown-assigned"`` — assigned pathway not in keyword map (can't judge)
      • ``"drift"``            — another pathway scores strictly higher
      • ``"no-text"``          — no inspectable prose
      • ``"no-assignment"``    — claim has no pathway set
    """

    assigned: str
    implied: str
    assigned_score: int
    top_alternative_score: int
    agree: bool
    reason: str
    scores: dict[str, int] = field(default_factory=dict)


def _flatten_text(claim: Mapping) -> str:
    chunks: list[str] = []
    for field_name in ("cellular_process", "effect_description",
                       "biological_consequence", "specific_effects",
                       "function"):
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


def _assigned_pathway_name(claim: Mapping) -> str:
    pw = claim.get("pathway")
    if isinstance(pw, dict):
        return str(pw.get("canonical_name") or pw.get("name") or "")
    if isinstance(pw, str):
        return pw.strip()
    return ""


def classify_pathway(claim: Mapping) -> PathwayVerdict:
    """Score claim prose against every known pathway keyword set."""
    assigned = _assigned_pathway_name(claim)
    text = _flatten_text(claim)

    # F6: skip drift detection for chain-derived hop claims.
    #
    # A chain hop's prose intentionally describes the upstream cascade
    # context (e.g. an ATF4→BECN1 hop says "PERK→eIF2α→ATF4→BECN1
    # nucleates autophagy via VPS34..."), which makes keyword-based
    # drift detection wrongly conclude the hop belongs to the upstream
    # pathway. The PERK run flagged PIK3C3 and ATG14 (both correctly
    # assigned to Autophagy) as drifting toward ISR with scores
    # 23-vs-9 and 18-vs-8 respectively — but those are real Autophagy
    # nucleation-complex members. The chain unification pass (P3.1)
    # already owns chain-derived pathway assignment with proper
    # cross-pathway tie-breaking; bypass the keyword heuristic for
    # those claims.
    fn_ctx = (claim.get("function_context") or "").strip().lower()
    if fn_ctx == "chain_derived" or claim.get("_inferred_from_chain"):
        return PathwayVerdict(
            assigned, assigned, 0, 0, True, "chain-derived-skip", scores={},
        )

    if not text:
        return PathwayVerdict(assigned, "", 0, 0, True, "no-text")
    if not assigned:
        return PathwayVerdict(assigned, "", 0, 0, True, "no-assignment")

    scores: dict[str, int] = {}
    for name in _SEED_PATHWAY_KEYWORDS:
        pat = _compiled_pattern(name)
        if pat is None:
            continue
        hits = len(pat.findall(text))
        if hits:
            scores[name] = hits

    assigned_score = scores.get(assigned, 0)

    if not scores:
        return PathwayVerdict(
            assigned, "", assigned_score, 0, True, "no-keywords-matched",
            scores=scores,
        )

    top_name, top_score = max(scores.items(), key=lambda kv: kv[1])

    if assigned not in _SEED_PATHWAY_KEYWORDS:
        # Can't judge — we don't have keywords for the assigned pathway.
        return PathwayVerdict(
            assigned, top_name, assigned_score, top_score,
            True, "unknown-assigned", scores=scores,
        )

    if top_score == assigned_score:
        return PathwayVerdict(
            assigned, top_name, assigned_score, top_score,
            True, "agree" if top_name == assigned else "tied",
            scores=scores,
        )
    if top_score > assigned_score:
        # Only call it drift if the gap is meaningful (≥ 2 hits AND
        # the top pathway's hits are at least 2× the assigned's).
        gap = top_score - assigned_score
        if gap >= 2 and (assigned_score == 0 or top_score >= 2 * assigned_score):
            return PathwayVerdict(
                assigned, top_name, assigned_score, top_score,
                False, "drift", scores=scores,
            )
    return PathwayVerdict(
        assigned, top_name, assigned_score, top_score,
        True, "close-enough", scores=scores,
    )


def validate_pathways(
    claims: list[dict],
    *,
    auto_correct: bool = False,
) -> list[PathwayVerdict]:
    """Inspect every claim's pathway assignment vs its prose keywords."""
    verdicts: list[PathwayVerdict] = []
    for claim in claims:
        verdict = classify_pathway(claim)
        verdicts.append(verdict)
        if auto_correct and verdict.reason == "drift":
            original = claim.get("pathway")
            claim["pathway"] = verdict.implied
            claim["_pathway_corrected_from"] = original
            claim["_pathway_correction_evidence"] = dict(verdict.scores)
    return verdicts
