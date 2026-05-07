"""Chat service: context building, prompt construction, LLM calls, and stateful sessions."""

import json
import os
import sys
import time
import threading

from services.state import CACHE_DIR
from services.data_builder import build_full_json_from_db
from utils.pruner import PROTEIN_RE
from utils.gemini_runtime import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    build_interaction_generation_config,
    build_interaction_tools,
    call_interaction,
    contains_url,
    extract_text_from_interaction,
    get_client,
    get_core_model,
    should_enable_google_search,
)

# ---------------------------------------------------------------------------
# Stateful session mapping: (protein, session_id) -> (interaction_id, timestamp)
# ---------------------------------------------------------------------------
_interaction_map: dict[tuple[str, str], tuple[str, float]] = {}
_map_lock = threading.Lock()
_MAP_TTL = 3600  # 1 hour


def get_interaction_id(protein: str, session_id: str) -> str | None:
    """Look up stored interaction_id for a (protein, session_id) pair."""
    with _map_lock:
        entry = _interaction_map.get((protein, session_id))
        if entry and (time.time() - entry[1]) < _MAP_TTL:
            return entry[0]
        return None


def store_interaction_id(protein: str, session_id: str, interaction_id: str) -> None:
    """Store interaction_id for a (protein, session_id) pair."""
    with _map_lock:
        _interaction_map[(protein, session_id)] = (interaction_id, time.time())
        # Evict stale entries
        cutoff = time.time() - _MAP_TTL
        stale = [k for k, v in _interaction_map.items() if v[1] < cutoff]
        for k in stale:
            del _interaction_map[k]


# ---------------------------------------------------------------------------
# Cache / normalize helpers
# ---------------------------------------------------------------------------

def read_cache_json(protein: str) -> dict:
    """Read and parse cache JSON for a protein."""
    try:
        json_path = os.path.join(CACHE_DIR, f"{protein}.json")
        if not os.path.exists(json_path):
            return {}
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        print(f"Warning: Failed to read cache for {protein}: {e}", file=sys.stderr)
        return {}


def normalize_arrow_value(arrow: str) -> str:
    """Normalize arrow value to standard abbreviation."""
    if not isinstance(arrow, str):
        arrow = str(arrow) if arrow else ""
    arrow_lower = arrow.lower().strip()
    if "activ" in arrow_lower:
        return "act"
    elif "inhib" in arrow_lower:
        return "inh"
    elif "regul" in arrow_lower or "modulat" in arrow_lower:
        return "reg"
    elif "bind" in arrow_lower:
        return "bind"
    else:
        return "unk"


def normalize_direction_value(direction: str) -> str:
    """Normalize direction value to standard abbreviation."""
    if not isinstance(direction, str):
        direction = str(direction) if direction else ""
    direction_lower = direction.lower().strip()
    if "bidir" in direction_lower:
        return "bidir"
    elif "main_to_primary" in direction_lower:
        return "m2p"
    elif "primary_to_main" in direction_lower:
        return "p2m"
    else:
        return "unk"


