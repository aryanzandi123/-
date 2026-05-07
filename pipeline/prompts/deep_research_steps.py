"""Chain resolution step factories (Phase 2b: steps 2ab-2az)."""
from __future__ import annotations

import copy
import os

from pipeline.types import StepConfig, FUNCTION_MAPPING_OUTPUT_SCHEMA, _FUNCTION_OBJECT
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

# ------------------------------------------------------------------
# Preamble blocks
# ------------------------------------------------------------------

_HEAVY_PREAMBLE = (
    DIFFERENTIAL_OUTPUT_RULES + "\n\n"
    + STRICT_GUARDRAILS + "\n\n"
    + FUNCTION_NAMING_RULES + "\n\n"
    + CONTENT_DEPTH_REQUIREMENTS + "\n\n"
    + FUNCTION_CONTEXT_LABELING + "\n\n"
)

_CHAIN_CLAIM_PREAMBLE = (
    STRICT_GUARDRAILS + "\n\n"
    + FUNCTION_NAMING_RULES + "\n\n"
    + FUNCTION_CONTEXT_LABELING + "\n\n"
    + "\n".join([
        "CHAIN-HOP CLAIM MODE",
        "",
        "You generate compact, pair-keyed claims for one binary hop inside a",
        "resolved protein cascade. Treat the requested SOURCE->TARGET pair as",
        "the whole local problem. Use search to verify the pair-specific biology,",
        "but do not expand into a review article.",
        "",
        "Hard output budget discipline:",
        "- Emit exactly one function per requested pair unless the directive",
        "  explicitly asks for more.",
        "- Use the 65k token limit only as emergency headroom. Do not spend it.",
        "- Prefer concise evidence-rich fields over long background prose.",
        "- The prose must not name the query protein unless the requested pair",
        "  itself includes the query protein.",
        "- Return only valid JSON matching the response schema.",
    ])
    + "\n\n"
)

_EXPECTED_COLUMNS = ["ctx_json", "step_json"]
_CHAIN_CLAIM_COLUMNS = ["chain_claims"]


# ------------------------------------------------------------------
# Lightweight response schemas (chain annotation steps)
# ------------------------------------------------------------------

CHAIN_DETERMINATION_SCHEMA = {
    "type": "object",
    "properties": {
        "chain_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "interactor": {"type": "string"},
                    "claim_index": {"type": "integer"},
                    "chain": {"type": "string"},
                    "intermediaries": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    # 1-based position of the query protein in the full
                    # ordered chain. Lets downstream code render chains
                    # where the query isn't at position 1.
                    "query_position": {"type": "integer"},
                    # For 2ab3 (hidden chains), 1-based position of the
                    # newly discovered protein in the chain.
                    "new_protein_position": {"type": "integer"},
                    # Total number of proteins in the chain. Optional —
                    # downstream code derives it from the intermediaries
                    # list when omitted.
                    "chain_length": {"type": "integer"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
                "required": ["interactor", "chain", "intermediaries"],
            },
        },
    },
    "required": ["chain_results"],
}

HIDDEN_INDIRECT_CONFIRMATION_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "interactor": {"type": "string"},
                    "candidate_protein": {"type": "string"},
                    "claim_index": {"type": "integer"},
                    "confirmed": {"type": "boolean"},
                    "chain_potential": {"type": "boolean"},
                    "implicated_proteins": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "interactor",
                    "candidate_protein",
                    "confirmed",
                    "chain_potential",
                ],
            },
        },
    },
    "required": ["confirmations"],
}

DUPLICATE_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "match_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chain": {"type": "string"},
                    "new_protein": {"type": "string"},
                    "existing_interactor": {"type": "string"},
                    "match_found": {"type": "boolean"},
                    "claim_v_index": {"type": "integer"},
                    "claim_v_text": {"type": "string"},
                    "criteria_satisfied": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "similarity_reasoning": {"type": "string"},
                },
                "required": ["chain", "new_protein", "match_found"],
            },
        },
    },
    "required": ["match_results"],
}

_CHAIN_CLAIM_FUNCTION_OBJECT = copy.deepcopy(_FUNCTION_OBJECT)

CHAIN_CLAIM_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "chain_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string"},
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "functions": {
                        "type": "array",
                        "items": _CHAIN_CLAIM_FUNCTION_OBJECT,
                    },
                },
                "required": ["pair", "source", "target", "functions"],
            },
        }
    },
    "required": ["chain_claims"],
}


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _chain_step(
    name: str,
    instruction: str,
    *,
    response_schema: dict,
    max_output_tokens: int = 65536,
    thinking_level: str = "low",
) -> StepConfig:
    """Build a lightweight Flash StepConfig for chain-annotation steps.

    These steps perform analytical reasoning over existing data (no
    web search) and return compact structured output.
    """
    return StepConfig(
        name=name,
        model="gemini-3-flash-preview",
        use_google_search=False,
        thinking_level=thinking_level,
        max_output_tokens=max_output_tokens,
        temperature=0.3,
        search_dynamic_mode=False,
        search_dynamic_threshold=None,
        expected_columns=_EXPECTED_COLUMNS,
        system_prompt=None,
        api_mode="generate",
        cache_system_prompt=False,
        prompt_template=instruction,
        response_schema=response_schema,
    )


