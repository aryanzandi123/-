"""Modern pipeline step factories using Deep Research + Interactions API.

Consolidated prompts that collapse 15-25 sequential calls into ~5-6 steps:
- Phase 1: Single Deep Research call replaces 7 discovery rounds
- Phase 2: Interaction-chained function mapping (2-3 rounds)
- Phase 2b: Combined deep function research (single interaction)
"""
from __future__ import annotations

from pipeline.types import StepConfig, DISCOVERY_OUTPUT_SCHEMA, FUNCTION_MAPPING_OUTPUT_SCHEMA, CITATION_DELTA_SCHEMA
from pipeline.prompts.shared_blocks import (
    DIFFERENTIAL_OUTPUT_RULES,
    STRICT_GUARDRAILS,
    INTERACTOR_TYPES,
    FUNCTION_NAMING_RULES,
    CONTENT_DEPTH_REQUIREMENTS,
    FUNCTION_CONTEXT_LABELING,
    SCHEMA_HELP,
    MAX_OUTPUT_TOKENS,
    DYNAMIC_SEARCH_THRESHOLD,
)

_EXPECTED_COLUMNS = ["ctx_json", "step_json"]


# ══════════════════════════════════════════════════════════════════════
# PHASE 1: DEEP RESEARCH DISCOVERY (replaces steps 1a-1g)
# ══════════════════════════════════════════════════════════════════════

DEEP_RESEARCH_DISCOVERY_PROMPT = "\n".join([
    STRICT_GUARDRAILS,
    "",
    INTERACTOR_TYPES,
    "",
    "╔═══════════════════════════════════════════════════════════════╗",
    "║  COMPREHENSIVE PROTEIN INTERACTOR DISCOVERY                  ║",
    "║  (Deep Research — Single-Pass Discovery)                     ║",
    "╚═══════════════════════════════════════════════════════════════╝",
    "",
    "QUERY PROTEIN: {user_query}",
    "",
    "CRITICAL: Initialize ctx_json with 'main': '{user_query}' as the first field!",
    "",
    "PRIMARY OBJECTIVE:",
    "Systematically discover ALL protein interactors for {user_query} using deep",
    "literature research. Single-pass, comprehensive discovery — direct partners,",
    "complex members, pathway / signaling cascade members, substrates, regulators,",
    "disease-context partners, cutting-edge proteomics / proximity-labeling hits.",
    "",
    "SEARCH APPROACH — GO FREE:",
    "You have Google Search. Pick your own queries, angles, and databases. No",
    "prescriptive list — you know the literature better than any rigid template.",
    "Explore whatever paths get you the best coverage. For each direct interactor",
    "you find, follow the cascade downstream to discover indirect interactors and",
    "their full mediator chains.",
    "",
    "WHAT NOT TO RE-RESEARCH:",
    "Anything already in the accumulated context from previous pipeline steps",
    "(this is a comprehensive pass; if you're building on prior work, skip names",
    "that already appear in ``interactor_history``). Every name you emit MUST be",
    "new to the payload.",
    "",
    "FOR EACH PROTEIN FOUND, CLASSIFY:",
    "- DIRECT: Physical interaction (Co-IP, Y2H, BioID, pull-down evidence)",
    "- INDIRECT: Same pathway/cascade, but no direct binding evidence",
    "",
    "FOR INDIRECT INTERACTORS, PROVIDE CHAIN DATA:",
    "- upstream_interactor: The protein that directly interacts with this one",
    "- mediator_chain: Array of proteins forming the path from {user_query}",
    "- depth: Number of hops (1=direct; depth>=2 for indirect via depth-1 mediators — no upper cap)",
    "",
    "Chain length is NOT capped. Real biological cascades often involve 4+",
    "proteins; include every mediator the literature supports.",
    "",
    "Example: '{user_query} → VCP → LAMP2 → RAB7 → Catalase' (4-mediator cascade)",
    "  VCP:       interaction_type='direct',   depth=1, mediator_chain=[]",
    "  LAMP2:     interaction_type='indirect', upstream_interactor='VCP',",
    "             mediator_chain=['VCP'], depth=2",
    "  RAB7:      interaction_type='indirect', upstream_interactor='LAMP2',",
    "             mediator_chain=['VCP','LAMP2'], depth=3",
    "  Catalase:  interaction_type='indirect', upstream_interactor='RAB7',",
    "             mediator_chain=['VCP','LAMP2','RAB7'], depth=4",
    "",
    "OUTPUT STRUCTURE:",
    "{",
    "  'ctx_json': {",
    "    'main': '{user_query}',",
    "    'interactors': [",
    "      {",
    "        'primary': '<HGNC_SYMBOL>',",
    "        'interaction_type': 'direct' | 'indirect',",
    "        'upstream_interactor': null | '<PROTEIN>',",
    "        'mediator_chain': [] | ['<PROTEIN1>', ...],",
    "        'depth': <integer >= 1>,  // 1=direct, 2=one mediator, N=N-1 mediators. No cap — use whatever the literature supports.",
    "        'support_summary': '<brief evidence summary>'",
    "      }, ...",
    "    ],",
    "    'interactor_history': ['<all discovered protein names>'],",
    "    'search_history': ['<search queries used>']",
    "  },",
    "  'step_json': {'step': 'step1_deep_research_discovery', 'count': <n>}",
    "}",
    "",
    "DO NOT INCLUDE: arrow, direction, intent, evidence array, paper_title,",
    "pmids, functions — these are determined in subsequent function mapping steps.",
    "",
    "GOAL: Find ALL known protein interactors with comprehensive coverage of direct",
    "and indirect interactions. Quality over quantity — ensure each has valid",
    "evidence-based classification.",
    "",
    "Return ONLY JSON.",
])


