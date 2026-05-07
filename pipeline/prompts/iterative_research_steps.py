"""Iterative research step factories using Interactions API.

Replaces the single Deep Research call (1 RPM rate-limited) with N focused
iterations via Gemini 3.1 Pro + Google Search + URL Context grounding.
Each iteration targets a different search angle to maximize coverage.

Default strategy (6 iterations):
  1. Broad discovery — well-established interactors
  2. Broad expansion — deeper/harder-to-find interactors (substrates, chaperones, proximity)
  3. Pathway & signaling — cascade/regulatory partners
  4. Disease & functional context — disease-model interactors
  5. Deep literature mining — recent/obscure findings
  6. Consolidation & gap-fill — verify, deduplicate, classify
"""
from __future__ import annotations

from pipeline.types import StepConfig, IterationConfig, DISCOVERY_OUTPUT_SCHEMA
from pipeline.prompts.shared_blocks import (
    DIFFERENTIAL_OUTPUT_RULES,
    STRICT_GUARDRAILS,
    INTERACTOR_TYPES,
    SCHEMA_HELP,
    MAX_OUTPUT_TOKENS,
)
from utils.gemini_runtime import get_model

_EXPECTED_COLUMNS = ["ctx_json", "step_json"]


# ── Shared system prompt for all iterations ─────────────────────────────
_ITERATIVE_SYSTEM_PROMPT = "\n\n".join([
    STRICT_GUARDRAILS,
    DIFFERENTIAL_OUTPUT_RULES,
    INTERACTOR_TYPES,
])


# ── Common output schema block (appended to every iteration prompt) ─────
# Shared freedom block — every iteration emits the same "go search freely,
# skip what you already know" directive. No prescriptive query lists, no
# numbered strategies, no quantity floors. The model has Google Search +
# its own judgement; the only constraints are (a) what NOT to re-fetch
# (already-in-context interactors from prior iterations in this run, PLUS
# the DB-exclusion list injected by the context builder) and (b) the
# output schema.
_FREE_RESEARCH_BLOCK = """
SEARCH APPROACH — GO FREE:
You have Google Search. Pick your own queries, angles, and databases.
You know the literature better than any rigid template; do not follow a
scripted list.

WHAT NOT TO RESEARCH AGAIN:
  1. Anything listed in ``interactor_history`` in the conversation context
     — already captured this run.
  2. The "ALREADY IN DATABASE" block, if present — partners already
     discovered in prior runs for this same query protein.

Skip both. Every name you emit this turn MUST be new relative to both
sources. Stop when you genuinely can't find new literature-supported
partners for the theme — don't pad.
"""


