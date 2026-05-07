#!/usr/bin/env python3
"""
Interaction Metadata Generator
Synthesizes comprehensive interaction-level metadata from function-level evidence.

This module runs AFTER evidence validation to:
1. Determine interaction arrow/intent based on ALL function-level arrows
2. Generate MECHANISM field from all cellular_process fields
3. Generate EFFECT field from all effect_description fields
4. Generate SUMMARY field (1-2 sentence biological overview)
5. Compile ALL evidence from ALL functions (deduplicate PMIDs)
6. Remove confidence fields from output
7. (Optional) LLM synthesis for publication-quality descriptions
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Check if Gemini is available for LLM synthesis
try:
    from google import genai as google_genai
    from .gemini_runtime import (
        DEFAULT_MAX_OUTPUT_TOKENS,
        build_generate_content_config,
        extract_text_from_generate_response,
        get_client,
        minimal_json_object_schema,
    )
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False


class MetadataGeneratorError(RuntimeError):
    """Raised when metadata generation fails."""
    pass


def determine_interaction_arrow(functions: List[Dict[str, Any]]) -> str:
    """
    Determine interaction-level arrow based on ALL function-level arrows.

    Logic:
    - If ALL functions activate: return "activates"
    - If ALL functions inhibit: return "inhibits"
    - If MIXED (some activate, some inhibit): return "regulates" or "modulates"
    - If only binding/no clear direction: return "binds"

    Args:
        functions: List of function dictionaries

    Returns:
        str: The determined arrow type ("activates", "inhibits", "regulates", or "binds")
    """
    if not functions:
        return "binds"

    arrows = [f.get("arrow", "").lower() for f in functions if f.get("arrow")]

    if not arrows:
        return "binds"

    # Count arrow types
    activates_count = sum(1 for a in arrows if a in ["activates", "activate", "promotes", "enhances"])
    inhibits_count = sum(1 for a in arrows if a in ["inhibits", "inhibit", "suppresses", "represses"])
    binds_count = sum(1 for a in arrows if a == "binds")

    # Decision logic
    if activates_count > 0 and inhibits_count == 0:
        return "activates"
    elif inhibits_count > 0 and activates_count == 0:
        return "inhibits"
    elif activates_count > 0 and inhibits_count > 0:
        return "regulates"
    elif binds_count > 0:
        return "binds"
    else:
        return "binds"


def determine_interaction_intent(functions: List[Dict[str, Any]], current_intent: str) -> str:
    """
    Determine or refine interaction-level intent based on functions.

    Args:
        functions: List of function dictionaries
        current_intent: Current intent from interaction level

    Returns:
        str: Refined intent description
    """
    if not functions:
        return current_intent or "binding"

    # If current intent is already specific, keep it
    if current_intent and current_intent not in ["binding", "interaction", "unknown"]:
        return current_intent

    # Extract mechanisms from cellular_process fields
    mechanisms = []
    for func in functions:
        cellular_process = func.get("cellular_process", "")
        if cellular_process:
            lower = cellular_process.lower()
            if "phosphorylat" in lower:
                mechanisms.append("phosphorylation")
            elif "ubiquitin" in lower:
                mechanisms.append("ubiquitination")
            elif "deubiquitin" in lower:
                mechanisms.append("deubiquitination")
            elif "acetylat" in lower:
                mechanisms.append("acetylation")
            elif "deacetylat" in lower:
                mechanisms.append("deacetylation")
            elif "methylat" in lower:
                mechanisms.append("methylation")
            elif "sumoylat" in lower:
                mechanisms.append("sumoylation")

    if mechanisms:
        return mechanisms[0]

    return current_intent or "regulation"


def generate_mechanism_field(functions: List[Dict[str, Any]]) -> str:
    """
    Synthesize MECHANISM field from ALL function cellular_process fields.

    Strategy: Pick the longest/most detailed cellular_process as the primary mechanism,
    then append unique molecular details from others.

    Args:
        functions: List of function dictionaries

    Returns:
        str: Synthesized mechanism description
    """
    if not functions:
        return None

    # Collect all cellular_process descriptions with their lengths
    mechanisms = []
    for func in functions:
        cellular_process = func.get("cellular_process", "")
        if cellular_process and len(cellular_process.strip()) > 10:
            mechanisms.append(cellular_process.strip())

    if not mechanisms:
        return None

    if len(mechanisms) == 1:
        return mechanisms[0]

    # Sort by length descending - pick the most detailed as primary
    mechanisms.sort(key=len, reverse=True)
    primary = mechanisms[0]

    # Extract unique molecular keywords from shorter descriptions not in primary
    primary_lower = primary.lower()
    supplementary_details = []
    seen_keywords = set(primary_lower.split())

    for mech in mechanisms[1:]:
        # Look for specific molecular terms not in primary
        for term in ["domain", "residue", "phospho", "ubiquit", "acetyl",
                      "complex", "conformation", "binding site", "motif"]:
            if term in mech.lower() and term not in primary_lower:
                # Extract the sentence containing this term
                sentences = [s.strip() for s in mech.split('.') if term in s.lower()]
                for sent in sentences[:1]:  # Take first matching sentence
                    if sent and sent not in supplementary_details:
                        supplementary_details.append(sent)

    # Combine primary + supplementary
    result = primary
    if supplementary_details:
        supplement = ". ".join(supplementary_details[:2])
        if not supplement.endswith("."):
            supplement += "."
        result = result.rstrip(".") + ". " + supplement

    # Limit to 800 chars
    if len(result) > 800:
        # Truncate at last complete sentence before limit
        truncated = result[:800]
        last_period = truncated.rfind(".")
        if last_period > 200:
            result = truncated[:last_period + 1]
        else:
            result = truncated + "..."

    return result


def generate_effect_field(functions: List[Dict[str, Any]]) -> str:
    """
    Synthesize EFFECT field from ALL function effect_description fields.

    Strategy: Pick the most informative effect_description, supplement with unique details.

    Args:
        functions: List of function dictionaries

    Returns:
        str: Synthesized effect description
    """
    if not functions:
        return None

    # Collect all effect_description fields
    effects = []
    for func in functions:
        effect_desc = func.get("effect_description", "")
        if effect_desc and len(effect_desc.strip()) > 5:
            effects.append(effect_desc.strip())

    if not effects:
        # Fallback: use function names + arrows
        effect_parts = []
        for func in functions:
            func_name = func.get("function", "")
            arrow = func.get("arrow", "")
            if func_name and arrow:
                if arrow.lower() in ["activates", "activate"]:
                    effect_parts.append(f"Enhances {func_name.lower()}")
                elif arrow.lower() in ["inhibits", "inhibit"]:
                    effect_parts.append(f"Reduces {func_name.lower()}")

        if effect_parts:
            return "; ".join(effect_parts[:3]) + ("..." if len(effect_parts) > 3 else "")

        return None

    if len(effects) == 1:
        return effects[0]

    # Sort by length descending - pick the most detailed as primary
    effects.sort(key=len, reverse=True)
    primary = effects[0]

    # Add unique details from other effects
    primary_lower = primary.lower()
    additional = []
    for eff in effects[1:]:
        # Only add if it contains substantially different content
        eff_words = set(eff.lower().split())
        primary_words = set(primary_lower.split())
        overlap = len(eff_words & primary_words) / max(len(eff_words), 1)
        if overlap < 0.5:  # Less than 50% word overlap = new information
            additional.append(eff)

    result = primary
    if additional:
        result = result.rstrip(".") + ". " + ". ".join(additional[:2])
        if not result.endswith("."):
            result += "."

    # Limit to 600 chars
    if len(result) > 600:
        truncated = result[:600]
        last_period = truncated.rfind(".")
        if last_period > 150:
            result = truncated[:last_period + 1]
        else:
            result = truncated + "..."

    return result


def generate_summary_field(
    main_protein: str,
    interactor: str,
    functions: List[Dict[str, Any]],
    arrow: str,
    direction: str = "main_to_primary"
) -> str:
    """
    Generate SUMMARY field: 1-2 sentence overview of interaction and biological significance.

    Uses the most specific biological consequence to build a meaningful summary
    rather than generic templates.

    Args:
        main_protein: Main protein symbol
        interactor: Interactor protein symbol
        functions: List of all function dictionaries for this interaction
        arrow: Determined interaction arrow
        direction: Interaction direction for proper subject/object framing

    Returns:
        str: 1-2 sentence summary
    """
    if not functions:
        return f"{main_protein} and {interactor} interact, though the functional significance remains to be fully elucidated."

    function_names = [f.get("function", "") for f in functions if f.get("function")]

    # Determine subject/object based on direction
    if direction == "primary_to_main":
        subject = interactor
        obj = main_protein
    else:
        subject = main_protein
        obj = interactor

    # Determine action verb based on arrow
    action_map = {
        "activates": "activates",
        "inhibits": "inhibits",
        "regulates": "regulates",
        "binds": "interacts with"
    }
    action = action_map.get(arrow, "interacts with")

    # Try to extract the best biological consequence for context
    best_consequence = ""
    best_specific_effect = ""
    for func in functions:
        # Get specific effects - most concrete
        specific_effects = func.get("specific_effects", [])
        if isinstance(specific_effects, list) and specific_effects:
            for se in specific_effects:
                if isinstance(se, str) and len(se) > len(best_specific_effect):
                    best_specific_effect = se

        # Get biological consequences
        bio_cons = func.get("biological_consequence", [])
        if isinstance(bio_cons, list) and bio_cons:
            for bc in bio_cons:
                if isinstance(bc, str) and len(bc) > len(best_consequence) and "→" in bc:
                    best_consequence = bc

    # Build summary using the best available data
    if best_specific_effect and len(best_specific_effect) > 20:
        # Use the most specific experimental fact
        summary = f"{subject} {action} {obj}. {best_specific_effect}"
    elif best_consequence:
        # Extract final outcome from cascade
        parts = best_consequence.split("→")
        final_outcome = parts[-1].strip()
        if len(function_names) == 1:
            summary = f"{subject} {action} {obj} in the context of {function_names[0].lower()}, ultimately leading to {final_outcome.lower()}"
        else:
            summary = f"{subject} {action} {obj}, affecting {', '.join(fn.lower() for fn in function_names[:2])}, ultimately leading to {final_outcome.lower()}"
    else:
        # Fallback to function-name-based summary
        if len(function_names) == 1:
            summary = f"{subject} {action} {obj} to modulate {function_names[0].lower()}"
        elif len(function_names) == 2:
            summary = f"{subject} {action} {obj} to regulate {function_names[0].lower()} and {function_names[1].lower()}"
        else:
            summary = f"{subject} {action} {obj} to control multiple processes including {function_names[0].lower()} and {function_names[1].lower()}"

    if not summary.endswith("."):
        summary += "."

    return summary


def compile_evidence(functions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Compile ALL evidence from ALL function boxes, deduplicating by PMID.

    Args:
        functions: List of function dictionaries

    Returns:
        List[Dict]: Compiled evidence array with duplicates removed
    """
    if not functions:
        return []

    all_evidence = []
    seen_pmids: Set[str] = set()

    for func in functions:
        func_evidence = func.get("evidence", [])
        if not isinstance(func_evidence, list):
            continue

        for evidence_entry in func_evidence:
            if not isinstance(evidence_entry, dict):
                continue

            pmid = evidence_entry.get("pmid", "")

            if pmid and pmid in seen_pmids:
                continue

            all_evidence.append(deepcopy(evidence_entry))

            if pmid:
                seen_pmids.add(pmid)

    return all_evidence