def extract_compact_functions(raw_functions: list) -> list:
    """Extract compact function data from raw functions array."""
    functions = []
    if not isinstance(raw_functions, list):
        return functions

    for fn in raw_functions[:5]:
        if not isinstance(fn, dict):
            continue

        try:
            fn_confidence = float(fn.get("confidence", 0.0))
        except (ValueError, TypeError):
            fn_confidence = 0.0

        compact_fn = {
            "name": str(fn.get("function", "Unknown")).strip(),
            "arrow": normalize_arrow_value(fn.get("arrow", "")),
            "confidence": fn_confidence,
            "pmids": [],
            "effect": str(fn.get("effect_description", "")).strip(),
            "biological_consequence": [],
            "specific_effects": []
        }

        fn_pmids = fn.get("pmids", [])
        if isinstance(fn_pmids, list):
            compact_fn["pmids"] = [str(p) for p in fn_pmids[:5] if p]

        bio_cons = fn.get("biological_consequence", [])
        if isinstance(bio_cons, list):
            compact_fn["biological_consequence"] = [
                str(b).strip() for b in bio_cons[:5] if b
            ]

        spec_eff = fn.get("specific_effects", [])
        if isinstance(spec_eff, list):
            compact_fn["specific_effects"] = [
                str(e).strip() for e in spec_eff[:3] if e
            ]

        functions.append(compact_fn)

    return functions


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def build_compact_rich_context(parent: str, visible_proteins: list) -> dict:
    """Build compact rich context with biological cascades, functions, summaries, effects."""
    visible_set = set(visible_proteins)
    interactions_map = {}

    for protein in visible_proteins:
        snapshot = None
        try:
            db_result = build_full_json_from_db(protein)
            if db_result:
                snapshot = db_result.get("snapshot_json", db_result)
        except Exception as e:
            print(f"[WARN]Database query failed for {protein}: {e}", file=sys.stderr)

        if not snapshot:
            root_data = read_cache_json(protein)
            if root_data:
                snapshot = root_data.get("snapshot_json", root_data)

        if not snapshot or not isinstance(snapshot, dict):
            continue

        raw_interactions = snapshot.get("interactions", None)

        if raw_interactions is not None and isinstance(raw_interactions, list):
            # NEW FORMAT: interactions array
            for interaction in raw_interactions:
                if not isinstance(interaction, dict):
                    continue

                source = interaction.get("source", "")
                target = interaction.get("target", "")

                if not source or not target:
                    continue
                if source not in visible_set or target not in visible_set:
                    continue

                canonical_key = "-".join(sorted([source, target]))
                if canonical_key in interactions_map:
                    continue

                try:
                    confidence = float(interaction.get("confidence", 0.0))
                except (ValueError, TypeError):
                    confidence = 0.0

                compact_inter = {
                    "source": str(source),
                    "target": str(target),
                    "type": str(interaction.get("type", "direct")),
                    "arrow": normalize_arrow_value(interaction.get("arrow", "")),
                    "direction": normalize_direction_value(interaction.get("direction", "")),
                    "confidence": confidence,
                    "pmids": [],
                    "summary": str(interaction.get("support_summary", "")).strip(),
                    "functions": []
                }

                raw_pmids = interaction.get("pmids", [])
                if isinstance(raw_pmids, list):
                    compact_inter["pmids"] = [str(p) for p in raw_pmids[:5] if p]

                compact_inter["functions"] = extract_compact_functions(interaction.get("functions", []))
                interactions_map[canonical_key] = compact_inter

        else:
            # OLD FORMAT: interactors array
            interactors = snapshot.get("interactors", [])
            if not isinstance(interactors, list):
                continue

            main_protein = snapshot.get("main", protein)

            for inter in interactors:
                if not isinstance(inter, dict):
                    continue

                primary = inter.get("primary", "")
                if not primary or primary not in visible_set:
                    continue

                canonical_key = "-".join(sorted([main_protein, primary]))
                if canonical_key in interactions_map:
                    continue

                try:
                    confidence = float(inter.get("confidence", 0.0))
                except (ValueError, TypeError):
                    confidence = 0.0

                compact_inter = {
                    "source": str(main_protein),
                    "target": str(primary),
                    "type": "direct",
                    "arrow": normalize_arrow_value(inter.get("arrow", "")),
                    "direction": normalize_direction_value(inter.get("direction", "")),
                    "confidence": confidence,
                    "pmids": [],
                    "summary": str(inter.get("support_summary", "")).strip(),
                    "functions": []
                }

                raw_pmids = inter.get("pmids", [])
                if isinstance(raw_pmids, list):
                    compact_inter["pmids"] = [str(p) for p in raw_pmids[:5] if p]

                compact_inter["functions"] = extract_compact_functions(inter.get("functions", []))
                interactions_map[canonical_key] = compact_inter

    interactions = list(interactions_map.values())

    return {
        "main": str(parent),
        "interactions": interactions
    }


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_chat_system_prompt(parent: str, rich_context: dict) -> str:
    """Build system prompt with compact rich context using 2-3 letter abbreviations."""
    legend = """ABBREVIATION LEGEND:
SUM=summary | FN=function | EFF=effect
BC=biological_consequence | SE=specific_effects

Arrows: -> (activates), -| (inhibits), <-> (binds)"""

    main_protein = rich_context.get("main", parent)
    interactions = rich_context.get("interactions", [])

    interactions_lines = []
    interactions_lines.append(f"ROOT PROTEIN: {main_protein}")
    interactions_lines.append("")

    if not interactions:
        interactions_lines.append("No interaction data available in current view.")
    else:
        interactions_lines.append("INTERACTIONS:")
        interactions_lines.append("")

        for i, inter in enumerate(interactions, 1):
            source = inter.get("source", "Unknown")
            target = inter.get("target", "Unknown")
            arrow = normalize_arrow_value(inter.get("arrow", "unk"))
            direction = normalize_direction_value(inter.get("direction", "unk"))
            summary = inter.get("summary", "")

            # Build SOURCE + ARROW + TARGET line based on direction
            if direction == "bidir":
                if arrow == "bind":
                    interaction_line = f"{source} <-> {target}"
                elif arrow == "act":
                    interaction_line = f"{source} <-> {target} (activates)"
                elif arrow == "inh":
                    interaction_line = f"{source} <-> {target} (inhibits)"
                elif arrow == "reg":
                    interaction_line = f"{source} <-> {target} (regulates)"
                else:
                    interaction_line = f"{source} <-> {target}"
            elif direction == "m2p":
                if arrow == "act":
                    interaction_line = f"{source} -> {target}"
                elif arrow == "inh":
                    interaction_line = f"{source} -| {target}"
                elif arrow == "bind":
                    interaction_line = f"{source} -> {target} (binds)"
                elif arrow == "reg":
                    interaction_line = f"{source} -> {target} (regulates)"
                else:
                    interaction_line = f"{source} -> {target}"
            elif direction == "p2m":
                if arrow == "act":
                    interaction_line = f"{target} -> {source}"
                elif arrow == "inh":
                    interaction_line = f"{target} -| {source}"
                elif arrow == "bind":
                    interaction_line = f"{target} -> {source} (binds)"
                elif arrow == "reg":
                    interaction_line = f"{target} -> {source} (regulates)"
                else:
                    interaction_line = f"{target} -> {source}"
            else:
                if arrow == "act":
                    interaction_line = f"{source} -> {target}"
                elif arrow == "inh":
                    interaction_line = f"{source} -| {target}"
                elif arrow == "bind":
                    interaction_line = f"{source} <-> {target}"
                elif arrow == "reg":
                    interaction_line = f"{source} -> {target} (regulates)"
                else:
                    interaction_line = f"{source} - {target}"

            header = f"{i}. {interaction_line}"
            interactions_lines.append(header)

            if summary:
                interactions_lines.append(f"   SUM: {summary}")

            functions = inter.get("functions", [])
            if functions:
                interactions_lines.append("   Functions:")
                for j, fn in enumerate(functions, 1):
                    fn_name = fn.get("name", "Unknown")
                    fn_arrow = fn.get("arrow", "unk")
                    fn_effect = fn.get("effect", "")
                    bio_cons = fn.get("biological_consequence", [])
                    spec_effs = fn.get("specific_effects", [])

                    interactions_lines.append(f"     F{j}: {fn_name} ACT:{fn_arrow}")

                    if fn_effect:
                        interactions_lines.append(f"         EFF: {fn_effect}")

                    if bio_cons:
                        bc_chain = " -> ".join(bio_cons)
                        interactions_lines.append(f"         BC: {bc_chain}")

                    if spec_effs:
                        se_list = "; ".join(spec_effs)
                        interactions_lines.append(f"         SE: {se_list}")

            interactions_lines.append("")

    interactions_text = "\n".join(interactions_lines)

    full_prompt = f"""\u256c\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551  PROTEIN INTERACTION NETWORK ANALYSIS ASSISTANT               \u2551
\u2551  Expert Molecular Biology Q&A System                          \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d

ROLE & EXPERTISE:
You are a senior molecular biologist and biochemist providing expert consultation
on protein-protein interaction networks. Your audience consists of research scientists,
graduate students, and clinicians who need ACCURATE, EVIDENCE-BASED answers about
protein interactions, functional outcomes, and biological mechanisms.

\u256c\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551  CRITICAL OPERATIONAL RULES (ABSOLUTE OVERRIDE)              \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d

STRICT EVIDENCE BOUNDARIES:
- Answer ONLY using the interaction data provided below
- NEVER extrapolate beyond the visible network context
- If asked about proteins/interactions NOT in the data: explicitly state "Not in current view"
- If data is ambiguous or incomplete: acknowledge uncertainty rather than speculate
- NEVER invent PMIDs, paper citations, or experimental details

ACCURACY > COMPLETENESS:
- A precise partial answer beats a comprehensive guess
- Distinguish between direct interactions and downstream consequences
- Note when evidence is human vs model organism

OUTPUT FORMATTING:
- Use clear, professional scientific prose (NOT markdown)
- NO asterisks, underscores, headers, bullets, or special formatting
- Write as if explaining to a colleague at a lab meeting
- Keep responses CONCISE (2-4 sentences) unless depth is explicitly requested

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
NETWORK DATA LEGEND
\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501

{legend}

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
CURRENT NETWORK CONTEXT
\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501

{interactions_text}

\u256c\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551  EXPERT RESPONSE FRAMEWORK                                    \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d

WHEN ANSWERING ABOUT INTERACTIONS:
1. Identify the relevant interaction(s) from the data above
2. State directionality clearly (X activates Y, Y inhibits X, bidirectional)
3. Explain biological context only if present in the data

WHEN ANSWERING ABOUT FUNCTIONS:
1. Link function to the SPECIFIC interaction that drives it
2. Distinguish between:
   - Effect (EFF): What happens to the function immediately
   - Biological consequences (BC): Downstream signaling cascades
   - Specific effects (SE): Direct molecular outcomes
3. Use arrow notation where appropriate (e.g., "TP53 stabilization leads to BAX upregulation")

WHEN DISCUSSING BIOLOGICAL SIGNIFICANCE:
1. Integrate information across multiple interactions when asked
2. Connect interaction mechanisms to functional outcomes
3. Explain cascades step-by-step when asked about pathways
4. Relate to disease contexts only if present in the data
5. Acknowledge gaps: "Function X is documented but mechanism details are not available"

WHEN HANDLING AMBIGUOUS QUESTIONS:
- If question is too broad: "Could you clarify which aspect/protein you're interested in?"
- If query protein not in network: "That protein is not in the current network view"
- If mechanism unclear from data: "The data shows interaction but mechanism is not specified"

RESPONSE LENGTH CALIBRATION:
- Brief query (e.g., "Does X interact with Y?"): 1-2 sentences
- Mechanism query (e.g., "How does X regulate Y?"): 2-4 sentences with key details
- Comprehensive query (e.g., "Explain X's role in pathway Z"): 4-6 sentences, integrate multiple functions
- Explicit detail request (e.g., "Give me all the details"): Expand fully with all cascades and effects

DOMAIN-SPECIFIC INTELLIGENCE:
- Recognize common post-translational modifications (phosphorylation, ubiquitination, etc.)
- Understand arrow semantics: activation vs inhibition vs binding
- Distinguish between interaction directionality (who regulates whom)
- Interpret biological consequences as signaling cascades
- Translate abbreviated data (BC, SE, EFF) into prose seamlessly

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501

EXAMPLES OF EXCELLENT RESPONSES:

Q: "Does ATXN3 interact with VCP?"
A: "Yes, ATXN3 directly interacts with VCP. ATXN3 binds VCP through its ubiquitin-binding domain, supported by Co-IP and pull-down assays in human cells."

Q: "What functions does the ATXN3-VCP interaction regulate?"
A: "The ATXN3-VCP interaction regulates protein quality control and autophagy. VCP binding enhances ATXN3's deubiquitinase activity, leading to substrate stabilization. This activates autophagy pathways through mTOR signaling modulation and promotes clearance of misfolded proteins via the ERAD pathway."

Q: "Tell me about the biological consequences"
A: "The interaction triggers multiple cascades. First, ATXN3 deubiquitinates VCP substrates, preventing their proteasomal degradation and stabilizing protein levels. This stabilization activates downstream autophagy machinery through BECN1 recruitment and LC3 lipidation. Additionally, VCP-ATXN3 complexes facilitate ER-associated degradation by extracting ubiquitinated proteins from the ER membrane, which maintains ER homeostasis under proteotoxic stress."

Q: "Is there evidence for this in disease?"
A: "The current network data does not include disease-specific contexts or patient studies. The interactions and functions shown are based on cell biology experiments in human cell lines."

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501

You are now ready to answer questions. Provide accurate, evidence-based responses
using ONLY the network data shown above. Maintain expert-level rigor."""

    return full_prompt