_OUTPUT_SCHEMA = """
OUTPUT STRUCTURE (MANDATORY — return ONLY this JSON).
The "interactors" array MUST contain EVERY protein you found, not just
one or two. The examples below are illustrative — emit the same shape
for every interactor (direct and indirect). NO UPPER LIMIT.

════════════════════════════════════════════════════════════════════
CRITICAL RULE FOR INDIRECT INTERACTORS — chain_context.full_chain
════════════════════════════════════════════════════════════════════
When interaction_type="indirect", you MUST emit
``chain_context.full_chain`` as the COMPLETE ORDERED biological
cascade. This array lists EVERY protein in the cascade in biological
order, including the query AND the target. Length is AT LEAST 3
(query + one mediator + target). Longer is REQUIRED whenever the
literature supports it.

The query protein can appear at ANY index in ``full_chain`` — start
(downstream-of-query cascade), end (upstream-of-query cascade), or
middle (query sits in the middle of a pathway). Do NOT force the
query to index 0.

Length-3 cascades (one mediator) are RARE and almost always signal
missing biology — real pathways are typically 4–6 proteins, often
longer. Before emitting any chain ask: "what happens BETWEEN each
adjacent pair? Am I missing an intermediate?" If a kinase
phosphorylates another kinase which phosphorylates the target, that
is length 4, not 3. Do NOT stop at length 3 just because it's the
minimum valid shape — stop when the literature stops naming new
intermediates. A length-3 emission is an admission that you don't
know the rest of the pathway.

If you cannot name every intermediate protein with confidence, set
``interaction_type="direct"`` instead — we would rather record a
direct interaction than fabricate mediators.

══ EXAMPLES (all required) ══════════════════════════════════════════

Length-4 cascade, query at start (downstream regulation):
  {user_query} → MDM2 → TP53 → BAX
    chain_context.full_chain: ["{user_query}", "MDM2", "TP53", "BAX"]
    depth: 4

Length-5 cascade, query mid-chain (pathway intermediate):
  VCP → {user_query} → MDM2 → TP53 → BAX
    chain_context.full_chain:
      ["VCP", "{user_query}", "MDM2", "TP53", "BAX"]
    depth: 5

Length-4 cascade, query at end (upstream-acting proteins):
  CHIP → STUB1 → HSPA8 → {user_query}
    chain_context.full_chain:
      ["CHIP", "STUB1", "HSPA8", "{user_query}"]
    depth: 4

Length-6 cascade (well-documented biology often has this shape):
  {user_query} → BECN1 → PIK3C3 → ATG14 → ATG7 → LC3
    chain_context.full_chain:
      ["{user_query}", "BECN1", "PIK3C3", "ATG14", "ATG7", "LC3"]
    depth: 6

═══ OUTPUT JSON TEMPLATE ═══════════════════════════════════════════
{
  "ctx_json": {
    "main": "{user_query}",
    "interactors": [
      {
        "primary": "<HGNC_SYMBOL_1>",
        "interaction_type": "direct",
        "depth": 1,
        "support_summary": "<brief evidence summary>"
      },
      {
        "primary": "<TARGET_OF_CASCADE>",
        "interaction_type": "indirect",
        "depth": <len(full_chain)>,
        "chain_context": {
          "full_chain": [
            "{user_query}",
            "<MEDIATOR_1>",
            "<MEDIATOR_2>",
            "<TARGET_OF_CASCADE>"
          ]
        },
        "support_summary": "<why this indirect cascade is real>"
      }
      // ... continue with EVERY additional interactor you find —
      //     no cap. Well-studied proteins have 40+ interactors.
    ],
    "interactor_history": ["<all discovered protein names>"],
    "search_history": ["<search queries used>"],
    "upstream_of_main": ["<proteins that act ON the query>"]
  },
  "step_json": {"step": "step1_iterative_research_discovery", "count": <n>}
}

NOTE on legacy fields: ``upstream_interactor`` and ``mediator_chain``
are DEPRECATED — do not emit them. ``chain_context.full_chain`` is
the sole source of truth for cascade topology going forward.

DO NOT INCLUDE: arrow, direction, intent, evidence array, paper_title,
pmids, functions — these are determined in subsequent function mapping steps.

Return ONLY JSON. No markdown, no prose, no explanations.
"""


# ════════════════════════════════════════════════════════════════════════
# ITERATION 0: UPSTREAM CONTEXT
# ════════════════════════════════════════════════════════════════════════
# Dedicated pass that goes looking for proteins that act ON the query —
# upstream regulators, kinases-of, phosphatases-of, ligases-of, etc. —
# not the query's downstream partners. Populates two things that the
# rest of the pipeline uses to render chains with the query in its
# biologically-correct position (even when that's mid-chain):
#
#   1. ``ctx_json.upstream_of_main`` — a flat list of proteins upstream
#      of the query. Lets later iterations know "VCP acts on my query;
#      if I discover a downstream partner D reached via the cascade, the
#      full chain is VCP → QUERY → ... → D, not QUERY → ... → D."
#   2. Interactors whose ``chain_context.full_chain`` places the query at
#      a non-zero index. db_sync already honors this — we just need the
#      LLM to actually emit it.
#
# Runs first so subsequent iterations see the upstream context in the
# accumulated conversation and don't re-discover it.

_ITER0_UPSTREAM = IterationConfig(
    name="upstream_context",
    focus="Find proteins that act on the query (upstream regulators)",
    prompt_template="\n".join([
        "ITERATION: UPSTREAM CONTEXT",
        "",
        "QUERY PROTEIN: {user_query}",
        "",
        "THEME: proteins that act ON {user_query} — upstream regulators,",
        "kinases-of, phosphatases-of, ligases-of, proteases-of, binders-upstream-of.",
        "Downstream partners of {user_query} belong to other iterations; skip them.",
        _FREE_RESEARCH_BLOCK,
        "OUTPUT RULES — upstream entries go in TWO places:",
        "  (a) ``interactors`` as a normal entry (direct or indirect).",
        "  (b) ``upstream_of_main`` at the ctx_json top level — flat symbol list.",
        "",
        "For upstream proteins, emit ``chain_context.full_chain`` with the query",
        "at the END of the chain, not the start:",
        "",
        "  VCP → {user_query}:",
        "    chain_context.full_chain: ['VCP', '{user_query}']",
        "",
        "  VCP → MDM2 → {user_query}:",
        "    interaction_type: 'indirect', upstream_interactor: 'MDM2'",
        "    mediator_chain: ['MDM2']",
        "    chain_context.full_chain: ['VCP', 'MDM2', '{user_query}']",
        _OUTPUT_SCHEMA,
    ]),
)


