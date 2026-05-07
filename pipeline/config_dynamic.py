"""Dynamic Pipeline Configuration Generator.

Composes step factories from pipeline.prompts modules to build
customizable pipelines with variable numbers of rounds.
"""
from __future__ import annotations

from pipeline.types import StepConfig
from pipeline.prompts.shared_blocks import (  # noqa: F401 -- re-exports for backward compat
    DIFFERENTIAL_OUTPUT_RULES,
    STRICT_GUARDRAILS,
    INTERACTOR_TYPES,
    FUNCTION_NAMING_RULES,
    CONTENT_DEPTH_REQUIREMENTS,
)
from pipeline.prompts import build_base_pipeline_steps
from pipeline.prompts.interactor_discovery import (
    BASE_DISCOVERY_FACTORIES,
    create_interactor_discovery_step,
)
from pipeline.prompts.function_mapping import (
    BASE_FUNCTION_FACTORIES,
    create_function_mapping_step,
)
from pipeline.prompts.deep_research_steps import (
    step2ab_chain_determination,
    # step2ab2_hidden_indirect_detection, step2ab3_hidden_chain_determination,
    # step2ab5_extract_pairs_explicit — NOT imported here because this
    # config skips the standalone steps; runner._run_chain_resolution_phase
    # imports and calls those factories directly (see runner.py:_run_track_a
    # and _run_track_b). They are ACTIVE, just orchestrated differently.
    step2ax_claim_generation_explicit,
    step2az_claim_generation_hidden,
)
from pipeline.prompts.qc_and_snapshot import step2g_final_qc, step3_snapshot


def generate_pipeline(
    num_interactor_rounds: int = 3,
    num_function_rounds: int = 3,
) -> list[StepConfig]:
    """Generate a complete pipeline with specified number of discovery rounds.

    Pipeline structure:
    1. Interactor Discovery (1a-1g+) - Find protein names only (direct/indirect)
    2. Function Discovery (2a-2a5+, 2b) - Find functions + paper titles + indirect
    3. Arrow Determination (2c) - GENERATED DYNAMICALLY BY RUNNER.PY (per-interactor)
    4. Final QC (2g) - Quality control
    5. Snapshot (3) - Create final output

    Args:
        num_interactor_rounds: Total interactor discovery rounds (default 3, min 1, max 10)
        num_function_rounds: Total function mapping rounds (default 3, min 1, max 10)

    Returns:
        List of StepConfig objects for the complete pipeline

    NOTE: Arrow determination steps (2c) are NOT included in this list.
          They are generated dynamically by runner.py based on interactor_history.
    """
    # Validate inputs
    num_interactor_rounds = max(1, min(10, num_interactor_rounds))
    num_function_rounds = max(1, min(10, num_function_rounds))

    steps: list[StepConfig] = []

    # Phase 1: Interactor discovery
    interactor_steps_to_add = min(num_interactor_rounds, len(BASE_DISCOVERY_FACTORIES))
    for i in range(interactor_steps_to_add):
        steps.append(BASE_DISCOVERY_FACTORIES[i]())

    # Extra interactor rounds beyond the 7 base steps
    if num_interactor_rounds > len(BASE_DISCOVERY_FACTORIES):
        for extra_round in range(
            len(BASE_DISCOVERY_FACTORIES) + 1, num_interactor_rounds + 1
        ):
            steps.append(create_interactor_discovery_step(extra_round))

    # Phase 2a: Function mapping
    function_steps_to_add = min(num_function_rounds, len(BASE_FUNCTION_FACTORIES))
    for i in range(function_steps_to_add):
        steps.append(BASE_FUNCTION_FACTORIES[i]())

    # Extra function rounds beyond the 5 base steps
    if num_function_rounds > len(BASE_FUNCTION_FACTORIES):
        for extra_round in range(
            len(BASE_FUNCTION_FACTORIES) + 1, num_function_rounds + 1
        ):
            steps.append(create_function_mapping_step(extra_round))

    # Phase 2b: Chain resolution (always included)
    steps.append(step2ab_chain_determination())
    # step2ab2_hidden_indirect_detection, step2ab3_hidden_chain_determination,
    # step2ab5_extract_pairs_explicit — retired. The chain resolution
    # orchestrator handles both explicit and hidden tracks in parallel and
    # emits the same outputs these sequential steps used to produce. The
    # runner used to log ``[SKIP] ...`` for each of them every query; with
    # the factories removed from the pipeline list, those SKIP lines are
    # gone and the pipeline config matches reality.
    steps.append(step2ax_claim_generation_explicit())
    steps.append(step2az_claim_generation_hidden())

    # Phase 2c: Arrow steps NOT included (generated dynamically by runner.py)

    # Phase 3: Snapshot (QC removed — _tag_shallow_functions does same check in pure code)
    steps.append(step3_snapshot())

    return steps