def _heavy_claim_step(
    name: str,
    instruction: str,
    suffix: str,
    *,
    api_mode: str = "generate",
    cache_system_prompt: bool = False,
) -> StepConfig:
    """Build a heavy Flash StepConfig for full claim-generation steps.

    Chain-claim generation is a separate shape from direct function mapping:
    one pair per call, Google Search on, strong typed JSON, and compact field
    ceilings. Keeping those instructions smaller avoids Flash spending several
    minutes trying to satisfy generic full-review depth for a single hop.
    """
    is_chain_claim_step = name in (
        "step2ax_claim_generation_explicit",
        "step2az_claim_generation_hidden",
    )
    # Chain-claim generation passes ``response_json_schema`` for
    # structured output, which Vertex caps at 8192 tokens — requesting
    # more (16384, 65536) silently fails or rejects the batch. Other
    # paths in this module use free-form generation and accept the
    # standard 65536. Env override CHAIN_CLAIM_MAX_OUTPUT_TOKENS lets
    # ops raise the chain-claim cap if Vertex relaxes the structured
    # cap upward.
    max_output_tokens = (
        int(os.environ.get("CHAIN_CLAIM_MAX_OUTPUT_TOKENS", "8192"))
        if is_chain_claim_step
        else 65536
    )
    temperature = (
        float(os.environ.get("CHAIN_CLAIM_TEMPERATURE", "0.3"))
        if is_chain_claim_step
        else 0.7
    )
    return StepConfig(
        name=name,
        model="gemini-3-flash-preview", #WAS GEMINI-3.1-PRO="gemini-3.1-pro-preview",
        use_google_search=True,
        # Dropped from "medium" → "low". Gemini 3 Flash with thinking_level
        # medium burns ~3× the output tokens of "low" without a measurable
        # quality gain on tight prompts like chain-claim generation. The
        # added token cost was a meaningful driver of both truncation
        # (24% rate at batch_size=2) and TPM-budget overshoot. Upstream
        # chain-resolution steps (2ab / 2ab3) already run at "low" — this
        # is parity, not a regression. Override via env if you need more.
        thinking_level=os.environ.get("CHAIN_CLAIM_THINKING_LEVEL", "low"),
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        search_dynamic_mode=True,
        search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=(
            _CHAIN_CLAIM_COLUMNS if is_chain_claim_step else _EXPECTED_COLUMNS
        ),
        system_prompt=(_CHAIN_CLAIM_PREAMBLE if is_chain_claim_step else None),
        api_mode=api_mode,
        cache_system_prompt=(True if is_chain_claim_step else cache_system_prompt),
        prompt_template=(
            ("" if is_chain_claim_step else _HEAVY_PREAMBLE)
            + instruction
            + "\n\n"
            + suffix
        ),
        response_schema=(
            CHAIN_CLAIM_OUTPUT_SCHEMA
            if is_chain_claim_step
            else FUNCTION_MAPPING_OUTPUT_SCHEMA
        ),
    )


# ══════════════════════════════════════════════════════════════════
# Shared claim-generation instructions (reused by 2ax and 2az)
# ══════════════════════════════════════════════════════════════════

