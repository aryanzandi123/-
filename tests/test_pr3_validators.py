"""Tests for PR-3 content-aware validators.

Covers:
  • direction_content_validator — agent-verb subject/object counting
  • pathway_content_validator   — keyword-score drift detection
  • upstream_interactor_validator — upstream hint validation + cycle detection
"""

from utils.direction_content_validator import (
    classify_direction,
    validate_directions,
)
from utils.pathway_content_validator import (
    classify_pathway,
    validate_pathways,
)
from utils.upstream_interactor_validator import (
    validate_upstream_hint,
    validate_chain_shape,
    validate_all_indirect_interactors,
)


# ── Direction validator ──────────────────────────────────────────────────

def test_direction_main_as_agent():
    """When prose says 'ATXN3 phosphorylates VCP', direction should be main_to_primary."""
    claim = {
        "interaction_direction": "main_to_primary",
        "cellular_process": "ATXN3 phosphorylates VCP at the outer membrane.",
    }
    v = classify_direction(claim, main_symbol="ATXN3", partner_symbol="VCP")
    assert v.implied == "main_to_primary"
    assert v.reason == "agree"


def test_direction_mismatch_when_partner_is_agent():
    """LLM says main_to_primary but prose has partner as agent → mismatch."""
    claim = {
        "interaction_direction": "main_to_primary",
        "cellular_process": "RAD23A binds and stabilizes ATXN3 via the UbS2 domain.",
    }
    v = classify_direction(claim, main_symbol="ATXN3", partner_symbol="RAD23A")
    assert v.reason == "mismatch"
    assert v.implied == "primary_to_main"


def test_direction_auto_correct_flips_field():
    claim = {
        "interaction_direction": "main_to_primary",
        "cellular_process": (
            "RAD23A binds and stabilizes the Josephin domain. "
            "RAD23A protects the substrate from turnover."
        ),
    }
    verdicts = validate_directions(
        [claim], main_symbol="ATXN3", partner_symbol="RAD23A",
        auto_correct=True,
    )
    assert verdicts[0].reason == "mismatch"
    assert claim["interaction_direction"] == "primary_to_main"
    assert claim["_direction_corrected_from"] == "main_to_primary"


def test_direction_no_verbs_yields_agree():
    claim = {
        "interaction_direction": "main_to_primary",
        "cellular_process": "Complex formation in the cytoplasm near the nuclear envelope.",
    }
    v = classify_direction(claim, main_symbol="ATXN3", partner_symbol="VCP")
    assert v.agree is True


# ── Pathway validator ───────────────────────────────────────────────────

def test_pathway_agrees_when_keywords_match():
    claim = {
        "pathway": "Mitophagy",
        "cellular_process": (
            "PRKN ubiquitinates mitochondrial substrates, triggering mitophagy "
            "through p62/SQSTM1 recruitment and LC3 lipidation."
        ),
    }
    v = classify_pathway(claim)
    assert v.reason in ("agree", "close-enough", "tied")


def test_pathway_drift_flagged():
    claim = {
        "pathway": "Cell Cycle",
        "cellular_process": (
            "PRKN ubiquitinates MFN1, triggering mitophagy and "
            "autophagic clearance of damaged mitochondria."
        ),
    }
    v = classify_pathway(claim)
    assert v.reason == "drift"
    assert v.implied in ("Mitophagy", "Autophagy",
                          "Mitochondrial Quality Control")


def test_pathway_drift_auto_correct_overwrites():
    claim = {
        "pathway": "Apoptosis",
        "cellular_process": (
            "ATXN3 binds the 26S proteasome via UbS2 and controls "
            "proteasomal turnover through K48-linked polyubiquitination."
        ),
    }
    validate_pathways([claim], auto_correct=True)
    assert claim["pathway"] == "Protein Quality Control"
    assert claim["_pathway_corrected_from"] == "Apoptosis"