def remove_confidence_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove all confidence fields from the payload (both interaction and function level).

    Args:
        data: The full payload dictionary

    Returns:
        Dict: Cleaned payload without confidence fields
    """
    cleaned = deepcopy(data)

    for key in ["ctx_json", "snapshot_json"]:
        if key in cleaned and "interactors" in cleaned[key]:
            for interactor in cleaned[key]["interactors"]:
                if "confidence" in interactor:
                    del interactor["confidence"]

                if "functions" in interactor:
                    for func in interactor["functions"]:
                        if "confidence" in func:
                            del func["confidence"]

    return cleaned


# ============================================================
# LLM SYNTHESIS (optional - requires Gemini API key)
# ============================================================

def _build_synthesis_prompt(
    main_protein: str,
    interactor_name: str,
    functions: List[Dict[str, Any]],
    arrow: str,
    direction: str
) -> str:
    """Build prompt for LLM metadata synthesis."""
    # Determine subject/object
    if direction == "primary_to_main":
        upstream = interactor_name
        downstream = main_protein
    else:
        upstream = main_protein
        downstream = interactor_name

    # Compile function data into a readable format
    func_summaries = []
    for i, fn in enumerate(functions[:6]):  # Limit to 6 functions for token efficiency
        func_summaries.append(
            f"Function {i+1}: {fn.get('function', 'Unknown')}\n"
            f"  Arrow: {fn.get('arrow', 'unknown')}\n"
            f"  Direction: {fn.get('interaction_direction', fn.get('direction', 'unknown'))}\n"
            f"  Cellular Process: {fn.get('cellular_process', 'N/A')}\n"
            f"  Effect: {fn.get('effect_description', 'N/A')}\n"
            f"  Cascades: {json.dumps(fn.get('biological_consequence', []), ensure_ascii=False)}\n"
            f"  Specific Effects: {json.dumps(fn.get('specific_effects', []), ensure_ascii=False)}"
        )

    return f"""You are a molecular biology expert writing publication-quality protein interaction descriptions.