# ════════════════════════════════════════════════════════════════════════
# ITERATION 1: BROAD DISCOVERY
# ════════════════════════════════════════════════════════════════════════

_ITER1_BROAD = IterationConfig(
    name="broad_discovery",
    focus="Cast a wide net for all known protein interactors",
    prompt_template="\n".join([
        "ITERATION: BROAD INTERACTOR DISCOVERY",
        "",
        "QUERY PROTEIN: {user_query}",
        "",
        "Initialize ctx_json with 'main': '{user_query}' as the first field.",
        "",
        "THEME: the unbiased opening pass — the full interactome. Physical",
        "binders, complex members, substrates, regulators, pathway partners,",
        "disease-context partners. Every well-documented interactor is in scope.",
        _FREE_RESEARCH_BLOCK,
        "CLASSIFICATION:",
        "  DIRECT   → physical interaction evidence (Co-IP, Y2H, BioID,",
        "             pull-down, SPR, structure).",
        "  INDIRECT → same pathway/cascade with no direct binding evidence;",
        "             set upstream_interactor + mediator_chain + depth.",
        "",
        "Chain length is uncapped. A real 4-, 5-, 6-protein cascade is emitted",
        "at full length, not truncated. Example:",
        "  '{user_query} → VCP → LAMP2 → Catalase'",
        "    VCP:     direct,   depth=1, mediator_chain=[]",
        "    LAMP2:   indirect, upstream_interactor='VCP',",
        "             mediator_chain=['VCP'], depth=2",
        _OUTPUT_SCHEMA,
    ]),
)


# ════════════════════════════════════════════════════════════════════════
# ITERATION 2: BROAD DISCOVERY EXPANSION (second pass)
# ════════════════════════════════════════════════════════════════════════

_ITER2_BROAD_EXPANSION = IterationConfig(
    name="broad_discovery_expansion",
    focus="Expand broad discovery — find interactors missed in first pass",
    prompt_template="\n".join([
        "ITERATION: BROAD DISCOVERY EXPANSION",
        "",
        "QUERY PROTEIN: {user_query}",
        "",
        "THEME: partners the opening pass missed — tissue-specific,",
        "condition-specific, recently published, or buried in proteomics /",
        "proximity-labeling / crosslinking datasets rather than the top",
        "interactome-database hits.",
        _FREE_RESEARCH_BLOCK,
        "Same classification + chain rules as iteration 1.",
        _OUTPUT_SCHEMA,
    ]),
)


# ════════════════════════════════════════════════════════════════════════
# ITERATION 3: PATHWAY & SIGNALING
# ════════════════════════════════════════════════════════════════════════

_ITER3_PATHWAY = IterationConfig(
    name="pathway_discovery",
    focus="Signaling pathway and regulatory cascade partners",
    prompt_template="\n".join([
        "ITERATION: PATHWAY & SIGNALING PARTNERS",
        "",
        "QUERY PROTEIN: {user_query}",
        "",
        "THEME: cascade members and regulatory relationships — upstream",
        "regulators, downstream effectors, kinase / phosphatase / GAP / GEF",
        "partners. Use whatever pathway resources the literature points to.",
        "Track the full mediator chain for every indirect link.",
        _FREE_RESEARCH_BLOCK,
        "Same classification + chain rules as iteration 1.",
        _OUTPUT_SCHEMA,
    ]),
)


# ════════════════════════════════════════════════════════════════════════
# ITERATION 3: DISEASE & FUNCTIONAL CONTEXT
# ════════════════════════════════════════════════════════════════════════

_ITER4_DISEASE = IterationConfig(
    name="disease_context",
    focus="Disease-associated interactors and clinical relevance",
    prompt_template="\n".join([
        "ITERATION: DISEASE & FUNCTIONAL CONTEXT",
        "",
        "QUERY PROTEIN: {user_query}",
        "",
        "THEME: partners only surfacing in disease models, mutant backgrounds,",
        "stress conditions, patient samples, aggregate pulldowns, or KO/KD",
        "proteomics. These typically aren't in standard interactome databases;",
        "they live in the primary literature.",
        _FREE_RESEARCH_BLOCK,
        "Same classification + chain rules as iteration 1.",
        _OUTPUT_SCHEMA,
    ]),
)


# ════════════════════════════════════════════════════════════════════════
# ITERATION 4: DEEP LITERATURE MINING
# ════════════════════════════════════════════════════════════════════════