# ---------------------------------------------------------------------------
# State extraction from request
# ---------------------------------------------------------------------------

def build_compact_state_from_request(state_data: dict) -> dict:
    """Extract and validate visible protein list from frontend request."""
    if not isinstance(state_data, dict):
        return {"parent": "", "visible_proteins": []}

    parent = str(state_data.get("parent", "")).strip()
    visible_proteins = state_data.get("visible_proteins", [])

    clean_visible = []
    if isinstance(visible_proteins, list):
        for protein in visible_proteins:
            if protein and isinstance(protein, str):
                clean_protein = str(protein).strip()
                if clean_protein and PROTEIN_RE.match(clean_protein):
                    clean_visible.append(clean_protein)

    return {
        "parent": parent,
        "visible_proteins": clean_visible
    }


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def call_chat_llm(
    messages: list,
    system_prompt: str,
    max_history: int = 10,
    previous_interaction_id: str | None = None,
) -> tuple[str, str | None]:
    """Call Gemini LLM for chat response using Interactions API."""
    client = get_client()

    trimmed_messages = messages[-max_history:] if len(messages) > max_history else messages

    if not trimmed_messages:
        raise RuntimeError("No valid messages to send to LLM")

    last_msg = trimmed_messages[-1]
    if last_msg.get("role") != "user":
        raise RuntimeError("Last message must be from user (Gemini API requirement)")

    latest_user_input = str(last_msg.get("content", "")).strip()
    if not latest_user_input:
        raise RuntimeError("Last user message is empty")

    # Stateless fallback for first turn: include recent transcript inline.
    if previous_interaction_id:
        interaction_input = latest_user_input
    else:
        transcript_lines = []
        for msg in trimmed_messages:
            role = "Assistant" if msg.get("role") == "assistant" else "User"
            text = str(msg.get("content", "")).strip()
            if text:
                transcript_lines.append(f"{role}: {text}")
        interaction_input = "Conversation so far:\n" + "\n".join(transcript_lines)

    use_url_context = contains_url(latest_user_input)
    use_google_search = should_enable_google_search(latest_user_input)
    tools = build_interaction_tools(
        use_google_search=use_google_search,
        use_url_context=use_url_context,
    )
    generation_config = build_interaction_generation_config(
        thinking_level="medium",
        thinking_summaries="auto",
        max_output_tokens=8000,
    )

    try:
        interaction = call_interaction(
            input_text=interaction_input,
            model=get_core_model(),
            system_instruction=system_prompt,
            generation_config=generation_config,
            tools=tools if tools else None,
            previous_interaction_id=previous_interaction_id,
            max_retries=3,
            base_delay=1.5,
        )
        text = extract_text_from_interaction(interaction).strip()
        if text:
            return text, getattr(interaction, "id", None)
        raise RuntimeError("LLM returned empty response")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Chat LLM call failed: {e}") from e
