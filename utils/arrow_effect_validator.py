"""
Arrow & Effect Validator
========================

Validates arrow notation, interaction directions, and effects for protein-protein interactions
using Gemini 3 Pro with thinking mode and Google Search.

Ensures:
- Correct arrow types (activates/inhibits/binds/regulates)
- Correct interaction directions (main_to_primary/primary_to_main)
- Correct interaction_effect alignment with arrow
- No double-negative issues (e.g., "inhibits Apoptosis Inhibition" → "activates Apoptosis Inhibition")
- Logical biological consequence chains

Processes 3-4 interactions in parallel for efficiency.
"""

import os
import sys
import json
import random
import re
import threading
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

# Check if Gemini is available
try:
    from google import genai as google_genai
    from google.genai import types
    from utils.gemini_runtime import (
        DEFAULT_MAX_OUTPUT_TOKENS,
        build_generate_content_config,
        extract_retry_after_seconds,
        extract_text_from_generate_response,
        get_arrow_model,
        get_client,
        get_fallback_model,
        is_daily_model_quota_exhausted,
        is_quota_error,
        is_transient_network_error,
    )
    from pipeline.types import ARROW_VALIDATION_OUTPUT_SCHEMA
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    print("[WARNING] google-genai not installed. Arrow validation will be skipped.")

from utils.interaction_contract import normalize_arrow, semantic_claim_direction


# Constants
MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS
# P1: Cap concurrent Gemini validation calls. Previously 999 (effectively
# uncapped) — with thinking-high + Search + 64K output per call, that
# burst would trip Gemini rate limits long before it helped throughput.
# 5 is tuned against Vertex AI's per-minute RPM quota for the arrow
# model: a 30-interactor query becomes 6 bursts of 5, each burst sized
# so latency (~3-5s per call) spaces calls well under typical 300+ RPM
# limits. A burst of 20 consistently tripped 429 RESOURCE_EXHAUSTED on
# TDP43-class queries, skipping every validation. Override via
# VALIDATION_MAX_WORKERS if you've confirmed your quota allows it.
DEFAULT_MAX_WORKERS = 5

# Transient 429 retry window. Daily quota exhaustion skips the retry path
# entirely (see is_daily_model_quota_exhausted). Per-minute 429s get
# exponential-with-jitter backoff (5, 10, 20, 40, 80, cap 120) and
# honour the server's Retry-After header when present — the same
# semantics as the main runner dispatcher.
TRANSIENT_RETRY_MAX = 6
TRANSIENT_RETRY_BASE_SLEEP = 5.0
TRANSIENT_RETRY_MAX_SLEEP = 120.0

# P1: Direct-mediator-link extraction (Tier 2) spawns a full nested
# ``run_pipeline_for_protein`` per indirect pair. For chain-heavy
# queries this is the single most expensive thing the pipeline does.
# Budget the number of Tier 2 spawns per invocation; pairs beyond the
# budget skip straight to Tier 3 (cheap evidence-only extraction from
# the existing chain data). Override via ARROW_TIER2_BUDGET.
DEFAULT_TIER2_BUDGET = 20
# Cap concurrent nested-pipeline spawns inside one Tier 2 batch so we
# don't fork 20 full pipelines simultaneously. Override via
# ARROW_TIER2_MAX_WORKERS.
DEFAULT_TIER2_MAX_WORKERS = 5

# Valid values reference
VALID_DIRECTIONS = ["main_to_primary", "primary_to_main"]
VALID_ARROWS = ["activates", "inhibits", "binds", "regulates"]
VALID_INTERACTION_TYPES = ["direct", "indirect"]

# Near-miss arrow normalization: common LLM hallucinations → valid values
_ARROW_NORMALIZE: Dict[str, str] = {
    "promotes": "activates",
    "enhances": "activates",
    "stimulates": "activates",
    "upregulates": "activates",
    "increases": "activates",
    "suppresses": "inhibits",
    "represses": "inhibits",
    "blocks": "inhibits",
    "downregulates": "inhibits",
    "decreases": "inhibits",
    "modulates": "regulates",
    "affects": "regulates",
    "interacts": "binds",
    "associates": "binds",
    "complex": "binds",
    "complex formation": "binds",
}

_DIRECTION_NORMALIZE: Dict[str, str] = {
    "forward": "main_to_primary",
    "reverse": "primary_to_main",
    "both": "main_to_primary",
    "mutual": "main_to_primary",
    "bidirectional": "main_to_primary",
}


def _interactor_key(interactor: Dict[str, Any]) -> str:
    return str((interactor or {}).get("primary") or "").strip().upper()


def _merge_arrow_validation_into_ctx(
    payload: Dict[str, Any],
    corrected_interactors: List[Dict[str, Any]],
) -> None:
    """Mirror arrow-validator corrections into ctx_json without flattening it."""
    ctx = payload.get("ctx_json")
    if not isinstance(ctx, dict):
        return
    ctx_interactors = ctx.get("interactors")
    if not isinstance(ctx_interactors, list):
        return

    by_key = {
        _interactor_key(item): item
        for item in ctx_interactors
        if isinstance(item, dict) and _interactor_key(item)
    }

    scalar_fields = (
        "arrow",
        "arrows",
        "direction",
        "intent",
        "interaction_effect",
        "function_context",
        "confidence",
        "_validation_metadata",
        "_validation_status",
        "_validation_skipped_reason",
    )
    function_fields = (
        "arrow",
        "interaction_effect",
        "direction",
        "interaction_direction",
        "likely_direction",
        "function_context",
        "_validation_metadata",
        "_validation_status",
    )

    for corrected in corrected_interactors or []:
        if not isinstance(corrected, dict):
            continue
        key = _interactor_key(corrected)
        if not key:
            continue
        target = by_key.get(key)
        if target is None:
            ctx_interactors.append(deepcopy(corrected))
            by_key[key] = ctx_interactors[-1]
            continue

        for field in scalar_fields:
            if field in corrected:
                target[field] = deepcopy(corrected[field])

        target_funcs = target.get("functions") or []
        corrected_funcs = corrected.get("functions") or []
        if not isinstance(target_funcs, list) or not isinstance(corrected_funcs, list):
            continue
        target_by_name = {
            str((fn or {}).get("function") or "").strip().lower(): fn
            for fn in target_funcs
            if isinstance(fn, dict) and (fn.get("function") or "").strip()
        }
        for corrected_fn in corrected_funcs:
            if not isinstance(corrected_fn, dict):
                continue
            fn_key = str(corrected_fn.get("function") or "").strip().lower()
            target_fn = target_by_name.get(fn_key)
            if target_fn is None:
                continue
            for field in function_fields:
                if field in corrected_fn:
                    target_fn[field] = deepcopy(corrected_fn[field])


def _normalize_enum(value: str, valid_set: list, normalize_map: dict, field_name: str) -> str:
    """Normalize an LLM-returned enum value, mapping near-misses to valid values."""
    if not value or not isinstance(value, str):
        return value
    lowered = value.strip().lower()
    if lowered in valid_set:
        return lowered
    if lowered in normalize_map:
        mapped = normalize_map[lowered]
        print(f"  [NORMALIZE] {field_name}: '{value}' → '{mapped}'")
        return mapped
    # Salvage pass: Gemini occasionally leaks JSON syntax or non-ASCII
    # runes into enum fields (e.g. ``"binds欢},"``). Extract the leading
    # ASCII-letter run and retry the valid-set / normalize-map lookup
    # before giving up and coercing to a vague default. This recovers
    # the real intent of the model on malformed responses without
    # silently flattening a real "binds" into "regulates".
    import re as _re
    _prefix_match = _re.match(r"[a-z_]+", lowered)
    if _prefix_match:
        prefix = _prefix_match.group(0)
        if prefix and prefix != lowered:
            if prefix in valid_set:
                print(f"  [NORMALIZE] {field_name}: salvaged '{value}' → '{prefix}'")
                return prefix
            if prefix in normalize_map:
                mapped = normalize_map[prefix]
                print(f"  [NORMALIZE] {field_name}: salvaged '{value}' → '{mapped}'")
                return mapped
    # S1: for direction fields, default to main_to_primary instead of
    # keeping invalid values (which used to leak 'bidirectional' through).
    if "arrow" in field_name:
        print(f"  [WARN] {field_name}: invalid value '{value}', defaulting to 'regulates'")
        return "regulates"
    elif field_name == "direction":
        print(f"  [NORMALIZE] {field_name}: invalid value '{value}' → 'main_to_primary'")
        return "main_to_primary"
    else:
        print(f"  [WARN] {field_name}: invalid value '{value}', keeping as-is")
        return value


def _classify_interaction_complexity(interactor: dict) -> str:
    """Return 'simple' or 'complex' based on interaction structure."""
    if interactor.get("interaction_type") == "indirect" and interactor.get("mediator_chain"):
        return "complex"
    if len(interactor.get("functions", [])) > 4:
        return "complex"
    return "simple"


class DailyQuotaExceededError(RuntimeError):
    """Raised when the model's daily quota is exhausted."""


