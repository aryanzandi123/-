"""QC and snapshot step factories (Phase 3: steps 2g, 3)."""
from __future__ import annotations

from pipeline.types import StepConfig, QC_OUTPUT_SCHEMA
from pipeline.prompts.shared_blocks import (
    DIFFERENTIAL_OUTPUT_RULES,
    STRICT_GUARDRAILS,
    SCHEMA_HELP,
    MAX_OUTPUT_TOKENS,
    DYNAMIC_SEARCH_THRESHOLD,
)


def step2g_final_qc() -> StepConfig:
    """Final quality-control pass over all interactors and functions (Phase 2g)."""
    instruction = "\n".join([
        "STEP 2g — FINAL QUALITY CONTROL",
        "",
        "STRUCTURAL CHECKS:",
        "  • arrows/directions set for every interactor (Step 2c)",
        "  • function names are ultra-specific (no 'Cell Survival', 'Regulation')",
        "  • indirect interactors have upstream_interactor set",
        "  • pathway field set on each function",
        "",
        "DEPTH SPOT-CHECK (10 random functions across interactors).",
        "For each one, count against CONTENT_DEPTH_REQUIREMENTS:",
        "  • cellular_process     — ≥ 6 sentences",
        "  • effect_description   — ≥ 4 sentences",
        "  • biological_consequence — ≥ 3 cascades, ≥ 6 steps each",
        "  • specific_effects     — ≥ 5 entries (technique + model + result)",
        "  • evidence             — ≥ 3 papers (paraphrased quote, assay,",
        "                           species, key_finding)",
        "",
        "Any function failing ANY check → add to flagged_for_enrichment.",
        "",
        "OUTPUT in step_json:",
        "  'flagged_for_enrichment': [{interactor, function} pairs that failed]",
        "  'depth_check_passed':     <passing>/10",
        "",
        "Note: evidence validation runs in a dedicated post-processing step.",
    ])
    return StepConfig(
        name="step2g_final_qc",
        model="gemini-3-flash-preview",

        use_google_search=False,
        thinking_level="medium",
        max_output_tokens=65536,
        temperature=0.3,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            DIFFERENTIAL_OUTPUT_RULES
            + "\n\n"
            + STRICT_GUARDRAILS
            + "\n\n"
            + instruction
            + "\n\n"
            + "Return ONLY JSON with step_json={'step':'step2g_final_qc','status':'validated'}"
        ),
        response_schema=QC_OUTPUT_SCHEMA,
    )


def step3_snapshot() -> StepConfig:
    """Snapshot step handled entirely by the runner (Phase 3)."""
    return StepConfig(
        name="step3_snapshot",
        model="gemini-3-flash-preview", #WAS GEMINI-3.1-PRO="gemini-3.1-pro-preview",

        use_google_search=False,
        thinking_level="medium",
        max_output_tokens=65536,
        expected_columns=["ctx_json", "snapshot_json", "ndjson", "step_json"],
        system_prompt=None,
        prompt_template="Snapshot handled by runner.",
    )