def _claim_generation_instruction(track_label: str) -> str:
    """Return the core claim-generation prompt body shared by 2ax and 2az.

    Args:
        track_label: Human label for the track origin, e.g.
                     "explicit indirect (2ab)" or "hidden indirect (2ab3)".
    """
    return "\n".join([
        "MAIN PROTEIN: {ctx_json.main}",
        "",
        FUNCTION_HISTORY_HEADER,
        "",
        "TASK: Generate full scientific claims for EACH AND EVERY interaction",
        f"pair identified by the {track_label} chain resolution track.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "ZERO-SKIP INVARIANT (HIGHEST PRIORITY)",
        "═══════════════════════════════════════════════════════════════",
        "",
        "Every pair listed in the BATCH ASSIGNMENT directive MUST receive",
        "at least ONE function entry under",
        "``interactors[<target>].chain_link_functions[\"<SOURCE>-><TARGET>\"]``.",
        "Silent omission of any pair is a contract violation. When the",
        "literature lacks pair-specific biology for a hop in the cascade's",
        "context, emit the honest thin-claim form shown in the batch",
        "directive — do NOT fabricate, do NOT skip. Downstream db_sync will",
        "log every missing pair at ERROR level.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "ORIGIN CLAIM — PRINCIPLE",
        "═══════════════════════════════════════════════════════════════",
        "",
        "A chain represents ONE biological cascade described by ONE",
        "scientific claim/function upstream in the pipeline. The batch",
        "directive below (when populated) lists the ORIGIN CLAIM for each",
        "set of hops — the specific sci-claim whose prose documented the",
        "cascade as a single mechanism.",
        "",
        "CARDINAL RULE: when an ORIGIN CLAIM block is provided for a hop,",
        "the hop's generated function MUST describe THIS PAIR'S role IN",
        "that same mechanism. You are extracting the pair-scoped view of a",
        "cascade that already exists in the literature — not performing",
        "fresh independent research that happens to share proteins with",
        "the origin. If the origin prose does not mention or imply this",
        "specific pair's role, state so explicitly and emit a thin claim",
        "rather than fabricating a separate mechanism. Do not stitch in",
        "unrelated biology just because proteins overlap.",
        "",
        "When NO ORIGIN CLAIM block is present for a hop (e.g. Track B",
        "candidates that upstream detection couldn't tie to a single",
        "claim), treat the cascade topology block as an advisory hint,",
        "and generate a hop claim that is biologically plausible without",
        "fabricating mechanisms.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "TWO GENERATION MODES",
        "═══════════════════════════════════════════════════════════════",
        "",
        "For EACH new interaction pair, check whether a Claim 'V' was found",
        "by the duplicate-check step:",
        "",
        "MODE 1 — NO Claim V found:",
        "  Generate NEW claims grounded in the original indirect claim context.",
        "  The claims must be independently researched and fully substantiated.",
        "  Use the chain context to guide the biological framing, but each claim",
        "  must stand on its own as a description of the BINARY interaction.",
        "",
        "MODE 2 — Claim V found:",
        "  Rephrase Claim V into two independent claims:",
        "",
        "  Claim 'Y' — for the FIRST link (e.g., {ctx_json.main} -> NEW_PROTEIN):",
        "    - Describes the {ctx_json.main} <-> NEW_PROTEIN interaction INDEPENDENTLY",
        "    - Framed in the context of Claim V's biological pathway",
        "    - Must stand alone without referencing the downstream target",
        "",
        "  Claim 'W' — for the SECOND link (e.g., NEW_PROTEIN -> ORIGINAL_TARGET):",
        "    - Describes the NEW_PROTEIN <-> ORIGINAL_TARGET interaction INDEPENDENTLY",
        "    - Framed in the context of Claim V's biological pathway",
        "    - Must stand alone without referencing the upstream query protein",
        "",
        "═══════════════════════════════════════════════════════════════",
        "DEPTH REQUIREMENTS FOR CHAIN-DERIVED CLAIMS",
        "═══════════════════════════════════════════════════════════════",
        "",
        "- function_context: SET TO 'chain_derived' for every Y and W claim.",
        "  These claims describe hop-specific biochemistry within a resolved",
        "  cascade. The runner will attach each claim to the target protein's",
        "  ``chain_link_functions[pair_key]`` slot. Your job is only to emit",
        "  pair-keyed claim records in the OUTPUT SHAPE below.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "OUTPUT SHAPE (MANDATORY — pair-keyed; code attaches to ctx_json)",
        "═══════════════════════════════════════════════════════════════",
        "",
        "HARD RULE — one line, no exceptions:",
        "Inside each claim's prose, name the query protein ({ctx_json.main})",
        "only when the requested pair itself contains the query. Otherwise",
        "talk about A acting on B, period. Other pathway members may be named",
        "when the biology truly requires them; off-pair query mentions are",
        "forbidden.",
        "",
        "WRONG (do not emit):",
        "  {\"pair\":\"VCP->LAMP2\", \"functions\":[{",
        "    \"effect_description\": \"{ctx_json.main} regulates VCP which then",
        "                   drives LAMP2 ...\"   ← names the query",
        "  }]}",
        "",
        "RIGHT:",
        "  {\"pair\":\"VCP->LAMP2\", \"functions\":[{",
        "    \"effect_description\": \"VCP ATPase activity drives autophagosome-",
        "                   lysosome fusion by recruiting LAMP2 to the",
        "                   membrane ...\"   ← only A and B",
        "  }]}",
        "",
        "For each requested hop pair X->Y in a cascade, emit exactly one",
        "record in top-level ``chain_claims``. The pair string MUST exactly",
        "match the requested pair, and target MUST be Y. Do NOT emit ctx_json.",
        "",
        "{",
        "  \"chain_claims\": [",
        "      {",
        "        \"pair\": \"SOURCE_PROTEIN->TARGET_PROTEIN\",",
        "        \"source\": \"SOURCE_PROTEIN\",",
        "        \"target\": \"TARGET_PROTEIN\",",
        "        \"functions\": [",
        "          {",
        "            \"function\": \"<short title — NOT a sentence>\",",
        "            \"function_context\": \"chain_derived\",",
        "            \"arrow\": \"activates|inhibits|binds|regulates\",",
        "            \"cellular_process\": \"<≥6 dense pair-specific sentences: domains, compartment, PTMs, triggers, conformation, stoichiometry — no query mention, no non-adjacent mediators>\",",
        "            \"effect_description\": \"<≥6 sentences: quantitative impact, signaling consequences, temporal dynamics, context specificity — pair-scoped>\",",
        "            \"biological_consequence\": [\"<cascade 1: ≥6 named steps with arrows between>\", \"<cascade 2: different pathway, ≥6 steps>\", \"<cascade 3: another pathway, ≥6 steps>\"],",
        "            \"specific_effects\": [\"<technique + model + measurable result, entry 1>\", \"<entry 2>\", \"<entry 3>\"],",
        "            \"evidence\": [{\"paper_title\":\"...\",\"relevant_quote\":\"paraphrase\",",
        "              \"year\": 2024, \"assay\":\"...\", \"species\":\"...\",",
        "              \"key_finding\":\"...\"}, {\"paper_title\":\"...\",\"...\":\"...\"}, {\"paper_title\":\"...\",\"...\":\"...\"}],",
        "            \"pathway\": \"<pathway name>\"",
        "          }",
        "        ]",
        "      }",
        "  ]",
        "}",
        "",
        "Do NOT put chain-hop claims in ``ctx_json.interactors[].functions``.",
        "Do NOT emit ``chain_link_functions`` yourself. Code attaches the",
        "pair-keyed records into nested storage after parsing.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "CONTENT DEPTH REQUIREMENTS (P2.1 — MATCH NORMAL FUNCTION MAPPING)",
        "═══════════════════════════════════════════════════════════════",
        "",
        "Chain-hop claims describe pair-specific biology between SOURCE and",
        "TARGET. The depth contract is the SAME as flat function mapping: each",
        "field must hit the minima below. Pair scope (no query-protein",
        "mentions, no cascade-level prose) is enforced separately by the",
        "Locus Router — depth and scope are orthogonal, hit BOTH.",
        "",
        "EACH FIELD STATES DISTINCT INFORMATION. Never paraphrase the same",
        "mechanism across fields. cellular_process = HOW it works;",
        "effect_description = WHAT RESULTS; biological_consequence =",
        "DOWNSTREAM CASCADE; specific_effects = EXPERIMENTAL EVIDENCE;",
        "evidence = PAPERS.",
        "",
        "1. cellular_process — 6+ SENTENCES MINIMUM (no ceiling — more is better)",
        "   Pair-specific mechanism: binding domains/residues, subcellular",
        "   compartment, PTMs (e.g. K48-linked Ub, phospho-Ser65), regulatory",
        "   conditions/triggers, conformational changes, stoichiometry,",
        "   species conservation. Stay scoped to SOURCE↔TARGET — do not",
        "   widen into the broader cascade or mention the query protein.",
        "   COUNT YOUR SENTENCES: if < 6, add more molecular detail.",
        "",
        "2. effect_description — 6+ SENTENCES MINIMUM (no ceiling — more is better)",
        "   Quantitative impact (fold-changes, Kd, half-life), downstream",
        "   signaling consequences within the SOURCE↔TARGET pair, temporal",
        "   dynamics, context specificity (cell type, stress, disease state).",
        "   COUNT: if < 6, add quantitative and contextual detail.",
        "",
        "3. biological_consequence — 3-5 CASCADES, 6+ NAMED STEPS EACH",
        "   (HARD MINIMUM 3 — runtime validator enforces)",
        "   Each cascade names every intermediate protein/complex involved",
        "   in the SOURCE→TARGET edge's downstream impact, specifies",
        "   molecular events (phosphorylation, ubiquitination,",
        "   translocation), ends with physiological outcome. Cascades must",
        "   cover DIFFERENT biological pathways, not paraphrases of one.",
        "   Cap at 5 — beyond that, merge close cascades.",
        "",
        "4. specific_effects — 3+ EXPERIMENTAL FINDINGS (no ceiling — more is better)",
        "   Each MUST cite: experimental technique (Co-IP, SPR, CRISPR KO,",
        "   ITC), model system (HEK293T, primary neurons, Drosophila),",
        "   measurable result (fold-change, Kd, p-value, half-life).",
        "",
        "5. evidence — 3+ PAPERS WITH CITATIONS (no ceiling — more is better)",
        "   Each MUST include: paper_title, relevant_quote (paraphrased,",
        "   NOT verbatim), year, assay, species, key_finding. Do NOT",
        "   fabricate citations — only cite papers you are confident exist.",
        "",
        "DEPTH ENFORCEMENT: a runtime validator (utils/quality_validator.py)",
        "counts cellular_process sentences, effect_description sentences,",
        "biological_consequence cascades, specific_effects entries, and",
        "evidence papers on every chain-hop claim. Functions falling short",
        "are flagged with `_depth_issues` and re-dispatched. Saving tokens",
        "by emitting shallow claims does NOT work — redispatch fires and",
        "the second pass costs more total than hitting depth on round 1.",
        "",
        "Function count per pair: prefer ONE function per hop. Emit a",
        "second only when the pair has a CLEARLY distinct mechanism with",
        "independent evidence. Do not split one mechanism across two",
        "functions to game the count.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "DEDUPLICATION (MANDATORY)",
        "═══════════════════════════════════════════════════════════════",
        "",
        "- Do NOT generate claims that duplicate existing claims on ANY interactor.",
        "- Before outputting each claim, verify it describes a DISTINCT mechanism",
        "  not already covered by existing functions on that interactor.",
        "- If Mode 2 (Claim V rephrasing), ensure Y and W each capture a DIFFERENT",
        "  aspect of V's mechanism.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "PATHWAY CONTEXT",
        "═══════════════════════════════════════════════════════════════",
        "",
        "- Use the chain's pathway context as a HINT, but each claim may belong",
        "  to a different pathway if the biology warrants it.",
        "- The pathway assignment step will finalize pathway mapping later.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "QUALITY CHECKLIST (VERIFY BEFORE OUTPUT)",
        "═══════════════════════════════════════════════════════════════",
        "",
        "Before outputting EACH function, count and verify minimums:",
        "",
        "[ ] cellular_process: ≥6 sentences? (no ceiling)",
        "[ ] effect_description: ≥6 sentences? (no ceiling)",
        "[ ] biological_consequence: 3-5 cascades, ≥6 named steps each?",
        "[ ] specific_effects: ≥3 entries with technique/model/result?",
        "[ ] evidence: ≥3 papers with paraphrased quotes + assay/species?",
        "[ ] pathway: assigned and consistent with the hop's biology?",
        "[ ] No duplicate content across fields (each says something NEW)?",
        "[ ] No duplicates against existing interactor functions?",
        "[ ] Pair scope: no mentions of the query protein or non-adjacent",
        "    chain mediators? (Locus Router will reject if you cross scope.)",
        "",
        "ANY function outside these minimums must be deepened before output.",
    ])