def _resolve_worker_count(total_interactors: int) -> int:
    configured = os.getenv("VALIDATION_MAX_WORKERS", str(DEFAULT_MAX_WORKERS))
    try:
        configured_int = int(configured)
    except ValueError:
        configured_int = DEFAULT_MAX_WORKERS
    configured_int = max(1, configured_int)
    return max(1, min(configured_int, total_interactors))


def _mark_validation_skipped(interactor: Dict[str, Any], reason: str) -> Dict[str, Any]:
    marked = dict(interactor)
    marked["_validation_skipped_reason"] = reason
    marked.setdefault("_validation_metadata", {})
    if isinstance(marked["_validation_metadata"], dict):
        marked["_validation_metadata"]["validated"] = False
        marked["_validation_metadata"]["validator"] = "arrow_effect_validator"
        marked["_validation_metadata"]["skipped"] = True
        marked["_validation_metadata"]["skip_reason"] = reason
    return marked


def validate_arrows_and_effects(
    payload: Dict[str, Any],
    api_key: str,
    verbose: bool = False,
    skip_indices: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Main entry point: validates all interactions in the payload.

    Args:
        payload: Full pipeline payload with snapshot_json and ctx_json
        api_key: Google AI API key
        verbose: Enable detailed logging
        skip_indices: Optional set of snapshot-interactor indices that have
            already been validated upstream (e.g. via DB Tier-1 short-circuit).
            Skipped interactors pass through unchanged with no LLM call.

    Returns:
        Updated payload with corrected arrows/directions/effects
    """
    if not GEMINI_AVAILABLE:
        if verbose:
            print("[SKIP] Arrow validation disabled (Gemini not available)")
        return payload

    if not api_key:
        if verbose:
            print("[SKIP] Arrow validation disabled (no API key)")
        return payload

    # Extract interactors from snapshot_json
    snapshot = payload.get("snapshot_json", payload)
    main_protein = snapshot.get("main", "UNKNOWN")
    interactors = snapshot.get("interactors", [])

    if not interactors:
        if verbose:
            print("[SKIP] No interactors to validate")
        return payload

    if verbose:
        print(f"\n{'='*60}")
        print(f"ARROW VALIDATION: {main_protein} ({len(interactors)} interactions)")
        print(f"{'='*60}")

    model_id = get_arrow_model()
    request_metrics = {
        # Total arrow LLM calls regardless of model — was previously
        # arrow_calls_2_5pro which only incremented on "2.5" model names,
        # making gemini-3-flash-preview calls invisible.
        "arrow_llm_calls": 0,
        # Calls that hit the 2.5-pro fallback path (kept separate from the
        # primary call counter so cost/quality regressions are visible).
        "arrow_fallback_to_pro": 0,
        # Pairs whose validated arrow came from the DB Tier-1 short-circuit
        # instead of an LLM call. Surfaces the speed win.
        "arrow_tier1_hits": 0,
        # Back-compat alias: equals arrow_fallback_to_pro. Kept so older
        # consumers reading "arrow_calls_2_5pro" don't break.
        "arrow_calls_2_5pro": 0,
        "quota_skipped_calls": 0,
    }
    quota_event = threading.Event()

    # Determine worker count (bounded by env + interactor count)
    worker_count = _resolve_worker_count(len(interactors))

    # Validate interactions with bounded in-flight scheduling.
    skip_set: set = set(skip_indices) if skip_indices else set()
    corrected_interactors = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results: List[Optional[Dict[str, Any]]] = [None] * len(interactors)
        future_to_idx = {}
        next_idx = 0
        quota_unscheduled_marked = False

        def _submit_one(index: int) -> None:
            # Tier-1 DB short-circuit hit — interactor already has a
            # validated arrow from chain resolution. Pass through and
            # don't burn an LLM call.
            if index in skip_set:
                results[index] = interactors[index]
                return
            future = executor.submit(
                validate_single_interaction,
                interactors[index],
                main_protein,
                api_key,
                verbose,
                model_id=model_id,
                quota_event=quota_event,
                request_metrics=request_metrics,
            )
            future_to_idx[future] = index

        # Advance through interactors until the in-flight pool is full of
        # actual LLM calls. Skip indices populate `results` directly, so
        # the cap counts only non-skipped work.
        while next_idx < len(interactors) and len(future_to_idx) < worker_count:
            _submit_one(next_idx)
            next_idx += 1

        while future_to_idx:
            done_futures, _ = wait(set(future_to_idx.keys()), return_when=FIRST_COMPLETED)
            for future in done_futures:
                idx = future_to_idx.pop(future)
                try:
                    corrected = future.result()
                    results[idx] = corrected
                    if verbose and corrected:
                        partner = corrected.get("primary", "UNKNOWN")
                        print(f"  ✓ Validated {main_protein} ↔ {partner}")
                except DailyQuotaExceededError as exc:
                    quota_event.set()
                    partner = interactors[idx].get("primary", "UNKNOWN")
                    print(f"  [WARN]Daily quota exhausted while validating {main_protein} ↔ {partner}: {exc}")
                    results[idx] = _mark_validation_skipped(interactors[idx], "quota_exhausted")
                    request_metrics["quota_skipped_calls"] += 1
                except Exception as exc:
                    partner = interactors[idx].get("primary", "UNKNOWN")
                    print(f"  ✗ Error validating {main_protein} ↔ {partner}: {exc}")
                    results[idx] = interactors[idx]  # Keep original on error

            if quota_event.is_set() and not quota_unscheduled_marked:
                # Primary model quota exhausted — dispatch remaining via fallback model
                fallback = get_fallback_model("arrow")
                if fallback and fallback != model_id:
                    print(f"  [FALLBACK] Switching {len(interactors) - next_idx} remaining interactions to {fallback}")
                    fallback_event = threading.Event()
                    for remaining_idx in range(next_idx, len(interactors)):
                        try:
                            results[remaining_idx] = validate_single_interaction(
                                interactors[remaining_idx], main_protein, api_key, verbose,
                                model_id=fallback, quota_event=fallback_event,
                                request_metrics=request_metrics,
                            )
                        except DailyQuotaExceededError:
                            # Fallback model also exhausted — now we truly must skip
                            print(f"  [WARN] Fallback model {fallback} also exhausted, skipping remaining")
                            for skip_idx in range(remaining_idx, len(interactors)):
                                results[skip_idx] = _mark_validation_skipped(interactors[skip_idx], "quota_exhausted")
                                request_metrics["quota_skipped_calls"] += 1
                            break
                        except Exception as exc:
                            partner = interactors[remaining_idx].get("primary", "UNKNOWN")
                            print(f"  [WARN] Fallback failed for {partner}: {exc}")
                            results[remaining_idx] = interactors[remaining_idx]
                else:
                    for remaining_idx in range(next_idx, len(interactors)):
                        results[remaining_idx] = _mark_validation_skipped(interactors[remaining_idx], "quota_exhausted")
                        request_metrics["quota_skipped_calls"] += 1
                quota_unscheduled_marked = True

            if not quota_event.is_set():
                while next_idx < len(interactors) and len(future_to_idx) < worker_count:
                    _submit_one(next_idx)
                    next_idx += 1

        corrected_interactors = []
        for idx, result in enumerate(results):
            if result is None:
                corrected_interactors.append(interactors[idx])
            else:
                corrected_interactors.append(result)

    # Update payload
    snapshot["interactors"] = corrected_interactors
    payload["snapshot_json"] = snapshot
    _merge_arrow_validation_into_ctx(payload, corrected_interactors)
    existing_metrics = payload.get("_request_metrics", {}) if isinstance(payload, dict) else {}
    if not isinstance(existing_metrics, dict):
        existing_metrics = {}
    for _metric_key in (
        "arrow_llm_calls",
        "arrow_fallback_to_pro",
        "arrow_tier1_hits",
        "arrow_calls_2_5pro",
        "quota_skipped_calls",
    ):
        existing_metrics[_metric_key] = int(existing_metrics.get(_metric_key, 0)) + int(
            request_metrics.get(_metric_key, 0)
        )
    payload["_request_metrics"] = existing_metrics

    # Surface validation warnings for unvalidated interactors
    skipped_partners = [
        i.get("primary", "UNKNOWN")
        for i in corrected_interactors
        if i.get("_validation_skipped_reason")
    ]
    if skipped_partners:
        payload.setdefault("_pipeline_metadata", {})["unvalidated_interactors"] = skipped_partners
        payload.setdefault("_pipeline_metadata", {})["validation_incomplete"] = True

    if verbose:
        print(f"{'='*60}")
        print(f"ARROW VALIDATION COMPLETE: {len(corrected_interactors)}/{len(interactors)} validated")
        print(f"{'='*60}\n")
    print(
        "[ARROW METRICS] "
        f"model={model_id}, "
        f"arrow_llm_calls={request_metrics.get('arrow_llm_calls', 0)}, "
        f"arrow_tier1_hits={request_metrics.get('arrow_tier1_hits', 0)}, "
        f"arrow_fallback_to_pro={request_metrics.get('arrow_fallback_to_pro', 0)}, "
        f"quota_skipped_calls={request_metrics.get('quota_skipped_calls', 0)}"
    )

    return payload


def validate_single_interaction(
    interactor: Dict[str, Any],
    main_protein: str,
    api_key: str,
    verbose: bool = False,
    *,
    model_id: Optional[str] = None,
    quota_event: Optional[threading.Event] = None,
    request_metrics: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """
    Validates a single protein-protein interaction using Gemini.

    Args:
        interactor: Interaction data for one partner protein
        main_protein: Query protein symbol
        api_key: Google AI API key
        verbose: Enable detailed logging

    Returns:
        Corrected interactor data
    """
    if quota_event and quota_event.is_set():
        return _mark_validation_skipped(interactor, "quota_exhausted")

    # Tiered model selection based on interaction complexity
    complexity = _classify_interaction_complexity(interactor)
    if complexity == "simple":
        model_name = model_id or os.getenv(
            "GEMINI_MODEL_ARROW_FAST", "gemini-3-flash-preview"
        )
        # Arrow validation on a single pair is a pure structured-correction
        # task — no chain-of-thought needed. Running with thinking disabled
        # cuts per-call latency meaningfully.
        thinking = "off"
    else:
        model_name = model_id or get_arrow_model()
        # Complex interactions (indirect chains with possible inversions)
        # still benefit from a little thinking, but medium was excessive
        # and was also eating into the output-token budget.
        thinking = "low"

    partner = interactor.get("primary", "UNKNOWN")

    try:
        # Build validation prompt
        prompt = build_validation_prompt(interactor, main_protein)

        # Call Gemini with thinking mode + Google Search
        client = get_client(api_key)

        config = build_generate_content_config(
            thinking_level=thinking,
            temperature=0.5,
            use_google_search=(complexity == "complex"),
            # 2026-05-03: bumped 8192/16384 → 12336/24000.
            # The "gemini-3-flash-preview's hard 8192 ceiling" claim was
            # wrong (real Flash 3 cap = 65,536). Per-function arrow output
            # is small (~50 tokens × ~15 functions = ~750 tokens), so even
            # the old 8192/16384 was excessive. The bump is for consistency
            # with the rest of the pipeline now that we know the real cap,
            # and for safety on the complex branch (indirect dual-arrow
            # output for cofactor-rich interactors can exceed 16K with
            # thinking_level=low overhead). max_output_tokens is a CEILING:
            # the model still emits only what the schema requires.
            max_output_tokens=12336 if complexity == "simple" else 24000,
            response_mime_type="application/json",
            response_json_schema=ARROW_VALIDATION_OUTPUT_SCHEMA,
            include_thoughts=False,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        if request_metrics is not None:
            request_metrics["arrow_llm_calls"] = int(request_metrics.get("arrow_llm_calls", 0)) + 1
            if "2.5" in model_name:
                request_metrics["arrow_fallback_to_pro"] = int(request_metrics.get("arrow_fallback_to_pro", 0)) + 1
                # Back-compat alias.
                request_metrics["arrow_calls_2_5pro"] = int(request_metrics.get("arrow_calls_2_5pro", 0)) + 1

        # Transient-429 retry loop around the actual LLM call. Daily
        # quota exhaustion and non-quota errors bypass retries and fall
        # through to the outer except for their normal handling.
        # Shares semantics with runner.call_gemini_model: prefer the
        # server's Retry-After when available, else exponential with
        # jitter and a hard cap. Matches the main dispatcher so a
        # validation pass doesn't give up where 2ax/2az would recover.
        _attempt = 0
        while True:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
                break
            except Exception as _call_exc:
                if is_daily_model_quota_exhausted(_call_exc):
                    raise
                # Treat quota (429) and socket-level transient errors (Errno 54
                # connection reset, broken pipe, read timeout) as the same
                # retryable class. Previously the gate only matched 429, so a
                # single TCP reset would silently drop validation for that
                # partner and the outer handler would return the interactor
                # unvalidated — 12 such drops appeared in one ATXN3 run.
                _is_net = is_transient_network_error(_call_exc)
                _is_quota = is_quota_error(_call_exc)
                if (_is_quota or _is_net) and _attempt < TRANSIENT_RETRY_MAX:
                    _attempt += 1
                    _retry_after = extract_retry_after_seconds(_call_exc)
                    if _retry_after is not None:
                        _sleep_s = float(_retry_after) + random.uniform(0.0, 2.0)
                        _source = " (honoring Retry-After)"
                    else:
                        _sleep_s = min(
                            TRANSIENT_RETRY_MAX_SLEEP,
                            TRANSIENT_RETRY_BASE_SLEEP * (2 ** (_attempt - 1)),
                        ) + random.uniform(0.0, 2.0)
                        _source = ""
                    _label = "transient 429" if _is_quota else f"transient network ({type(_call_exc).__name__})"
                    print(
                        f"  [RETRY {_attempt}/{TRANSIENT_RETRY_MAX}] "
                        f"{main_protein} ↔ {partner}: {_label}, "
                        f"sleeping {_sleep_s:.1f}s before retry{_source}",
                        file=sys.stderr, flush=True,
                    )
                    time.sleep(_sleep_s)
                    continue
                # Non-retryable or retries exhausted — propagate to outer handler
                raise

        # Parse corrections from response
        corrections = parse_gemini_response(response)

        # Apply corrections to interactor
        if corrections:
            interactor = apply_corrections(interactor, corrections, main_protein, verbose)
            if verbose:
                print(f"    → Applied {len(corrections)} correction(s) to {partner}")

        return interactor

    except Exception as e:
        if is_daily_model_quota_exhausted(e):
            # Try fallback model before giving up
            fallback = get_fallback_model("arrow")
            if fallback and fallback != model_name:
                try:
                    print(f"  [FALLBACK] {model_name} quota exhausted for {partner}, retrying with {fallback}")
                    return validate_single_interaction(
                        interactor, main_protein, api_key, verbose,
                        model_id=fallback, quota_event=None,
                        request_metrics=request_metrics,
                    )
                except Exception as fallback_exc:
                    print(f"  [FALLBACK FAILED] {fallback} also failed for {partner}: {fallback_exc}")
            if quota_event is not None:
                quota_event.set()
            raise DailyQuotaExceededError(f"{model_name}: {e}") from e
        if quota_event and quota_event.is_set():
            return _mark_validation_skipped(interactor, "quota_exhausted")
        if is_quota_error(e) or is_transient_network_error(e):
            # Per-minute / transient quota OR socket-level transient (Errno
            # 54 connection reset, broken pipe, timeout) — retries above
            # have already failed. Count the skip so the final
            # [ARROW METRICS] line reports honestly, and mark the interactor
            # so the read-time UI can badge it as "validation incomplete"
            # instead of silently trusting an unvalidated arrow.
            _kind = "quota/transient" if is_quota_error(e) else f"network/{type(e).__name__}"
            print(
                f"[ERROR] Failed to validate {main_protein} ↔ {partner} "
                f"({_kind} after {TRANSIENT_RETRY_MAX} retries): {e}",
                file=sys.stderr, flush=True,
            )
            if request_metrics is not None:
                request_metrics["quota_skipped_calls"] = (
                    int(request_metrics.get("quota_skipped_calls", 0)) + 1
                )
            return _mark_validation_skipped(interactor, "quota_transient")
        # Truly unexpected error — log exception class so future triage can
        # distinguish silent failures from silent successes.
        print(
            f"[ERROR] Failed to validate {main_protein} ↔ {partner} "
            f"({type(e).__name__}): {e}",
            file=sys.stderr, flush=True,
        )
        return interactor  # Return original on error


def build_validation_prompt(interactor: Dict[str, Any], main_protein: str) -> str:
    """
    Builds a detailed validation prompt for Gemini.

    Args:
        interactor: Interaction data
        main_protein: Query protein symbol

    Returns:
        Formatted prompt string
    """
    partner = interactor.get("primary", "UNKNOWN")
    direction = interactor.get("direction", "unknown")
    arrow = interactor.get("arrow", "unknown")
    interaction_type = interactor.get("interaction_type", "direct")

    # Extract chain data for indirect interactions
    upstream_interactor = interactor.get("upstream_interactor")
    mediator_chain = interactor.get("mediator_chain", [])
    depth = interactor.get("depth", 1)
    is_indirect = (interaction_type == "indirect" and mediator_chain)

    # Extract function data
    functions = interactor.get("functions", [])
    function_summary = []
    missing_function_arrows = []  # Track functions with missing arrows

    for idx, func in enumerate(functions):
        func_name = func.get("function", "Unknown")
        func_arrow = func.get("arrow", "")
        func_effect = func.get("interaction_effect", "unknown")
        func_direction = func.get("interaction_direction", "unknown")
        consequences = func.get("biological_consequence", [])

        # Detect missing or empty function arrows (normalize falsy values first)
        _normalized_arrow = str(func_arrow).strip().lower() if func_arrow else ""
        if not _normalized_arrow or _normalized_arrow in ("unknown", "none", "null"):
            missing_function_arrows.append({
                "index": idx,
                "function": func_name,
                "current_arrow": func_arrow,
                "interaction_arrow": arrow
            })
            func_arrow = f"MISSING (falls back to interaction '{arrow}')"

        function_summary.append({
            "name": func_name,
            "arrow": func_arrow,
            "interaction_direction": func_direction,
            "interaction_effect": func_effect,
        })

    # Extract evidence (paper titles only)
    evidence = interactor.get("evidence", [])
    paper_titles = [ev.get("paper_title", "Unknown") for ev in evidence[:3]]  # Limit to 3

    # Build chain context section for indirect interactions
    chain_context = ""
    if is_indirect:
        chain_str = " → ".join([main_protein] + mediator_chain + [partner])
        chain_context = f"""

**⚠️ INDIRECT INTERACTION (via mediator chain):**

This is NOT a direct interaction between {main_protein} and {partner}.
The effect occurs through intermediary protein(s).

**Chain Structure:**
- Full pathway: {chain_str}
- Upstream interactor: {upstream_interactor} (last protein before {partner})
- Chain depth: {depth}
- Mediator chain: {' → '.join(mediator_chain)}

**CRITICAL VALIDATION RULES FOR INDIRECT INTERACTIONS:**

1. **Net Arrow Logic:**
   - The 'arrow' field ({arrow}) represents the NET EFFECT of {main_protein} on {partner}
   - This may DIFFER from the direct effect of {upstream_interactor} on {partner}
   - Example: If {main_protein} activates an inhibitor ({upstream_interactor}),
     the net effect on {partner} is INHIBITION (activating inhibitor = net inhibition)
   - Count inhibitory steps: Even number = net activation, Odd number = net inhibition

2. **Function Arrows (CRITICAL - DUAL ARROWS FOR INDIRECT):**
   For indirect interactions, you MUST provide TWO arrows per function:

   a) **NET ARROW** (arrow field):
      - Describes {main_protein}'s effect on the FUNCTION through the full chain
      - Consider the FULL CHAIN context when validating
      - Example: If {main_protein} activates an inhibitor that inhibits a function,
        net arrow is "inhibits" (activating inhibitor = net inhibition)

   b) **DIRECT ARROW** (direct_arrow field - NEW):
      - Describes {upstream_interactor}'s DIRECT effect on the FUNCTION
      - Independent of the chain - just the immediate mediator's effect
      - Example: If {upstream_interactor} directly inhibits {partner}'s apoptosis,
        direct_arrow is "inhibits"

   **Both arrows must be included in your response for each function!**