_ITER4_DEEP_LIT = IterationConfig(
    name="deep_literature",
    focus="Recent publications, proteomics datasets, obscure interactors",
    prompt_template="\n".join([
        "ITERATION: DEEP LITERATURE MINING",
        "",
        "QUERY PROTEIN: {user_query}",
        "",
        "THEME: the cutting edge — recent publications, preprints,",
        "proximity-labeling hits, crosslinking-MS results, single-cell",
        "proteomics, anything not yet consolidated into standard databases.",
        _FREE_RESEARCH_BLOCK,
        "Same classification + chain rules as iteration 1.",
        _OUTPUT_SCHEMA,
    ]),
)


# ════════════════════════════════════════════════════════════════════════
# ITERATION 5: CONSOLIDATION & GAP-FILL
# ════════════════════════════════════════════════════════════════════════

_ITER5_CONSOLIDATION = IterationConfig(
    name="consolidation",
    focus="Verify, deduplicate, fill gaps, and finalize classifications",
    prompt_template="\n".join([
        "ITERATION: CONSOLIDATION & GAP-FILL",
        "",
        "QUERY PROTEIN: {user_query}",
        "",
        "THEME: final sweep over the accumulated context.",
        "  1. Verify chain data on every indirect interactor — correct",
        "     upstream, mediator_chain ordering, and depth.",
        "  2. Reclassify any direct/indirect call the literature now",
        "     contradicts. Emit only interactors whose classification changed.",
        "  3. Gap-fill any well-known partner the previous iterations genuinely",
        "     missed. Tight — real omissions, not padding.",
        "",
        "Emit ONLY new or MODIFIED interactors. Do NOT re-output anything",
        "already correct in the accumulated context.",
        _FREE_RESEARCH_BLOCK,
        _OUTPUT_SCHEMA,
    ]),
)


# ── Ordered tuple of all default iterations ─────────────────────────────
# Broad discovery runs FIRST so iteration 1 is the unbiased interactome
# sweep every time — no narrow upstream-only pass getting in the way of
# a clean opening snapshot. Upstream-context runs later (slot 6, right
# before consolidation) so by the time it executes, ``interactor_history``
# already carries the broad pool and the LLM can explicitly skip those
# names and focus on the upstream-only subset it still hasn't covered.
# Consolidation stays last so it sees everything.
ALL_DEFAULT_ITERATIONS: tuple[IterationConfig, ...] = (
    _ITER1_BROAD,
    _ITER2_BROAD_EXPANSION,
    _ITER3_PATHWAY,
    _ITER4_DISEASE,
    _ITER4_DEEP_LIT,
    _ITER0_UPSTREAM,
    _ITER5_CONSOLIDATION,
)


def build_default_iteration_configs(
    n: int = 6,
) -> tuple[IterationConfig, ...]:
    """Return the first *n* default iteration configs (clamped 1–6)."""
    n = max(1, min(len(ALL_DEFAULT_ITERATIONS), n))
    return ALL_DEFAULT_ITERATIONS[:n]


def get_iterative_system_prompt() -> str:
    """Return the shared system prompt sent on every iteration turn."""
    return _ITERATIVE_SYSTEM_PROMPT


def step1_iterative_research_discovery(
    num_iterations: int = 6,
) -> StepConfig:
    """Phase 1 discovery via iterative Interactions API calls.

    Creates a single StepConfig whose ``iteration_configs`` tuple drives the
    internal loop inside ``_call_iterative_research_mode()`` in runner.py.
    """
    configs = build_default_iteration_configs(num_iterations)
    return StepConfig(
        name="step1_iterative_research_discovery",
        model=get_model("iterative"),
        api_mode="iterative_research",
        prompt_template=configs[0].prompt_template,
        expected_columns=_EXPECTED_COLUMNS,

        use_google_search=True,
        thinking_level="medium",
        # Free-form discovery — no response_json_schema. With Google Search
        # tools enabled and a strict schema, the model burns the full
        # candidate-token budget on internal reasoning + tool roundtrips
        # and finishes with `FinishReason.MAX_TOKENS` and zero visible
        # output. The system prompt already instructs JSON output; the
        # downstream parser tolerates free-form JSON. Verified empirically
        # on 2026-05-04: schema-on → 0 chars + MAX_TOKENS at 65,536;
        # schema-off → valid JSON with finish_reason=STOP.
        max_output_tokens=65536,
        temperature=0.3,
        cache_system_prompt=True,
        iteration_configs=configs,
    )