INTERACTION: {upstream} {arrow} {downstream}
Direction: {direction} ({upstream} acts on {downstream})

FUNCTION-LEVEL DATA:
{chr(10).join(func_summaries)}

TASK: Synthesize the above function-level data into THREE fields. Be SPECIFIC and DETAILED.
Use molecular terminology. Reference specific domains, residues, and experimental findings.

Return ONLY valid JSON with these three fields:

{{
  "mechanism": "<3-5 sentences describing HOW the interaction occurs at the molecular level. Include binding domains, modifications, conformational changes. Be specific about protein domains and residues.>",
  "effect": "<2-3 sentences describing WHAT happens as a result. Include cellular-level changes and downstream consequences.>",
  "summary": "<1-2 sentences capturing the biological significance. Reference the most notable experimental finding.>"
}}

IMPORTANT:
- Write in scientific prose, not bullet points
- Be SPECIFIC (name domains, residues, experimental methods)
- Do NOT use generic phrases like "plays a role in" or "is involved in"
- The mechanism should explain the molecular basis, not just restate the function name
- Return ONLY the JSON object, no markdown, no explanation"""


def synthesize_single_interactor(
    interactor: Dict[str, Any],
    main_protein: str,
    api_key: str,
    verbose: bool = False
) -> Dict[str, Any]:
    """
    Use Gemini to synthesize publication-quality mechanism/effect/summary for one interactor.

    Args:
        interactor: Interactor dict with functions
        main_protein: Query protein symbol
        api_key: Google AI API key
        verbose: Enable logging

    Returns:
        Updated interactor dict (or original if synthesis fails)
    """
    interactor_name = interactor.get("primary", "UNKNOWN")
    functions = interactor.get("functions", [])
    arrow = interactor.get("arrow", "binds")
    direction = interactor.get("direction", "main_to_primary")

    if not functions:
        return interactor

    # Skip LLM synthesis for evidence-validated interactors
    if _is_evidence_validated(interactor):
        if verbose:
            print(f"    [PROTECT] Skipping LLM synthesis for {interactor_name} — evidence-validated")
        return interactor

    try:
        prompt = _build_synthesis_prompt(main_protein, interactor_name, functions, arrow, direction)

        client = get_client(api_key)
        config = build_generate_content_config(
            thinking_level="low",
            max_output_tokens=65536,
            temperature=0.3,
            response_mime_type="application/json",
            response_json_schema=minimal_json_object_schema(),
            include_thoughts=False,
        )

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
            config=config,
        )

        # Parse JSON from response
        text = extract_text_from_generate_response(response)
        if not text:
            return interactor

        # Extract JSON
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if not json_match:
            return interactor

        synthesized = json.loads(json_match.group(0))

        # Apply synthesized fields (only if they're better than existing)
        for field in ["mechanism", "effect", "summary"]:
            new_val = synthesized.get(field, "")
            old_val = interactor.get(field, "")
            # Use LLM version if it's substantially longer/better
            if new_val and (len(new_val) > len(old_val) * 0.8 or len(old_val) < 50):
                interactor[field] = new_val

        interactor["_llm_synthesized"] = True

        if verbose:
            print(f"    [LLM] Synthesized metadata for {interactor_name}")

        return interactor

    except Exception as e:
        if verbose:
            print(f"    [WARN] LLM synthesis failed for {interactor_name}: {e}")
        return interactor


def _is_evidence_validated(interactor: Dict[str, Any]) -> bool:
    """Check if an interactor has evidence-validated content that should be preserved."""
    if interactor.get("_validation_status"):
        return True
    if interactor.get("mechanism_correction"):
        return True
    return False


def generate_interaction_metadata(
    payload: Dict[str, Any],
    verbose: bool = False,
    api_key: str = None,
) -> Dict[str, Any]:
    """
    Main function: Generate comprehensive interaction-level metadata from function-level data.

    For each interactor:
    1. Determine interaction arrow from ALL function arrows
    2. Refine interaction intent
    3. Generate MECHANISM field from all cellular_process fields
    4. Generate EFFECT field from all effect_description fields
    5. Generate SUMMARY field (1-2 sentence overview)
    6. Compile ALL evidence from ALL functions
    7. Remove confidence fields
    8. (Optional) LLM synthesis for richer descriptions

    Args:
        payload: The full pipeline payload with ctx_json and snapshot_json
        verbose: Enable detailed logging
        api_key: Google AI API key for LLM synthesis (optional)

    Returns:
        Dict: Updated payload with synthesized interaction metadata
    """
    if verbose:
        print(f"\n{'='*80}")
        print("GENERATING INTERACTION-LEVEL METADATA")
        print(f"{'='*80}")

    result = deepcopy(payload)
    main_protein = result.get("ctx_json", {}).get("main", "UNKNOWN")
    ctx_interactors = result.get("ctx_json", {}).get("interactors", [])

    if verbose:
        print(f"Processing {len(ctx_interactors)} interactors for {main_protein}...")

    for idx, interactor in enumerate(ctx_interactors):
        interactor_name = interactor.get("primary", "UNKNOWN")
        functions = interactor.get("functions", [])
        direction = interactor.get("direction", "main_to_primary")

        if verbose:
            print(f"\n[{idx+1}/{len(ctx_interactors)}] {main_protein} <-> {interactor_name}")
            print(f"  Functions: {len(functions)}")

        # Check if already validated by arrow_effect_validator
        validation_meta = interactor.get("_validation_metadata", {})
        is_validated = validation_meta.get("validated", False)

        if is_validated:
            if verbose:
                print(f"  [SKIP] Arrow already validated by {validation_meta.get('validator', 'unknown')}")
            determined_arrow = interactor.get("arrow", "regulates")
        else:
            # 1. Determine interaction arrow
            determined_arrow = determine_interaction_arrow(functions)
            old_arrow = interactor.get("arrow", "")
            interactor["arrow"] = determined_arrow

            if verbose and old_arrow != determined_arrow:
                print(f"  Arrow: {old_arrow} -> {determined_arrow}")

        # 2. Refine interaction intent
        current_intent = interactor.get("intent", "")
        refined_intent = determine_interaction_intent(functions, current_intent)
        interactor["intent"] = refined_intent

        # Check whether evidence validator has already set these fields
        ev_protected = _is_evidence_validated(interactor)

        if ev_protected:
            if verbose:
                print(f"  [PROTECT] Skipping mechanism/effect/summary — evidence-validated")
        else:
            # 3. Generate MECHANISM field (improved string processing)
            mechanism = generate_mechanism_field(functions)
            interactor["mechanism"] = mechanism

            if verbose:
                print(f"  Mechanism: {mechanism[:80]}..." if mechanism else "  Mechanism: None")

            # 4. Generate EFFECT field (improved string processing)
            effect = generate_effect_field(functions)
            interactor["effect"] = effect

            if verbose:
                print(f"  Effect: {effect[:80]}..." if effect else "  Effect: None")

            # 5. Generate SUMMARY field (improved with direction awareness)
            summary = generate_summary_field(
                main_protein, interactor_name, functions, determined_arrow, direction
            )
            interactor["summary"] = summary

            if verbose:
                print(f"  Summary: {summary[:80]}..." if summary else "  Summary: None")

        # 6. Compile evidence from all functions
        compiled_evidence = compile_evidence(functions)

        existing_evidence = interactor.get("evidence", [])
        if not isinstance(existing_evidence, list):
            existing_evidence = []

        seen_pmids = {e.get("pmid") for e in existing_evidence if e.get("pmid")}
        for evidence_entry in compiled_evidence:
            pmid = evidence_entry.get("pmid")
            if not pmid or pmid not in seen_pmids:
                existing_evidence.append(evidence_entry)
                if pmid:
                    seen_pmids.add(pmid)

        interactor["evidence"] = existing_evidence

        if verbose:
            print(f"  Evidence: {len(existing_evidence)} total citations")

    # 7. (Optional) LLM synthesis pass - parallel processing
    if api_key is None:
        api_key = os.getenv("GOOGLE_CLOUD_PROJECT", "")

    if api_key and GEMINI_AVAILABLE and ctx_interactors:
        # Gate: only call LLM for interactors where pure-code left fields sparse
        _MIN_FIELD_LEN = 50
        needs_llm = [
            (idx, interactor) for idx, interactor in enumerate(ctx_interactors)
            if (
                len(interactor.get("mechanism", "") or "") < _MIN_FIELD_LEN
                or len(interactor.get("summary", "") or "") < _MIN_FIELD_LEN
            )
        ]

        if verbose:
            print(f"\n{'='*60}")
            print(f"LLM SYNTHESIS PASS (parallel) — {len(needs_llm)}/{len(ctx_interactors)} need LLM")
            print(f"{'='*60}")

        worker_count = len(needs_llm) if needs_llm else 0
        if needs_llm:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_to_idx = {
                    executor.submit(
                        synthesize_single_interactor,
                        interactor,
                        main_protein,
                        api_key,
                        verbose
                    ): idx
                    for idx, interactor in needs_llm
                }

                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        ctx_interactors[idx] = future.result()
                    except Exception as e:
                        if verbose:
                            name = ctx_interactors[idx].get("primary", "UNKNOWN")
                            print(f"    [ERROR] Synthesis failed for {name}: {e}")

    # Update snapshot_json if present
    if "snapshot_json" in result:
        snapshot_interactors = result["snapshot_json"].get("interactors", [])
        ctx_map = {i.get("primary"): i for i in ctx_interactors}

        for snap_int in snapshot_interactors:
            primary = snap_int.get("primary")
            if primary in ctx_map:
                ctx_int = ctx_map[primary]
                for field in ["arrow", "intent", "mechanism", "effect", "summary", "evidence"]:
                    if field in ctx_int:
                        snap_int[field] = ctx_int[field]

    # 8. Remove all confidence fields
    result = remove_confidence_fields(result)

    if verbose:
        print(f"\n{'='*80}")
        print("[OK] INTERACTION METADATA GENERATION COMPLETE")
        print(f"{'='*80}")

    return result


def main():
    """CLI entry point for testing/debugging."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate interaction-level metadata from function-level data"
    )
    parser.add_argument(
        "input_json",
        type=str,
        help="Path to validated JSON file"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output path (default: <input>_with_metadata.json)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output"
    )

    args = parser.parse_args()

    # Load input
    input_path = Path(args.input_json)
    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")

    print(f"Loading {input_path}...")
    with open(input_path, 'r', encoding='utf-8') as f:
        payload = json.load(f)

    # Generate metadata
    result = generate_interaction_metadata(payload, verbose=args.verbose)

    # Save output
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.parent / f"{input_path.stem}_with_metadata{input_path.suffix}"

    print(f"\nSaving to {output_path}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("[OK] Done!")


if __name__ == "__main__":
    main()