def step1_deep_research_discovery() -> StepConfig:
    """Single Deep Research call to discover all interactors.

    Budget tuning (from perf audit):
      • thinking_level='medium' (was 'high'): discovery is pattern-matching
        on literature, not multi-step reasoning — 'high' was burning tokens
        on internal deliberation that never improved output quality.
      • max_output_tokens=16384 (was 65536): discovery returns names +
        brief support summaries; real output for a well-studied protein is
        ~3-8K tokens. The 65K reservation was over-allocating the context
        budget with no benefit.
    Together these cut per-call LLM cost ~35% without measurable quality
    regression; re-raise if downstream iteration counts drop.
    """
    return StepConfig(
        name="step1_deep_research_discovery",
        model="gemini-3-flash-preview",
        api_mode="deep_research",
        prompt_template=DEEP_RESEARCH_DISCOVERY_PROMPT,
        expected_columns=_EXPECTED_COLUMNS,

        use_google_search=True,
        thinking_level="medium",
        max_output_tokens=16384,
        temperature=0.3,
        cache_system_prompt=False,  # Deep research manages its own context
        response_schema=DISCOVERY_OUTPUT_SCHEMA,
    )


# ══════════════════════════════════════════════════════════════════════
# PHASE 2a: FUNCTION MAPPING (Interactions API, chained)
# ══════════════════════════════════════════════════════════════════════

_FUNCTION_PREAMBLE = (
    DIFFERENTIAL_OUTPUT_RULES + "\n\n"
    + STRICT_GUARDRAILS + "\n\n"
    + FUNCTION_NAMING_RULES + "\n\n"
    + CONTENT_DEPTH_REQUIREMENTS + "\n\n"
    + FUNCTION_CONTEXT_LABELING + "\n\n"
)