def generate_modern_pipeline(
    num_function_rounds: int = 2,
    skip_citation_verification: bool = False,
) -> list[StepConfig]:
    """Generate a modernized pipeline using Deep Research + Interactions API.

    Collapses 15-25 sequential generate_content() calls into ~5-6 steps:
    1. Deep Research: comprehensive interactor discovery (replaces steps 1a-1g)
    2. Interaction chain: function mapping (2-3 rounds with chaining)
    3. Interaction: combined deep function research (single chained call)
    4. QC pass
    5. Snapshot (local JSON assembly, no model call)

    Args:
        num_function_rounds: Function mapping rounds (default 2, min 1, max 3)

    Returns:
        List of StepConfig objects for the modern pipeline
    """
    from pipeline.prompts.modern_steps import (
        step1_deep_research_discovery,
        step2a_interaction_functions,
        step2e_citation_verification,
    )

    steps: list[StepConfig] = []

    # Phase 1: Single deep research call replaces 7 discovery rounds
    steps.append(step1_deep_research_discovery())

    # Phase 2a: Single marker — runner batches ALL interactors in parallel
    steps.append(step2a_interaction_functions(round_num=1))

    # Phase 2b: Chain resolution steps
    steps.append(step2ab_chain_determination())
    # step2ab2_hidden_indirect_detection, step2ab3_hidden_chain_determination,
    # step2ab5_extract_pairs_explicit — retired. The chain resolution
    # orchestrator handles both explicit and hidden tracks in parallel and
    # emits the same outputs these sequential steps used to produce. The
    # runner used to log ``[SKIP] ...`` for each of them every query; with
    # the factories removed from the pipeline list, those SKIP lines are
    # gone and the pipeline config matches reality.
    steps.append(step2ax_claim_generation_explicit())
    steps.append(step2az_claim_generation_hidden())

    # Phase 2e: Citation verification (optional — skippable via UI flag)
    if not skip_citation_verification:
        steps.append(step2e_citation_verification())

    # Phase 3: Snapshot
    steps.append(step3_snapshot())

    return steps


def generate_iterative_pipeline(
    num_function_rounds: int = 2,
    discovery_iterations: int = 5,
    skip_citation_verification: bool = False,
) -> list[StepConfig]:
    """Generate pipeline using iterative Interactions API for discovery.

    Replaces Deep Research (1 RPM) with N chained Interactions API calls
    to Gemini 3.1 Pro, each with Google Search + URL Context grounding.

    Pipeline structure:
    1. Iterative Research: N focused discovery iterations (replaces step1)
    2. Interaction chain: function mapping (2-3 rounds)
    3. Interaction: combined deep function research
    4. QC pass
    5. Snapshot

    Args:
        num_function_rounds: Function mapping rounds (default 2, min 1, max 3)
        discovery_iterations: Number of discovery iterations (default 5, min 1, max 10)
    """
    from pipeline.prompts.iterative_research_steps import (
        step1_iterative_research_discovery,
    )
    from pipeline.prompts.modern_steps import (
        step2a_interaction_functions,
        step2e_citation_verification,
    )

    discovery_iterations = max(1, min(10, discovery_iterations))

    steps: list[StepConfig] = []

    # Phase 1: Iterative research (N chained interaction calls, single StepConfig)
    steps.append(step1_iterative_research_discovery(num_iterations=discovery_iterations))

    # Phase 2a: Single marker — runner batches ALL interactors in parallel
    steps.append(step2a_interaction_functions(round_num=1))

    # Phase 2b: Chain resolution steps
    steps.append(step2ab_chain_determination())
    # step2ab2_hidden_indirect_detection, step2ab3_hidden_chain_determination,
    # step2ab5_extract_pairs_explicit — retired. The chain resolution
    # orchestrator handles both explicit and hidden tracks in parallel and
    # emits the same outputs these sequential steps used to produce. The
    # runner used to log ``[SKIP] ...`` for each of them every query; with
    # the factories removed from the pipeline list, those SKIP lines are
    # gone and the pipeline config matches reality.
    steps.append(step2ax_claim_generation_explicit())
    steps.append(step2az_claim_generation_hidden())

    # Phase 2e: Citation verification (optional — skippable via UI flag)
    if not skip_citation_verification:
        steps.append(step2e_citation_verification())

    # Phase 3: Snapshot
    steps.append(step3_snapshot())

    return steps


def get_default_pipeline() -> list[StepConfig]:
    """Get the default pipeline (same as base config)."""
    return build_base_pipeline_steps()


# For backwards compatibility, export PIPELINE_STEPS
PIPELINE_STEPS = get_default_pipeline()
