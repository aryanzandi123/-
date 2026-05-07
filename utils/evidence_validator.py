#!/usr/bin/env python3
"""
Evidence Validator & Citation Enricher (Integrated Fact-Checker)
Post-processes pipeline JSON to validate biological accuracy, check mechanisms, and enrich with citations.
Uses Gemini 3.0 Pro Preview with Google Search for maximum rigor.
"""

from __future__ import annotations

import json
import os
import sys
import time
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Fix Windows console encoding
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from google import genai
from google.genai import types
from dotenv import load_dotenv
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception
from utils.gemini_runtime import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    build_generate_content_config,
    build_interaction_generation_config,
    build_interaction_tools,
    call_interaction,
    extract_text_from_generate_response,
    extract_text_from_interaction,
    get_client,
    get_evidence_model,
    get_fallback_model,
    is_daily_model_quota_exhausted,
    is_quota_error,
)
from pipeline.types import EVIDENCE_VALIDATION_OUTPUT_SCHEMA

# Constants
MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

class EvidenceValidatorError(RuntimeError):
    """Raised when evidence validation fails."""
    pass


class DailyQuotaExceededError(EvidenceValidatorError):
    """Raised when per-model daily quota is exhausted."""
    pass


def load_json_file(json_path: Path) -> Dict[str, Any]:
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise EvidenceValidatorError(f"Failed to load JSON: {e}")


def save_json_file(data: Dict[str, Any], output_path: Path) -> None:
    try:
        output_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )
        print(f"[OK]Saved validated output to: {output_path}")
    except Exception as e:
        raise EvidenceValidatorError(f"Failed to save JSON: {e}")


def extract_json_from_response(text: str) -> Dict[str, Any]:
    """Extract JSON from model response, handling markdown fences."""
    cleaned = text.strip()
    if not cleaned:
        raise EvidenceValidatorError("Model returned empty response (no text output)")
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Try fuzzy extraction
        start = cleaned.find('{')
        end = cleaned.rfind('}') + 1
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except Exception:
                pass
        # Try repair_truncated_json as last resort
        try:
            from utils.json_helpers import repair_truncated_json
            repaired = repair_truncated_json(cleaned[start:] if start >= 0 else cleaned)
            result = json.loads(repaired)
            print(f"[INFO] Repaired truncated JSON in evidence validation response", flush=True)
            return result
        except Exception:
            pass
        raise EvidenceValidatorError(f"Failed to parse JSON: {e}")


def _is_retryable_quota_error(exc: BaseException) -> bool:
    """Return True for transient 429s, False for daily quota exhaustion."""
    if isinstance(exc, DailyQuotaExceededError):
        return False
    return is_quota_error(exc) or isinstance(exc, EvidenceValidatorError) and "quota" in str(exc).lower()


@retry(
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_is_retryable_quota_error),
    reraise=True,
)
def call_gemini_validation(
    prompt: str,
    api_key: str,
    verbose: bool = False,
    request_metrics: Optional[Dict[str, int]] = None,
) -> str:
    """Call Gemini with Google Search for rigorous validation."""
    model_id = get_evidence_model()
    client = get_client(api_key)

    # Configuration: High reasoning, Search enabled
    # NOTE: Do NOT combine use_google_search with response_mime_type="application/json"
    # — Google Search tool-use mode prematurely terminates structured JSON output on
    #   Gemini 3 Flash, causing truncation. Let the prompt handle JSON formatting.
    config = build_generate_content_config(
        thinking_level="medium",
        temperature=0.5,
        use_google_search=True,
        max_output_tokens=65536,
        include_thoughts=False,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    if verbose:
        print(f"\n--- Calling {model_id} for Validation ---")

    try:
        if request_metrics is not None:
            if "2.5" in model_id:
                request_metrics["evidence_calls_2_5pro"] = int(request_metrics.get("evidence_calls_2_5pro", 0)) + 1
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=config
        )
        return extract_text_from_generate_response(response)
    except Exception as e:
        if is_daily_model_quota_exhausted(e):
            # Try fallback model before giving up
            fallback = get_fallback_model("evidence")
            if fallback and fallback != model_id:
                print(f"  [FALLBACK] {model_id} quota exhausted, retrying with {fallback}")
                try:
                    response = client.models.generate_content(
                        model=fallback, contents=prompt, config=config,
                    )
                    return extract_text_from_generate_response(response)
                except Exception as fallback_exc:
                    print(f"  [FALLBACK FAILED] {fallback} also failed: {fallback_exc}")
            raise DailyQuotaExceededError(
                f"Daily quota exhausted for {model_id}: {e}"
            ) from e
        if is_quota_error(e):
            raise EvidenceValidatorError(f"Validation quota/transient error on {model_id}: {e}") from e
        raise EvidenceValidatorError(f"Validation failed on {model_id}: {e}") from e