3. **Chain Consistency Checks:**
   - Verify that {upstream_interactor} is the correct last step in the chain
   - Check if mediator chain makes biological sense
   - Validate that net arrow matches expected chain logic
   - Validate that direct arrow matches {upstream_interactor} → {partner} relationship

4. **Google Search Strategy:**
   - Search for direct link: "{upstream_interactor} {partner} interaction"
   - Search for net effect: "{main_protein} {partner} pathway"
   - Search for mechanism: "{' '.join(mediator_chain)} {partner} regulation"
   - Verify both effects separately

**Important:**
- Net arrow should reflect the COMBINED effect through ALL steps in the chain
- Direct arrow should reflect ONLY the {upstream_interactor} → {partner} relationship
"""

    # Build prompt
    prompt = f"""You are a molecular biology expert validating protein interaction notation.

**TASK:** Check the following interaction for correctness and logical consistency.

**PROTEINS:**
- Main Protein: {main_protein}
- Partner Protein: {partner}

**CURRENT ANNOTATION:**
- Interaction Direction: {direction}
- Interaction Arrow: {arrow}
- Interaction Type: {interaction_type}
{chain_context}
**FUNCTIONS (with arrows and effects):**
{json.dumps(function_summary, indent=2)}

**SUPPORTING EVIDENCE (paper titles):**
{json.dumps(paper_titles, indent=2)}