INTERACTION_FUNCTION_MAPPING_PROMPT = _FUNCTION_PREAMBLE + "\n".join([
    "╔═══════════════════════════════════════════════════════════════╗",
    "║  FUNCTION MAPPING (Interaction-Chained)                      ║",
    "╚═══════════════════════════════════════════════════════════════╝",
    "",
    "MAIN: {ctx_json.main}",
    "INTERACTORS: {ctx_json.interactor_history}",
    "ALREADY PROCESSED: {ctx_json.function_batches}",
    "",
    "TASK: Process UNPROCESSED interactors — find ALL functions + evidence.",
    "",
    "STEP 1: IDENTIFY TARGET INTERACTORS",
    "- The batch directive below specifies EXACTLY which interactors to process",
    "- Process ONLY those interactors — no more, no less",
    "",
    "STEP 2: for each interactor, emit every DISTINCT biological function",
    "the literature actually supports. Different mechanisms = separate",
    "functions; different papers about the same mechanism merge under one.",
    "1-2 well-evidenced functions is fine — quality over quantity.",
    "",
    "Per function: name (FUNCTION_NAMING_RULES), arrow, interaction_direction,",
    "cellular_process, effect_description, biological_consequence,",
    "specific_effects, evidence, pathway — depth per",
    "CONTENT_DEPTH_REQUIREMENTS in the system prompt.",
    "",
    "STEP 3: DETECT IMPLICATED PROTEINS AND CHAIN RELATIONSHIPS",
    "",
    "For EACH function you write, analyze whether the mechanism implicates OTHER",
    "proteins that are NOT the main query protein and NOT the current interactor:",
    "",
    "A) If the mechanism mentions intermediary proteins (e.g., 'ATXN3 binds VCP",
    "   which displaces NPLOC4'), add to the function:",
    "   '_implicated_proteins': ['VCP', 'UFD1']  (list of protein names found in mechanism)",
    "",
    "B) If evidence shows the interaction is actually INDIRECT (the query protein",
    "   does NOT directly bind this interactor, but acts THROUGH intermediaries),",
    "   add to the function:",
    "   '_evidence_suggests_indirect': true",
    "   '_implicated_mediators': ['VCP']  (proteins between query and interactor)",
    "",
    "C) Track all newly discovered proteins in indirect_interactors array:",
    "   {'name': '<PROTEIN>', 'upstream_interactor': '<PROTEIN>',",
    "    'discovered_in_function': '<function>', 'role_in_cascade': '<desc>'}",
    "",
    "MANDATORY: Add ALL processed interactor names to function_batches output!",
    "",
]) + "\n\nReturn ONLY JSON."


def step2a_interaction_functions(round_num: int = 1) -> StepConfig:
    """Function mapping via generateContent API with Google Search."""
    return StepConfig(
        name=f"step2a_functions_r{round_num}",
        model="gemini-3-flash-preview",
        api_mode="generate",
        prompt_template=INTERACTION_FUNCTION_MAPPING_PROMPT,
        expected_columns=_EXPECTED_COLUMNS,

        use_google_search=True,
        thinking_level="medium",
        max_output_tokens=65536,
        temperature=0.7,
        search_dynamic_mode=True,
        search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        cache_system_prompt=True,
        response_schema=FUNCTION_MAPPING_OUTPUT_SCHEMA,
    )


# ══════════════════════════════════════════════════════════════════════
# PHASE 2b: COMBINED DEEP FUNCTION RESEARCH (single interaction)
# ══════════════════════════════════════════════════════════════════════