# ══════════════════════════════════════════════════════════════════
#  STEP 2ab — Chain Determination for Explicit Indirects
# ══════════════════════════════════════════════════════════════════

def step2ab_chain_determination() -> StepConfig:
    """Determine protein chains for explicit indirect interactions."""
    instruction = "\n".join([
        "STEP 2ab — Chain determination for explicit indirects",
        "",
        "MAIN PROTEIN: {ctx_json.main}",
        "",
        "For every interactor with interaction_type='indirect', examine each",
        "scientific claim and build the full ordered protein chain that",
        "explains the mechanism.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "HARD RULES (read twice)",
        "═══════════════════════════════════════════════════════════════",
        "",
        "1. The chain is an ordered list of proteins, upstream → downstream.",
        "   Default assumption: chain_length ≥ 4. Emit length 3 ONLY when",
        "   the literature genuinely describes ONE mediator between query",
        "   and target. Collapsing a described 5-step cascade to length 3",
        "   is the single most common failure mode of this step — do NOT",
        "   do it. Emit 4, 5, 6, 7, 8+ as the biology dictates.",
        "",
        "1b. Every chain node must be a specific protein/gene symbol.",
        "   Do NOT use generic/non-protein entities as nodes: RNA, mRNA,",
        "   DNA, generic 'Ubiquitin', proteasome, ribosome, chromatin,",
        "   pathways, compartments, or complexes. Keep those in mechanism",
        "   prose. Use UBB/UBC/other HGNC symbols only when the literature",
        "   names that exact gene product.",
        "",
        "2. The query {ctx_json.main} may sit at ANY position in the chain —",
        "   head (position 1), middle, or tail. Do NOT force it to the head.",
        "",
        "3. intermediaries = full_chain MINUS query MINUS target.",
        "   NEVER include the query or the target interactor in the",
        "   intermediaries list. Mark every intermediary with ^...^ in the",
        "   chain string; the query and target appear unmarked at their",
        "   positions.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "WORKED EXAMPLES",
        "═══════════════════════════════════════════════════════════════",
        "",
        "EXAMPLE A — query at HEAD, length 5",
        "  Chain:          {ctx_json.main} -> ^VCP^ -> ^HDAC6^ -> ^DYNC1H1^ -> TARGET_A",
        "  intermediaries: ['VCP', 'HDAC6', 'DYNC1H1']",
        "  query_position: 1   (1-based)",
        "  chain_length:   5",
        "",
        "EXAMPLE B — query in MIDDLE, length 6",
        "  Chain:          ^UBQLN2^ -> ^HNRNPA1^ -> {ctx_json.main} -> ^SQSTM1^ -> ^BECN1^ -> TARGET_B",
        "  intermediaries: ['UBQLN2', 'HNRNPA1', 'SQSTM1', 'BECN1']",
        "  query_position: 3   (1-based)",
        "  chain_length:   6",
        "",
        "EXAMPLE C — query at TAIL, length 4",
        "  Chain:          ^ATG7^ -> ^MAP1LC3B^ -> ^SQSTM1^ -> {ctx_json.main}",
        "  intermediaries: ['ATG7', 'MAP1LC3B', 'SQSTM1']",
        "  query_position: 4   (1-based)",
        "  chain_length:   4",
        "",
        "EXAMPLE D — length 7 (do NOT collapse this to 3)",
        "  Chain:          {ctx_json.main} -> ^HNRNPA1^ -> ^STMN2^ -> ^CAMK2A^ -> ^PSD95^ -> ^CREB^ -> TARGET_D",
        "  intermediaries: ['HNRNPA1', 'STMN2', 'CAMK2A', 'PSD95', 'CREB']",
        "  query_position: 1   (1-based)",
        "  chain_length:   7",
        "",
        "COUNTER-EXAMPLE (WRONG — DO NOT EMIT)",
        "  Biology described: {ctx_json.main} → HNRNPA1 → STMN2 → CAMK2A → PSD95 → CREB → TARGET",
        "  WRONG output:      {ctx_json.main} -> ^HNRNPA1^ -> TARGET   (chain_length=3)",
        "  Why it's wrong:    five documented proteins were discarded. The",
        "                     cascade is length 7; emit all seven.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "OUTPUT",
        "═══════════════════════════════════════════════════════════════",
        "",
        "JSON with 'chain_results' — an array where each entry has:",
        "- interactor:      indirect target HGNC symbol",
        "- claim_index:     0-based index in the interactor's functions[]",
        "- chain:           full chain string, ^...^ on intermediaries only",
        "- intermediaries:  ordered HGNC symbols of intermediaries",
        "                   (= full_chain MINUS query MINUS target)",
        "- query_position:  1-based index of {ctx_json.main} in the full chain",
        "- chain_length:    total proteins in the chain (N)",
        "- confidence:      'high' | 'medium' | 'low'",
        "",
        "Return ONLY JSON.",
    ])
    return _chain_step(
        name="step2ab_chain_determination",
        instruction=instruction,
        response_schema=CHAIN_DETERMINATION_SCHEMA,
    )