---

**VALIDATION REQUIREMENTS:**

1. **Direction Accuracy:**
   - Valid values: 'main_to_primary' | 'primary_to_main' — only these two.
   - Check: Does the direction match the biological mechanism described in functions?
   - For symmetric binding, use "main_to_primary" — query protein is the canonical subject
   - Example: If {main_protein} phosphorylates {partner}, direction should be "main_to_primary"

2. **Interaction Arrow (Protein-Level Effect):**
   - Valid values: "activates" | "inhibits" | "binds" | "regulates"
   - Check: Does the arrow match the predominant effect across functions?
   - Example: If most functions show inhibition, arrow should be "inhibits"
   - This describes the effect on the PARTNER PROTEIN

3. **Function Arrows (CRITICAL - Can Differ from Interaction Arrow):**
   - EVERY function MUST have its own "arrow" field
   - Function arrow describes effect ON THE FUNCTION, not on the protein
   - Function arrows can and should differ from interaction arrows

   **Examples:**
   - Interaction: {main_protein} → {partner} (binds)
   - Function: "Apoptosis Promotion" with arrow: "inhibits"
     → Correct: {main_protein} binds {partner} and thereby inhibits apoptosis promotion

   - Interaction: VCP → ATXN3 (binds)
   - Function: "Promotion of Pathogenic ATXN3 Aggregation" with arrow: "inhibits"
     → Correct: VCP binds ATXN3 but inhibits/prevents the promotion of aggregation

   **Missing Function Arrows:**
   {json.dumps(missing_function_arrows, indent=2) if missing_function_arrows else "None - all functions have arrows"}

4. **Double-Negative Detection (CRITICAL):**
   - Check each function name + arrow combination
   - Examples of double negatives to fix:
     * Function: "Apoptosis Inhibition" + Arrow: "inhibits" → FIX: Arrow should be "activates"
     * Function: "Cell Death Suppression" + Arrow: "inhibits" → FIX: Arrow should be "activates"
     * Function: "Degradation Prevention" + Arrow: "inhibits" → FIX: Arrow should be "activates"
     * Function: "Proliferation" + Arrow: "inhibits" → OK (no double negative)
   - Rule: If function name contains negative terms (inhibition, suppression, repression, prevention, etc.)
     and arrow is "inhibits", change arrow to "activates" (inhibiting an inhibitor = activation)

5. **Function-Level Consistency:**
   - Check: Does `interaction_effect` match `arrow` for each function?
   - Check: Does `interaction_direction` align with the main interaction direction?
   - Check: Are `biological_consequence` chains logically sound (A → B → C)?

6. **Interaction Type:**
   - Valid values: "direct" | "indirect"
   - Check: Does this match the evidence (direct binding vs pathway-mediated)?

---

**INSTRUCTIONS:**

1. **PRIORITY:** Populate missing function arrows (marked as "MISSING" above)
2. For each missing arrow, determine the correct effect based on:
   - Function name semantics (e.g., "Promotion" vs "Inhibition")
   - Mechanism description
   - Biological context from evidence
3. Use Google Search to verify mechanisms if uncertain about biological accuracy
4. Search for: "{main_protein} {partner} interaction mechanism" and "{main_protein} {partner} [function name]"
5. Focus on recent papers (2015+) and review articles
6. Return ONLY corrections in JSON format (omit unchanged fields)
7. If no corrections needed, return empty JSON: {{}}

**OUTPUT FORMAT (flat per-function fields — no nested "corrections", no
prose). Each entry lists ONLY the fields that need to change:**

```json
{{
  "interaction_level": {{
    "direction": "corrected_value",  // Only if changed
    "arrow": "corrected_value"        // Only if changed
  }},
  "functions": [
    {{
      "function": "Function Name Here",
      "arrow": "corrected_value",               // Net arrow (only if changed)
      "direct_arrow": "corrected_value",         // Only for indirect (only if changed)
      "interaction_effect": "corrected_value",   // Only if changed
      "interaction_direction": "corrected_value" // Only if changed
    }}
  ]
}}
```

**HARD RULES:**
- Do NOT emit a "reasoning" field, a "validation_summary" field, or any
  other free-text explanation. The response_json_schema rejects them,
  and any prose you generate is wasted output budget that can truncate
  later corrections.
- Do NOT nest corrections under a "corrections" key — put fields flat
  on each function entry.
- Only include fields that actually need correction. Omit entries
  entirely when a function is already correct.
- Preserve biological accuracy over notation consistency. Double-check
  for double negatives — the most common error.

