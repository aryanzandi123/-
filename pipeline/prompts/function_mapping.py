"""Function mapping step factories (Phase 2a: steps 2a-2a5+)."""
from __future__ import annotations

from pipeline.types import StepConfig, FUNCTION_MAPPING_OUTPUT_SCHEMA
from pipeline.prompts.shared_blocks import (
    DIFFERENTIAL_OUTPUT_RULES,
    STRICT_GUARDRAILS,
    FUNCTION_NAMING_RULES,
    CONTENT_DEPTH_REQUIREMENTS,
    FUNCTION_CONTEXT_LABELING,
    FUNCTION_HISTORY_HEADER,
    SCHEMA_HELP,
    MAX_OUTPUT_TOKENS,
    DYNAMIC_SEARCH_THRESHOLD,
)

_FUNCTION_PREAMBLE = (
    DIFFERENTIAL_OUTPUT_RULES + "\n\n"
    + STRICT_GUARDRAILS + "\n\n"
    + FUNCTION_NAMING_RULES + "\n\n"
    + CONTENT_DEPTH_REQUIREMENTS + "\n\n"
    + FUNCTION_CONTEXT_LABELING + "\n\n"
)

_FUNCTION_HISTORY_BLOCK = "\n\n" + FUNCTION_HISTORY_HEADER

_EXPECTED_COLUMNS = ["ctx_json", "step_json"]