def test_pathway_unknown_assigned_skips_judgment():
    claim = {
        "pathway": "My Custom Pathway That's Not In The Seed",
        "cellular_process": "Cells proliferate.",
    }
    v = classify_pathway(claim)
    assert v.agree is True  # can't judge


# ── Upstream validator ──────────────────────────────────────────────────

def test_upstream_valid_when_in_known_set():
    interactor = {"primary": "MFN1", "upstream_interactor": "PRKN"}
    v = validate_upstream_hint(
        interactor, main_symbol="ATXN3",
        known_interactors=["PRKN", "VCP", "VDAC1"],
    )
    assert v.reason == "valid"


def test_upstream_orphan_when_not_in_known_set():
    interactor = {"primary": "MFN1", "upstream_interactor": "NONEXISTENT_X"}
    v = validate_upstream_hint(
        interactor, main_symbol="ATXN3",
        known_interactors=["PRKN", "VCP", "VDAC1"],
    )
    assert v.reason == "orphan"


def test_upstream_self_reference_flagged():
    interactor = {"primary": "MFN1", "upstream_interactor": "MFN1"}
    v = validate_upstream_hint(
        interactor, main_symbol="ATXN3",
        known_interactors=["MFN1", "PRKN"],
    )
    assert v.reason == "self-reference"


def test_upstream_alias_match_accepted():
    interactor = {"primary": "MFN1", "upstream_interactor": "Parkin"}
    v = validate_upstream_hint(
        interactor, main_symbol="ATXN3",
        known_interactors=["PRKN"],
        aliases={"PRKN": ["Parkin"]},
    )
    assert v.reason == "valid"


# ── Chain shape validator ───────────────────────────────────────────────

def test_chain_shape_valid_no_cycles():
    interactor = {"primary": "MFN1", "mediator_chain": ["PRKN", "VDAC1"]}
    v = validate_chain_shape(interactor)
    assert v.reason == "valid"
    assert v.cycles == []
    assert v.self_loops == []


def test_chain_shape_cycle_detected():
    interactor = {"primary": "MFN1", "mediator_chain": ["PRKN", "VDAC1", "PRKN"]}
    v = validate_chain_shape(interactor)
    assert v.reason == "cycle"
    assert "PRKN" in v.cycles


def test_chain_shape_adjacent_self_loop_detected():
    interactor = {"primary": "MFN1", "mediator_chain": ["PRKN", "PRKN", "VDAC1"]}
    v = validate_chain_shape(interactor)
    # PRKN appears twice — caught as cycle; adjacency also caught as self-loop
    assert "PRKN" in v.cycles
    assert "PRKN" in v.self_loops


def test_chain_shape_primary_in_own_chain_flagged():
    interactor = {"primary": "MFN1", "mediator_chain": ["PRKN", "MFN1", "VDAC1"]}
    v = validate_chain_shape(interactor)
    assert "MFN1" in v.cycles


def test_validate_all_returns_two_lists():
    interactors = [
        {"primary": "MFN1", "interaction_type": "indirect",
         "upstream_interactor": "PRKN", "mediator_chain": ["PRKN"]},
        {"primary": "VDAC1", "interaction_type": "indirect",
         "upstream_interactor": "GHOST", "mediator_chain": ["PRKN", "VDAC1"]},
        {"primary": "PRKN", "interaction_type": "direct"},
    ]
    u_v, c_v = validate_all_indirect_interactors(
        interactors, main_symbol="ATXN3",
    )
    assert len(u_v) == 2  # only two indirect interactors
    assert len(c_v) == 2
    # MFN1 valid (PRKN in known set); VDAC1 orphan (GHOST not in set);
    # VDAC1 cycle (VDAC1 == primary appears in own chain)
    assert any(v.reason == "valid" for v in u_v)
    assert any(v.reason == "orphan" for v in u_v)
    assert any(v.reason == "cycle" for v in c_v)