def call_gemini_validation_chained(
    prompt: str,
    api_key: str,
    previous_interaction_id: Optional[str] = None,
    verbose: bool = False,
    request_metrics: Optional[Dict[str, int]] = None,
) -> tuple:
    """Call Gemini via Interactions API for chained validation.

    Returns (response_text, interaction_id) for chaining across batches.
    """
    model_id = get_evidence_model()

    sys_instruction = (
        "You are a RIGOROUS SCIENTIFIC ADVERSARY and FACT-CHECKER. "
        "Use Google Search to verify every protein interaction claim against primary literature. "
        "If you validated proteins in previous turns, maintain consistency with those findings. "
        "Cross-reference mechanisms you've already confirmed."
    )

    gen_config = build_interaction_generation_config(
        thinking_level="medium",
        temperature=0.5,
        max_output_tokens=65536,
    )

    tools = build_interaction_tools(use_google_search=True)

    if verbose:
        print(f"\n--- Calling {model_id} via Interactions API (chained) ---")

    try:
        if request_metrics is not None:
            request_metrics["evidence_interaction_calls"] = int(
                request_metrics.get("evidence_interaction_calls", 0)
            ) + 1

        interaction = call_interaction(
            input_text=prompt,
            model=model_id,
            system_instruction=sys_instruction,
            generation_config=gen_config,
            tools=tools,
            response_format=EVIDENCE_VALIDATION_OUTPUT_SCHEMA,
            store=True,
            previous_interaction_id=previous_interaction_id,
            api_key=api_key,
        )

        text = extract_text_from_interaction(interaction)
        interaction_id = getattr(interaction, "id", None) or getattr(interaction, "name", None)
        return text, interaction_id

    except Exception as e:
        if is_daily_model_quota_exhausted(e):
            raise DailyQuotaExceededError(
                f"Daily quota exhausted for {model_id}: {e}"
            ) from e
        if is_quota_error(e):
            raise EvidenceValidatorError(f"Validation quota/transient error on {model_id}: {e}") from e
        raise EvidenceValidatorError(f"Interaction validation failed on {model_id}: {e}") from e


