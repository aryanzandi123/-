"""Tests for the runtime PhD-depth validator (step 9 / #1).

Pins the 6-10 sentences × 3-5 cascades rule as executable code so
prompt drift gets caught by CI, not by a user staring at a shallow
report.
"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.quality_validator import (
    MAX_CASCADES,
    MAX_SENTENCES,
    MIN_CASCADES,
    MIN_CASCADE_CHARS,
    MIN_SENTENCES,
    count_cascades,
    count_sentences,
    validate_function_depth,
    validate_payload_depth,
    write_quality_report,
    run_quality_validation,
)


# ---------------------------------------------------------------------------
# count_sentences
# ---------------------------------------------------------------------------


def test_count_sentences_empty():
    assert count_sentences("") == 0
    assert count_sentences(None) == 0


def test_count_sentences_single():
    assert count_sentences("This is a single sentence.") == 1


def test_count_sentences_multiple():
    text = "One. Two. Three. Four. Five. Six. Seven."
    assert count_sentences(text) == 7


def test_count_sentences_with_question_and_exclamation():
    text = "Is this a question? Yes! And this is a statement."
    assert count_sentences(text) == 3


def test_count_sentences_ignores_trailing_whitespace():
    assert count_sentences("A. B.   ") == 2


def test_count_sentences_non_string_returns_zero():
    assert count_sentences(42) == 0
    assert count_sentences(["a", "b"]) == 0


# ---------------------------------------------------------------------------
# count_cascades
# ---------------------------------------------------------------------------


def test_count_cascades_none():
    assert count_cascades(None) == 0


def test_count_cascades_short_stubs_ignored():
    """Single-word or tiny stub entries should NOT count as cascades — they
    mean the model returned category labels, not real cascades."""
    stubs = ["apoptosis", "ERAD", "mitophagy"]
    # Each stub is < MIN_CASCADE_CHARS (25).
    assert count_cascades(stubs) == 0


def test_count_cascades_real_descriptions_counted():
    cascades = [
        "Accumulation of misfolded huntingtin leads to nuclear inclusion formation and neuronal dysfunction.",
        "Loss of VCP segregase activity triggers ER stress and unfolded protein response activation.",
        "Deranged ERAD allows misfolded proteins to escape, overloading proteasomes and impairing proteostasis.",
    ]
    assert count_cascades(cascades) == 3


def test_count_cascades_mixed():
    items = [
        "Real cascade describing a biological effect with sufficient detail.",
        "stub",
        "Another detailed cascade explaining how the downstream process unfolds in cells.",
    ]
    assert count_cascades(items) == 2


def test_count_cascades_single_string():
    """A single long string is one cascade; a short one is zero."""
    assert count_cascades("Detailed long cascade description for a biological effect in cells.") == 1
    assert count_cascades("stub") == 0


def test_count_cascades_non_list_non_string():
    assert count_cascades(42) == 0
    assert count_cascades({"not": "a list"}) == 0


# ---------------------------------------------------------------------------
# validate_function_depth — flagged cases
# ---------------------------------------------------------------------------


def _long_cascade(i: int) -> str:
    return f"Cascade number {i} describes how a downstream biological process unfolds in cells with measurable functional consequence."


def _healthy_function():
    """A function meeting all 5 depth-field minima (P2.2 — effect_description,
    specific_effects, and evidence are now validated too)."""
    sentences = ". ".join([f"Sentence {i}" for i in range(1, 8)]) + "."
    effect_sentences = ". ".join([f"Effect {i}" for i in range(1, 8)]) + "."
    return {
        "function": "Protein quality control",
        "cellular_process": sentences,
        "effect_description": effect_sentences,
        "biological_consequence": [_long_cascade(i) for i in range(4)],
        "specific_effects": [
            "Co-IP in HEK293T showing 5-fold enrichment over control",
            "CRISPR knockout in MEFs reduced target abundance by 70 percent",
            "SPR measurement Kd = 1.2 uM at 25C in physiological buffer",
        ],
        "evidence": [
            {"paper_title": "Paper A", "relevant_quote": "binding shown by Co-IP", "year": 2024, "assay": "Co-IP", "species": "human"},
            {"paper_title": "Paper B", "relevant_quote": "knockout reduces target", "year": 2023, "assay": "CRISPR", "species": "mouse"},
            {"paper_title": "Paper C", "relevant_quote": "Kd 1.2 uM", "year": 2022, "assay": "SPR", "species": "human"},
        ],
    }


def test_healthy_function_has_no_violations():
    violations = validate_function_depth(_healthy_function(), "VCP")
    assert violations == []


def test_too_few_sentences_flagged():
    fn = _healthy_function()
    fn["cellular_process"] = "Only three. Sentences here. Too shallow."
    violations = validate_function_depth(fn, "VCP")
    rules = {v.rule for v in violations}
    assert "min_sentences" in rules
    # The specific count must be recorded so humans can triage.
    min_viol = next(v for v in violations if v.rule == "min_sentences")
    assert min_viol.actual == 3
    assert min_viol.expected_min == MIN_SENTENCES


def test_too_many_sentences_flagged():
    fn = _healthy_function()
    # 15 sentences > MAX_SENTENCES (10).
    fn["cellular_process"] = ". ".join([f"Sentence {i}" for i in range(15)]) + "."
    violations = validate_function_depth(fn, "VCP")
    rules = {v.rule for v in violations}
    assert "max_sentences" in rules


def test_too_few_cascades_flagged():
    fn = _healthy_function()
    fn["biological_consequence"] = [_long_cascade(0), _long_cascade(1)]  # only 2
    violations = validate_function_depth(fn, "VCP")
    assert "min_cascades" in {v.rule for v in violations}


def test_too_many_cascades_flagged():
    fn = _healthy_function()
    fn["biological_consequence"] = [_long_cascade(i) for i in range(7)]  # 7 > 5
    violations = validate_function_depth(fn, "VCP")
    assert "max_cascades" in {v.rule for v in violations}


def test_stubs_in_biological_consequence_flagged_as_too_few():
    """If the model returned category labels instead of real cascades, we
    should flag min_cascades because we only count substantive entries."""
    fn = _healthy_function()
    fn["biological_consequence"] = ["apoptosis", "ERAD", "mitophagy", "autophagy", "ubiquitination"]
    violations = validate_function_depth(fn, "VCP")
    # All 5 are too short — counted as 0 cascades → min_cascades fires.
    rules = {v.rule for v in violations}
    assert "min_cascades" in rules


def test_empty_cellular_process_falls_back_to_evidence_quotes():
    """If ``cellular_process`` is empty, we count sentences in evidence
    quotes so the validator doesn't spuriously fire on claims that put
    their narrative in the evidence block."""
    fn = {
        "function": "X",
        "cellular_process": "",
        "biological_consequence": [_long_cascade(i) for i in range(4)],
        "evidence": [
            {"pmid": "1", "quote": "One. Two. Three. Four. Five. Six. Seven."},
        ],
    }
    violations = validate_function_depth(fn, "VCP")
    rules = {v.rule for v in violations}
    # No min_sentences violation because evidence gives us 7 sentences.
    assert "min_sentences" not in rules


# ---------------------------------------------------------------------------
# validate_payload_depth — structural
# ---------------------------------------------------------------------------


def test_validate_payload_empty_ctx():
    report = validate_payload_depth({"ctx_json": {"interactors": []}})
    assert report.total_functions == 0
    assert report.flagged_functions == 0
    assert report.violations == []
    assert report.pass_rate == 1.0


def test_validate_payload_no_ctx_returns_empty_report():
    report = validate_payload_depth({"snapshot_json": {}})
    assert report.total_functions == 0


def test_validate_payload_tags_in_place():
    payload = {
        "ctx_json": {
            "interactors": [
                {
                    "primary": "VCP",
                    "functions": [
                        {
                            "function": "Shallow",
                            "cellular_process": "One. Two.",  # too few
                            "biological_consequence": [],       # too few
                            "evidence": [],
                        },
                        _healthy_function(),
                    ],
                }
            ]
        }
    }
    report = validate_payload_depth(payload, tag_in_place=True)

    assert report.total_functions == 2
    assert report.flagged_functions == 1

    shallow = payload["ctx_json"]["interactors"][0]["functions"][0]
    healthy = payload["ctx_json"]["interactors"][0]["functions"][1]

    assert "min_sentences" in shallow["_depth_issues"]
    assert "min_cascades" in shallow["_depth_issues"]
    assert "_depth_issues" not in healthy


def test_validate_payload_pass_rate_calculation():
    payload = {
        "ctx_json": {
            "interactors": [
                {
                    "primary": "A",
                    "functions": [
                        _healthy_function(),
                        _healthy_function(),
                        {
                            "function": "bad",
                            "cellular_process": ".",
                            "biological_consequence": [],
                            "evidence": [],
                        },
                    ],
                }
            ]
        }
    }
    report = validate_payload_depth(payload)
    assert report.total_functions == 3
    assert report.flagged_functions == 1
    assert 0.66 < report.pass_rate < 0.67


# ---------------------------------------------------------------------------
# write_quality_report — idempotent, writes valid JSON
# ---------------------------------------------------------------------------


def test_write_quality_report_roundtrip(tmp_path):
    payload = {
        "ctx_json": {
            "interactors": [
                {
                    "primary": "VCP",
                    "functions": [
                        {
                            "function": "Shallow",
                            "cellular_process": "Too short.",
                            "biological_consequence": [],
                            "evidence": [],
                        },
                    ],
                }
            ]
        }
    }
    report = validate_payload_depth(payload)
    target_path = write_quality_report(
        report, protein_symbol="ATXN3", logs_root=str(tmp_path),
    )
    assert target_path is not None
    assert Path(target_path).exists()
    data = json.loads(Path(target_path).read_text())
    assert data["total_functions"] == 1
    assert data["flagged_functions"] == 1
    assert data["thresholds"]["min_sentences"] == MIN_SENTENCES
    assert data["thresholds"]["max_sentences"] == MAX_SENTENCES
    assert data["thresholds"]["min_cascades"] == MIN_CASCADES
    assert data["thresholds"]["max_cascades"] == MAX_CASCADES
    assert len(data["violations"]) >= 1


def test_run_quality_validation_returns_report(tmp_path):
    payload = {
        "ctx_json": {
            "interactors": [
                {"primary": "A", "functions": [_healthy_function()]},
            ]
        }
    }
    out, report = run_quality_validation(
        payload, protein_symbol="TEST_PROTEIN", logs_root=str(tmp_path),
    )
    assert out is payload
    assert report.flagged_functions == 0
    # The report file must always be written, even on clean runs.
    assert (tmp_path / "TEST_PROTEIN" / "quality_report.json").exists()