def _function_step(
    *,
    name: str,
    instruction: str,
    max_output_tokens: int = 65536,
) -> StepConfig:
    prompt = _FUNCTION_HISTORY_BLOCK + instruction + "\n\n" + "Return ONLY JSON."
    return StepConfig(
        name=name,
        model="gemini-3-flash-preview", #WAS GEMINI-3.1-PRO="gemini-3.1-pro-preview",
        use_google_search=True,
        thinking_level="high",
        max_output_tokens=max_output_tokens,
        temperature=1.0,
        search_dynamic_mode=True,
        search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=_EXPECTED_COLUMNS,
        system_prompt=_FUNCTION_PREAMBLE,
        cache_system_prompt=True,
        prompt_template=prompt,
        response_schema=FUNCTION_MAPPING_OUTPUT_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Step 2a: MAP FUNCTIONS (first batch) + TRACK INDIRECT INTERACTORS
# ---------------------------------------------------------------------------

def step2a_functions() -> StepConfig:
    """First batch of function mapping with indirect interactor tracking."""
    return _function_step(
        name="step2a_functions",

        instruction="\n".join([
            "STEP 2a — MAP FUNCTIONS + COLLECT PAPER TITLES + TRACK INDIRECT",
            "",
            "MAIN: {ctx_json.main}",
            "INTERACTORS: {ctx_json.interactor_history}",
            "ALREADY PROCESSED: {ctx_json.function_batches}",
            "",
            "TASK: process the first 8-10 interactors NOT in function_batches.",
            "Before researching, list your target batch explicitly.",
            "",
            "FOR EACH INTERACTOR, find every distinct biological function the",
            "literature actually supports. For each function, emit:",
            "  • function name (see FUNCTION_NAMING_RULES)",
            "  • arrow ('activates' | 'inhibits') — effect on the named function",
            "  • interaction_direction ('main_to_primary' | 'primary_to_main')",
            "  • cellular_process, effect_description, biological_consequence,",
            "    specific_effects, evidence, pathway — per",
            "    CONTENT_DEPTH_REQUIREMENTS in the system prompt",
            "",
            "TRACK INDIRECT INTERACTORS found in cascades:",
            "  If a cascade mentions OTHER proteins (e.g. 'VCP activates mTOR",
            "  which activates S6K'), add to indirect_interactors:",
            "    {'name': 'S6K', 'upstream_interactor': 'mTOR',",
            "     'discovered_in_function': '<name>',",
            "     'role_in_cascade': 'downstream effector'}",
            "",
            "SEARCH FREELY. No prescribed query list.",
            "",
            "OUTPUT:",
            "  ctx_json.interactors[].functions — function dicts per schema",
            "  ctx_json.indirect_interactors   — cascade proteins discovered",
            "  ctx_json.function_batches       — every processed interactor name",
            "",
            "function_batches length MUST equal the batch size.",
        ]),
    )


# ---------------------------------------------------------------------------
# Step 2a2: Continue function mapping + paper titles + indirect tracking
# ---------------------------------------------------------------------------

def step2a2_functions_batch() -> StepConfig:
    """Continue function mapping for the next batch of interactors."""
    return _function_step(
        name="step2a2_functions_batch",

        instruction="\n".join([
            "STEP 2a2 — CONTINUE FUNCTION MAPPING",
            "",
            "ALREADY PROCESSED: {ctx_json.function_batches}",
            "",
            "TASK: process the next batch of unprocessed interactors.",
            "  1. Identify interactors NOT in function_batches.",
            "  2. Generate functions + collect paper titles + track indirect",
            "     interactors found in cascades.",
            "  3. Add every processed name to function_batches output.",
            "",
            "DEPTH: apply CONTENT_DEPTH_REQUIREMENTS from the system prompt",
            "(cellular_process, effect_description, biological_consequence,",
            "specific_effects, evidence) + FUNCTION_NAMING_RULES. Set",
            "``pathway`` on each function.",
            "",
            "Search freely — no prescribed query list.",
        ]),
    )


# ---------------------------------------------------------------------------
# Step 2a3: Exhaustive function sweep + paper titles + indirect tracking
# ---------------------------------------------------------------------------

def step2a3_functions_exhaustive() -> StepConfig:
    """Exhaustive sweep to achieve 100% interactor coverage."""
    return _function_step(
        name="step2a3_functions_exhaustive",

        instruction="\n".join([
            "STEP 2a3 — EXHAUSTIVE FUNCTION SWEEP",
            "",
            "ALREADY PROCESSED: {ctx_json.function_batches}",
            "",
            "TASK: process EVERY remaining interactor not in function_batches.",
            "At the end of this step, function_batches should equal",
            "interactor_history — no interactor left without functions.",
            "",
            "Generate functions, collect paper titles, track indirect",
            "interactors from cascades. Apply CONTENT_DEPTH_REQUIREMENTS",
            "and FUNCTION_NAMING_RULES from the system prompt.",
            "",
            "Search freely. Add every processed name to function_batches.",
        ]),
    )


# ---------------------------------------------------------------------------
# Step 2a4: Round 2 function mapping + paper titles + indirect tracking
# ---------------------------------------------------------------------------

def step2a4_functions_round2() -> StepConfig:
    """Round 2 revisit for additional/context-dependent functions."""
    return _function_step(
        name="step2a4_functions_round2",

        instruction="\n".join([
            "STEP 2a4 — ROUND 2 FUNCTION REVISIT",
            "",
            "Revisit interactors for additional functions: alternative outcomes,",
            "context-dependent mechanisms, recent papers. Track indirect",
            "interactors from any new cascades.",
            "",
            "UNIQUENESS: check accumulated context before emitting. Each new",
            "function MUST describe a DIFFERENT biological mechanism — a",
            "rephrase of an existing function is a duplicate.",
            "",
            "Apply CONTENT_DEPTH_REQUIREMENTS and FUNCTION_NAMING_RULES from",
            "the system prompt. Search freely.",
        ]),
    )


# ---------------------------------------------------------------------------
# Step 2a5: Round 3 function mapping + paper titles + indirect tracking
# ---------------------------------------------------------------------------

def step2a5_functions_round3() -> StepConfig:
    """Round 3 final creative sweep for remaining functions."""
    return _function_step(
        name="step2a5_functions_round3",

        instruction="\n".join([
            "STEP 2a5 — FINAL FUNCTION SWEEP",
            "",
            "Final sweep for missing functions — alternative outcomes,",
            "context-specific mechanisms, anything the prior rounds missed.",
            "Track indirect interactors from every cascade.",
            "",
            "UNIQUENESS: each function MUST describe a DIFFERENT biological",
            "mechanism, not a rephrase of an existing one.",
            "",
            "Apply CONTENT_DEPTH_REQUIREMENTS and FUNCTION_NAMING_RULES from",
            "the system prompt. Search freely.",
        ]),
    )


# ---------------------------------------------------------------------------
# Ordered list of the 5 base factory functions
# ---------------------------------------------------------------------------

BASE_FUNCTION_FACTORIES = [
    step2a_functions,
    step2a2_functions_batch,
    step2a3_functions_exhaustive,
    step2a4_functions_round2,
    step2a5_functions_round3,
]


# ---------------------------------------------------------------------------
# Dynamic step creator for additional rounds (round 4, 5, 6, ...)
# ---------------------------------------------------------------------------

def create_function_mapping_step(round_num: int) -> StepConfig:
    """
    Create an additional function mapping step dynamically.
    NEW: Includes paper title collection + indirect interactor tracking

    Args:
        round_num: Round number (4, 5, 6, etc.)

    Returns:
        StepConfig for this round
    """
    # Function rounds: 2a4, 2a5, 2a6, etc.
    step_name = f"step2a{round_num}_functions_round{round_num}"

    ordinals = {
        4: "Fourth", 5: "Fifth", 6: "Sixth", 7: "Seventh",
        8: "Eighth", 9: "Ninth", 10: "Tenth"
    }
    ordinal = ordinals.get(round_num, f"{round_num}th")

    return _function_step(
        name=step_name,

        instruction="\n".join([
            f"STEP 2a{round_num} — {ordinal.upper()} ROUND FUNCTION MAPPING",
            "",
            "MAIN: {ctx_json.main}",
            "INTERACTORS: {ctx_json.interactor_history}",
            "COVERAGE: {ctx_json.function_batches}",
            "",
            f"TASK: round {round_num} — add new, distinct functions across interactors.",
            "Each function MUST describe a mechanism not already in ctx_json.",
            "",
            "Apply CONTENT_DEPTH_REQUIREMENTS and FUNCTION_NAMING_RULES from",
            "the system prompt. Track indirect interactors found in cascades",
            "with FULL chains (no truncation). Example full chain:",
            "  '{main} → VCP → LAMP2 → RAB7 → LAMP1'",
            "    LAMP2: upstream='VCP',   chain=['VCP'],             depth=2",
            "    RAB7:  upstream='LAMP2', chain=['VCP','LAMP2'],     depth=3",
            "    LAMP1: upstream='RAB7',  chain=['VCP','LAMP2','RAB7'], depth=4",
            "",
            "Search freely.",
        ]),
    )
