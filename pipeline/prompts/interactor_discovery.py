"""Interactor discovery step factories (Phase 1: steps 1a-1g+).

Legacy per-step discovery pipeline. Prompts are deliberately slim —
theme + classification + schema + "go search freely, skip what's already
in context". No prescribed search query lists, no quantity floors;
quantity follows from thorough research, and the exclusion block injected
by ``build_known_interactions_context`` tells the model what to skip.
"""
from __future__ import annotations

from pipeline.types import StepConfig, DISCOVERY_OUTPUT_SCHEMA
from pipeline.prompts.shared_blocks import (
    DIFFERENTIAL_OUTPUT_RULES,
    STRICT_GUARDRAILS,
    INTERACTOR_TYPES,
    MAX_OUTPUT_TOKENS,
    DYNAMIC_SEARCH_THRESHOLD,
)

_SEARCH_DEDUP_RULE = (
    "SEARCH HISTORY (skip these): {ctx_json.search_history}\n"
    "Pick novel angles — don't repeat queries already tried.\n\n"
)

_DISCOVERY_PREAMBLE = (
    DIFFERENTIAL_OUTPUT_RULES + "\n\n"
    + STRICT_GUARDRAILS + "\n\n"
    + INTERACTOR_TYPES + "\n\n"
    + _SEARCH_DEDUP_RULE
)

_EXPECTED_COLUMNS = ["ctx_json", "step_json"]


# ── Shared blocks reused by every discovery step body ────────────────

_FREE_SEARCH = (
    "SEARCH APPROACH — GO FREE:\n"
    "You have Google Search. Pick your own queries, angles, and databases.\n"
    "You know the literature better than any rigid template.\n"
    "\n"
    "WHAT NOT TO RESEARCH AGAIN:\n"
    "  - Anything in ``interactor_history`` already captured this run.\n"
    "  - The 'ALREADY IN DATABASE' block, if present — partners found in\n"
    "    prior runs for this same query protein.\n"
    "Skip both. Every name emitted this turn MUST be new relative to them.\n"
)

_CLASSIFICATION = (
    "CLASSIFICATION:\n"
    "  DIRECT   → physical interaction (Co-IP, Y2H, BioID, pull-down, SPR, structure)\n"
    "  INDIRECT → same pathway/cascade, no direct binding evidence;\n"
    "             set upstream_interactor, mediator_chain, depth.\n"
    "Chain length is uncapped — emit the true cascade length.\n"
)

_MINIMAL_SCHEMA = "\n".join([
    "OUTPUT (names + classification only — NO arrows, evidence, or functions):",
    "{",
    "  'ctx_json': {",
    "    'main': '{user_query}',",
    "    'interactors': [",
    "      {'primary': 'VCP', 'interaction_type': 'direct',",
    "       'upstream_interactor': null, 'mediator_chain': [], 'depth': 1,",
    "       'support_summary': '<brief>'},",
    "      {'primary': 'LAMP2', 'interaction_type': 'indirect',",
    "       'upstream_interactor': 'VCP', 'mediator_chain': ['VCP'], 'depth': 2,",
    "       'support_summary': '<brief>'}",
    "    ],",
    "    'interactor_history': ['VCP', 'LAMP2', ...],",
    "    'search_history': ['<queries used>']",
    "  }",
    "}",
    "",
    "DO NOT INCLUDE: arrow, direction, intent, evidence, paper_title, pmids, functions.",
])


# ------------------------------------------------------------------
# Internal helper
# ------------------------------------------------------------------

def _discovery_step(
    *,
    name: str,
    instruction: str,
    max_output_tokens: int = 65536,
) -> StepConfig:
    """Build a StepConfig for an interactor-discovery step."""
    return StepConfig(
        name=name,
        model="gemini-3-flash-preview",
        use_google_search=True,
        thinking_level="medium",
        max_output_tokens=max_output_tokens,
        temperature=0.3,
        search_dynamic_mode=True,
        search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=_EXPECTED_COLUMNS,
        system_prompt=_DISCOVERY_PREAMBLE,
        cache_system_prompt=True,
        prompt_template=instruction,
        response_schema=DISCOVERY_OUTPUT_SCHEMA,
    )


def _themed_step(name: str, header: str, theme: str) -> StepConfig:
    """Build a themed discovery step from a one-line theme sentence."""
    instruction = "\n".join([
        header,
        "",
        "QUERY PROTEIN: {user_query}",
        "EXISTING INTERACTORS: {ctx_json.interactor_history}",
        "",
        f"THEME: {theme}",
        "",
        _FREE_SEARCH,
        _CLASSIFICATION,
        _MINIMAL_SCHEMA,
        "",
        "Return ONLY JSON.",
    ])
    return _discovery_step(name=name, instruction=instruction)


# ------------------------------------------------------------------
# Step 1a – Initial interactor discovery
# ------------------------------------------------------------------