# ══════════════════════════════════════════════════════════════════
#  STEP 2ab2 — Hidden Indirect Detection (LLM Confirmation)
# ══════════════════════════════════════════════════════════════════

def step2ab2_hidden_indirect_detection() -> StepConfig:
    """Confirm hidden indirect candidates flagged by the code stage."""
    instruction = "\n".join([
        "╔═══════════════════════════════════════════════════════════════╗",
        "║  STEP 2ab2: HIDDEN INDIRECT DETECTION (LLM CONFIRMATION)     ║",
        "╚═══════════════════════════════════════════════════════════════╝",
        "",
        "MAIN PROTEIN: {ctx_json.main}",
        "",
        "PURPOSE: A code stage has already extracted candidate proteins from",
        "direct interaction claims — proteins that may represent hidden indirect",
        "interactions buried in the text. Your job is to CONFIRM or REJECT each",
        "candidate.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "INPUT",
        "═══════════════════════════════════════════════════════════════",
        "",
        "You will receive a list of candidates, each with:",
        "- The source interactor (a direct interactor of {ctx_json.main})",
        "- The candidate protein name extracted from the claim text",
        "- The claim index and relevant text snippet",
        "",
        "═══════════════════════════════════════════════════════════════",
        "DECISION TREE — Two independent flags",
        "═══════════════════════════════════════════════════════════════",
        "",
        "For each candidate, decide TWO things independently:",
        "",
        "FLAG 1 — `confirmed`: is this a real functional participant?",
        "   CONFIRM (confirmed=true) when the protein:",
        "   - Plays a specific, named role in the cascade or mechanism",
        "   - Acts as a substrate, enzyme, cofactor, or signaling intermediate",
        "   - Is required for the described biological effect to occur",
        "",
        "   REJECT (confirmed=false) when the protein is:",
        "   - A non-protein or generic entity: RNA, mRNA, DNA, generic",
        "     'Ubiquitin', proteasome, ribosome, chromatin, a pathway,",
        "     a compartment, or a complex rather than a specific HGNC symbol",
        "   - A generic reference (e.g., 'kinases', 'ubiquitin ligases')",
        "   - Mentioned only as a family name, not a specific gene product",
        "   - Part of a large complex mentioned in passing",
        "   - A synonym or alias for the source interactor itself",
        "   - Only mentioned in the evidence/citation section, not the mechanism",
        "",
        "FLAG 2 — `chain_potential`: does this unlock a NEW hop pair?",
        "   PRE-FILTER: Is the candidate already listed as a DIRECT interactor",
        "   of {ctx_json.main} in ctx_json.interactors[]?",
        "     YES, AND any of its existing direct claims describes the SAME",
        "         mechanism as the source claim:",
        "           → chain_potential=false, reason='already_covered_as_direct'.",
        "           (2ab5 would just rediscover this — skip it here.)",
        "     YES, but the mechanism is distinct:",
        "           → chain_potential=true. We're unlocking a new hop pair",
        "           (source_interactor -> candidate) that isn't in step 2a.",
        "     NO, not a direct interactor yet:",
        "           → chain_potential=true. It needs promotion + claim gen.",
        "",
        "   A candidate can be confirmed=true with chain_potential=false.",
        "   A rejected candidate (confirmed=false) is always chain_potential=false.",
        "",
        "When chain_potential=true, list every implicated protein name in",
        "`implicated_proteins` (the candidate itself plus any other proteins",
        "named in the same chain segment — upstream or downstream partners).",
        "",
        "═══════════════════════════════════════════════════════════════",
        "OUTPUT",
        "═══════════════════════════════════════════════════════════════",
        "",
        "Return a JSON object with 'confirmations' — an array of entries,",
        "one per candidate. Each entry must include:",
        "- interactor: source interactor HGNC symbol",
        "- candidate_protein: the candidate protein HGNC symbol",
        "- claim_index: zero-based index in the source interactor's functions[]",
        "- confirmed: true if genuinely implicated, false otherwise",
        "- chain_potential: true ONLY when this unlocks a new hop pair",
        "  (see pre-filter above; a confirmed protein may still have",
        "   chain_potential=false if already covered as a direct claim)",
        "- implicated_proteins: array of protein HGNC symbols (empty when",
        "  chain_potential=false)",
        "- reasoning: brief explanation — include the pre-filter outcome",
        "  (e.g., 'already_covered_as_direct', 'new_hop_pair',",
        "   'not_a_specific_gene_product')",
        "",
        "Return ONLY JSON.",
    ])
    return _chain_step(
        name="step2ab2_hidden_indirect_detection",
        instruction=instruction,
        response_schema=HIDDEN_INDIRECT_CONFIRMATION_SCHEMA,
    )