def _merge_validated_interactor(orig: dict, val_int: dict) -> None:
    """Merge validation results into original interactor without losing pipeline data.

    Instead of orig.update(val_int) which overwrites the entire functions list,
    this selectively copies validation metadata and deep-merges functions by name.
    """
    # 1. Copy validation-only metadata fields
    VALIDATION_FIELDS = {
        "is_valid", "mechanism_correction", "_validation_status",
    }
    for key in VALIDATION_FIELDS:
        if key in val_int:
            orig[key] = val_int[key]

    # 2. Deep-merge functions by name
    if "functions" in val_int and "functions" in orig:
        orig_funcs_by_name: Dict[str, Dict[str, Any]] = {}
        for f in orig["functions"]:
            key = f.get("function", "").strip().lower()
            if key:
                orig_funcs_by_name[key] = f

        for vf in val_int["functions"]:
            vf_name = vf.get("function", "").strip().lower()
            if vf_name and vf_name in orig_funcs_by_name:
                # Enrich existing function — add evidence, correct arrow/process if changed
                of = orig_funcs_by_name[vf_name]
                ENRICH_KEYS = {
                    "evidence", "arrow", "cellular_process",
                    "biological_consequence", "specific_effects",
                    "effect_description",
                }
                for k in ENRICH_KEYS:
                    if k in vf:
                        of[k] = vf[k]
            else:
                # Fuzzy match before appending: check if semantically equivalent
                # function already exists under a different name
                vf_words = set(vf_name.split()) if vf_name else set()
                is_fuzzy_dup = False
                if vf_words:
                    for existing_name, existing_fn in orig_funcs_by_name.items():
                        existing_words = set(existing_name.split())
                        if not existing_words:
                            continue
                        overlap = len(vf_words & existing_words) / min(len(vf_words), len(existing_words))
                        if overlap >= 0.6:
                            # Enrich the existing function instead of appending
                            for k in ("evidence", "arrow", "cellular_process",
                                      "biological_consequence", "specific_effects",
                                      "effect_description"):
                                if k in vf and not existing_fn.get(k):
                                    existing_fn[k] = vf[k]
                            is_fuzzy_dup = True
                            break
                if not is_fuzzy_dup:
                    orig["functions"].append(vf)
    elif "functions" in val_int and "functions" not in orig:
        # No original functions — use validated ones directly
        orig["functions"] = val_int["functions"]
    # If val_int has no functions key, original functions are preserved as-is


def create_validation_prompt(
    main_protein: str,
    interactors: List[Dict[str, Any]],
    batch_start: int,
    batch_end: int,
    total: int
) -> str:
    """
    Constructs a rigorous "Scientific Adversary" prompt.
    """
    
    items_str = json.dumps(interactors, separators=(",", ":"))
    
    return f"""
You are a RIGOROUS SCIENTIFIC ADVERSARY and FACT-CHECKER.
Your task is to validate protein interaction claims between {main_protein} and a list of interactors.
You must use Google Search to verify every claim against primary literature.

**CORE OBJECTIVE:**
Detect and FIX "Mechanistic Opposites" and "Contextual Errors".
A common error is conflating **Transcriptional Repression** with **Protein Instability**, or **Activator** with **Repressor**.

**CRITICAL FAILURE EXAMPLES (DO NOT COMMIT THESE):**
1. **The "ATXN3-PTEN" Fallacy:**
   - *Input Claim:* ATXN3 deubiquitinates and STABILIZES PTEN protein (Activates).
   - *Reality:* ATXN3 transcriptionally REPRESSES the PTEN gene (Inhibits).
   - *Verdict:* WRONG MECHANISM. The effect is INHIBITORY (lowers PTEN levels), not ACTIVATING.
   
2. **The "Transcriptional vs Post-Translational" Confusion:**
   - *Input:* Protein A degrades Protein B.
   - *Reality:* Protein A represses Protein B's mRNA.
   - *Verdict:* The OUTCOME (lower Protein B) is the same, but the MECHANISM is different. You must be precise.

**INSTRUCTIONS:**

1. **INDEPENDENT RESEARCH:** For each interactor, search for the interaction mechanism *from scratch*. Do not blindly trust the input.
   - Search queries like: "{main_protein} {interactors[0]['primary']} interaction mechanism", "{main_protein} regulates {interactors[0]['primary']} transcription or stability".

2. **BIOLOGICAL CASCADE (MUST BE DETAILED):**
   - **REQUIREMENT:** Create detailed, multi-step molecular pathways.
   - **FORMAT:** "Event A (upstream) → Molecular Intermediate B → Downstream Effector C → Cellular Consequence D".
   - **DETAIL:** Include specific phosphorylation sites (e.g. Ser473), domains (e.g. SH2), co-factors, and cellular locations (e.g. Nuclear translocation).
   - **EXAMPLE:** "ATXN3 binds VCP → Deubiquitinates K48-linked chains on substrates → Prevents proteasomal degradation → Stabilizes protein X → Induces Autophagy."
   - **BAN:** Do NOT use vague single-step descriptions like "ATXN3 regulates VCP".

3. **SPECIFIC EFFECTS (MOLECULAR PRECISION):**
   - **REQUIREMENT:** Describe the EXACT molecular change.
   - **DETAIL:** Use precise terms: "Increases binding affinity by 2-fold", "Promotes nuclear translocation", "Inhibits enzymatic activity at site X", "Stabilizes protein half-life".
   - **AVOID:** Generic terms like "Regulates", "Affects", "Modulates", "Controls" without specific qualification.

4. **EVIDENCE & PUBLICATIONS (VERBATIM PROOF):**
   - **REQUIREMENT:** Evidence must be IRREFUTABLE and VERIFIABLE.
   - **FIELDS:** You MUST provide the **EXACT paper title**, **Journal**, **Year**.
   - **QUOTE:** You MUST include a **VERBATIM QUOTE** from the paper's abstract or results that proves the specific mechanism.
   - **RULE:** If you cannot find a specific paper supporting the mechanism, mark the claim as INVALID or CORRECT it to what the literature actually says.

**INPUT DATA (Batch {batch_start+1}-{batch_end} of {total}):**
{items_str}

**OUTPUT SCHEMA (JSON):**
{{
  "interactors": [
    {{
      "primary": "ProteinSymbol",
      "is_valid": true, // Set false if NO interaction exists
      "mechanism_correction": "Corrected detailed mechanism...", // Explain the REAL mechanism if input was wrong
      "functions": [
        {{
            "function": "Specific Function Name", // Corrected if necessary
            "arrow": "activates" | "inhibits" | "binds" | "regulates", // CRITICAL: Verify direction!
            "cellular_process": "Detailed biological explanation...",
            "effect_description": "Outcome of the interaction...",
            "biological_consequence": [ "Step 1 -> Step 2 -> Step 3 (Detailed Pathway)" ],
            "specific_effects": [ "Precise molecular effect 1", "Precise molecular effect 2" ],
            "evidence": [
                {{
                    "paper_title": "EXACT Title from PubMed",
                    "journal": "Journal Name",
                    "year": 2024,
                    "relevant_quote": "Verbatim quote supporting the mechanism."
                }}
            ]
        }}
      ]
    }}
  ]
}}
"""


