"""Modular prompt architecture for the Gemini pipeline.

This package decomposes the monolithic config_gemini_MAXIMIZED.py into
focused modules organized by pipeline phase.

Modules
-------
shared_blocks
    Six text constants + system prompt helpers.
interactor_discovery
    Phase 1 step factories (steps 1a-1g+).
function_mapping
    Phase 2a step factories (steps 2a-2a5+).
deep_research_steps
    Chain resolution step factories (Phase 2b: steps 2ab-2az).
arrow_determination
    Phase 2c arrow template.
qc_and_snapshot
    Phase 3 step factories (steps 2g, 3).
"""
from __future__ import annotations

from typing import List

from pipeline.types import StepConfig
from pipeline.prompts.interactor_discovery import BASE_DISCOVERY_FACTORIES
from pipeline.prompts.function_mapping import BASE_FUNCTION_FACTORIES
from pipeline.prompts.deep_research_steps import (
    step2ab_chain_determination,
    step2ab2_hidden_indirect_detection,
    step2ab3_hidden_chain_determination,
    step2ab5_extract_pairs_explicit,
    step2ax_claim_generation_explicit,
    step2az_claim_generation_hidden,
)
from pipeline.prompts.arrow_determination import step2c_arrow_template
from pipeline.prompts.qc_and_snapshot import step2g_final_qc, step3_snapshot


def build_base_pipeline_steps() -> List[StepConfig]:
    """Build the full base PIPELINE_STEPS list from the factory modules.

    This replaces the 2000-line monolithic list with composed factory calls.
    The output is equivalent to config_gemini_MAXIMIZED.PIPELINE_STEPS.
    """
    steps: List[StepConfig] = []

    # Phase 1: Interactor discovery (7 base steps)
    for factory in BASE_DISCOVERY_FACTORIES:
        steps.append(factory())

    # Phase 2a: Function mapping (5 base steps)
    for factory in BASE_FUNCTION_FACTORIES:
        steps.append(factory())

    # Phase 2b: Chain resolution (6 steps)
    steps.append(step2ab_chain_determination())
    steps.append(step2ab2_hidden_indirect_detection())
    steps.append(step2ab3_hidden_chain_determination())
    steps.append(step2ab5_extract_pairs_explicit())
    steps.append(step2ax_claim_generation_explicit())
    steps.append(step2az_claim_generation_hidden())

    # Phase 2c: Arrow template (skipped by generate_pipeline but in base list)
    steps.append(step2c_arrow_template())

    # Phase 3: QC + Snapshot
    steps.append(step2g_final_qc())
    steps.append(step3_snapshot())

    return steps