Begin validation:"""

    return prompt


def parse_gemini_response(response) -> Optional[Dict[str, Any]]:
    """Extracts corrections JSON from a Gemini response.

    Uses utils.llm_response_parser.extract_json_from_llm_response with its
    6-strategy salvage chain (brace-balanced, code-fence-strip, etc.) so
    HTTP-header-like preambles or trailing prose can never leak into the
    parsed corrections dict. The legacy regex `\\{.*\\}` (greedy) was the
    root cause of the
    ``main_to_primaryStatus: 200 OK Content-Type: application/json {``
    direction corruption seen on the 2026-04-29 ULK1 run.

    Returns:
        Corrections dict or None if response is empty or unparseable.
    """
    try:
        text = extract_text_from_generate_response(response)
        if not text:
            return None

        try:
            from utils.llm_response_parser import extract_json_from_llm_response
            corrections = extract_json_from_llm_response(text)
        except Exception:
            # Last-resort fallback: code-fence regex for cached responses.
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
            if not json_match:
                return None
            try:
                corrections = json.loads(json_match.group(1))
            except json.JSONDecodeError:
                from utils.json_helpers import repair_truncated_json
                repaired = repair_truncated_json(json_match.group(1))
                corrections = json.loads(repaired)
                print(
                    "[INFO] Repaired truncated JSON in arrow validation response (legacy path)",
                    flush=True,
                )

        if isinstance(corrections, dict) and corrections.get("_salvage_wrapped_array"):
            return None
        if not corrections or corrections == {}:
            return None
        return corrections

    except Exception as e:
        print(f"[WARNING] Failed to parse Gemini arrow response: {e}", flush=True)
        return None


def apply_corrections(
    interactor: Dict[str, Any],
    corrections: Dict[str, Any],
    main_protein: str,
    verbose: bool = False
) -> Dict[str, Any]:
    """
    Applies corrections to interactor data.

    Args:
        interactor: Original interactor data
        corrections: Corrections dict from Gemini
        main_protein: Query protein symbol
        verbose: Enable detailed logging

    Returns:
        Updated interactor data
    """
    partner = interactor.get("primary", "UNKNOWN")

    # Apply interaction-level corrections
    interaction_corrections = corrections.get("interaction_level", {})
    for field, new_value in interaction_corrections.items():
        old_value = interactor.get(field, None)
        if old_value != new_value:
            interactor[field] = new_value
            if verbose:
                print(f"      ✎ {field}: {old_value} → {new_value}")

    # Enforce valid enums on interaction-level fields
    if interactor.get("arrow"):
        interactor["arrow"] = _normalize_enum(
            interactor["arrow"], VALID_ARROWS, _ARROW_NORMALIZE, "arrow"
        )
    if interactor.get("direction"):
        interactor["direction"] = _normalize_enum(
            interactor["direction"], VALID_DIRECTIONS, _DIRECTION_NORMALIZE, "direction"
        )
    if interactor.get("interaction_type"):
        interactor["interaction_type"] = _normalize_enum(
            interactor["interaction_type"], VALID_INTERACTION_TYPES, {}, "interaction_type"
        )

    # Auto-generate interaction_effect from arrow if not set
    arrow = interactor.get("arrow", "")
    if not interactor.get("interaction_effect") and arrow:
        # Map arrow to effect: activates → activation, inhibits → inhibition, binds → binding
        effect_map = {
            "activates": "activation",
            "inhibits": "inhibition",
            "binds": "binding",
            "regulates": "regulation"
        }
        interaction_effect = effect_map.get(arrow, arrow)
        interactor["interaction_effect"] = interaction_effect
        if verbose:
            print(f"      ✎ interaction_effect: (auto-generated) → {interaction_effect}")

    # Apply function-level corrections
    function_corrections = corrections.get("functions", [])
    functions = interactor.get("functions", [])

    # Accept BOTH the new flat shape (corrections as top-level fields on
    # the function entry) AND the legacy nested shape (``corrections:
    # {...}``). The new schema emits flat so cached responses, unit
    # tests, or older model outputs that still use the nested form
    # don't break when we tighten the schema.
    _CORRECTABLE_FIELDS = (
        "arrow", "direct_arrow", "interaction_effect", "interaction_direction",
    )

    for func_correction in function_corrections:
        func_name = func_correction.get("function", "")
        nested_changes = func_correction.get("corrections") or {}
        flat_changes = {
            k: v for k, v in func_correction.items()
            if k in _CORRECTABLE_FIELDS and v is not None
        }
        # Flat shape wins — it's the canonical one per the new schema —
        # but we union with any nested fields the legacy path provided.
        func_changes = {**nested_changes, **flat_changes}
        reasoning = func_correction.get("reasoning", "")

        # Find matching function
        for func in functions:
            if func.get("function", "") == func_name:
                # Apply corrections
                for field, new_value in func_changes.items():
                    old_value = func.get(field, None)
                    if old_value != new_value:
                        func[field] = new_value
                        if verbose:
                            print(f"      ✎ {func_name}.{field}: {old_value} → {new_value}")
                            if reasoning and field in ["arrow", "direct_arrow"]:
                                print(f"        → {reasoning}")
                # Enforce valid enum on function arrow
                if func.get("arrow"):
                    func["arrow"] = _normalize_enum(
                        func["arrow"], VALID_ARROWS, _ARROW_NORMALIZE, "function.arrow"
                    )
                break

    interactor["functions"] = functions

    # Auto-generate function_effect for each function based on arrow
    effect_map = {
        "activates": "activation",
        "inhibits": "inhibition",
        "binds": "binding",
        "regulates": "regulation"
    }

    for func in functions:
        func_arrow = func.get("arrow", "")
        if func_arrow and not func.get("function_effect"):
            func["function_effect"] = effect_map.get(func_arrow, func_arrow)

    # Add dual arrow context for indirect interactions
    interaction_type = interactor.get("interaction_type", "direct")
    if interaction_type == "indirect":
        # For indirect interactions, add arrow_context to each function
        upstream_interactor = interactor.get("upstream_interactor")
        mediator_chain = interactor.get("mediator_chain", [])
        main_protein_symbol = main_protein  # query protein from outer scope

        for func in functions:
            # The 'arrow' field represents the NET effect (query → target function)
            net_arrow = func.get("arrow", "regulates")

            # The 'direct_arrow' field (if set by Gemini) represents the DIRECT effect
            # (mediator → target function). If not set, default to net_arrow.
            # WARN if missing - Gemini should provide both for indirect interactions!
            direct_arrow_inferred = False
            if "direct_arrow" not in func:
                if verbose:
                    func_name = func.get("function", "Unknown")
                    print(f"      ⚠ INDIRECT: {func_name} missing direct_arrow, defaulting to net_arrow ({net_arrow})")
                direct_arrow = net_arrow
                direct_arrow_inferred = True
            else:
                direct_arrow = func["direct_arrow"]

            # Add arrow_context with both perspectives
            func["arrow_context"] = {
                "direct_from": upstream_interactor,  # Last protein in chain (mediator)
                "direct_arrow": direct_arrow,  # Mediator's direct effect
                "net_from": main_protein_symbol,  # Query protein
                "net_arrow": net_arrow,  # Query's net effect through chain
                "mediator_chain": mediator_chain,
                "is_indirect": True,
                # Signals that direct_arrow was copied from net_arrow because
                # the LLM didn't provide a distinct value. Chain-inversion
                # analysis treats this as unreliable rather than pretending
                # the mediator link matches the net chain sign.
                "_direct_arrow_inferred": direct_arrow_inferred,
            }

            # Store both arrows at function level for easy access
            func["net_arrow"] = net_arrow
            func["direct_arrow"] = direct_arrow

            # Keep 'arrow' field as net_arrow for backward compatibility
            func["arrow"] = net_arrow

            # Generate separate effect labels for both perspectives
            effect_map = {
                "activates": "activation",
                "inhibits": "inhibition",
                "binds": "binding",
                "regulates": "regulation"
            }
            func["net_effect"] = effect_map.get(net_arrow, net_arrow)
            func["direct_effect"] = effect_map.get(direct_arrow, direct_arrow)

    # Add validation metadata
    interactor["_validation_metadata"] = {
        "validated": True,
        "validator": "arrow_effect_validator",
        "corrections_applied": len(interaction_corrections) + len(function_corrections)
    }

    # Add top-level arrow validation flag (makes it easy for subsequent stages to check)
    interactor["_arrow_validated"] = True

    return interactor


# Standalone test function
def test_validator():
    """Test the validator on a sample interaction."""
    api_key = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not api_key:
        print("ERROR: GOOGLE_CLOUD_PROJECT not set")
        return

    # Sample interaction with double-negative issue
    sample_interactor = {
        "primary": "VCP",
        "direction": "main_to_primary",
        "arrow": "inhibits",
        "interaction_type": "direct",
        "functions": [
            {
                "function": "ER-Associated Degradation (ERAD) Inhibition",
                "arrow": "inhibits",  # DOUBLE NEGATIVE: should be "activates"
                "interaction_effect": "inhibits",
                "interaction_direction": "main_to_primary",
                "biological_consequence": [
                    "ATXN3 inhibits VCP → ERAD inhibition increases → Protein accumulation"
                ]
            }
        ],
        "evidence": [
            {
                "paper_title": "Ataxin-3 binds VCP/p97 and regulates retrotranslocation of ERAD substrates",
                "year": 2006
            }
        ]
    }

    print("\nTesting arrow validator...")
    print(f"Original arrow: {sample_interactor['arrow']}")
    print(f"Original function arrow: {sample_interactor['functions'][0]['arrow']}")

    corrected = validate_single_interaction(
        sample_interactor,
        "ATXN3",
        api_key,
        verbose=True
    )

    print(f"\nCorrected arrow: {corrected['arrow']}")
    print(f"Corrected function arrow: {corrected['functions'][0]['arrow']}")
    print("\nTest complete!")


# ---------------------------------------------------------------------------
# Direct mediator link extraction (merged from arrow_validator_integrated.py)
# ---------------------------------------------------------------------------

def extract_direct_mediator_links_from_json(
    payload: Dict[str, Any],
    api_key: str = None,
    verbose: bool = False,
    flask_app=None,
) -> List[Dict[str, Any]]:
    """Extract direct mediator links from indirect interactions in pipeline JSON.

    For chains like ATXN3->RHEB->MTOR, extracts RHEB->MTOR as a direct link.
    Uses 3-tier strategy:
    - Tier 1: Skip (requires database - manual script only)
    - Tier 2: Query pipeline for direct pair
    - Tier 3: Extract from chain evidence
    """
    snapshot = payload.get('snapshot_json', {})
    interactors = snapshot.get('interactors', [])

    if not interactors:
        if verbose:
            print("[DIRECT LINK EXTRACTION] No interactors to process")
        return []

    # Build deduplicated list of chain-adjacent pairs to process. Prefer
    # chain_context.full_chain because legacy mediator_chain/upstream fields
    # can describe a query-relative view and produce self-pairs such as
    # NPLOC4->NPLOC4 on query-tail or hidden chains.
    indirect_pairs = []
    processed_pairs: set = set()
    pair_interactor_map: Dict[tuple, Dict] = {}
    ctx_payload = payload.get("ctx_json") if isinstance(payload.get("ctx_json"), dict) else {}
    main_symbol = snapshot.get("main") or ctx_payload.get("main") or ""

    def _add_indirect_pair(source: str, target: str, interactor_data: Dict[str, Any]) -> None:
        source = str(source or "").strip()
        target = str(target or "").strip()
        if not source or not target:
            return
        if source.upper() == target.upper():
            return
        pair_key = tuple(sorted([source.upper(), target.upper()]))
        if pair_key in processed_pairs:
            return
        processed_pairs.add(pair_key)
        indirect_pairs.append((source, target))
        pair_interactor_map[pair_key] = interactor_data

    for interactor_data in interactors:
        interaction_type = interactor_data.get('interaction_type', 'direct')
        if interaction_type != 'indirect':
            continue

        primary = interactor_data.get('primary')
        try:
            from utils.chain_view import ChainView

            view = ChainView.from_interaction_data(
                interactor_data,
                query_protein=main_symbol or None,
            )
            if not view.is_empty:
                full_chain = list(view.full_chain)
                for idx in range(len(full_chain) - 1):
                    _add_indirect_pair(
                        full_chain[idx],
                        full_chain[idx + 1],
                        interactor_data,
                    )
                continue
        except Exception:
            pass

        upstream_interactor = interactor_data.get('upstream_interactor')
        mediator_chain = interactor_data.get('mediator_chain', [])
        mediator = upstream_interactor or (mediator_chain[-1] if mediator_chain else None)

        if mediator and primary:
            _add_indirect_pair(mediator, primary, interactor_data)

    if not indirect_pairs:
        return []

    # P1: Split indirect_pairs into a Tier-2 eligible slice and a Tier-3
    # only slice. Pairs inside the budget get a full nested-pipeline
    # attempt; pairs beyond it skip straight to evidence-only extraction.
    # This keeps chain-heavy queries from forking N full pipelines.
    try:
        tier2_budget_env = max(0, int(os.getenv("ARROW_TIER2_BUDGET", str(DEFAULT_TIER2_BUDGET))))
    except ValueError:
        tier2_budget_env = DEFAULT_TIER2_BUDGET
    try:
        tier2_max_workers = max(1, int(os.getenv("ARROW_TIER2_MAX_WORKERS", str(DEFAULT_TIER2_MAX_WORKERS))))
    except ValueError:
        tier2_max_workers = DEFAULT_TIER2_MAX_WORKERS
    # Scale the budget to the size of the pair list: at minimum the env
    # setting, but never less than 60% of the pairs. On the ATXN3 run
    # with 29 indirect pairs and env=10, this raises the budget to 18 so
    # only ~11 pairs fall through to evidence-only Tier 3 instead of 19.
    # Cap via env override for cost-bounded runs.
    import math as _math_t2
    _scaled_floor = int(_math_t2.ceil(0.6 * len(indirect_pairs)))
    _hard_cap = int(os.getenv("ARROW_TIER2_HARD_CAP", "60"))
    tier2_budget = max(tier2_budget_env, min(_scaled_floor, _hard_cap))

    tier2_pairs = indirect_pairs[:tier2_budget] if api_key else []
    tier3_only_pairs = indirect_pairs[len(tier2_pairs):]

    print(
        f"  [DIRECT LINKS] Processing {len(indirect_pairs)} indirect pairs "
        f"(Tier2 budget={len(tier2_pairs)}/{tier2_budget}, "
        f"Tier3-only={len(tier3_only_pairs)})",
        flush=True,
    )
    if tier3_only_pairs:
        print(
            f"  [DIRECT LINKS] Budget exhausted — {len(tier3_only_pairs)} pairs "
            f"will skip Tier 2 nested-pipeline and use Tier 3 evidence-only.",
            flush=True,
        )

    direct_links = []

    def _tier3_for(mediator, primary):
        pair_key = tuple(sorted([str(mediator).upper(), str(primary).upper()]))
        interactor_data = pair_interactor_map.get(pair_key, {})
        return _extract_from_chain_evidence(mediator, primary, interactor_data)

    # ── Tier 1: DB lookup (FREE — chain resolution already created these rows) ──
    # 2ax/2az chain resolution creates direct Interaction rows + claims for
    # each mediator→target hop BEFORE arrow_validation runs. Check the DB
    # first and skip the expensive Tier 2 nested-pipeline for pairs that
    # already have a row with claims.
    tier1_found = 0
    remaining_for_tier2 = []
    try:
        from models import Protein as _Protein, Interaction as _Interaction, InteractionClaim as _Claim
        # Tier1 needs Flask app context for DB queries. The PostProcessor
        # passes flask_app through the adapter chain — use it here.
        from contextlib import nullcontext
        _ctx = flask_app.app_context() if flask_app else nullcontext()
        with _ctx:
            for pair in indirect_pairs:
                mediator, primary = pair
                prot_a = _Protein.query.filter_by(symbol=mediator).first()
                prot_b = _Protein.query.filter_by(symbol=primary).first()
                if prot_a and prot_b:
                    a_id, b_id = min(prot_a.id, prot_b.id), max(prot_a.id, prot_b.id)
                    existing = _Interaction.query.filter_by(
                        protein_a_id=a_id, protein_b_id=b_id
                    ).first()
                    if existing:
                        claims = _Claim.query.filter_by(interaction_id=existing.id).limit(1).all()
                        if claims:
                            # Row exists with claims — rehydrate as a direct link
                            all_claims = _Claim.query.filter_by(interaction_id=existing.id).all()
                            rehydrated_funcs = [{
                                "function": c.function_name,
                                "arrow": normalize_arrow(c.arrow, default="regulates"),
                                "cellular_process": c.mechanism or "",
                                "effect_description": c.effect_description or "",
                                "biological_consequence": c.biological_consequences or [],
                                "specific_effects": c.specific_effects or [],
                                "evidence": c.evidence or [],
                                "pmids": c.pmids or [],
                                "pathway": c.pathway_name,
                                "function_context": "direct",
                                "direction": semantic_claim_direction(c.direction),
                            } for c in all_claims if c.function_name]
                            if rehydrated_funcs:
                                from utils.direction import infer_direction_from_arrow
                                link_arrow = normalize_arrow(existing.primary_arrow, default="binds")
                                direct_links.append({
                                    "primary": primary,
                                    "direction": existing.direction or infer_direction_from_arrow(link_arrow),
                                    "arrow": link_arrow,
                                    "confidence": float(existing.confidence) if existing.confidence else 0.5,
                                    "functions": rehydrated_funcs,
                                    "evidence": rehydrated_funcs[0].get("evidence", []),
                                    "pmids": rehydrated_funcs[0].get("pmids", []),
                                    "function_context": "direct",
                                    "_inferred_from_chain": True,
                                    "_evidence_tier": 1,
                                })
                                tier1_found += 1
                                print(f"    [TIER1-DB] {mediator} -> {primary}: found in DB ({len(rehydrated_funcs)} claims)", flush=True)
                                continue
                remaining_for_tier2.append(pair)
    except Exception as e:
        print(f"    [TIER1-DB] DB lookup failed ({e}), falling through to Tier 2", flush=True)
        remaining_for_tier2 = list(indirect_pairs)

    if tier1_found:
        print(f"  [DIRECT LINKS] Tier 1 (DB): {tier1_found}/{len(indirect_pairs)} found, {len(remaining_for_tier2)} need Tier 2/3", flush=True)

    # ── Tier 2: Nested pipeline (expensive — only for pairs NOT in DB) ──
    tier2_pairs = remaining_for_tier2[:tier2_budget] if api_key else []
    tier3_only_pairs = remaining_for_tier2[len(tier2_pairs):]

    def _process_pair_with_tier2(pair):
        mediator, primary = pair
        direct_link = _query_direct_pair_simple(mediator, primary, api_key, verbose=verbose)
        if direct_link:
            return direct_link
        return _tier3_for(mediator, primary)

    if tier2_pairs:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        workers = min(len(tier2_pairs), tier2_max_workers)
        completed = 0
        total = len(tier2_pairs)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process_pair_with_tier2, p): p for p in tier2_pairs}
            for future in as_completed(futures):
                completed += 1
                pair = futures[future]
                try:
                    result = future.result()
                    if result:
                        direct_links.append(result)
                        print(f"    [{completed}/{total}] {pair[0]} -> {pair[1]}: found direct link", flush=True)
                    else:
                        print(f"    [{completed}/{total}] {pair[0]} -> {pair[1]}: no link found", flush=True)
                except Exception as e:
                    print(f"    [{completed}/{total}] {pair[0]} -> {pair[1]}: FAILED ({e})", flush=True)

    # Tier 3 pass: evidence-only extraction for anything outside the budget
    for pair in tier3_only_pairs:
        result = _tier3_for(*pair)
        if result:
            direct_links.append(result)

    print(f"  [DIRECT LINKS] Extracted {len(direct_links)} / {len(indirect_pairs)} links (Tier1={tier1_found})", flush=True)

    return direct_links


def _query_direct_pair_simple(
    protein_a: str,
    protein_b: str,
    api_key: str,
    verbose: bool = False
) -> Optional[Dict[str, Any]]:
    """Query the pipeline for a direct protein-protein interaction (Tier 2).

    Gated behind ``ENABLE_TIER2_NESTED_PIPELINE`` — the shim returns ``{}``
    when disabled so we preserve the "off by default" posture (Tier 3
    fallback stays the common path). When enabled, the shim handles the
    Flask ``app_context()`` wrap required when this runs inside a
    ``ThreadPoolExecutor`` worker.
    """
    from utils.observability import log_event

    try:
        from runner import run_pipeline_for_protein
    except ImportError as exc:
        log_event(
            "tier2_import_failed",
            level="warn",
            tag="TIER 2",
            protein_a=protein_a,
            protein_b=protein_b,
            error=str(exc),
        )
        return None

    try:
        result = run_pipeline_for_protein(
            protein_symbol=protein_a,
            max_interactor_rounds=1,
            max_function_rounds=1,
            api_key=api_key,
            verbose=verbose,
        )
    except Exception as exc:
        log_event(
            "tier2_call_failed",
            level="warn",
            tag="TIER 2",
            protein_a=protein_a,
            protein_b=protein_b,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None

    # Gate returned ``{}`` — shim is disabled, nothing to do.
    if not result or not isinstance(result, dict):
        return None

    snapshot = result.get("snapshot_json") or {}
    interactors = snapshot.get("interactors") or snapshot.get("interactions") or []

    target_upper = (protein_b or "").strip().upper()
    for interactor in interactors:
        if not isinstance(interactor, dict):
            continue
        primary = (interactor.get("primary") or interactor.get("target") or "").strip().upper()
        if primary != target_upper:
            continue
        # Stamp the Tier 2 marker shape the caller expects.
        interactor = dict(interactor)
        interactor["function_context"] = "direct"
        interactor["_inferred_from_chain"] = True
        interactor["_evidence_tier"] = 2
        for fn in interactor.get("functions", []) or []:
            if isinstance(fn, dict) and not fn.get("function_context"):
                fn["function_context"] = "direct"
        log_event(
            "tier2_extraction_success",
            level="info",
            tag="TIER 2",
            protein_a=protein_a,
            protein_b=protein_b,
            function_count=len(interactor.get("functions", []) or []),
        )
        return interactor

    log_event(
        "tier2_extraction_no_match",
        level="debug",
        tag="TIER 2",
        protein_a=protein_a,
        protein_b=protein_b,
    )
    return None


def _extract_from_chain_evidence(
    mediator: str,
    target: str,
    chain_interaction: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Extract direct mediator link from chain interaction evidence (Tier 3 fallback)."""
    functions = chain_interaction.get('functions', [])
    if not functions:
        return None

    extracted_functions = []
    for func in functions:
        evidence = func.get('evidence', [])
        relevant_evidence = []
        for paper in evidence:
            quote = paper.get('relevant_quote', '').lower()
            title = paper.get('paper_title', '').lower()
            if (mediator.lower() in quote or mediator.lower() in title) and \
               (target.lower() in quote or target.lower() in title):
                relevant_evidence.append(paper)

        if relevant_evidence:
            extracted_func = {
                'function': func.get('function'),
                'arrow': func.get('arrow', 'regulates'),
                'cellular_process': func.get('cellular_process'),
                'effect_description': func.get('effect_description'),
                'evidence': relevant_evidence,
                'pmids': [e.get('pmid') for e in relevant_evidence if e.get('pmid')],
                'confidence': func.get('confidence', 0.5)
            }
            extracted_functions.append(extracted_func)

    if not extracted_functions:
        return None

    # Infer direction from the extracted arrow rather than falling back to
    # "bidirectional" — an activates/inhibits arrow at the direct-link level
    # means a real asymmetric direction, not a placeholder. The old
    # "bidirectional" default leaked into chain-derived records and caused
    # downstream db_sync to treat them as "direction not set".
    from utils.direction import infer_direction_from_arrow
    inferred_arrow = extracted_functions[0].get('arrow', 'regulates')
    direction = chain_interaction.get('direction')
    if not direction or direction == 'bidirectional':
        direction = infer_direction_from_arrow(inferred_arrow)
    return {
        'primary': target,
        'direction': direction,
        'arrow': inferred_arrow,
        'confidence': chain_interaction.get('confidence', 0.5),
        'intent': chain_interaction.get('intent', 'binding'),
        'functions': extracted_functions,
        'evidence': extracted_functions[0].get('evidence', []),
        'pmids': extracted_functions[0].get('pmids', []),
        'function_context': 'direct',
        '_inferred_from_chain': True,
        '_evidence_tier': 3,
        '_original_chain': f"{chain_interaction.get('main', 'Unknown')}->{mediator}->{target}"
    }


def _merge_direct_links_into_payload(
    payload: Dict[str, Any],
    direct_links: List[Dict[str, Any]],
    verbose: bool = False
) -> Dict[str, Any]:
    """Merge extracted direct links into the payload, avoiding duplicates."""
    if not direct_links:
        return payload

    snapshot = payload.get('snapshot_json', {})
    snapshot_interactors = snapshot.setdefault('interactors', [])
    snapshot_existing = {
        _interactor_key(i)
        for i in snapshot_interactors
        if isinstance(i, dict) and _interactor_key(i)
    }

    ctx = payload.get("ctx_json")
    ctx_interactors = None
    ctx_existing = set()
    if isinstance(ctx, dict):
        ctx_interactors = ctx.setdefault("interactors", [])
        if isinstance(ctx_interactors, list):
            ctx_existing = {
                _interactor_key(i)
                for i in ctx_interactors
                if isinstance(i, dict) and _interactor_key(i)
            }

    added_count = 0
    for link in direct_links:
        primary = link.get('primary')
        key = _interactor_key(link)
        if not key:
            continue
        if key not in snapshot_existing:
            snapshot_interactors.append(link)
            snapshot_existing.add(key)
            added_count += 1
        elif verbose:
            print(f"  [MERGE] Skipping {primary} in snapshot (already exists)")

        if isinstance(ctx_interactors, list) and key not in ctx_existing:
            ctx_interactors.append(deepcopy(link))
            ctx_existing.add(key)

        if verbose:
            print(f"  [MERGE] Added {primary} as direct mediator link")

    if verbose:
        print(f"[MERGE] Added {added_count}/{len(direct_links)} new direct links")

    snapshot['interactors'] = snapshot_interactors
    payload['snapshot_json'] = snapshot

    return payload


def _preflight_tier1_arrows(payload: Dict[str, Any], flask_app=None) -> Tuple[set, int]:
    """Pre-flight DB lookup for arrow validation.

    For each interactor in the snapshot, check whether its (main, primary)
    pair already exists as a saved Interaction with a non-empty primary_arrow.
    If so, copy the DB-validated arrow/direction onto the snapshot interactor
    and add its index to the returned skip set so Stage 1's LLM dispatch
    avoids re-validating it.

    Returns (skip_indices, total_interactors). On any DB error, returns an
    empty set so Stage 1 runs as before — pre-flight is best-effort speed,
    never a correctness gate.
    """
    if not isinstance(payload, dict):
        return set(), 0
    snapshot = payload.get("snapshot_json", payload) or {}
    interactors = snapshot.get("interactors", []) or []
    main_protein = snapshot.get("main") or (
        payload.get("ctx_json", {}).get("main") if isinstance(payload.get("ctx_json"), dict) else None
    )
    if not main_protein or not interactors:
        return set(), len(interactors)

    skip_indices: set = set()
    try:
        from models import Protein as _Protein, Interaction as _Interaction
        from contextlib import nullcontext
        _ctx = flask_app.app_context() if flask_app else nullcontext()
        with _ctx:
            main_prot = _Protein.query.filter_by(symbol=str(main_protein).upper()).first()
            if not main_prot:
                return set(), len(interactors)
            for idx, intr in enumerate(interactors):
                primary = intr.get("primary")
                if not primary:
                    continue
                p_prot = _Protein.query.filter_by(symbol=str(primary).upper()).first()
                if not p_prot:
                    continue
                a_id, b_id = min(main_prot.id, p_prot.id), max(main_prot.id, p_prot.id)
                existing = _Interaction.query.filter_by(
                    protein_a_id=a_id, protein_b_id=b_id
                ).first()
                if not existing or not existing.primary_arrow:
                    continue
                # Copy DB-validated values onto snapshot so downstream
                # consumers (dedup, finalize_metadata) see the validated
                # arrow without an LLM call.
                intr["arrow"] = normalize_arrow(existing.primary_arrow, default="binds")
                if getattr(existing, "direction", None):
                    intr["direction"] = existing.direction
                # Tag for observability — surfaces in serialized payload.
                intr.setdefault("_arrow_validation_source", "tier1_db")
                skip_indices.add(idx)
    except Exception as exc:
        # Any DB issue → run Stage 1 normally, no harm done.
        print(f"[ARROW TIER1] pre-flight failed ({type(exc).__name__}: {exc}); running full Stage 1", flush=True)
        return set(), len(interactors)

    return skip_indices, len(interactors)


def _apply_tier1_normalization_to_payload(
    payload: Dict[str, Any],
    tier1_indices: set,
    verbose: bool = False,
) -> None:
    """Run deterministic ``apply_corrections({})`` on Tier-1-hit interactors.

    Equivalent to running the LLM validator with no corrections to apply
    — only the auto-generation steps execute:

      - ``interaction_effect`` derived from ``arrow``
      - per-function ``function_effect`` derived from each function's arrow
      - ``arrow_context`` (dual ``net_arrow``/``direct_arrow``) for
        indirect interactors
      - ``_arrow_validated = True`` flag on the interactor

    This makes the Tier-1 path produce the SAME shape of validated
    payload as the LLM path. Without this, new chain-derived claims
    emitted during a re-run had no ``arrow_context`` / no
    ``_arrow_validated`` flag, and the modal's dual-arrow rendering
    (``hasDualTrack`` branch) silently fell back to single-arrow
    display for those claims.

    Mutates ``payload`` in place. No-op if the payload shape is bad.
    """
    if not isinstance(payload, dict) or not tier1_indices:
        return
    snapshot = payload.get("snapshot_json", payload) or {}
    interactors = snapshot.get("interactors") or []
    if not interactors:
        return
    main_protein = (
        snapshot.get("main")
        or (payload.get("ctx_json", {}) or {}).get("main")
        or ""
    )
    normalized_count = 0
    for idx in tier1_indices:
        if idx < 0 or idx >= len(interactors):
            continue
        intr = interactors[idx]
        if not isinstance(intr, dict):
            continue
        try:
            apply_corrections(intr, {}, main_protein, verbose=False)
            normalized_count += 1
        except Exception as exc:
            # Never let one malformed interactor break the rest.
            print(
                f"[ARROW TIER1] normalization failed for "
                f"{intr.get('primary', '?')}: {type(exc).__name__}: {exc}",
                flush=True,
            )
    if normalized_count and verbose:
        print(
            f"[ARROW TIER1] Applied deterministic normalization "
            f"(interaction_effect, function_effect, arrow_context, "
            f"_arrow_validated) to {normalized_count} Tier-1 interactor(s).",
            flush=True,
        )


def validate_arrows_for_payload(
    payload: Dict[str, Any],
    api_key: str = None,
    verbose: bool = False,
    flask_app=None,
) -> Dict[str, Any]:
    """Full validation pipeline: arrows + direct link extraction.

    This is the main entry point called by runner.py (via PostProcessor).
    Combines arrow/effect validation with direct mediator link extraction.
    """
    if verbose:
        print("\n" + "="*60)
        print("INTEGRATED ARROW VALIDATION + DIRECT LINK EXTRACTION")
        print("="*60)

    # Pre-flight: DB-first short-circuit. If a pair already has a saved
    # Interaction row with primary_arrow, the LLM call for that pair is
    # redundant — chain resolution already populated the row before the
    # post-processing chain ran. Skipping those calls turned a 12-minute
    # ATXN3 stage into ~10 seconds when DB hits dominated.
    tier1_skip_indices, _total_interactors = _preflight_tier1_arrows(payload, flask_app=flask_app)
    if tier1_skip_indices:
        # Surface the speed win in the metric stream.
        existing = payload.get("_request_metrics") or {}
        if not isinstance(existing, dict):
            existing = {}
        existing["arrow_tier1_hits"] = int(existing.get("arrow_tier1_hits", 0)) + len(tier1_skip_indices)
        payload["_request_metrics"] = existing
        print(
            f"[ARROW TIER1] DB pre-flight: {len(tier1_skip_indices)}/{_total_interactors} "
            f"interactor(s) already validated in DB — skipping LLM for those.",
            flush=True,
        )

    # Step 1: Arrow/effect validation
    #
    # Two paths converge here:
    #   (a) Run validate_arrows_and_effects → LLM call → apply_corrections
    #       per interactor.  apply_corrections() is the function that
    #       populates ``_arrow_validated``, ``interaction_effect``,
    #       per-function ``function_effect``, and (critically for
    #       indirect interactors) ``arrow_context`` with the dual
    #       ``net_arrow`` / ``direct_arrow`` pair plus the
    #       ``_direct_arrow_inferred`` flag the modal needs.
    #
    #   (b) Tier-1 DB hit → arrow/direction copied off the saved
    #       Interaction row → no LLM call needed for THAT pair. But
    #       any new chain-derived claims emitted in THIS run still
    #       need post-validation normalization (function_effect,
    #       arrow_context, etc.) — otherwise the modal renders
    #       incomplete dual-arrow data for new claims and the
    #       frontend has no way to tell that a claim has been
    #       through the validator at all.
    #
    # Pre-fix bug: when ALL interactors were Tier-1 hits the entire
    # stage was skipped — log line ``[ARROW VALIDATION] All
    # interactors hit Tier-1 DB — skipping LLM Stage 1 entirely``.
    # That meant ``apply_corrections`` never ran for any interactor,
    # so new chain claims went straight to DB / frontend without
    # ``arrow_context``, ``_arrow_validated``, or auto-generated
    # ``function_effect``. This was the root cause of the user's
    # "utils post-processing might fuck things up arrow-determination
    # wise" concern — the post-processing wasn't fucking it up, it
    # wasn't running at all.
    #
    # Fix: ALWAYS run the post-validation normalization for every
    # interactor. For LLM-validated ones it runs after the LLM
    # corrections. For Tier-1 ones it runs with an empty corrections
    # dict (so only the deterministic auto-generation steps execute).
    if GEMINI_AVAILABLE:
        non_tier1_count = _total_interactors - len(tier1_skip_indices or set())
        if non_tier1_count > 0:
            if verbose:
                print("[STAGE 1] Running arrow/effect validation for non-Tier-1 interactors...")
            payload = validate_arrows_and_effects(
                payload=payload,
                api_key=api_key,
                verbose=verbose,
                skip_indices=tier1_skip_indices or None,
            )
            if verbose:
                print("[STAGE 1] Arrow validation complete")
        else:
            print(
                "[ARROW VALIDATION] All interactors hit Tier-1 DB — skipping LLM, "
                "running deterministic normalization on cached arrows.",
                flush=True,
            )

        # Tier-1 normalization: even if the LLM stage was skipped
        # for some / all pairs, every Tier-1 interactor still needs
        # ``apply_corrections({})`` to populate auto-generated effect
        # fields and (for indirect) the dual ``arrow_context`` block.
        # Without this, new chain claims show up at the modal with
        # missing ``net_arrow``/``direct_arrow`` / no
        # ``_arrow_validated`` flag.
        if tier1_skip_indices:
            _apply_tier1_normalization_to_payload(
                payload=payload,
                tier1_indices=tier1_skip_indices,
                verbose=verbose,
            )
    elif verbose:
        print("[STAGE 1] Arrow validator not available, skipping")

    # Step 2: Extract direct mediator links (skippable — often the slowest part)
    skip_direct_links = os.getenv("SKIP_DIRECT_LINK_EXTRACTION", "").lower() in ("1", "true", "yes")
    if skip_direct_links:
        print("  [STAGE 2] Direct link extraction SKIPPED (SKIP_DIRECT_LINK_EXTRACTION=true)", flush=True)
        direct_links = []
    else:
        print("  [STAGE 2] Extracting direct mediator links...", flush=True)
        direct_links = extract_direct_mediator_links_from_json(
            payload=payload, api_key=api_key, verbose=verbose, flask_app=flask_app,
        )

    # Step 3: Merge into payload
    if direct_links:
        if verbose:
            print(f"\n[STAGE 3] Merging {len(direct_links)} direct links...")
        payload = _merge_direct_links_into_payload(
            payload=payload, direct_links=direct_links, verbose=verbose,
        )

    if verbose:
        print("\n" + "="*60)
        print("VALIDATION COMPLETE")
        print("="*60 + "\n")

    return payload


if __name__ == "__main__":
    test_validator()
