"""Runtime validation of PhD-level output depth for interaction claims.

The prompt-level directive asks the model for 6-10 sentences of mechanism
narrative and 3-5 biological-consequence cascades per function (see the
recent ``fix: restore PhD-level depth requirements`` commit). There was
no corresponding runtime check — if the model drifted under token
pressure or batch mode, shallow claims silently reached the user.

This module provides a pure validator that can be called from post-
processing to:
  1. count sentences in each function's mechanism/evidence text,
  2. count cascade entries in ``biological_consequence``,
  3. return a structured report listing every violation, and
  4. (optionally) tag the in-payload function dict with ``_depth_issues``
     so the UI or downstream code can surface the problem.

It does NOT call the model or try to repair the data — that's a separate
decision the caller makes based on the report.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Depth thresholds (named for tunability)
# ---------------------------------------------------------------------------

MIN_SENTENCES = 6
MAX_SENTENCES = 10
MIN_CASCADES = 3
MAX_CASCADES = 5
MIN_CASCADE_CHARS = 25  # a cascade shorter than this is a stub, not a cascade

# P2.2 — effect_description, specific_effects, evidence are now validated
# too. Without these, claims that pass the cellular_process/cascade gate
# can still be shallow on quantitative impact, experimental support, and
# citations — exactly the failure mode the depth contract is supposed to
# prevent.
MIN_EFFECT_SENTENCES = 6
MIN_SPECIFIC_EFFECTS = 3
MIN_EVIDENCE_PAPERS = 3
MIN_EFFECT_CHARS = 20      # below this, an entry is too short to count
MIN_EVIDENCE_FIELDS = 2    # paper_title + at least one of quote/year/assay

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class DepthViolation:
    """One specific depth issue on a specific function."""
    interactor: str
    function_name: str
    field: str           # e.g. "cellular_process", "biological_consequence"
    rule: str            # e.g. "min_sentences", "max_sentences", "min_cascades"
    actual: int
    expected_min: Optional[int] = None
    expected_max: Optional[int] = None


@dataclass
class DepthReport:
    """Structured summary of all depth violations for one payload."""
    total_functions: int = 0
    flagged_functions: int = 0
    violations: List[DepthViolation] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if self.total_functions == 0:
            return 1.0
        return 1.0 - (self.flagged_functions / self.total_functions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_functions": self.total_functions,
            "flagged_functions": self.flagged_functions,
            "pass_rate": round(self.pass_rate, 3),
            "thresholds": {
                "min_sentences": MIN_SENTENCES,
                "max_sentences": MAX_SENTENCES,
                "min_cascades": MIN_CASCADES,
                "max_cascades": MAX_CASCADES,
                "min_effect_sentences": MIN_EFFECT_SENTENCES,
                "min_specific_effects": MIN_SPECIFIC_EFFECTS,
                "min_evidence_papers": MIN_EVIDENCE_PAPERS,
            },
            "violations": [asdict(v) for v in self.violations],
        }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def count_sentences(text: str) -> int:
    """Count sentences in ``text`` via a simple punctuation heuristic.

    Splits on ``. ``, ``! ``, ``? `` (and equivalents at line breaks). Any
    non-empty chunk counts as a sentence. An empty/whitespace string yields
    0. This is intentionally simple — it's for threshold comparison, not
    NLP accuracy.
    """
    if not text or not isinstance(text, str):
        return 0
    chunks = _SENTENCE_SPLIT_RE.split(text.strip())
    return sum(1 for c in chunks if c.strip())


def count_cascades(bio_consequence: Any) -> int:
    """Count substantive cascade entries in ``biological_consequence``.

    ``bio_consequence`` can be a list (expected) or a single string. Short
    stub strings (below ``MIN_CASCADE_CHARS``) are not counted as cascades
    because the PhD-depth rule expects real descriptions, not category
    labels.
    """
    if bio_consequence is None:
        return 0
    if isinstance(bio_consequence, str):
        return 1 if len(bio_consequence.strip()) >= MIN_CASCADE_CHARS else 0
    if not isinstance(bio_consequence, list):
        return 0
    return sum(
        1 for item in bio_consequence
        if isinstance(item, str) and len(item.strip()) >= MIN_CASCADE_CHARS
    )


def count_specific_effects(specific_effects: Any) -> int:
    """Count substantive entries in ``specific_effects``.

    Each entry should describe one experimental finding (technique, model
    system, measurable result). Stub entries below ``MIN_EFFECT_CHARS``
    don't count — they don't satisfy the prompt's "technique + model +
    result" requirement.
    """
    if specific_effects is None:
        return 0
    if isinstance(specific_effects, str):
        return 1 if len(specific_effects.strip()) >= MIN_EFFECT_CHARS else 0
    if not isinstance(specific_effects, list):
        return 0
    return sum(
        1 for item in specific_effects
        if isinstance(item, str) and len(item.strip()) >= MIN_EFFECT_CHARS
    )


def count_evidence_papers(evidence: Any) -> int:
    """Count valid citation entries in ``evidence``.

    A citation counts if it's a dict with at least ``MIN_EVIDENCE_FIELDS``
    populated fields among (paper_title, relevant_quote/quote, year,
    assay, species, key_finding). Bare strings are tolerated as one paper
    each — the prompt formally asks for the dict shape, but legacy data
    sometimes has plain quotes.
    """
    if evidence is None:
        return 0
    if isinstance(evidence, str):
        return 1 if evidence.strip() else 0
    if not isinstance(evidence, list):
        return 0

    def _is_valid_paper(e: Any) -> bool:
        if isinstance(e, str):
            return bool(e.strip())
        if not isinstance(e, dict):
            return False
        # Need a title plus at least one supporting field.
        if not (e.get("paper_title") or "").strip():
            return False
        populated = sum(
            1 for k in (
                "relevant_quote", "quote", "year", "assay", "species", "key_finding",
            )
            if str(e.get(k) or "").strip()
        )
        return populated >= 1  # title + at least one other field

    return sum(1 for e in evidence if _is_valid_paper(e))


# ---------------------------------------------------------------------------
# Per-function validation
# ---------------------------------------------------------------------------


def validate_function_depth(
    function: Dict[str, Any],
    interactor_name: str = "?",
) -> List[DepthViolation]:
    """Return a list of violations for a single function dict.

    Sentence source of truth: ``cellular_process`` first, falling back to
    concatenated evidence quotes if ``cellular_process`` is empty. This
    matches the prompt's guidance, which asks the model to put the narrative
    mechanism in ``cellular_process`` with evidence quotes as support.

    Cascade source: ``biological_consequence``.
    """
    violations: List[DepthViolation] = []
    fn_name = function.get("function", "?")

    # --- Sentences -------------------------------------------------------
    cp = function.get("cellular_process", "") or ""
    sentence_count = count_sentences(cp)
    sentence_field = "cellular_process"
    if sentence_count == 0:
        # Fall back to evidence quotes — PhD depth can live there too.
        ev = function.get("evidence", []) or []
        if isinstance(ev, list):
            combined = " ".join(
                e.get("quote", "") for e in ev if isinstance(e, dict)
            )
            if combined.strip():
                sentence_count = count_sentences(combined)
                sentence_field = "evidence.quote"

    if sentence_count < MIN_SENTENCES:
        violations.append(DepthViolation(
            interactor=interactor_name,
            function_name=fn_name,
            field=sentence_field,
            rule="min_sentences",
            actual=sentence_count,
            expected_min=MIN_SENTENCES,
            expected_max=MAX_SENTENCES,
        ))
    elif sentence_count > MAX_SENTENCES:
        violations.append(DepthViolation(
            interactor=interactor_name,
            function_name=fn_name,
            field=sentence_field,
            rule="max_sentences",
            actual=sentence_count,
            expected_min=MIN_SENTENCES,
            expected_max=MAX_SENTENCES,
        ))

    # --- Cascades --------------------------------------------------------
    cascade_count = count_cascades(function.get("biological_consequence"))
    if cascade_count < MIN_CASCADES:
        violations.append(DepthViolation(
            interactor=interactor_name,
            function_name=fn_name,
            field="biological_consequence",
            rule="min_cascades",
            actual=cascade_count,
            expected_min=MIN_CASCADES,
            expected_max=MAX_CASCADES,
        ))
    elif cascade_count > MAX_CASCADES:
        violations.append(DepthViolation(
            interactor=interactor_name,
            function_name=fn_name,
            field="biological_consequence",
            rule="max_cascades",
            actual=cascade_count,
            expected_min=MIN_CASCADES,
            expected_max=MAX_CASCADES,
        ))

    # --- effect_description sentences (P2.2) ----------------------------
    effect_text = function.get("effect_description", "") or ""
    effect_sentences = count_sentences(effect_text)
    if effect_sentences < MIN_EFFECT_SENTENCES:
        violations.append(DepthViolation(
            interactor=interactor_name,
            function_name=fn_name,
            field="effect_description",
            rule="min_effect_sentences",
            actual=effect_sentences,
            expected_min=MIN_EFFECT_SENTENCES,
            expected_max=None,
        ))

    # --- specific_effects entries (P2.2) --------------------------------
    se_count = count_specific_effects(function.get("specific_effects"))
    if se_count < MIN_SPECIFIC_EFFECTS:
        violations.append(DepthViolation(
            interactor=interactor_name,
            function_name=fn_name,
            field="specific_effects",
            rule="min_specific_effects",
            actual=se_count,
            expected_min=MIN_SPECIFIC_EFFECTS,
            expected_max=None,
        ))

    # --- evidence papers (P2.2) -----------------------------------------
    ev_count = count_evidence_papers(function.get("evidence"))
    if ev_count < MIN_EVIDENCE_PAPERS:
        violations.append(DepthViolation(
            interactor=interactor_name,
            function_name=fn_name,
            field="evidence",
            rule="min_evidence_papers",
            actual=ev_count,
            expected_min=MIN_EVIDENCE_PAPERS,
            expected_max=None,
        ))

    return violations


# ---------------------------------------------------------------------------
# Payload-level validation + in-place tagging
# ---------------------------------------------------------------------------


def validate_payload_depth(
    payload: Dict[str, Any],
    tag_in_place: bool = True,
) -> DepthReport:
    """Validate every function across all interactors in ``payload``.

    If ``tag_in_place`` is True, each function that has a violation gets a
    ``_depth_issues`` list attached with the rule names it failed. This is
    convenient for downstream UI/flagging and does not interfere with the
    existing ``_tag_shallow_functions`` helper in runner.py (which uses a
    different, looser check).
    """
    report = DepthReport()
    ctx = payload.get("ctx_json") if isinstance(payload, dict) else None
    if not isinstance(ctx, dict):
        return report

    for interactor in ctx.get("interactors", []) or []:
        if not isinstance(interactor, dict):
            continue
        interactor_name = interactor.get("primary", "?")

        # Flat functions[] — direct/net claims for this interactor.
        for function in interactor.get("functions", []) or []:
            if not isinstance(function, dict):
                continue
            report.total_functions += 1
            violations = validate_function_depth(function, interactor_name)
            if violations:
                report.flagged_functions += 1
                report.violations.extend(violations)
                if tag_in_place:
                    function["_depth_issues"] = sorted(
                        {v.rule for v in violations}
                    )

        # P2.2 — chain_link_functions[pair] hop claims. Previously these
        # escaped depth validation entirely (the redispatch path only
        # walked flat functions[]), so chain hops could ship at 1
        # cascade / 1 evidence forever. Now they're audited the same
        # way as flat claims.
        clf = interactor.get("chain_link_functions")
        if isinstance(clf, dict):
            for pair_key, pair_funcs in clf.items():
                if not isinstance(pair_funcs, list):
                    continue
                # Tag the violation with the hop signature so the
                # downstream report and frontend can attribute it to
                # the right edge.
                hop_label = f"{interactor_name}[{pair_key}]"
                for function in pair_funcs:
                    if not isinstance(function, dict):
                        continue
                    report.total_functions += 1
                    violations = validate_function_depth(function, hop_label)
                    if violations:
                        report.flagged_functions += 1
                        report.violations.extend(violations)
                        if tag_in_place:
                            function["_depth_issues"] = sorted(
                                {v.rule for v in violations}
                            )

    return report


# ---------------------------------------------------------------------------
# Report persistence
# ---------------------------------------------------------------------------


def write_quality_report(
    report: DepthReport,
    protein_symbol: str,
    logs_root: str = "Logs",
) -> Optional[str]:
    """Persist ``report`` to ``{logs_root}/{protein_symbol}/quality_report.json``.

    Returns the full path written, or None if the directory could not be
    created (which is logged but never raises — this is a non-critical
    observability hook and should never block the pipeline).
    """
    try:
        target_dir = os.path.join(logs_root, protein_symbol)
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "quality_report.json")
        with open(target, "w", encoding="utf-8") as fp:
            json.dump(report.to_dict(), fp, indent=2, ensure_ascii=False)
        return target
    except OSError as exc:
        # Don't crash the pipeline over a failed report write; emit a
        # visible warning instead so the failure can be triaged.
        import sys as _sys
        print(
            f"[WARN] Could not write quality report for {protein_symbol}: "
            f"{type(exc).__name__}: {exc}",
            file=_sys.stderr,
        )
        return None


# ---------------------------------------------------------------------------
# Pipeline-stage wrapper
# ---------------------------------------------------------------------------


def run_quality_validation(
    payload: Dict[str, Any],
    protein_symbol: str,
    logs_root: str = "Logs",
    verbose: bool = False,
) -> Tuple[Dict[str, Any], DepthReport]:
    """Convenience wrapper for ``utils/post_processor.py``.

    Validates the payload in-place, writes the report next to per-step logs,
    and returns ``(payload, report)``. On a clean run the report is still
    written (with ``flagged_functions=0``) so downstream tooling can rely
    on the file always existing.
    """
    report = validate_payload_depth(payload, tag_in_place=True)
    write_quality_report(report, protein_symbol, logs_root=logs_root)
    if verbose:
        import sys as _sys
        print(
            f"[QUALITY] {protein_symbol}: "
            f"{report.flagged_functions}/{report.total_functions} functions "
            f"flagged for depth issues (pass_rate={report.pass_rate:.1%})",
            file=_sys.stderr,
        )
    return payload, report
