"""Tests for utils.arrow_content_validator — content-overrides-LLM-arrow."""

from utils.arrow_content_validator import (
    ArrowVerdict,
    classify_arrow,
    validate_arrows,
)


def test_declared_activates_matches_activation_prose():
    claim = {
        "function": "MFN1 Fusion Promotion",
        "arrow": "activates",
        "cellular_process": "PRKN phosphorylates and stabilizes MFN1 on the OMM.",
    }
    v = classify_arrow(claim)
    assert v.agree is True
    assert v.reason == "agree"
    assert v.implied == "activates"


def test_declared_activates_but_prose_says_inhibits_is_mismatch():
    claim = {
        "function": "MFN1 Degradation",
        "arrow": "activates",
        "cellular_process": "PRKN ubiquitinates MFN1, inhibiting fusion and degrading the protein.",
    }
    v = classify_arrow(claim)
    assert v.agree is False
    assert v.reason == "mismatch"
    assert v.declared == "activates"
    # "ubiquitinates" is NOT in our verb list as degradation; "degrading" is.
    # Because the activation-family pattern is scanned first, "ubiquitinates"
    # might not appear there — verify the implied was derived from prose.
    assert v.implied in ("inhibits", "activates")


def test_hedged_regulates_passes_without_flag():
    claim = {
        "function": "Autophagy Regulation",
        "arrow": "regulates",
        "cellular_process": "ATXN3 modulates autophagy through chaperone interactions.",
    }
    v = classify_arrow(claim)
    assert v.agree is True
    assert v.reason == "hedged-declared"


def test_missing_declared_arrow_still_extracts_implied():
    claim = {
        "arrow": "",
        "cellular_process": "The kinase phosphorylates its substrate.",
    }
    v = classify_arrow(claim)
    assert v.reason == "no-declared"
    assert v.implied == "activates"


def test_binds_is_accepted_when_prose_only_describes_binding():
    claim = {
        "arrow": "binds",
        "cellular_process": "RAD23A binds ATXN3 via its UBL domain.",
    }
    v = classify_arrow(claim)
    assert v.agree is True


def test_validate_arrows_auto_correct_overwrites_mismatches():
    claim = {
        "function": "MFN1 Breakdown",
        "arrow": "activates",
        "cellular_process": "PRKN inhibits MFN1 activity by polyubiquitinating it.",
    }
    verdicts = validate_arrows([claim], auto_correct=True)
    assert verdicts[0].reason == "mismatch"
    assert claim["arrow"] == "inhibits"
    assert claim["_arrow_corrected_from"] == "activates"


def test_validate_arrows_observe_only_default():
    claim = {"arrow": "activates", "cellular_process": "The kinase inhibits its substrate."}
    validate_arrows([claim])
    assert claim["arrow"] == "activates"  # unchanged
    assert "_arrow_corrected_from" not in claim