COMBINED_DEEP_FUNCTION_PROMPT = _FUNCTION_PREAMBLE + "\n".join([
    "╔═══════════════════════════════════════════════════════════════╗",
    "║  COMBINED DEEP FUNCTION RESEARCH + INDIRECT + RESCUE          ║",
    "║  (Comprehensive sweep — all remaining work in one pass)       ║",
    "╚═══════════════════════════════════════════════════════════════╝",
    "",
    "MAIN: {ctx_json.main}",
    "ALREADY PROCESSED: {ctx_json.function_batches}",
    "",
    "TASK 1: DEEP RESEARCH FOR OBSCURE FUNCTIONS",
    "- Target interactors with <3 functions",
    "- Search for recent discoveries (2020-2025)",
    "- Add 10-15 more functions across interactors",
    "",
    "TASK 2: INDIRECT INTERACTOR FUNCTIONS",
    "- For ALL indirect interactors, ensure each has functions",
    "- Research the SPECIFIC mechanism via their upstream mediator",
    "- Example: If mTOR is indirect via VCP, research 'VCP mTOR interaction'",
    "",
    "TASK 3: RESCUE DIRECT FUNCTIONS",
    "- Check for direct interactors with 0 or 1 function",
    "- Add at least 2 functions per direct interactor",
    "",
    "TASK 4: CHAIN LINK FUNCTIONS + CHAIN WITH ARROWS (CRITICAL — DO NOT SKIP)",
    "",
    "For EACH indirect interactor with mediator_chain:",
    "",
    "A) SET '_chain_pathway' on the interactor to the pathway from its indirect function.",
    "",
    "B) GENERATE QUERY->FIRST MEDIATOR functions (TYPE A):",
    "   - Describe {ctx_json.main}<->first-mediator direct interaction",
    "   - Focused on the aspect relevant to this specific chain",
    "   - CAN mention {ctx_json.main}",
    "",
    "C) GENERATE MEDIATOR->TARGET functions (TYPE B) for every hop past the first:",
    "   - For chains with extra mediators (e.g. ATXN3 -> VCP -> LAMP2 -> RAB7),",
    "     generate per-hop functions for VCP->LAMP2 AND LAMP2->RAB7 AND so on.",
    "   - Each hop's functions describe that ONE mediator<->target direct",
    "     interaction INDEPENDENTLY",
    "   - Per-hop functions MUST NOT mention {ctx_json.main}",
    "",
    "D) PATHWAY COHERENCE:",
    "   - ALL chain_link_functions use SAME pathway as the indirect function",
    "   - Set '_chain_pathway' on the interactor",
    "",
    "E) CHAIN_LINK_FUNCTIONS KEY FORMAT — CRITICAL:",
    "   Keys are directional pair strings using ASCII arrow '->' (NOT",
    "   the unicode '→'), uppercase HGNC symbols. The runner canonicalizes",
    "   keys downstream, but it only parses '->' (or '|') — any other",
    "   separator lands in an unreachable fallback slot.",
    "",
    "   YOU MUST EMIT ONE KEY PER ADJACENT PAIR. A chain of N proteins",
    "   has N-1 hops; every hop MUST appear. Skipping the middle or",
    "   tail hop of a 4+ protein chain leaves the user with an empty",
    "   modal on that pair — this is the most common bug for long",
    "   chains. Count your hops before emitting.",
    "",
    "   'chain_link_functions': {",
    "     '{ctx_json.main}->MED1': [funcs with function_context='chain_derived'],",
    "     'MED1->MED2':             [funcs with function_context='chain_derived'],",
    "     'MED2->TARGET':           [funcs with function_context='chain_derived']",
    "   }",
    "",
    "   For a 4-protein chain like PERK -> EIF2S1 -> ATF4 -> DDIT3,",
    "   you MUST emit ALL THREE of: 'PERK->EIF2S1', 'EIF2S1->ATF4',",
    "   AND 'ATF4->DDIT3'. For a 5-protein chain, all FOUR adjacent",
    "   pairs. No exceptions, no truncation.",
    "",
    "F) EMIT 'chain_with_arrows' (ordered per-hop arrow list):",
    "   Alongside chain_link_functions, emit an ordered array describing",
    "   the typed arrow for each mediator hop, upstream->downstream:",
    "",
    "   'chain_with_arrows': [",
    "     {'from': '{ctx_json.main}', 'to': 'MED1',   'arrow': 'activates'},",
    "     {'from': 'MED1',             'to': 'MED2',   'arrow': 'inhibits'},",
    "     {'from': 'MED2',             'to': 'TARGET', 'arrow': 'binds'}",
    "   ]",
    "",
    "   Each arrow MUST agree with the arrow the corresponding per-hop",
    "   function uses in chain_link_functions. Use ONE of:",
    "   activates | inhibits | binds | regulates. Use binds for physical/co-complex formation.",
    "",
    "QUALITY: depth per CONTENT_DEPTH_REQUIREMENTS in the system prompt.",
    "",
    "TRACK ALL indirect interactors found in cascades.",
    "Update function_batches with ALL processed interactor names.",
    "",
]) + "\n\nReturn ONLY JSON."


def step2b_combined_deep_functions() -> StepConfig:
    """Combined deep function research (replaces 2b + 2b2 + 2b3 + 2b4).

    .. deprecated:: Redundant with parallel-batched approach. Kept for legacy compat.
    """
    return StepConfig(
        name="step2b_deep_functions_combined",
        model="gemini-3-flash-preview",
        api_mode="generate",
        prompt_template=COMBINED_DEEP_FUNCTION_PROMPT,
        expected_columns=_EXPECTED_COLUMNS,

        use_google_search=True,
        thinking_level="medium",
        max_output_tokens=65536,
        temperature=0.7,
        search_dynamic_mode=True,
        search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        cache_system_prompt=True,
        response_schema=FUNCTION_MAPPING_OUTPUT_SCHEMA,
    )