# ══════════════════════════════════════════════════════════════════
#  STEP 2ab3 — Chain Determination for Hidden Indirects
# ══════════════════════════════════════════════════════════════════

def step2ab3_hidden_chain_determination() -> StepConfig:
    """Determine chain position for newly discovered hidden indirects."""
    instruction = "\n".join([
        "STEP 2ab3 — Chain determination for hidden indirects",
        "",
        "MAIN PROTEIN: {ctx_json.main}",
        "",
        "For each confirmed chain_potential claim from step 2ab2, build the",
        "full ordered protein chain that explains the mechanism. The chain",
        "must contain both the newly discovered protein and {ctx_json.main},",
        "AND the original source interactor the candidate was extracted from",
        "(this is the chain's target endpoint).",
        "",
        "═══════════════════════════════════════════════════════════════",
        "HARD RULES (identical to 2ab)",
        "═══════════════════════════════════════════════════════════════",
        "",
        "1. Chain is an ordered list of proteins, upstream → downstream.",
        "   Default assumption: chain_length ≥ 4. Length 3 is rare — emit",
        "   it only when the biology truly has a single mediator. Do NOT",
        "   collapse a described 5-step cascade to length 3.",
        "",
        "1b. Every chain node must be a specific protein/gene symbol.",
        "   Do NOT put RNA, mRNA, DNA, generic 'Ubiquitin', proteasome,",
        "   ribosome, chromatin, pathways, compartments, or complexes in",
        "   the chain. Those are mechanism context, not graph nodes. Use",
        "   UBB/UBC/other HGNC symbols only when specifically named.",
        "",
        "2. The query {ctx_json.main} may sit at ANY position (head, middle,",
        "   or tail). Do NOT force it to the head.",
        "",
        "3. The newly discovered protein goes WHERE THE BIOLOGY PUTS IT.",
        "   Do NOT always place it next to the query.",
        "",
        "4. intermediaries = full_chain MINUS query MINUS target.",
        "   The `target` here is the ORIGINAL source interactor (the direct",
        "   interactor of {ctx_json.main} the candidate was extracted from —",
        "   it sits at the LAST position of the chain, unless the biology",
        "   places it elsewhere).",
        "   NEVER include the query or target in the intermediaries list.",
        "   The newly discovered protein IS an intermediary (unless it",
        "   happens to be the target itself).",
        "",
        "5. Mark the new protein with **...** in the chain string.",
        "   Mark every OTHER intermediary with ^...^. The query and target",
        "   appear unmarked at their positions.",
        "",
        "═══════════════════════════════════════════════════════════════",
        "WORKED EXAMPLES",
        "═══════════════════════════════════════════════════════════════",
        "",
        "EXAMPLE A — query at HEAD, new protein mid-chain, length 5",
        "  Source interactor (= target): SOURCE_A",
        "  New protein:                   NEW",
        "  Chain:          {ctx_json.main} -> ^UBE2D2^ -> **NEW** -> ^RNF168^ -> SOURCE_A",
        "  intermediaries: ['UBE2D2', 'NEW', 'RNF168']",
        "  new_protein_position: 3",
        "  query_position:       1",
        "  chain_length:         5",
        "",
        "EXAMPLE B — query in MIDDLE, new protein upstream of query, length 6",
        "  Source interactor (= target): SOURCE_B",
        "  New protein:                   NEW",
        "  Chain:          ^A^ -> **NEW** -> {ctx_json.main} -> ^B^ -> ^C^ -> SOURCE_B",
        "  intermediaries: ['A', 'NEW', 'B', 'C']",
        "  new_protein_position: 2",
        "  query_position:       3",
        "  chain_length:         6",
        "",
        "EXAMPLE C — query at TAIL, new protein at head, length 4",
        "  Source interactor (= target): SOURCE_C   (NOTE: target may sit upstream",
        "  of the query when the biology is upstream-regulation of the query)",
        "  New protein:                   NEW",
        "  Chain:          **NEW** -> ^X^ -> SOURCE_C -> {ctx_json.main}",
        "  intermediaries: ['NEW', 'X']",
        "  new_protein_position: 1",
        "  query_position:       4",
        "  chain_length:         4",
        "  (target SOURCE_C is at position 3; query at 4. When the source",
        "   claim is upstream-of-query biology, the target is NOT always",
        "   the last element — it is the source interactor the candidate",
        "   was extracted from.)",
        "",
        "═══════════════════════════════════════════════════════════════",
        "OUTPUT",
        "═══════════════════════════════════════════════════════════════",
        "",
        "JSON with 'chain_results' — an array where each entry has:",
        "- interactor:           source interactor HGNC symbol (= target)",
        "- chain:                full chain string; **...** on new protein,",
        "                        ^...^ on every OTHER intermediary",
        "- intermediaries:       ordered HGNC symbols of intermediaries",
        "                        (= full_chain MINUS query MINUS target)",
        "- new_protein_position: 1-based index of the new protein in full_chain",
        "- query_position:       1-based index of {ctx_json.main} in full_chain",
        "- chain_length:         total proteins in the chain (N)",
        "- confidence:           'high' | 'medium' | 'low'",
        "",
        "Return ONLY JSON.",
    ])
    return _chain_step(
        name="step2ab3_hidden_chain_determination",
        instruction=instruction,
        response_schema=CHAIN_DETERMINATION_SCHEMA,
    )