def validate_evidence_parallel(
    main_protein: str,
    interactors: List[Dict[str, Any]],
    api_key: str,
    batch_size: int = int(os.getenv("VALIDATION_BATCH_SIZE", "4")),
    verbose: bool = False,
    request_metrics: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Validate all interactors in sequential batches with delay between calls.

    Uses tenacity retry on transient 429s instead of parallel fire-all-at-once.
    """
    if not interactors:
        return []

    batch_delay = float(os.getenv("VALIDATION_BATCH_DELAY", "0.5"))

    # Split into batches
    batches = [interactors[i:i + batch_size] for i in range(0, len(interactors), batch_size)]
    total = len(interactors)
    quota_exhausted = False

    # Try Interactions API with chaining for cumulative context across batches
    use_interactions_api = os.getenv("EVIDENCE_USE_INTERACTIONS", "0") == "1"
    # When chaining is off, batches are independent and can run in parallel
    parallel_validation = not use_interactions_api
    validation_workers = len(batches) if parallel_validation else 1

    print(
        f"[INFO] Validating {total} interactors in {len(batches)} batches "
        f"(batch_size={batch_size}, delay={batch_delay}s, "
        f"mode={'parallel' if parallel_validation else 'chained'}, "
        f"workers={validation_workers})"
    )

    results = []
    previous_interaction_id: Optional[str] = None
    validated_names: List[str] = []

    def _validate_single_batch(batch_idx, batch, prev_id=None, prev_names=None):
        """Process one batch. Returns result dict."""
        batch_start = batch_idx * batch_size
        batch_end = min(batch_start + len(batch), total)

        prompt = create_validation_prompt(
            main_protein, batch, batch_start, batch_end, total
        )

        # Add consistency hint for chained batches
        if prev_names and use_interactions_api:
            prompt += (
                f"\n\nCONSISTENCY NOTE: You have already validated these interactors "
                f"in previous turns: {', '.join(prev_names)}. Ensure your findings "
                f"for this batch are mechanistically consistent with prior validated results."
            )

        interaction_id = None
        if use_interactions_api and (prev_id is not None or batch_idx == 0):
            try:
                response, interaction_id = call_gemini_validation_chained(
                    prompt, api_key,
                    previous_interaction_id=prev_id,
                    verbose=verbose,
                    request_metrics=request_metrics,
                )
            except Exception as chain_err:
                print(f"  [FALLBACK] Interactions API failed ({chain_err}), using generate_content")
                response = call_gemini_validation(prompt, api_key, verbose, request_metrics=request_metrics)
        else:
            response = call_gemini_validation(prompt, api_key, verbose, request_metrics=request_metrics)

        if not response.strip():
            time.sleep(2)
            print(f"  [RETRY] Batch {batch_idx + 1}: empty response, retrying...", flush=True)
            response = call_gemini_validation(prompt, api_key, verbose, request_metrics=request_metrics)
        result = extract_json_from_response(response)

        validated = []
        if 'interactors' in result:
            original_names = {(x.get('primary') or '').upper() for x in batch}
            for val_int in result['interactors']:
                val_name = (val_int.get('primary') or '').upper()
                if val_name not in original_names:
                    continue
                orig = next((x for x in batch if (x.get('primary') or '').upper() == val_name), None)
                if orig:
                    if not val_int.get('is_valid', True):
                        print(f"  ❌ {val_int['primary']} flagged as INVALID interaction.")
                        orig['_validation_status'] = 'rejected'
                        orig['mechanism'] = "EVIDENCE REJECTED: " + val_int.get('mechanism_correction', 'No interaction found')
                    else:
                        print(f"  ✅ {val_int['primary']} validated.")
                        _merge_validated_interactor(orig, val_int)
                    validated.append(orig)
        else:
            validated = batch

        batch_validated_names = [
            v['primary'] for v in validated
            if v.get('_validation_status') != 'rejected'
        ]

        return {
            'batch_idx': batch_idx,
            'interactors': validated,
            'error': None,
            'interaction_id': interaction_id,
            'validated_names': batch_validated_names,
        }

    # ── Parallel path: independent batches via ThreadPoolExecutor ──
    if parallel_validation:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = [None] * len(batches)
        with ThreadPoolExecutor(max_workers=validation_workers) as executor:
            futures = {
                executor.submit(_validate_single_batch, bi, batch): bi
                for bi, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                bi = futures[future]
                try:
                    results[bi] = future.result()
                except DailyQuotaExceededError as e:
                    print(f"[WARN] Batch {bi + 1} halted: {e}")
                    results[bi] = {
                        'batch_idx': bi, 'interactors': batches[bi],
                        'error': str(e), 'quota_exhausted': True,
                        'skipped_due_to_quota': True,
                    }
                except Exception as e:
                    print(f"[WARN] Batch {bi + 1} failed: {e}")
                    results[bi] = {
                        'batch_idx': bi, 'interactors': batches[bi], 'error': str(e),
                    }
        results = [r for r in results if r is not None]

    # ── Sequential path: chained Interactions API ──
    else:
        for batch_idx, batch in enumerate(batches):
            if quota_exhausted:
                if request_metrics is not None:
                    request_metrics["quota_skipped_calls"] = int(request_metrics.get("quota_skipped_calls", 0)) + 1
                results.append({
                    'batch_idx': batch_idx, 'interactors': batch,
                    'error': "quota_exhausted", 'skipped_due_to_quota': True,
                })
                continue

            try:
                r = _validate_single_batch(
                    batch_idx, batch,
                    prev_id=previous_interaction_id,
                    prev_names=validated_names,
                )
                previous_interaction_id = r.get('interaction_id')
                validated_names.extend(r.get('validated_names', []))
                results.append(r)
            except DailyQuotaExceededError as e:
                previous_interaction_id = None
                quota_exhausted = True
                print(f"[WARN] Batch {batch_idx + 1} halted: {e}")
                results.append({
                    'batch_idx': batch_idx, 'interactors': batch,
                    'error': str(e), 'quota_exhausted': True,
                    'skipped_due_to_quota': True,
                })
            except Exception as e:
                previous_interaction_id = None
                print(f"[WARN] Batch {batch_idx + 1} failed: {e}")
                results.append({
                    'batch_idx': batch_idx, 'interactors': batch, 'error': str(e),
                })

            if batch_idx < len(batches) - 1 and not quota_exhausted:
                time.sleep(batch_delay)

    validated = []
    errors = 0
    quota_errors = 0
    skipped_batches = 0
    for r in results:
        if r.get('error'):
            errors += 1
        if r.get("quota_exhausted"):
            quota_errors += 1
        if r.get("skipped_due_to_quota"):
            skipped_batches += 1
        validated.extend(r.get('interactors', []))

    print(
        f"[INFO] Validation complete. {len(validated)} interactors processed, "
        f"{errors} batch errors, quota_errors={quota_errors}, skipped_batches={skipped_batches}"
    )

    return validated


def validate_and_enrich_evidence(
    json_data: Dict[str, Any],
    api_key: str,
    verbose: bool = False,
    batch_size: int = int(os.getenv("VALIDATION_BATCH_SIZE", "4")),
    step_logger = None
) -> Dict[str, Any]:
    """
    Main validation function.
    """
    if 'ctx_json' not in json_data:
        print("[WARN] No ctx_json found, skipping validation.")
        return json_data

    main_protein = json_data['ctx_json'].get('main', 'Unknown')
    interactors = json_data['ctx_json'].get('interactors', [])
    
    request_metrics: Dict[str, int] = {
        "evidence_calls_2_5pro": 0,
        "quota_skipped_calls": 0,
    }
    model_id = get_evidence_model()

    print(f"\n{'='*60}")
    print(f"🔍 RIGOROUS EVIDENCE VALIDATION FOR: {main_protein}")
    print(f"   Model: {model_id} (Scientific Adversary Mode)")
    print(f"   Total interactors: {len(interactors)}")
    print(f"{'='*60}")

    # Use parallel validation
    validated_interactors = validate_evidence_parallel(
        main_protein,
        interactors,
        api_key,
        batch_size,
        verbose,
        request_metrics=request_metrics,
    )

    # Update payload
    json_data['ctx_json']['interactors'] = validated_interactors
    
    # Also update snapshot if present — use independent copy to avoid shared mutation
    if 'snapshot_json' in json_data:
        json_data['snapshot_json']['interactors'] = deepcopy(validated_interactors)

    existing_metrics = json_data.get("_request_metrics", {}) if isinstance(json_data, dict) else {}
    if not isinstance(existing_metrics, dict):
        existing_metrics = {}
    existing_metrics["evidence_calls_2_5pro"] = int(existing_metrics.get("evidence_calls_2_5pro", 0)) + int(
        request_metrics.get("evidence_calls_2_5pro", 0)
    )
    existing_metrics["quota_skipped_calls"] = int(existing_metrics.get("quota_skipped_calls", 0)) + int(
        request_metrics.get("quota_skipped_calls", 0)
    )
    json_data["_request_metrics"] = existing_metrics

    print(
        "[EVIDENCE METRICS] "
        f"model={model_id}, "
        f"evidence_calls_2_5pro={request_metrics.get('evidence_calls_2_5pro', 0)}, "
        f"quota_skipped_calls={request_metrics.get('quota_skipped_calls', 0)}"
    )

    return json_data


if __name__ == "__main__":
    # CLI testing
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json")
    parser.add_argument("--output", default="validated_output.json")
    parser.add_argument("--api-key", default=os.getenv("GOOGLE_CLOUD_PROJECT"))
    args = parser.parse_args()

    if not args.api_key:
        sys.exit("GOOGLE_CLOUD_PROJECT required.")
        
    data = load_json_file(Path(args.input_json))
    validated = validate_and_enrich_evidence(data, args.api_key, verbose=True)
    save_json_file(validated, Path(args.output))