def step1a_discover() -> StepConfig:
    """Factory for step 1a: initial discovery (names only)."""
    instruction = "\n".join([
        "STEP 1a — INITIAL INTERACTOR DISCOVERY",
        "",
        "QUERY PROTEIN: {user_query}",
        "Initialize ctx_json with 'main': '{user_query}' as the first field.",
        "",
        "THEME: the unbiased opening pass — the full interactome. Physical",
        "binders, complex members, substrates, regulators, pathway partners,",
        "disease-context partners. Every well-documented interactor is in scope.",
        "",
        _FREE_SEARCH,
        _CLASSIFICATION,
        "",
        "For indirect interactors include the FULL chain, not a truncated one.",
        "Example: '{user_query} → VCP → LAMP2 → Catalase'",
        "  VCP:      direct,   depth=1, mediator_chain=[]",
        "  LAMP2:    indirect, upstream_interactor='VCP',",
        "            mediator_chain=['VCP'], depth=2",
        "  Catalase: indirect, upstream_interactor='LAMP2',",
        "            mediator_chain=['VCP','LAMP2'], depth=3",
        "",
        _MINIMAL_SCHEMA,
        "",
        "Return ONLY JSON with ctx_json and step_json={'step':'step1a_discover','count':<n>}",
    ])
    return _discovery_step(name="step1a_discover", instruction=instruction)


# ------------------------------------------------------------------
# Step 1b – Expand interactor network
# ------------------------------------------------------------------

def step1b_expand() -> StepConfig:
    """Factory for step 1b: expand interactor network (names only)."""
    return _themed_step(
        name="step1b_expand",
        header="STEP 1b — EXPAND INTERACTOR NETWORK",
        theme=(
            "adaptors, scaffolds, and substrates of {ctx_json.main} — "
            "proteins that physically bridge or scaffold interactions, "
            "plus substrates {ctx_json.main} directly acts on."
        ),
    )


# ------------------------------------------------------------------
# Step 1c – Deep literature mining
# ------------------------------------------------------------------

def step1c_deep_mining() -> StepConfig:
    """Factory for step 1c: deep literature mining (names only)."""
    return _themed_step(
        name="step1c_deep_mining",
        header="STEP 1c — DEEP LITERATURE MINING",
        theme=(
            "pathway partners and co-complex members — proteins in the same "
            "signaling cascades as {ctx_json.main}, including upstream "
            "kinases and downstream effectors."
        ),
    )


# ------------------------------------------------------------------
# Step 1d – Round 2 interactor discovery
# ------------------------------------------------------------------

def step1d_discover_round2() -> StepConfig:
    """Factory for step 1d: round 2 interactor discovery (names only)."""
    return _themed_step(
        name="step1d_discover_round2",
        header="STEP 1d — ROUND 2 INTERACTOR DISCOVERY",
        theme=(
            "disease-associated and stress-responsive interactors — partners "
            "linked to {ctx_json.main} in disease contexts, stress responses "
            "(ER stress, oxidative stress, DNA damage), or pathological states."
        ),
    )


# ------------------------------------------------------------------
# Step 1e – Round 3 interactor discovery
# ------------------------------------------------------------------

def step1e_discover_round3() -> StepConfig:
    """Factory for step 1e: round 3 interactor discovery (names only)."""
    return _themed_step(
        name="step1e_discover_round3",
        header="STEP 1e — ROUND 3 INTERACTOR DISCOVERY",
        theme=(
            "tissue-specific and recently published interactors — recent "
            "publications and tissue-specific partners of {ctx_json.main}."
        ),
    )


# ------------------------------------------------------------------
# Step 1f – Round 4 interactor discovery
# ------------------------------------------------------------------

def step1f_discover_round4() -> StepConfig:
    """Factory for step 1f: round 4 interactor discovery (names only)."""
    return _themed_step(
        name="step1f_discover_round4",
        header="STEP 1f — ROUND 4 INTERACTOR DISCOVERY",
        theme=(
            "rare / obscure partners — proximity labeling hits, "
            "high-throughput screens, specialized interaction databases."
        ),
    )


# ------------------------------------------------------------------
# Step 1g – Round 5 interactor discovery
# ------------------------------------------------------------------

def step1g_discover_round5() -> StepConfig:
    """Factory for step 1g: round 5 interactor discovery (names only)."""
    return _themed_step(
        name="step1g_discover_round5",
        header="STEP 1g — ROUND 5 INTERACTOR DISCOVERY",
        theme=(
            "cross-species and computational partners — model organism "
            "studies (mouse, fly, worm, yeast) and computationally predicted "
            "interactions not yet covered."
        ),
    )


# ------------------------------------------------------------------
# Ordered list of the 7 base discovery factories
# ------------------------------------------------------------------

BASE_DISCOVERY_FACTORIES = [
    step1a_discover,
    step1b_expand,
    step1c_deep_mining,
    step1d_discover_round2,
    step1e_discover_round3,
    step1f_discover_round4,
    step1g_discover_round5,
]


# ------------------------------------------------------------------
# Dynamic step creator for additional rounds (round 4+)
# ------------------------------------------------------------------

def create_interactor_discovery_step(round_num: int) -> StepConfig:
    """Create an additional interactor discovery step dynamically.

    Args:
        round_num: Round number (4, 5, 6, etc.)

    Returns:
        StepConfig for this round
    """
    letter = chr(ord('f') + (round_num - 4))
    ordinals = {
        4: "Fourth", 5: "Fifth", 6: "Sixth", 7: "Seventh",
        8: "Eighth", 9: "Ninth", 10: "Tenth",
    }
    ordinal = ordinals.get(round_num, f"{round_num}th")

    return _themed_step(
        name=f"step1{letter}_discover_round{round_num}",
        header=f"STEP 1{letter.upper()} — {ordinal.upper()} ROUND INTERACTOR DISCOVERY",
        theme=(
            "any additional partners the prior rounds missed. Follow whichever "
            "angle you think hasn't been mined yet — proteomics screens, recent "
            "preprints, cross-species work, or specialist databases."
        ),
    )