# ══════════════════════════════════════════════════════════════════
#  STEP 2ab5 — Duplicate Check for Explicit Indirects
# ══════════════════════════════════════════════════════════════════

def step2ab5_extract_pairs_explicit() -> StepConfig:
    """Check for duplicate claims when a chain protein is already a direct interactor."""
    instruction = "\n".join([
        "╔═══════════════════════════════════════════════════════════════╗",
        "║  STEP 2ab5: DUPLICATE CHECK FOR EXPLICIT INDIRECT CHAINS     ║",
        "╚═══════════════════════════════════════════════════════════════╝",
        "",
        "MAIN PROTEIN: {ctx_json.main}",
        "",
        "PURPOSE: When a new protein from an explicit indirect chain (2ab)",
        "already exists as a step 1 direct interactor, compare claims to",
        "find potential duplicate claims (Claim 'V').",
        "",
        "═══════════════════════════════════════════════════════════════",
        "INPUT",
        "═══════════════════════════════════════════════════════════════",
        "",
        "You will receive:",
        "- The chain results from step 2ab (explicit indirect chains)",
        "- The full interactor list from ctx_json, including all direct",
        "  interactors and their existing claims from step 2a",
        "",
        "═══════════════════════════════════════════════════════════════",
        "TASK — For each chain with a NEW_PROTEIN that also exists as",
        "a direct interactor:",
        "═══════════════════════════════════════════════════════════════",
        "",
        "1. IDENTIFY chains where an intermediary protein (from the ^...^",
        "   notation) also appears as a 'direct' interactor in ctx_json.",
        "   Call this protein 'NEW_GUY'.",
        "",
        "2. LOOK UP the {ctx_json.main} <-> NEW_GUY interaction in the 2a output.",
        "   Read ALL scientific claims (functions[]) on that interactor.",
        "",
        "3. SCORE each direct claim against the indirect claim using the",
        "   4-point rubric. Match when ≥ 3 of 4 criteria are satisfied:",
        "",
        "   Criterion 1 — Same pathway family",
        "     (autophagy, UPS, apoptosis, splicing, DDR, translation, …)",
        "",
        "   Criterion 2 — ≥ 2 proteins shared in the described mechanism",
        "     beyond {ctx_json.main} + NEW_GUY. Count only proteins named",
        "     in the mechanism prose; ignore citations.",
        "",
        "   Criterion 3 — Same direction of effect",
        "     (activation / inhibition / scaffolding / recruitment /",
        "      degradation / stabilization)",
        "",
        "   Criterion 4 — Same molecular event class",
        "     (binding, phosphorylation, ubiquitination, acetylation,",
        "      cleavage, transport, transcription, translation, …)",
        "",
        "   Score:",
        "     ≥ 3 of 4 → match_found = true (this is Claim V)",
        "     ≤ 2 of 4 → match_found = false",
        "",
        "4. OUTPUT the result:",
        "   - match_found = true: report the Claim V index, text, and",
        "     criteria_satisfied (which of 1/2/3/4 hit)",
        "   - match_found = false: output 'no_match' with the criteria that",
        "     DID hit (for audit)",
        "",
        "═══════════════════════════════════════════════════════════════",
        "EXAMPLES",
        "═══════════════════════════════════════════════════════════════",
        "",
        "EXAMPLE — MATCH",
        "Chain: ATXN3 -> ^VCP^ -> LAMP2",
        "VCP is also a direct interactor of ATXN3.",
        "Indirect claim: 'ATXN3 modulates autophagosome clearance via",
        "  VCP-mediated LAMP2 trafficking'",
        "VCP direct claim #2: 'VCP ATPase drives autophagosome-lysosome",
        "  fusion through LAMP2 membrane recruitment'",
        "Rubric: (1) autophagy ✓  (2) LAMP2 shared ✓  (3) positive flux ✓",
        "        (4) transport/trafficking ✓  → 4/4 → match_found=true",
        "",
        "EXAMPLE — NO MATCH",
        "Chain: ATXN3 -> ^HDAC6^ -> HSF1",
        "HDAC6 is also a direct interactor of ATXN3.",
        "Indirect claim: 'ATXN3 affects heat shock response through",
        "  HDAC6-mediated HSF1 deacetylation'",
        "HDAC6 direct claims: all about aggresome formation, none about HSF1",
        "Rubric: (1) different pathway (HSR vs aggresome) ✗",
        "        (2) no proteins shared beyond query+NEW_GUY ✗",
        "        (3) different direction ✗  (4) different event ✗",
        "        → 0/4 → match_found=false",
        "",
        "EXAMPLE — EDGE CASE, NOT A MATCH",
        "Chain: ATXN3 -> ^VCP^ -> ERAD_SUBSTRATE",
        "Indirect claim describes VCP acting DOWNSTREAM of ATXN3 in ERAD.",
        "VCP direct claim describes VCP acting UPSTREAM of ATXN3 during",
        "  DNA damage response.",
        "Rubric: (1) different pathway (ERAD vs DDR) ✗",
        "        (2) no proteins shared ✗   (3) opposite direction ✗",
        "        (4) different event class ✗  → 0/4 → match_found=false",
        "",
        "═══════════════════════════════════════════════════════════════",
        "OUTPUT",
        "═══════════════════════════════════════════════════════════════",
        "",
        "Return a JSON object with 'match_results' — an array of entries,",
        "one per chain that has a NEW_GUY overlapping with direct interactors.",
        "Each entry must include:",
        "- chain: the full chain string",
        "- new_protein: the intermediary protein that overlaps",
        "- existing_interactor: the direct interactor name (same as new_protein)",
        "- match_found: true/false",
        "- claim_v_index: zero-based index of the matching claim (-1 if no match)",
        "- claim_v_text: first 200 chars of the matching claim (empty if no match)",
        "- criteria_satisfied: list of integers 1-4 showing which rubric",
        "  criteria hit (e.g. [1, 2, 4]); report this for BOTH match and",
        "  no-match outcomes so we can audit borderline cases",
        "- similarity_reasoning: brief explanation of why match/no-match",
        "",
        "Return ONLY JSON.",
    ])
    return _chain_step(
        name="step2ab5_extract_pairs_explicit",
        instruction=instruction,
        response_schema=DUPLICATE_CHECK_SCHEMA,
        max_output_tokens=65536,
        thinking_level="medium",
    )