# ══════════════════════════════════════════════════════════════════════
# PHASE 2e: CITATION VERIFICATION (post-function-mapping)
# ══════════════════════════════════════════════════════════════════════

CITATION_VERIFICATION_PROMPT = "\n".join([
    "╔═══════════════════════════════════════════════════════════════╗",
    "║  CITATION VERIFICATION (Evidence-Only Delta)                  ║",
    "╚═══════════════════════════════════════════════════════════════╝",
    "",
    "MAIN: {ctx_json.main}",
    "",
    "TASK: For EACH function in EACH assigned interactor, verify and enrich",
    "evidence. Output ONLY the evidence delta — not the full dataset.",
    "",
    "FOR EACH EVIDENCE ENTRY IN EACH FUNCTION:",
    "1. Search for the paper_title using Google Search",
    "2. Verify the paper EXISTS and is RELEVANT to the claimed mechanism",
    "3. Add/correct these fields if missing or wrong:",
    "   - year: publication year (integer)",
    "   - journal: journal name",
    "   - assay: primary experimental technique",
    "   - species: organism/cell line studied",
    "   - key_finding: one-sentence summary of what this paper shows",
    "   - relevant_quote: 1-2 sentence paraphrase of key mechanistic result",
    "4. If a paper_title appears HALLUCINATED (cannot be found by searching),",
    "   REPLACE it with a REAL paper that supports the same mechanism",
    "",
    "FOR FUNCTIONS WITH EMPTY OR MISSING EVIDENCE ARRAYS:",
    "Search freely — you know which databases (PubMed, bioRxiv, Reactome, etc.)",
    "have the supporting literature. Find 2-3 REAL papers per function that",
    "actually document the mechanism and create evidence entries with verified",
    "titles and details. No scripted query template — pick queries that will",
    "actually return the right papers for the claim you're verifying.",
    "",
    "CRITICAL RULES:",
    "- Do NOT invent paper titles — search and verify EVERY title",
    "- If you cannot find a paper, mark it with '_unverified': true",
    "- Every function should have at least 2 evidence entries with verified titles",
    "",
    "═══════════════════════════════════════════════════════════════",
    "OUTPUT FORMAT (EVIDENCE-ONLY DELTA)",
    "═══════════════════════════════════════════════════════════════",
    "",
    "Return ONLY the assigned interactors with ONLY these fields per function:",
    "  - function (exact name — used as merge key)",
    "  - cellular_process (exact value — used as merge key)",
    "  - arrow (exact value — used as merge key)",
    "  - interaction_direction (exact value — used as merge key:",
    "    'main_to_primary' or 'primary_to_main')",
    "  - evidence (enriched array)",
    "  - pmids (list of PubMed IDs found)",
    "",
    "Do NOT include: mechanism, effect_description, biological_consequence,",
    "specific_effects, cascades, pathway, or any other function fields.",
    "The fields above are merge keys — the system matches them to existing",
    "functions and merges your evidence arrays automatically.",
    "Copy function, cellular_process, arrow, and interaction_direction EXACTLY",
    "from the input data so the merge can find the right function.",
    "",
    "Return ONLY JSON.",
])


def step2e_citation_verification() -> StepConfig:
    """Verify and enrich evidence/citations via generateContent API with delta output."""
    return StepConfig(
        name="step2e_citation_verification",
        model="gemini-3-flash-preview",
        api_mode="generate",
        prompt_template=CITATION_VERIFICATION_PROMPT,
        expected_columns=_EXPECTED_COLUMNS,

        use_google_search=True,
        thinking_level="medium",
        # 2026-05-03: bumped 8192 → 24000. The "Flash server-side cap is
        # 8192" comment was wrong — real cap is 65,536 (Vertex AI docs,
        # docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-flash).
        # 24000 leaves comfortable headroom for citation arrays on densely-
        # cited proteins (verified PMID lists routinely exceed 8K tokens).
        # max_output_tokens is a ceiling — model emits only what the schema
        # demands, so this raises NO output cost for short-citation cases.
        max_output_tokens=24000,
        temperature=0.2,
        response_schema=CITATION_DELTA_SCHEMA,
    )