# ══════════════════════════════════════════════════════════════════
#  STEP 2ax — Claim Generation for Explicit Indirect Chains
# ══════════════════════════════════════════════════════════════════

def step2ax_claim_generation_explicit(
    *,
    api_mode: str = "generate",
    cache_system_prompt: bool = False,
) -> StepConfig:
    """Generate full scientific claims for explicit indirect chain pairs."""
    instruction = "\n".join([
        "╔═══════════════════════════════════════════════════════════════╗",
        "║  STEP 2ax: CLAIM GENERATION FOR EXPLICIT INDIRECT CHAINS     ║",
        "╚═══════════════════════════════════════════════════════════════╝",
        "",
        _claim_generation_instruction("explicit indirect (2ab)"),
        "",
        "═══════════════════════════════════════════════════════════════",
        "EXPLICIT-TRACK SPECIFICS",
        "═══════════════════════════════════════════════════════════════",
        "",
        "This step processes chains from step 2ab (explicit indirects —",
        "interactions that were originally flagged as indirect in step 2a,",
        "with chains resolved in 2ab).",
        "",
        "For each chain resolved by 2ab:",
        "1. Read the chain determination (interactor, chain, intermediaries).",
        "2. Read the duplicate-check result from 2ab5.",
        "3. Apply Mode 1 or Mode 2 as appropriate.",
        "4. Generate claims for EACH consecutive pair in the chain.",
        "",
        "EXAMPLE:",
        "Chain: {ctx_json.main} -> ^VCP^ -> LAMP2",
        "Pairs to generate claims for:",
        "  - {ctx_json.main} <-> VCP  (first link)",
        "  - VCP <-> LAMP2  (second link)",
        "",
        "If Claim V found on the {ctx_json.main}<->VCP interaction:",
        "  - Claim Y describes {ctx_json.main}<->VCP in V's pathway context",
        "  - Claim W describes VCP<->LAMP2 in V's pathway context",
        "",
        "If no Claim V:",
        "  - Generate new claims for each pair, grounded in the original",
        "    indirect claim's biological context.",
    ])
    return _heavy_claim_step(
        name="step2ax_claim_generation_explicit",
        instruction=instruction,
        suffix="Return ONLY JSON with top-level chain_claims.",
        api_mode=api_mode,
        cache_system_prompt=cache_system_prompt,
    )


# ══════════════════════════════════════════════════════════════════
#  STEP 2az — Claim Generation for Hidden Indirect Chains
# ══════════════════════════════════════════════════════════════════

def step2az_claim_generation_hidden(
    *,
    api_mode: str = "generate",
    cache_system_prompt: bool = False,
) -> StepConfig:
    """Generate full scientific claims for hidden indirect chain pairs."""
    instruction = "\n".join([
        "╔═══════════════════════════════════════════════════════════════╗",
        "║  STEP 2az: CLAIM GENERATION FOR HIDDEN INDIRECT CHAINS       ║",
        "╚═══════════════════════════════════════════════════════════════╝",
        "",
        _claim_generation_instruction("hidden indirect (2ab3)"),
        "",
        "═══════════════════════════════════════════════════════════════",
        "HIDDEN-TRACK SPECIFICS",
        "═══════════════════════════════════════════════════════════════",
        "",
        "This step processes chains from step 2ab3 (hidden indirects —",
        "proteins discovered within direct interaction claims, confirmed",
        "by 2ab2, with chain positions resolved by 2ab3).",
        "",
        "For each chain resolved by 2ab3:",
        "1. Read the chain determination (interactor, chain, new protein",
        "   marked with **...**).",
        "2. Read any duplicate-check results (if applicable).",
        "3. Apply Mode 1 or Mode 2 as appropriate.",
        "4. Generate claims for EACH consecutive pair in the chain.",
        "",
        "EXAMPLE:",
        "Chain: {ctx_json.main} -> **RHEB** -> mTOR",
        "The **...** marks the NEWLY DISCOVERED protein. Pairs:",
        "  - {ctx_json.main} <-> RHEB  (first link)",
        "  - RHEB <-> mTOR  (second link)",
        "",
        "The original claim was on the {ctx_json.main}<->mTOR direct",
        "interaction. RHEB was hidden inside that claim's mechanism.",
        "Now generate independent claims for each binary pair.",
    ])
    return _heavy_claim_step(
        name="step2az_claim_generation_hidden",
        instruction=instruction,
        suffix="Return ONLY JSON with top-level chain_claims.",
        api_mode=api_mode,
        cache_system_prompt=cache_system_prompt,
    )
