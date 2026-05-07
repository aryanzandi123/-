#!/usr/bin/env python3
"""
ENHANCED Pipeline Runner with Evidence Validation
Runs the maximized pipeline + optional post-validation for citations
UPDATED: Flask-compatible with run_full_job() for web integration
UPDATED: Protein database integration for cross-query knowledge building
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
import os
import random
import re
import sys
import time
import threading
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait as futures_wait
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence
from collections import defaultdict, deque

# Module-level counters for parse/call failures during a pipeline run.
# Not thread-locked — we only ever write to these from the main pipeline
# driver thread (chain steps run serially). Used by _run_chain_step_internal
# to turn the old silent ``return None`` into a structured signal that
# the run summary can surface. Reset at pipeline start.
_PARSE_FAILURE_COUNTERS: Dict[str, int] = defaultdict(int)

# Rolling 60-second token-per-minute tracker shared across every parallel
# phase in a run. Each ``_worker`` in ``_run_parallel_batched_phase``
# records its observed token usage here; the dispatcher then waits
# adaptively before firing the next group if the rolling sum is above
# ``GEMINI_TPM_BUDGET``. Sharing a single tracker across phases prevents
# phase A from bursting its quota and leaving phase B to eat the 429s.
_TPM_TRACKER_LOCK = threading.Lock()
_TPM_TRACKER_DEQUE: "deque[tuple[float, int]]" = deque()

# Process-wide lock guarding per-job os.environ mutations (P1-B4 interim).
# Acquired only when a job has non-empty env_dict (advanced overrides);
# released in finally after env is restored. Jobs without overrides skip
# the lock entirely so the common case stays fully parallel.
_RUN_ENV_OVERRIDE_LOCK = threading.Lock()
_TPM_WINDOW_SECONDS = 60.0
# In-flight token reservation. Incremented when a dispatch is about to
# send calls that haven't yet recorded their actual usage. Decremented
# in the worker after ``_tpm_record_tokens`` has recorded the real
# consumption (so a single call's tokens aren't double-counted). Without
# this, a burst of N workers can all pass the "<budget" gate before any
# of them return to record usage, so the observed 60s window blows past
# the budget by the time the tokens land (861k / 600k observed in one
# ATXN3 run). See _tpm_reserve / _tpm_release / _tpm_current_load.
_TPM_RESERVED_TOKENS = 0


def _tpm_record_tokens(token_count: int) -> None:
    """Record a token-usage sample on the rolling TPM window."""
    if token_count <= 0:
        return
    with _TPM_TRACKER_LOCK:
        _TPM_TRACKER_DEQUE.append((time.time(), int(token_count)))


def _tpm_current_usage() -> int:
    """Return total tokens observed in the last ``_TPM_WINDOW_SECONDS``."""
    cutoff = time.time() - _TPM_WINDOW_SECONDS
    with _TPM_TRACKER_LOCK:
        while _TPM_TRACKER_DEQUE and _TPM_TRACKER_DEQUE[0][0] < cutoff:
            _TPM_TRACKER_DEQUE.popleft()
        return sum(tokens for _, tokens in _TPM_TRACKER_DEQUE)


def _tpm_current_load() -> int:
    """Observed + in-flight tokens — the value to gate dispatch against."""
    with _TPM_TRACKER_LOCK:
        reserved = _TPM_RESERVED_TOKENS
    return _tpm_current_usage() + max(0, reserved)


def _tpm_reserve(estimated_tokens: int) -> None:
    """Reserve in-flight budget for a call that is about to dispatch."""
    if estimated_tokens <= 0:
        return
    global _TPM_RESERVED_TOKENS
    with _TPM_TRACKER_LOCK:
        _TPM_RESERVED_TOKENS += int(estimated_tokens)


def _tpm_release(estimated_tokens: int) -> None:
    """Release the reservation made by ``_tpm_reserve`` (call once per reserve)."""
    if estimated_tokens <= 0:
        return
    global _TPM_RESERVED_TOKENS
    with _TPM_TRACKER_LOCK:
        _TPM_RESERVED_TOKENS = max(0, _TPM_RESERVED_TOKENS - int(estimated_tokens))


def _estimate_tokens_per_call(step: Any) -> int:
    """Realistic average-case token estimate for one call to ``step``.

    Used for in-flight reservation accounting. Two design points learned
    the hard way:

    1. Use a FRACTION of max_output_tokens, not the full cap. Most calls
       never come close to the cap — chain-claim generation typically
       lands ~12-25k output even with max_out=65536. Reserving the full
       cap caused single groups to estimate over the entire 600k TPM
       budget, deadlocking the throttle (a fresh dispatcher with
       observed=0 + reserved=0 + headroom=684288 against budget=600000
       waited forever for tokens that never arrived).

    2. Tunable via env so high-tier accounts can over-reserve safely.
    """
    try:
        max_out = int(getattr(step, "max_output_tokens", 0) or 0)
    except Exception:
        max_out = 0
    if max_out <= 0:
        max_out = 16384
    # Default to 30% of the output cap. Live observation on TDP43 + ATXN3:
    # actual chain-claim usage averages ~30k/call against a 65k cap (~46%).
    # Estimating at 30% (~20k) intentionally under-reserves a bit so 10
    # workers fit under the 600k budget without throttle waits, accepting
    # that ~10% of dispatch waves will briefly tip ~10% over budget. The
    # observed-window check on the NEXT iteration absorbs that overshoot
    # naturally. Earlier 50% setting was provably too conservative — it
    # forced 5+ minute throttle waits between groups.
    output_fraction = float(os.environ.get("GEMINI_TPM_OUTPUT_FRACTION", "0.3"))
    output_est = int(max_out * max(0.1, min(1.0, output_fraction)))
    prompt_est = int(os.environ.get("GEMINI_TPM_PROMPT_ESTIMATE", "5000"))
    return output_est + prompt_est


def _tpm_wait_for_budget(budget: int, phase_name: str, *, needed_headroom: int = 0) -> None:
    """Block until observed + reserved tokens fit under budget.

    Sleeps in 1-second ticks, re-checking each time. A no-op when
    ``budget <= 0`` (disabled). Logs the first tick of waiting so
    stalls are visible without spamming the log every second.

    ``needed_headroom`` is the in-flight cost the caller is about to add.
    The gate is ``load + needed_headroom <= budget`` — but with a
    deadlock guard: if ``needed_headroom`` ALONE exceeds the budget
    (e.g. a 8-call group with conservative reservations ≈ 684k against
    a 600k cap), the caller would wait forever for an observed window
    that never opens. In that case we wait only until ``load <= budget``
    (i.e. prior groups have flushed), then dispatch and accept a brief
    over-reservation. The actual call-completion accounting still
    enforces the budget on subsequent groups.
    """
    if budget <= 0:
        return
    deadlock_mode = needed_headroom >= budget
    if deadlock_mode:
        print(
            f"[PARALLEL:{phase_name}] TPM headroom request {needed_headroom} "
            f">= budget {budget}; will gate on observed+reserved alone "
            f"(group too big to pre-fit; brief overshoot accepted).",
            file=sys.stderr, flush=True,
        )
    logged = False
    while True:
        load = _tpm_current_load()
        if deadlock_mode:
            fits = load <= budget
        else:
            fits = load + needed_headroom <= budget
        if fits:
            if logged:
                tag = "" if deadlock_mode else f"+{needed_headroom}"
                print(
                    f"[PARALLEL:{phase_name}] TPM budget cleared "
                    f"({load}{tag}/{budget}) — resuming dispatch.",
                    file=sys.stderr, flush=True,
                )
            return
        if not logged:
            tag = "" if deadlock_mode else f"+{needed_headroom}"
            print(
                f"[PARALLEL:{phase_name}] TPM throttle: load "
                f"{load}{tag}/{budget} (observed + reserved"
                f"{' + about-to-dispatch' if not deadlock_mode else ''}) "
                "— waiting for window to slide...",
                file=sys.stderr, flush=True,
            )
            logged = True
        time.sleep(1.0)

# Fix Windows console encoding for Greek letters and special characters
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Suppress noisy httpx INFO logs (floods terminal with "HTTP Request: POST ...")
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("google.genai").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("google_genai.models").setLevel(logging.WARNING)
logging.getLogger("google.auth").setLevel(logging.WARNING)

import httpx
from google.genai import types

from dotenv import load_dotenv

# Load .env before module-level tuning constants are read. app.py already does
# this before importing runner, but direct/local runner imports need the same
# behavior so CHAIN_CLAIM_* knobs never silently fall back to defaults.
load_dotenv(override=True)

# Import protein database for cross-query knowledge

# Import the dynamic config (supports dynamic rounds)
from pipeline.config_dynamic import generate_pipeline, PIPELINE_STEPS as DEFAULT_PIPELINE_STEPS
DYNAMIC_CONFIG_AVAILABLE = True

from pipeline.types import StepConfig
# Step factories used by the parallel phase dispatch. These used to be
# imported lazily inside the ``step.name.startswith("step2a_functions_r")``
# block, which was only entered for the modern pipeline. The iterative
# pipeline emits step name "step2a_functions" (no _rN), so the lazy import
# was skipped and the step2ab chain-resolution branch hit UnboundLocalError
# when it referenced the factory. Hoisting to module scope makes the name
# unconditionally bound for every code path.
from pipeline.prompts.modern_steps import step2a_interaction_functions
from pipeline.prompts.shared_blocks import make_batch_directive, make_depth_expand_batch_directive
from utils.quality_validator import validate_payload_depth
from visualizer import create_visualization
from utils.gemini_runtime import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_THINKING_LEVEL,
    build_generate_content_config,
    build_interaction_generation_config,
    build_interaction_tools,
    call_deep_research,
    call_interaction,
    create_or_get_system_cache,
    describe_empty_response,
    extract_text_from_generate_response,
    extract_text_from_interaction,
    extract_usage_token_stats,
    get_client,
    get_gemini_3_pro_pricing,
    get_core_model,
    get_model,
    get_request_mode,
    is_daily_model_quota_exhausted,
    is_quota_error,
    is_transient_network_error,
    parse_request_mode,
    resolve_batch_max_wait_seconds,
    resolve_batch_poll_seconds,
    submit_batch_job,
)

# Post-processing pipeline (handles all utility availability checks internally)
from utils.post_processor import PostProcessor

# Import step logger for comprehensive logging
try:
    from utils.step_logger import StepLogger
    STEP_LOGGER_AVAILABLE = True
except ImportError:
    STEP_LOGGER_AVAILABLE = False

try:
    from utils.structured_log import StructuredPipelineLog
    STRUCTURED_LOG_AVAILABLE = True
except ImportError:
    STRUCTURED_LOG_AVAILABLE = False

try:
    from services import metrics as metrics_registry
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False

# Evidence validator — still needed by CLI __main__ block
try:
    from utils.evidence_validator import validate_and_enrich_evidence
    VALIDATOR_AVAILABLE = True
except ImportError:
    VALIDATOR_AVAILABLE = False

# Unified pathway pipeline flag (Stage 11)
try:
    from scripts.pathway_v2.run_pipeline import run_pathway_pipeline  # noqa: F401
    PATHWAY_PIPELINE_AVAILABLE = True
except ImportError:
    PATHWAY_PIPELINE_AVAILABLE = False

CACHE_DIR = "cache"

# Parallel batch execution tuning (env-configurable).
#
# The real bottleneck on gemini-3-flash-preview is TPM (tokens-per-minute),
# not RPM. A thinking-mode call burns ~25–95k tokens (prompt + thinking +
# up to 65k output), so three concurrent calls every 10s can easily punch
# through the per-minute TPM budget even though RPM is fine. The
# dispatcher therefore runs a *token-aware* throttle (see
# _run_parallel_batched_phase): a rolling 60-s deque of observed tokens
# gates the next group dispatch against ``GEMINI_TPM_BUDGET``. The old
# fixed-sleep throttle is kept as a floor but defaults to 0.
PARALLEL_BATCH_SIZE = int(os.getenv("PARALLEL_BATCH_SIZE", "5"))
PARALLEL_MAX_WORKERS = int(os.getenv("PARALLEL_MAX_WORKERS", "10"))
# Chain-claim generation (2ax/2az) is Flash-only and output-heavy. Keep
# batches small; the runner performs a dedicated single-pair recovery pass
# for any hop that still lacks nested chain_link_functions.
CHAIN_CLAIM_BATCH_SIZE = int(os.getenv("CHAIN_CLAIM_BATCH_SIZE", "1"))
CHAIN_CLAIM_MAX_WORKERS = int(
    os.getenv("CHAIN_CLAIM_MAX_WORKERS", str(min(PARALLEL_MAX_WORKERS, 6)))
)
CHAIN_CLAIM_RECOVERY_MAX_WORKERS = int(
    os.getenv("CHAIN_CLAIM_RECOVERY_MAX_WORKERS", str(min(CHAIN_CLAIM_MAX_WORKERS, 1)))
)
CHAIN_CLAIM_MAX_RETRIES = int(os.getenv("CHAIN_CLAIM_MAX_RETRIES", "1"))
CHAIN_CLAIM_FAILED_BATCH_RETRIES = int(os.getenv("CHAIN_CLAIM_FAILED_BATCH_RETRIES", "1"))
CHAIN_CLAIM_RETRY_BASE_DELAY = float(os.getenv("CHAIN_CLAIM_RETRY_BASE_DELAY", "3.0"))
PARALLEL_GROUP_HEARTBEAT_SECONDS = max(
    1.0, float(os.getenv("PARALLEL_GROUP_HEARTBEAT_SECONDS", "30"))
)
CHAIN_CLAIM_ROLLING_DISPATCH = os.getenv(
    "CHAIN_CLAIM_ROLLING_DISPATCH", "true"
).strip().lower() not in {"0", "false", "no", "off"}
CHAIN_CLAIM_ADAPTIVE_DISPATCH = os.getenv(
    "CHAIN_CLAIM_ADAPTIVE_DISPATCH", "true"
).strip().lower() not in {"0", "false", "no", "off"}
CHAIN_CLAIM_ADAPTIVE_WORKERS = int(os.getenv("CHAIN_CLAIM_ADAPTIVE_WORKERS", "4"))
FUNCTION_MAPPING_ROLLING_DISPATCH = os.getenv(
    "FUNCTION_MAPPING_ROLLING_DISPATCH", "true"
).strip().lower() not in {"0", "false", "no", "off"}
FUNCTION_MAPPING_BATCH_SIZE = int(
    os.getenv("FUNCTION_MAPPING_BATCH_SIZE", str(PARALLEL_BATCH_SIZE))
)
FUNCTION_MAPPING_MAX_WORKERS = int(
    os.getenv("FUNCTION_MAPPING_MAX_WORKERS", str(PARALLEL_MAX_WORKERS))
)
FUNCTION_MAPPING_REQUEST_TIMEOUT_MS = int(
    os.getenv("FUNCTION_MAPPING_REQUEST_TIMEOUT_MS", "120000")
)
FUNCTION_MAPPING_MAX_RETRIES = int(os.getenv("FUNCTION_MAPPING_MAX_RETRIES", "1"))
FUNCTION_MAPPING_FAILED_BATCH_RETRIES = int(
    os.getenv("FUNCTION_MAPPING_FAILED_BATCH_RETRIES", "1")
)
FUNCTION_MAPPING_RETRY_BASE_DELAY = float(
    os.getenv("FUNCTION_MAPPING_RETRY_BASE_DELAY", "2.0")
)
FUNCTION_MAPPING_HTTP_RETRY_ATTEMPTS = int(
    os.getenv("FUNCTION_MAPPING_HTTP_RETRY_ATTEMPTS", "1")
)
CHAIN_CLAIM_REQUEST_TIMEOUT_MS = int(
    os.getenv("CHAIN_CLAIM_REQUEST_TIMEOUT_MS", "0")
)
CHAIN_CLAIM_HTTP_RETRY_ATTEMPTS = int(
    os.getenv("CHAIN_CLAIM_HTTP_RETRY_ATTEMPTS", "1")
)
# Rolling 60-second token budget for the parallel dispatcher. Conservative
# Tier-1 paid-preview estimate; raise on higher tiers or lower if 429s
# still appear. The dispatcher will sleep adaptively to stay under this
# budget. Set to 0 to disable token-aware throttling.
GEMINI_TPM_BUDGET = int(os.getenv("GEMINI_TPM_BUDGET", "600000"))
# Request mode for chain-claim generation (2ax/2az). Default "standard"
# keeps calls on the synchronous endpoint. Set to "batch" to route
# through Gemini's async Batch API (quota-exempt but adds poll overhead).
CHAIN_CLAIM_REQUEST_MODE = os.getenv("CHAIN_CLAIM_REQUEST_MODE", "standard")

_run_metrics_local = threading.local()


def _set_pipeline_status(protein_symbol: str, status: str, flask_app, phase: str = None) -> None:
    """Update protein pipeline_status in DB. Best-effort (swallows errors)."""
    try:
        with flask_app.app_context():
            from models import Protein, db
            protein = Protein.query.filter_by(symbol=protein_symbol).first()
            if protein:
                protein.pipeline_status = status
                if phase:
                    protein.last_pipeline_phase = phase
                db.session.commit()
    except Exception as exc:
        print(f"[WARN] Failed to set pipeline_status={status}: {exc}", file=sys.stderr)


def _reset_run_request_metrics() -> None:
    """Reset per-thread request counters used for run diagnostics."""
    _run_metrics_local.counters = {
        "core_calls_3pro": 0,
        "evidence_calls_2_5pro": 0,
        # Legacy counter (only fires for "2.5" model names — invisible on
        # gemini-3-flash-preview). Kept for back-compat consumers.
        "arrow_calls_2_5pro": 0,
        # F5: real arrow counters (P1.3) — track LLM calls regardless of
        # model name, plus the DB short-circuit win and any 2.5-pro
        # fallback fires. The aggregate of these is the real cost picture.
        "arrow_llm_calls": 0,
        "arrow_tier1_hits": 0,
        "arrow_fallback_to_pro": 0,
        "quota_skipped_calls": 0,
    }


def _get_run_request_metrics() -> Dict[str, int]:
    counters = getattr(_run_metrics_local, "counters", None)
    if not isinstance(counters, dict):
        _reset_run_request_metrics()
        counters = _run_metrics_local.counters
    return counters


def _increment_run_request_metric(metric_name: str, amount: int = 1) -> None:
    counters = _get_run_request_metrics()
    counters[metric_name] = int(counters.get(metric_name, 0)) + int(amount)


def _coerce_token_count(value: Any) -> int:
    """Safely convert token counters to ints, treating None/missing as 0."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0



# ── Extracted helpers (backward-compat re-exports) ──────────────────────
from utils.json_helpers import (                       # noqa: E402
    PipelineError,
    strip_code_fences,
    deep_merge_interactors,
    parse_json_output,
    repair_truncated_json,
)
from pipeline.context_builders import (                # noqa: E402
    dumps_compact,
    build_known_interactions_context,
    build_prompt,
)


def ensure_env() -> None:
    """Load environment variables and verify Vertex AI config exists."""
    load_dotenv()
    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        sys.exit("GOOGLE_CLOUD_PROJECT is not set. Add it to your environment or .env file.")
    if not os.getenv("GOOGLE_CLOUD_LOCATION"):
        sys.exit("GOOGLE_CLOUD_LOCATION is not set. Add it to your environment or .env file.")


def validate_steps(steps: Iterable[StepConfig]) -> List[StepConfig]:
    """Ensure step configuration is sane before executing the pipeline."""
    seen_names: set[str] = set()
    validated: List[StepConfig] = []

    for step in steps:
        if step.name in seen_names:
            raise PipelineError(f"Duplicate step name detected: {step.name}")
        if not step.expected_columns:
            raise PipelineError(f"Step '{step.name}' must declare expected_columns.")
        seen_names.add(step.name)
        validated.append(step)

    if not validated:
        raise PipelineError("PIPELINE_STEPS is empty.")

    return validated




# parse_json_output, strip_code_fences, deep_merge_interactors
# imported from utils.json_helpers above


def _is_truncated_output(text: str) -> bool:
    """Detect output truncation via unbalanced braces/brackets."""
    opens = text.count("{") + text.count("[")
    closes = text.count("}") + text.count("]")
    return opens > closes


_DEPTH_CRITICAL_STEP_PREFIXES = (
    "step2a", "step2ax", "step2az", "function_mapping",
)


def _is_depth_critical_step(step_name: str) -> bool:
    """Whether a parse failure on this step puts cascade depth at risk."""
    if not step_name:
        return False
    name = step_name.lower()
    return any(name.startswith(p) for p in _DEPTH_CRITICAL_STEP_PREFIXES)


def _handle_parse_failed_flag(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Check for poison-pill _parse_failed flag on a payload, log it, and strip the markers.

    Call this after every _parse_with_retry invocation so all pipeline paths surface
    failed-step warnings consistently instead of silently carrying corrupted state.

    Depth-critical steps (step2a*, step2ax_*, step2az_*) get an extra
    visibility boost: their failures are stamped on
    ``_pipeline_metadata.depth_critical_parse_failures`` so the API
    response can render a "shallow-output risk" banner. The retry loop
    inside ``_parse_with_retry`` already burned 3 retries with reduced
    prompt before flagging — adding more retries here would just compound
    cost. The right next move is to surface the failure to the user.
    """
    if not payload or not payload.get("_parse_failed"):
        return payload
    failed_step = payload.get("_parse_failed_step", "unknown")
    is_critical = _is_depth_critical_step(failed_step)

    severity = "[ERROR]" if is_critical else "[WARN]"
    print(
        f"{severity} Step {failed_step} produced degraded data — "
        f"pipeline continuing with partial results"
        + (
            " (DEPTH-CRITICAL — depth pass-rate at risk)" if is_critical else ""
        ),
        file=sys.stderr, flush=True,
    )

    if is_critical:
        meta = payload.setdefault("_pipeline_metadata", {})
        meta.setdefault("depth_critical_parse_failures", []).append(failed_step)

    payload.pop("_parse_failed", None)
    payload.pop("_parse_failed_step", None)
    return payload


def _strip_json_array_value(text: str, key: str) -> str:
    """Replace `"key": [ ... ]` (array value, possibly multi-line, possibly nested)
    with `"key": []`. Returns text unchanged if the key isn't found.

    Uses a brace-counting scan rather than regex so nested brackets,
    escaped quotes, and multi-line values are handled correctly.
    """
    needle = f'"{key}"'
    idx = text.find(needle)
    while idx != -1:
        # Advance past the key and any whitespace/colon to the opening '['
        cursor = idx + len(needle)
        while cursor < len(text) and text[cursor] in ' \t\n\r:':
            cursor += 1
        if cursor >= len(text) or text[cursor] != '[':
            # Not an array value — skip to next occurrence
            idx = text.find(needle, cursor)
            continue
        # Walk forward matching brackets, respecting strings
        depth = 0
        i = cursor
        in_str = False
        escaped = False
        while i < len(text):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == '\\':
                    escaped = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        # Replace [cursor..i] (inclusive) with []
                        text = text[:cursor] + '[]' + text[i + 1:]
                        break
            i += 1
        else:
            # Unterminated array — give up on this key entirely
            break
        idx = text.find(needle)
    return text


def _reduce_prompt_for_retry(prompt: str, attempt: int, hard_char_cap: int = 80000) -> str:
    """Trim verbose context from a prompt to stay within output token limits.

    Strategy by attempt:
      0 — remove search_history / interactor_history arrays
      1 — also strip function detail blocks
      2+ — truncate to hard_char_cap as ultimate fallback

    Always prepends a compactness instruction so Gemini knows the retry is
    operating under a tighter budget.
    """
    reduced = prompt

    # Tier 1: drop history arrays on every retry
    for key in ("search_history", "interactor_history"):
        reduced = _strip_json_array_value(reduced, key)

    # Tier 2: drop function detail blocks on later retries
    if attempt >= 1:
        for key in ("functions", "chain_link_functions", "evidence"):
            reduced = _strip_json_array_value(reduced, key)

    # Tier 3: hard character cap as ultimate fallback (preserve tail where
    # the actual instructions live; drop the front of the context blob)
    if attempt >= 2 and len(reduced) > hard_char_cap:
        # Keep last hard_char_cap chars plus a note
        reduced = (
            "[CONTEXT TRUNCATED FOR RETRY - earlier history omitted]\n\n"
            + reduced[-hard_char_cap:]
        )

    # Always prepend a compactness hint so Gemini reserves output budget
    header = (
        "IMPORTANT: Previous attempt hit output length limit. "
        "Keep your response compact — output ONLY essential fields, "
        "no verbose prose, and avoid repeating prior context.\n\n"
    )
    if "Previous attempt hit output length limit" not in reduced:
        reduced = header + reduced
    return reduced


def _parse_with_retry(
    step: StepConfig,
    prompt: str,
    raw_output: str,
    current_payload: Optional[Dict[str, Any]],
    call_kwargs: Dict[str, Any],
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Parse model output; on failure, retry with exponential backoff.

    Adaptive strategy:
    - If output looks truncated, attempt repair before full retry.
    - On subsequent retries, reduce prompt context to fit within token limits.
    - After max_retries exhausted, returns current_payload with _parse_failed flag
      so downstream code can distinguish failure from empty results.
    """
    # --- Fix 2.4: Try truncation repair before first parse attempt ---
    output_to_parse = raw_output
    if _is_truncated_output(raw_output):
        repaired = repair_truncated_json(raw_output)
        if repaired != raw_output:
            output_to_parse = repaired
            print(
                f"[INFO] Repaired truncated output for {step.name} "
                f"(balanced {raw_output.count('{')}/{raw_output.count('}')} braces)",
                file=sys.stderr, flush=True,
            )

    try:
        return parse_json_output(
            output_to_parse, list(step.expected_columns), previous_payload=current_payload,
        )
    except PipelineError as first_exc:
        last_exc = first_exc
        for attempt in range(max_retries):
            wait = 2 ** attempt
            print(
                f"[WARN] Parse failed for {step.name} (attempt {attempt + 1}/{max_retries}), "
                f"retrying in {wait}s: {last_exc}",
                file=sys.stderr, flush=True,
            )
            time.sleep(wait)
            try:
                # --- Fix 2.2: Reduce context on retries to avoid repeat truncation ---
                retry_prompt = _reduce_prompt_for_retry(prompt, attempt)
                retry_output, _ = call_gemini_model(step, retry_prompt, **call_kwargs)

                # Attempt repair if retry output is also truncated
                if _is_truncated_output(retry_output):
                    retry_output = repair_truncated_json(retry_output)

                return parse_json_output(
                    retry_output, list(step.expected_columns),
                    previous_payload=current_payload,
                )
            except (PipelineError, Exception) as exc:
                last_exc = exc

        # --- Fix 2.3: Mark payload as failed so downstream knows ---
        print(
            f"[ERROR] All {max_retries} retries failed for {step.name}: {last_exc}",
            file=sys.stderr, flush=True,
        )
        if current_payload is None:
            current_payload = {}
        current_payload["_parse_failed"] = True
        current_payload["_parse_failed_step"] = step.name
        current_payload.setdefault("_pipeline_metadata", {}).setdefault(
            "failed_steps", []
        ).append({"step": step.name, "error": str(last_exc)})
        return current_payload


def _get_all_indirect_interactors(ctx_json: dict) -> List[str]:
    """Return names of ALL indirect interactors (deduplicated, regardless of existing functions).

    Step 2b2 now ALWAYS runs for all indirect interactors, adding chain-context
    functions on top of whatever step 2a already found.
    """
    seen: set[str] = set()
    result: List[str] = []
    for i in ctx_json.get("interactors", []):
        name = i.get("primary")
        if name and i.get("interaction_type") == "indirect" and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _get_chained_needing_link_functions(ctx_json: dict) -> List[str]:
    """Return names of indirect interactors that need chain link functions.

    Reads chain state via the canonical ``ChainView`` — ``chain_context.
    full_chain`` is the source of truth, falling back to the denormalised
    columns only when ``ChainView`` can reconstruct from them.

    Silent. This function is called as a pre-chain-resolution diagnostic
    (see the ``[INDIRECT] N indirect interactors, M with chain data``
    log) where "M=0" is the normal state — step2ab hasn't run yet.
    Logging here would false-fire on every run. The only meaningful
    warning about a *consequential* skip lives in ``utils/db_sync.py``
    as ``[CHAIN HOP CLAIM MISSING]`` — that log fires when a chain hop
    actually reaches the DB write without a claim, which is the moment
    a user actually needs to know about a missing claim.
    """
    from utils.chain_view import ChainView

    existing_links = ctx_json.get("chain_link_functions", {})
    query = (ctx_json.get("main") or "").strip() or None
    results = []
    for i in ctx_json.get("interactors", []):
        if i.get("interaction_type") != "indirect":
            continue
        primary = i.get("primary")
        if not primary:
            continue
        view = ChainView.from_interaction_data(i, query_protein=query)
        chain = view.mediator_chain
        if not chain:
            continue
        # Check if any link pair for this interactor already exists
        has_links = any(
            primary in key or any(m in key for m in chain)
            for key in existing_links
        )
        if not has_links:
            results.append(primary)
    return results


# ── Chain resolution helpers ───────────────────────────────────────────────


def _parse_chain_string(chain) -> List[str]:
    """Parse a chain string like 'ATXN3 → ^Rheb^ → mTOR' into a list."""
    if isinstance(chain, list):
        return [p.strip().strip("^*") for p in chain]
    return [p.strip().strip("^*") for p in chain.replace("→", "->").split("->")]


def _validate_chain_intermediaries(
    main_query: str,
    interactor_name: str,
    intermediaries: List[str],
    existing_interactors: List[Dict[str, Any]],
) -> List[str]:
    """Clean up LLM-returned chain intermediaries.

    Chains can have the query in ANY position (head, middle, or tail) —
    this helper does NOT assume a fixed orientation. The only
    intermediary we unconditionally drop is the target ``interactor_name``
    itself, because listing the chain tail as its own mediator is a
    degenerate self-reference. Everything else (including the query and
    existing direct interactors) is valid: a chain like
    ``VCP → TDP43 → GRN`` where TDP43 is the query is a real biological
    structure and the pipeline must preserve it.

    ``existing_interactors`` is kept in the signature for API stability
    with callers / tests that pass it, but we no longer use it as a
    rejection filter — forcing existing directs out of chains was the
    bug that broke the query-in-middle case in the first place.

    Pure function — takes only plain data, returns a new list, no I/O.
    """
    if not intermediaries:
        return []

    target_upper = (interactor_name or "").upper()

    cleaned: List[str] = []
    for raw in intermediaries:
        if not isinstance(raw, str):
            continue
        sym = raw.strip().strip("^*")
        if not sym:
            continue
        # Only unconditional rejection: the target itself can't be a
        # mediator of a chain that ends at it. Everything else — query,
        # existing directs, anything — is a legitimate chain member.
        if sym.upper() == target_upper:
            continue
        cleaned.append(sym)
    return cleaned


def _run_single_chain_step(
    step: StepConfig,
    current_payload: Dict[str, Any],
    user_query: str,
    *,
    cancel_event=None,
    request_mode: Optional[str] = None,
    batch_poll_seconds: Optional[int] = None,
    batch_max_wait_seconds: Optional[int] = None,
    known_interactions: Optional[List[Dict[str, Any]]] = None,
    **_extra,  # absorb update_status, step_idx, total_steps from web kwargs
) -> Optional[Dict[str, Any]]:
    """Run a single lightweight chain step and return parsed result.

    Chain steps output custom schemas (chain_results, confirmations, etc.)
    NOT the standard ctx_json/step_json format. We parse the JSON directly
    instead of using _parse_with_retry which expects ctx_json/step_json.
    """
    from pipeline.context_builders import build_prompt
    from utils.json_helpers import strip_code_fences

    print(f"\n[CHAIN:{step.name}] Running...", file=sys.stderr, flush=True)
    prompt = build_prompt(step, current_payload, user_query, False,
                          known_interactions=known_interactions)
    try:
        raw_output, token_stats = call_gemini_model(
            step, prompt,
            cancel_event=cancel_event, request_mode=request_mode,
            batch_poll_seconds=batch_poll_seconds,
            batch_max_wait_seconds=batch_max_wait_seconds,
        )
    except PipelineError as exc:
        from utils.observability import log_event
        _PARSE_FAILURE_COUNTERS["call_error"] += 1
        log_event(
            "chain_step_call_error",
            level="warn",
            tag="CHAIN",
            step=step.name,
            error=str(exc),
            counters=dict(_PARSE_FAILURE_COUNTERS),
        )
        return None

    # Parse JSON directly — chain steps use custom schemas, not ctx_json/step_json
    try:
        cleaned = strip_code_fences(raw_output)
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        # Try to find a JSON object in the output
        try:
            start = cleaned.index("{")
            from utils.json_helpers import repair_truncated_json
            candidate = repair_truncated_json(cleaned[start:])
            parsed = json.loads(candidate)
        except (ValueError, json.JSONDecodeError) as exc:
            # Previously a silent ``print(...)``: the caller treated None
            # as "no result" and kept going, so flaky JSON parses were
            # invisible downstream. Now we emit a structured event and
            # bump a module-level counter so the run summary surfaces
            # how many steps silently dropped. Retry logic is
            # deliberately NOT added here — changing the control flow
            # on a flaky parse is riskier than signaling it clearly.
            from utils.observability import log_event
            _PARSE_FAILURE_COUNTERS["parse_failure"] += 1
            log_event(
                "chain_step_parse_failed",
                level="warn",
                tag="CHAIN",
                step=step.name,
                error=str(exc),
                response_preview=(cleaned or "")[:300],
                counters=dict(_PARSE_FAILURE_COUNTERS),
            )
            return None

    print(f"[CHAIN:{step.name}] Complete.", file=sys.stderr, flush=True)
    return parsed


def _run_track_a(
    payload: Dict[str, Any],
    user_query: str,
    **llm_kwargs,
) -> Dict[str, Any]:
    """Track A: Explicit indirect chain resolution (2ab → 2ab5 → ready for 2ax).

    1. Step 2ab (LLM): Determine chains for explicit indirects.
    2. Step 2ab5 code stage: Extract new pairs, check if new proteins already exist.
    3. Step 2ab5 LLM stage (conditional): Compare claims only if new protein
       already exists as a direct interactor.
    """
    from pipeline.prompts.deep_research_steps import (
        step2ab_chain_determination,
        step2ab5_extract_pairs_explicit,
    )
    from utils.chain_resolution import extract_new_pairs_with_existing_check

    ctx = payload.setdefault("ctx_json", {})

    # ── 2ab: Determine chains for explicit indirects (LLM, batched 6 at a time) ──
    indirect_interactors = [
        i for i in ctx.get("interactors", [])
        if i.get("interaction_type") == "indirect"
    ]
    if not indirect_interactors:
        print("[TRACK-A] No indirect interactors — skipping", file=sys.stderr, flush=True)
        ctx["_chain_annotations_explicit"] = []
        return payload

    # Batch indirect claims 6 at a time per the user's spec
    _BATCH_SIZE = 6
    chain_annotations = []
    indirect_names = [i.get("primary", "") for i in indirect_interactors]

    for batch_start in range(0, len(indirect_names), _BATCH_SIZE):
        batch = indirect_names[batch_start:batch_start + _BATCH_SIZE]
        print(
            f"[TRACK-A:2ab] Batch {batch_start // _BATCH_SIZE + 1}: "
            f"{len(batch)} indirects ({', '.join(batch)})",
            file=sys.stderr, flush=True,
        )

        # Inject batch directive into payload so the prompt scopes to these indirects
        ctx["_2ab_batch_directive"] = (
            f"Process ONLY these {len(batch)} indirect interactors:\n"
            + ", ".join(batch)
            + "\nDo NOT process interactors outside this list."
        )

        step_2ab = step2ab_chain_determination()
        result_2ab = _run_single_chain_step(step_2ab, payload, user_query, **llm_kwargs)

        if result_2ab:
            batch_chains = (
                result_2ab.get("chain_results")
                or result_2ab.get("ctx_json", {}).get("chain_results", [])
            )
            chain_annotations.extend(batch_chains)

    ctx.pop("_2ab_batch_directive", None)  # Clean up
    ctx["_chain_annotations_explicit"] = chain_annotations

    # Validate + apply chain annotations to their matching interactors.
    #
    # Pipeline:
    #   1. Parse the chain string from the LLM annotation
    #   2. Run it through ``validate_chain_on_ingest`` (strips markers,
    #      dedupes consecutive repeats, warns on
    #      missing query)
    #   3. Build a single ``ChainView`` from the cleaned chain
    #   4. Call ``ChainView.apply_to_dict`` — this is the SINGLE write
    #      surface that sets ``mediator_chain`` / ``depth`` /
    #      ``upstream_interactor`` / ``chain_context`` from one source.
    #      No more setting fields independently and praying they stay
    #      in sync.
    from utils.chain_resolution import validate_chain_on_ingest
    from utils.chain_view import ChainView

    main_query = ctx.get("main", "") or user_query or ""
    existing_interactors = ctx.get("interactors", [])
    for entry in chain_annotations:
        interactor_name = entry.get("interactor", "")
        if not interactor_name:
            continue

        raw_chain = _parse_chain_string(entry.get("chain", []))
        cleaned_chain, errors = validate_chain_on_ingest(
            raw_chain, query_protein=main_query,
        )
        if errors:
            print(
                f"[TRACK-A:2ab] Chain for interactor '{interactor_name}': "
                f"{', '.join(errors)}",
                file=sys.stderr, flush=True,
            )
        if len(cleaned_chain) < 2:
            continue
        if interactor_name.upper() not in (p.upper() for p in cleaned_chain):
            print(
                f"[TRACK-A:2ab] Chain for '{interactor_name}' does not "
                f"include the target — skipping: {cleaned_chain}",
                file=sys.stderr, flush=True,
            )
            continue

        # Build the canonical ChainView and apply it to every matching
        # interactor in one call.
        chain_view = ChainView.from_full_chain(
            cleaned_chain, query_protein=main_query,
        )
        for interactor in existing_interactors:
            if interactor.get("primary", "").upper() == interactor_name.upper():
                chain_view.apply_to_dict(interactor)

    if not chain_annotations:
        print("[TRACK-A] No chains resolved — skipping 2ab5", file=sys.stderr, flush=True)
        return payload

    # ── 2ab5 code stage: Extract new pairs from chains ──
    main = ctx.get("main", "")
    existing_interactors = ctx.get("interactors", [])
    explicit_pairs_data = []  # Store per-chain pair extraction results
    needs_llm_comparison = False

    for entry in chain_annotations:
        chain = _parse_chain_string(entry.get("chain", []))
        intermediaries = entry.get("intermediaries", [])
        interactor_name = entry.get("interactor", "")
        if len(chain) < 2:
            continue

        # The original pair is (query, indirect_target)
        original_pair = (main, interactor_name)
        pair_result = extract_new_pairs_with_existing_check(
            chain, original_pair, existing_interactors,
        )
        pair_result["source_chain_entry"] = entry
        explicit_pairs_data.append(pair_result)

        if pair_result.get("new_protein_already_exists"):
            needs_llm_comparison = True

    ctx["_explicit_pairs_data"] = explicit_pairs_data

    print(
        f"[TRACK-A:2ab5-code] Extracted pairs from {len(chain_annotations)} chains, "
        f"LLM comparison needed: {needs_llm_comparison}",
        file=sys.stderr, flush=True,
    )

    # ── 2ab5 LLM stage (conditional): Compare claims for duplicate detection ──
    if needs_llm_comparison:
        step_2ab5 = step2ab5_extract_pairs_explicit()
        result_2ab5 = _run_single_chain_step(step_2ab5, payload, user_query, **llm_kwargs)
        if result_2ab5:
            matches = (
                result_2ab5.get("match_results")
                or result_2ab5.get("ctx_json", {}).get("match_results", [])
            )
            ctx["_claim_v_matches"] = [m for m in matches if m.get("match_found")]
        else:
            ctx["_claim_v_matches"] = []
    else:
        ctx["_claim_v_matches"] = []

    return payload


def _run_track_b(
    payload: Dict[str, Any],
    user_query: str,
    **llm_kwargs,
) -> Dict[str, Any]:
    """Track B: Hidden indirect chain resolution (2ab2 → 2ab3 → 2ab4 → ready for 2az).

    1. Step 2ab2 code stage: Extract candidate proteins from direct claims.
    2. Step 2ab2 LLM stage: Confirm candidates are genuinely implicated.
    3. Step 2ab3 (LLM): Determine chain positions for confirmed candidates.
    4. Step 2ab4 (pure code): Extract new interaction pairs from chains.
    """
    from pipeline.prompts.deep_research_steps import (
        step2ab2_hidden_indirect_detection,
        step2ab3_hidden_chain_determination,
    )
    from utils.chain_resolution import (
        extract_candidates_from_payload,
        extract_new_interaction_pairs,
    )

    ctx = payload.setdefault("ctx_json", {})

    # ── 2ab2 code stage: Extract candidate proteins from direct claims ──
    candidates = extract_candidates_from_payload(payload)
    if not candidates:
        print("[TRACK-B] No hidden indirect candidates found — skipping", file=sys.stderr, flush=True)
        ctx["_hidden_indirect_candidates"] = []
        ctx["_chain_annotations_hidden"] = []
        ctx["_hidden_pairs_data"] = []
        return payload

    print(
        f"[TRACK-B:2ab2-code] Found {len(candidates)} candidate claims with non-pair proteins",
        file=sys.stderr, flush=True,
    )

    # Inject candidates into payload for the LLM to confirm
    ctx["_2ab2_code_candidates"] = candidates

    # ── 2ab2 LLM stage: Confirm candidates ──
    step_2ab2 = step2ab2_hidden_indirect_detection()
    result_2ab2 = _run_single_chain_step(step_2ab2, payload, user_query, **llm_kwargs)

    confirmed = []
    if result_2ab2:
        confirmations = (
            result_2ab2.get("confirmations")
            or result_2ab2.get("ctx_json", {}).get("confirmations", [])
        )
        confirmed = [c for c in confirmations if c.get("confirmed") or c.get("chain_potential")]

    ctx["_hidden_indirect_candidates"] = confirmed

    if not confirmed:
        print("[TRACK-B] No candidates confirmed — skipping 2ab3/2ab4", file=sys.stderr, flush=True)
        ctx["_chain_annotations_hidden"] = []
        ctx["_hidden_pairs_data"] = []
        return payload

    print(
        f"[TRACK-B:2ab2-llm] Confirmed {len(confirmed)} hidden indirect candidates",
        file=sys.stderr, flush=True,
    )

    # ── 2ab3 (LLM): Determine chain positions for confirmed candidates ──
    step_2ab3 = step2ab3_hidden_chain_determination()
    result_2ab3 = _run_single_chain_step(step_2ab3, payload, user_query, **llm_kwargs)

    hidden_chains = []
    if result_2ab3:
        hidden_chains = (
            result_2ab3.get("chain_results")
            or result_2ab3.get("ctx_json", {}).get("chain_results", [])
        )
    ctx["_chain_annotations_hidden"] = hidden_chains

    if not hidden_chains:
        print("[TRACK-B] No chains resolved — skipping 2ab4", file=sys.stderr, flush=True)
        ctx["_hidden_pairs_data"] = []
        return payload

    # ── 2ab4 (pure code): Extract new interaction pairs from chains ──
    # Every hidden chain is put through ``validate_chain_on_ingest`` so
    # chains that contain duplicates or lack the query get cleaned or
    # skipped before they reach the pair extractor.
    from utils.chain_resolution import validate_chain_on_ingest

    main = ctx.get("main", "")
    hidden_pairs_data = []
    for entry in hidden_chains:
        raw_chain = _parse_chain_string(entry.get("chain", []))
        interactor_name = entry.get("interactor", "")
        if not interactor_name:
            continue
        cleaned_chain, errors = validate_chain_on_ingest(
            raw_chain, query_protein=main,
        )
        if errors:
            print(
                f"[TRACK-B:2ab3] Chain for '{interactor_name}': "
                f"{', '.join(errors)}",
                file=sys.stderr, flush=True,
            )
        if len(cleaned_chain) < 2:
            continue
        # Write the cleaned chain back into the entry so downstream
        # consumers (promotion, post_processor chain_group tagging) read
        # the validated version rather than the raw LLM output.
        entry["chain"] = " -> ".join(cleaned_chain)
        entry["intermediaries"] = [
            p for p in cleaned_chain
            if p.upper() != main.upper() and p.upper() != interactor_name.upper()
        ]

        original_pair = (main, interactor_name)
        pair_result = extract_new_interaction_pairs(cleaned_chain, original_pair)
        pair_result["source_chain_entry"] = entry
        hidden_pairs_data.append(pair_result)

    ctx["_hidden_pairs_data"] = hidden_pairs_data

    # Apply hidden-chain ChainView to every interactor the chain references,
    # whether it's pre-existing (from broad_discovery) or about to be promoted.
    # Track A does this at lines 811-818 for explicit chains; this is parity
    # for Track B so hidden-chain targets get chain_context populated. Without
    # this, the downstream _get_chain_claim_targets enumerator reads an empty
    # ChainView for hidden-chain targets and skips every hop past the first —
    # which is exactly the [CHAIN HOP CLAIM MISSING] failure mode.
    from utils.chain_view import ChainView as _ChainView_B
    main_query_b = ctx.get("main", "") or ""
    for entry in hidden_chains:
        interactor_name = (entry.get("interactor") or "").strip()
        if not interactor_name:
            continue
        raw_chain = _parse_chain_string(entry.get("chain", []))
        cleaned_chain, _errors = validate_chain_on_ingest(
            raw_chain, query_protein=main_query_b,
        )
        if len(cleaned_chain) < 2:
            continue
        view_b = _ChainView_B.from_full_chain(
            cleaned_chain, query_protein=main_query_b,
        )
        # Apply to every interactor named in the chain (query + mediators +
        # target). Each one may be a future hop's source or target, so all of
        # them benefit from having chain_context populated.
        target_names = {p.strip().upper() for p in cleaned_chain if p}
        target_names.discard((main_query_b or "").strip().upper())
        for interactor in ctx.get("interactors", []):
            if (interactor.get("primary", "") or "").strip().upper() in target_names:
                view_b.apply_to_dict(interactor)

    print(
        f"[TRACK-B:2ab4-code] Extracted pairs from {len(hidden_chains)} hidden chains",
        file=sys.stderr, flush=True,
    )

    return payload


def _promote_chain_interactors(payload: Dict[str, Any]) -> List[str]:
    """Promote new proteins discovered by chain resolution into the interactors array.

    Scans _explicit_pairs_data and _hidden_pairs_data for new proteins and
    adds them as interactors (direct or indirect) so 2ax/2az can generate
    claims for them.

    Returns list of newly promoted interactor names.
    """
    from utils.chain_view import ChainView

    ctx = payload.get("ctx_json", {})
    interactors = ctx.get("interactors", [])
    existing = {i.get("primary", "").upper() for i in interactors}
    main_query = ctx.get("main") or ""
    main_upper = main_query.upper()
    promoted: List[str] = []

    def _add_indirect(name: str, full_chain: List[str]) -> None:
        """Promote ``name`` as a new indirect interactor whose chain is
        ``full_chain``. All four chain fields (mediator_chain, depth,
        upstream_interactor, chain_context) come from a single ChainView
        so they cannot drift.
        """
        if name.upper() in existing or name.upper() == main_upper:
            return
        new_dict: Dict[str, Any] = {
            "primary": name,
            "interaction_type": "indirect",
            "support_summary": "Discovered via chain resolution",
            "functions": [],
        }
        ChainView.from_full_chain(
            full_chain, query_protein=main_query,
        ).apply_to_dict(new_dict)
        interactors.append(new_dict)
        existing.add(name.upper())
        promoted.append(name)
        history = ctx.setdefault("interactor_history", [])
        if name not in history:
            history.append(name)

    def _add_direct(name: str) -> None:
        """Promote ``name`` as a new direct interactor (no chain)."""
        if name.upper() in existing or name.upper() == main_upper:
            return
        interactors.append({
            "primary": name,
            "interaction_type": "direct",
            "upstream_interactor": None,
            "mediator_chain": [],
            "depth": 1,
            "support_summary": "Discovered via chain resolution",
            "functions": [],
        })
        existing.add(name.upper())
        promoted.append(name)
        history = ctx.setdefault("interactor_history", [])
        if name not in history:
            history.append(name)

    # Process INDIRECTS first — the relationship to the QUERY protein takes
    # priority over chain-link relationships.  For chain ATXN3→VCP→RNF8:
    #   - ATXN3→RNF8 is INDIRECT (this is what the interactor entry should reflect)
    #   - VCP→RNF8 is a chain link (doesn't need its own interactor entry)
    for pairs_list_key in ("_explicit_pairs_data", "_hidden_pairs_data"):
        for pair_result in ctx.get(pairs_list_key, []):
            new_prot = pair_result.get("new_protein", "")
            if not new_prot:
                continue
            for ind in pair_result.get("new_indirects", []):
                chain = ind.get("chain", [])
                if not isinstance(chain, list) or len(chain) < 2:
                    continue
                _add_indirect(new_prot, chain)
            # Only add as direct if no indirect relationship was established
            if new_prot.upper() not in existing:
                for d in pair_result.get("new_directs", []):
                    _add_direct(new_prot)

    if promoted:
        print(
            f"[CHAIN:promote] Promoted {len(promoted)} new interactors: {promoted}",
            file=sys.stderr, flush=True,
        )
    return promoted


def _run_chain_resolution_phase(
    payload: Dict[str, Any],
    user_query: str,
    **llm_kwargs,
) -> Dict[str, Any]:
    """Run the full chain resolution phase (Track A + Track B in parallel).

    This replaces the old steps 2b/2b2/2b3/2b4. After both tracks complete,
    new interactors are promoted so 2ax/2az can generate claims for them.
    """
    print(
        f"\n{'='*70}\n"
        f"[CHAIN RESOLUTION] Starting (Track A + Track B in parallel)\n"
        f"{'='*70}",
        file=sys.stderr, flush=True,
    )

    # Deep-copy payload for each track so they don't interfere
    payload_a = deepcopy(payload)
    payload_b = deepcopy(payload)

    # Run both tracks in parallel
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(_run_track_a, payload_a, user_query, **llm_kwargs)
        future_b = executor.submit(_run_track_b, payload_b, user_query, **llm_kwargs)
        payload_a = future_a.result()
        payload_b = future_b.result()

    # Merge track results back into the main payload
    ctx = payload.setdefault("ctx_json", {})
    ctx_a = payload_a.get("ctx_json", {})
    ctx_b = payload_b.get("ctx_json", {})

    # Track A results
    ctx["_chain_annotations_explicit"] = ctx_a.get("_chain_annotations_explicit", [])
    ctx["_explicit_pairs_data"] = ctx_a.get("_explicit_pairs_data", [])
    ctx["_claim_v_matches"] = ctx_a.get("_claim_v_matches", [])
    # PR-3d: normalize-before-compare so "Parkin" in one track and "PRKN"
    # in the other resolve to the same interactor. Raw `==` missed these
    # and silently duplicated the entry.
    def _sym_key(s: str) -> str:
        return (s or "").strip().upper()

    # Merge chain state from Track A's deep-copied ctx back into the main
    # payload. Track A calls ChainView.apply_to_dict() on each interactor
    # it resolves, which writes all four chain fields (chain_context,
    # mediator_chain, upstream_interactor, depth) atomically. We read the
    # full state back through ChainView so we never write a partial mix.
    # The old merge copied only mediator_chain + depth, which left
    # chain_context.full_chain empty on the main ctx's original indirects
    # and caused _reconcile_chain_fields to wrongly flag them as
    # [CHAIN BUG]s even though Track A had resolved their cascades.
    from utils.chain_view import ChainView as _ChainView
    _main_query_for_merge = ctx.get("main") or ""
    for i_a in ctx_a.get("interactors", []):
        key_a = _sym_key(i_a.get("primary"))
        if not key_a:
            continue
        view = _ChainView.from_interaction_data(
            i_a, query_protein=_main_query_for_merge or None,
        )
        if view.is_empty:
            continue
        for i_main in ctx.get("interactors", []):
            if _sym_key(i_main.get("primary")) == key_a:
                view.apply_to_dict(i_main)

    # Track B results
    ctx["_hidden_indirect_candidates"] = ctx_b.get("_hidden_indirect_candidates", [])
    ctx["_chain_annotations_hidden"] = ctx_b.get("_chain_annotations_hidden", [])
    ctx["_hidden_pairs_data"] = ctx_b.get("_hidden_pairs_data", [])
    ctx["_2ab2_code_candidates"] = ctx_b.get("_2ab2_code_candidates", [])

    # Symmetric merge for Track B's interactors: pick up chain_context /
    # mediator_chain / upstream_interactor / depth from Track B's deep-copied
    # payload back into the main ctx. Track A has this at lines 1146-1157;
    # without the parity code here, hidden-chain targets that Track B's
    # fix-1.2 block annotated end up back in an interactor entry without
    # chain_context in the main ctx, and _get_chain_claim_targets can't see
    # the chain.
    for i_b in ctx_b.get("interactors", []):
        key_b = _sym_key(i_b.get("primary"))
        if not key_b:
            continue
        view = _ChainView.from_interaction_data(
            i_b, query_protein=_main_query_for_merge or None,
        )
        if view.is_empty:
            continue
        for i_main in ctx.get("interactors", []):
            if _sym_key(i_main.get("primary")) == key_b:
                view.apply_to_dict(i_main)

    # Promote newly discovered interactors so 2ax/2az can target them
    promoted = _promote_chain_interactors(payload)

    # "Promoted: N" counts NEW interactors added to ctx.interactors[],
    # not chains themselves. Most hidden chains confirm relationships
    # whose endpoints are ALREADY known interactors — in that case no
    # promotion happens (the chain is still valid, just not new). The
    # phrasing below makes that distinction explicit so the log doesn't
    # read as "16 of 19 chains were rejected".
    n_hidden = len(ctx.get('_chain_annotations_hidden', []))
    n_explicit = len(ctx.get('_chain_annotations_explicit', []))
    n_promoted = len(promoted)
    n_reconfirmed = max(0, n_hidden + n_explicit - n_promoted)
    print(
        f"\n{'='*70}\n"
        f"[CHAIN RESOLUTION] Complete. "
        f"Explicit chains: {n_explicit}, "
        f"Hidden chains: {n_hidden}. "
        f"New interactors promoted to ctx.interactors[]: {n_promoted} "
        f"(remaining {n_reconfirmed} chain(s) re-confirmed already-known interactors).\n"
        f"{'='*70}",
        file=sys.stderr, flush=True,
    )

    return payload, promoted


def _enforce_chain_pathway_consistency(payload: Dict[str, Any]) -> None:
    """DEPRECATED: Chain claims now get independent pathway assignments.

    Previously forced all claims in the same chain to share one dominant
    pathway, but this caused scientifically incorrect assignments (e.g.
    all links in PERK->STING1->TBK1->IRF3 forced to 'Protein Stability').
    """
    return


def _relocate_flat_chain_hop_functions(payload: Dict[str, Any]) -> int:
    """Move chain-hop functions from flat ``functions[]`` to ``chain_link_functions[pair]``.

    The 2ax/2az prompts instruct the LLM to emit per-hop claims under
    ``chain_link_functions["SOURCE->TARGET"]``. When the LLM drifts — or
    a future prompt-rev forgets — and emits them on the target
    interactor's flat ``functions[]`` list instead, db_sync silently
    drops them and every chain-hop modal rehydrates from the parent
    direct interaction. This is the bug that produced "16 claims for 73
    interactions" in the TDP43 run.

    Root fix for the LLM output shape lives in the prompt. This helper
    is the defensive net: scan each interactor's flat functions list
    for entries whose ``function_context == 'chain_derived'`` or whose
    target participates in a pair carried on ``ctx_json._chain_pair_context``
    (the per-batch chain map populated by ``_get_chain_claim_targets``),
    and move them into the nested slot keyed by ``"SOURCE->TARGET"``.

    Returns the number of relocated functions (0 when nothing moves).
    """
    ctx = payload.get("ctx_json") or {}
    pair_ctx = ctx.get("_chain_pair_context") or {}
    if not pair_ctx:
        return 0

    # Build target → list[source] index from the pair context.
    # "VCP->NPLOC4" becomes index entry NPLOC4 -> [("VCP->NPLOC4", "VCP")].
    targets_to_pairs: Dict[str, List[tuple]] = {}
    for pair_key in pair_ctx.keys():
        if "->" not in pair_key:
            continue
        src, tgt = pair_key.split("->", 1)
        src_u = src.strip().upper()
        tgt_u = tgt.strip().upper()
        if not src_u or not tgt_u:
            continue
        targets_to_pairs.setdefault(tgt_u, []).append((pair_key, src_u))

    if not targets_to_pairs:
        return 0

    relocated = 0
    for interactor in ctx.get("interactors", []):
        primary = (interactor.get("primary") or "").strip().upper()
        pair_candidates = targets_to_pairs.get(primary)
        if not pair_candidates:
            continue

        flat_funcs = interactor.get("functions") or []
        if not flat_funcs:
            continue

        clf = interactor.setdefault("chain_link_functions", {})
        kept_flat: List[Dict[str, Any]] = []

        for fn in flat_funcs:
            if not isinstance(fn, dict):
                kept_flat.append(fn)
                continue

            ctx_label = str(fn.get("function_context") or "").strip().lower()
            is_chain_derived = ctx_label == "chain_derived"
            # Atom B: when the LLM drifts and omits ``function_context``
            # (common Gemini failure mode), a flat function on a known
            # hop target is still almost certainly a chain claim — the
            # 2ax/2az prompt only asked for claims on pair targets, so
            # any flat claim on a target that matches a pair_candidate
            # belongs in the nested slot. "direct"-labeled claims are
            # left flat (genuine non-chain direct biology).
            is_unlabeled_on_hop_target = (
                ctx_label == "" and bool(pair_candidates)
            )

            # Default pair: the first known pair pointing at this target.
            # A function that's truly chain-derived always fits one of the
            # active hops — single-target chains are the common case.
            pair_key_to_use = None
            if is_chain_derived or is_unlabeled_on_hop_target:
                pair_key_to_use = pair_candidates[0][0]

            if pair_key_to_use is None:
                kept_flat.append(fn)
                continue

            # Tag the relocated function with chain_derived so downstream
            # db_sync and the locus router treat it consistently. Without
            # this, unlabeled-relocated claims would re-appear in the
            # locus router as "no context" and be harder to classify.
            if is_unlabeled_on_hop_target:
                fn["function_context"] = "chain_derived"
                fn.setdefault("_relocated_unlabeled", True)

            clf.setdefault(pair_key_to_use, []).append(fn)
            relocated += 1

        if relocated:
            interactor["functions"] = kept_flat

    if relocated:
        print(
            f"   [MERGE] Relocated {relocated} flat chain-derived function(s) "
            f"into chain_link_functions — this indicates the LLM emitted them "
            f"on the flat functions[] list instead of the nested slot.",
            file=sys.stderr, flush=True,
        )
    return relocated


def _generated_chain_pair_keys(ctx_json: Dict[str, Any]) -> set:
    """Return canonical pair keys that already have chain_link_functions."""
    from utils.chain_resolution import canonical_pair_key as _canon_pair_key

    generated: set = set()
    for interactor in ctx_json.get("interactors", []) or []:
        if not isinstance(interactor, dict):
            continue
        chain_link_map = interactor.get("chain_link_functions") or {}
        if not isinstance(chain_link_map, dict):
            continue
        for pair_key, funcs in chain_link_map.items():
            if not funcs or not isinstance(pair_key, str):
                continue
            if "->" in pair_key:
                src, tgt = pair_key.split("->", 1)
                generated.add(_canon_pair_key(src, tgt))
            elif "|" in pair_key:
                left, right = pair_key.split("|", 1)
                generated.add(_canon_pair_key(left, right))
    return generated


def _missing_chain_claim_pairs(
    ctx_json: Dict[str, Any],
    target_pairs: List[str],
) -> List[str]:
    """Target pairs whose nested chain_link_functions are still empty."""
    from utils.chain_resolution import canonical_pair_key as _canon_pair_key

    generated = _generated_chain_pair_keys(ctx_json)
    missing: List[str] = []
    seen: set = set()
    for pair in target_pairs or []:
        if not isinstance(pair, str) or "->" not in pair:
            continue
        src, tgt = pair.split("->", 1)
        sig = _canon_pair_key(src, tgt)
        if sig in generated or sig in seen:
            continue
        seen.add(sig)
        missing.append(pair)
    return missing


def _shallow_chain_hop_pairs(ctx_json: Dict[str, Any]) -> List[str]:
    """Return ``"src->tgt"`` for chain_link_functions hops with depth issues.

    R1 — chain-hop counterpart of ``_shallow_interactor_names``. Walks
    every interactor's ``chain_link_functions`` dict, looks for any
    function dict that carries ``_depth_issues`` (set by
    ``utils.quality_validator.validate_payload_depth`` after P2.2), and
    returns the unique source/target pair strings the chain-claim
    redispatch should expand. The pair format matches what
    ``step2ax_claim_generation_explicit`` expects.

    Pair keys can be either canonical (``SRC|TGT``) or legacy
    directional (``SRC->TGT``); both shapes are handled. Self-hops are
    skipped defensively.
    """
    pairs: List[str] = []
    seen: set = set()
    for inter in ctx_json.get("interactors", []) or []:
        if not isinstance(inter, dict):
            continue
        clf = inter.get("chain_link_functions") or {}
        if not isinstance(clf, dict):
            continue
        for pair_key, funcs in clf.items():
            if not isinstance(pair_key, str) or not isinstance(funcs, list):
                continue
            src = tgt = None
            if "|" in pair_key:
                parts = pair_key.split("|", 1)
                if len(parts) == 2:
                    src, tgt = parts
            elif "->" in pair_key:
                parts = pair_key.split("->", 1)
                if len(parts) == 2:
                    src, tgt = parts
            if not src or not tgt or src == tgt:
                continue
            has_shallow = any(
                isinstance(f, dict) and f.get("_depth_issues")
                for f in funcs
            )
            if not has_shallow:
                continue
            sig = (src.upper().strip(), tgt.upper().strip())
            if sig in seen:
                continue
            seen.add(sig)
            pairs.append(f"{src}->{tgt}")
    return pairs


def _extract_json_dict_segments(text: str) -> List[Dict[str, Any]]:
    """Return JSON dict objects found in an LLM response."""
    cleaned = strip_code_fences(text or "")
    decoder = json.JSONDecoder()
    idx = 0
    segments: List[Dict[str, Any]] = []
    while idx < len(cleaned):
        try:
            obj, end_idx = decoder.raw_decode(cleaned, idx)
            idx = end_idx
            if isinstance(obj, dict):
                segments.append(obj)
            elif isinstance(obj, list):
                segments.extend(item for item in obj if isinstance(item, dict))
        except json.JSONDecodeError:
            idx += 1

    if segments:
        return segments

    for pos, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            repaired = repair_truncated_json(cleaned[pos:])
            obj = json.loads(repaired)
            if isinstance(obj, dict):
                return [obj]
            if isinstance(obj, list):
                return [item for item in obj if isinstance(item, dict)]
        except Exception:
            continue
    return []


def _chain_claim_records_from_obj(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract pair-keyed chain-claim records from modern or legacy shapes."""
    if not isinstance(obj, dict):
        return []

    containers: List[Any] = []
    for key in ("chain_claims", "claims", "pairs"):
        if key in obj:
            containers.append(obj.get(key))
    ctx = obj.get("ctx_json")
    if isinstance(ctx, dict):
        for key in ("chain_claims", "claims", "pairs"):
            if key in ctx:
                containers.append(ctx.get(key))
        for interactor in ctx.get("interactors", []) or []:
            if not isinstance(interactor, dict):
                continue
            clf = interactor.get("chain_link_functions") or {}
            if not isinstance(clf, dict):
                continue
            target = interactor.get("primary") or ""
            for pair, funcs in clf.items():
                containers.append([{
                    "pair": pair,
                    "target": target,
                    "functions": funcs,
                }])

    if obj.get("pair") and (obj.get("functions") is not None or obj.get("function")):
        containers.append([obj])

    records: List[Dict[str, Any]] = []
    for container in containers:
        if isinstance(container, dict):
            for pair, value in container.items():
                if isinstance(value, dict) and (
                    value.get("functions") is not None or value.get("function")
                ):
                    rec = dict(value)
                    rec.setdefault("pair", pair)
                    records.append(rec)
                else:
                    records.append({"pair": pair, "functions": value})
        elif isinstance(container, list):
            for item in container:
                if isinstance(item, dict):
                    records.append(item)
    return records


def _thin_chain_claim_function(pair: str) -> Dict[str, Any]:
    src, tgt = pair.split("->", 1) if "->" in pair else ("SOURCE", "TARGET")
    return {
        "function": "Pair biology not characterized in the cascade context",
        "function_context": "chain_derived",
        "arrow": "regulates",
        "cellular_process": (
            f"No peer-reviewed characterization of the {src}-{tgt} "
            "interaction in this cascade context could be retrieved. The hop "
            "is retained as part of the resolved cascade topology."
        ),
        "effect_description": (
            "Pair role within the cascade is topologically inferred rather "
            "than mechanistically characterized."
        ),
        "biological_consequence": "",
        "specific_effects": [],
        "evidence": [],
        "pathway": "",
        "_thin_claim": True,
    }


def _normalize_chain_claim_function(fn: Any, pair: str) -> Dict[str, Any]:
    if not isinstance(fn, dict):
        return _thin_chain_claim_function(pair)
    normalized = deepcopy(fn)
    if not str(normalized.get("function") or "").strip():
        normalized["function"] = "Pair biology not characterized in the cascade context"
        normalized["_thin_claim"] = True
    normalized["function_context"] = "chain_derived"
    normalized.setdefault("arrow", "regulates")
    normalized.setdefault("cellular_process", "")
    normalized.setdefault("effect_description", "")
    normalized.setdefault("biological_consequence", "")
    if not isinstance(normalized.get("specific_effects"), list):
        normalized["specific_effects"] = []
    if not isinstance(normalized.get("evidence"), list):
        normalized["evidence"] = []
    normalized.setdefault("pathway", "")
    return normalized


def _attach_chain_claim_records(
    payload: Dict[str, Any],
    records: List[Dict[str, Any]],
    requested_pairs: Optional[List[str]] = None,
    *,
    phase_name: str = "",
) -> int:
    """Attach pair-keyed chain claims into ctx_json.chain_link_functions."""
    from utils.chain_resolution import canonical_pair_key as _canon_pair_key

    ctx = payload.setdefault("ctx_json", {})
    interactors = ctx.setdefault("interactors", [])
    if not isinstance(interactors, list):
        ctx["interactors"] = []
        interactors = ctx["interactors"]
    main_symbol = str(ctx.get("main") or "").strip().upper()

    requested_pairs = [p for p in (requested_pairs or []) if isinstance(p, str)]
    requested_exact = {p.strip(): p.strip() for p in requested_pairs if "->" in p}
    requested_by_canon: Dict[str, str] = {}
    for pair in requested_exact:
        src, tgt = pair.split("->", 1)
        requested_by_canon[_canon_pair_key(src, tgt)] = pair

    by_primary = {
        str((i or {}).get("primary") or "").strip().upper(): i
        for i in interactors
        if isinstance(i, dict)
    }
    chain_owners_by_pair: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    try:
        from utils.chain_view import ChainView

        for owner in interactors:
            if not isinstance(owner, dict):
                continue
            owner_primary = str(owner.get("primary") or "").strip().upper()
            if main_symbol and owner_primary == main_symbol:
                continue
            view = ChainView.from_interaction_data(
                owner,
                query_protein=ctx.get("main") or None,
            )
            if view.is_empty:
                continue
            chain = list(view.full_chain)
            for idx in range(len(chain) - 1):
                source = str(chain[idx]).strip()
                target = str(chain[idx + 1]).strip()
                if not source or not target or source.upper() == target.upper():
                    continue
                exact_key = f"{source}->{target}"
                canon_key = _canon_pair_key(source, target)
                if owner not in chain_owners_by_pair[exact_key]:
                    chain_owners_by_pair[exact_key].append(owner)
                if owner not in chain_owners_by_pair[canon_key]:
                    chain_owners_by_pair[canon_key].append(owner)
    except Exception:
        chain_owners_by_pair = defaultdict(list)

    attached = 0
    attached_pairs: set[str] = set()

    def _attach_to_interactor(
        interactor: Dict[str, Any],
        pair: str,
        functions: List[Dict[str, Any]],
    ) -> int:
        if not isinstance(interactor, dict):
            return 0

        clf = interactor.setdefault("chain_link_functions", {})
        if not isinstance(clf, dict):
            clf = {}
            interactor["chain_link_functions"] = clf
        existing = clf.setdefault(pair, [])
        if not isinstance(existing, list):
            existing = []
            clf[pair] = existing

        existing_names = {
            str((fn or {}).get("function") or "").strip().lower()
            for fn in existing
            if isinstance(fn, dict)
        }
        added = 0
        for fn in functions:
            name_key = str(fn.get("function") or "").strip().lower()
            if name_key and name_key in existing_names:
                continue
            existing.append(deepcopy(fn))
            if name_key:
                existing_names.add(name_key)
            added += 1
        return added

    for record in records or []:
        if not isinstance(record, dict):
            continue
        pair = str(record.get("pair") or record.get("pair_key") or "").strip()
        if not pair and len(requested_pairs) == 1:
            pair = requested_pairs[0]
        if "->" not in pair:
            continue
        src, tgt = [part.strip() for part in pair.split("->", 1)]
        if not src or not tgt or src.upper() == tgt.upper():
            continue

        canonical = _canon_pair_key(src, tgt)
        if requested_by_canon:
            expected_pair = requested_by_canon.get(canonical)
            if not expected_pair:
                continue
            pair = expected_pair
            src, tgt = [part.strip() for part in pair.split("->", 1)]

        target = str(record.get("target") or tgt).strip() or tgt
        # Trust the requested pair's target over an LLM-provided target that
        # drifted; the pair key is the routing contract.
        target = tgt
        target_key = target.upper()
        target_interactor = by_primary.get(target_key)
        if target_interactor is None and target_key != main_symbol:
            target_interactor = {
                "primary": target,
                "interaction_type": "direct",
                "functions": [],
            }
            interactors.append(target_interactor)
            by_primary[target_key] = target_interactor

        raw_functions = record.get("functions")
        if raw_functions is None and record.get("function"):
            raw_functions = [record]
        if not isinstance(raw_functions, list):
            raw_functions = []
        if not raw_functions:
            raw_functions = [_thin_chain_claim_function(pair)]

        functions = [
            _normalize_chain_claim_function(fn, pair)
            for fn in raw_functions
        ]
        if not functions:
            functions = [_thin_chain_claim_function(pair)]

        destinations: List[Dict[str, Any]] = []
        if target_interactor is not None:
            destinations.append(target_interactor)

        owner_candidates = list(chain_owners_by_pair.get(pair, []))
        owner_candidates.extend(chain_owners_by_pair.get(_canon_pair_key(src, tgt), []))
        for owner in owner_candidates:
            if owner not in destinations:
                destinations.append(owner)

        pair_attached = 0
        for destination in destinations:
            pair_attached += _attach_to_interactor(destination, pair, functions)
        attached += pair_attached
        if pair_attached > 0:
            attached_pairs.add(pair)

    if phase_name and requested_pairs:
        print(
            f"[PARALLEL:{phase_name}] Attached chain claims for "
            f"{len(attached_pairs)}/{len(requested_pairs)} requested pair(s)",
            file=sys.stderr, flush=True,
        )
    return attached


def _merge_chain_claim_output(
    raw_output: str,
    current_payload: Dict[str, Any],
    batch_names: List[str],
    phase_name: str,
) -> Dict[str, Any]:
    """Merge modern pair-keyed chain-claim output, with legacy fallback."""
    records: List[Dict[str, Any]] = []
    for segment in _extract_json_dict_segments(raw_output):
        records.extend(_chain_claim_records_from_obj(segment))

    if records:
        attached = _attach_chain_claim_records(
            current_payload,
            records,
            requested_pairs=batch_names,
            phase_name=phase_name,
        )
        if attached > 0 or not _missing_chain_claim_pairs(
            current_payload.get("ctx_json", {}), batch_names
        ):
            return current_payload

    # Fallback for older prompt/output shapes while the app is warm-reloaded.
    merged = parse_json_output(
        raw_output,
        ["ctx_json"],
        previous_payload=current_payload,
    )
    _relocate_flat_chain_hop_functions(merged)
    return merged


def _get_chain_claim_targets(ctx_json: dict, step_name: str) -> List[str]:
    """Return interaction pair names for chain claim generation (2ax/2az).

    Filters out self-referencing pairs (query→query) and pairs where both
    sides are the same protein. As a side effect, stashes a per-pair chain
    context map on ``ctx_json['_chain_pair_context']`` so the batch
    directive builder can tell the LLM which cascade each pair belongs to
    and its position within it — the single biggest lever for getting
    coherent chain-claim generation instead of isolated per-pair stubs.

    Returns list of 'PROTEIN_A->PROTEIN_B' pair strings.
    """
    pairs: List[str] = []
    main = (ctx_json.get("main") or "").upper()
    pair_context: Dict[str, Dict[str, Any]] = ctx_json.setdefault(
        "_chain_pair_context", {}
    )

    interactors_by_name_case_any: Dict[str, Dict[str, Any]] = {
        (i.get("primary") or "").strip().upper(): i
        for i in ctx_json.get("interactors", [])
    }

    def _origin_claim_from_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
        """Look up the sci-claim that originated this cascade.

        Track A's ``step2ab_chain_determination`` returns each chain
        annotation with ``interactor`` (the indirect target) and
        ``claim_index`` (0-based index into that interactor's
        ``functions[]`` list — the specific claim whose prose described
        the cascade). We resolve that here so the per-hop claim
        generator (ax/az) can ground every hop in the ORIGINATING
        scientific claim rather than re-deriving biology pair-by-pair.

        The origin is what makes the chain biologically one cascade —
        the user's principle: one chain = one sci-claim's described
        mechanism.
        """
        interactor_name = (entry.get("interactor") or "").strip().upper()
        claim_index = entry.get("claim_index")
        if not interactor_name:
            return {}
        interactor = interactors_by_name_case_any.get(interactor_name)
        if not interactor or not isinstance(claim_index, int):
            return {}
        funcs = interactor.get("functions") or []
        if claim_index < 0 or claim_index >= len(funcs):
            return {}
        claim = funcs[claim_index]
        if not isinstance(claim, dict):
            return {}
        origin_prose = " ".join(
            str(claim.get(k) or "").strip()
            for k in ("function", "cellular_process", "effect_description")
            if claim.get(k)
        ).strip()
        return {
            "origin_interactor": interactor.get("primary") or interactor_name,
            "origin_claim_index": claim_index,
            "origin_claim_name": claim.get("function") or "",
            "origin_claim_pathway": claim.get("pathway") or "",
            "origin_claim_prose": origin_prose,
        }

    def _add_pair(
        a: str,
        b: str,
        chain: List[str] = None,
        source: str = "",
        origin: Dict[str, Any] = None,
    ):
        if a.upper() == b.upper():
            return
        pair_key = f"{a}->{b}"
        if pair_key not in pairs:
            pairs.append(pair_key)
        if chain and len(chain) >= 2 and pair_key not in pair_context:
            try:
                hop_index = next(
                    idx for idx in range(len(chain) - 1)
                    if chain[idx].upper() == a.upper()
                    and chain[idx + 1].upper() == b.upper()
                )
            except StopIteration:
                hop_index = None
            pair_context[pair_key] = {
                "full_chain": list(chain),
                "hop_index": hop_index,
                "chain_length": len(chain),
                "source": source,
                **(origin or {}),
            }

    if step_name == "step2ax_claim_generation_explicit":
        for entry in ctx_json.get("_chain_annotations_explicit", []):
            chain = _parse_chain_string(entry.get("chain", []))
            origin = _origin_claim_from_entry(entry)
            for j in range(len(chain) - 1):
                _add_pair(chain[j], chain[j+1], chain=chain, source="explicit", origin=origin)

        # Atom K — catch chains that never reached _chain_annotations_explicit.
        # Any interactor with a populated ``mediator_chain`` has a chain that
        # db_sync will process. If Track A's step2ab didn't emit an entry for
        # that interactor (batch LLM failure, truncation, or the chain came
        # from step2a's ``chain_context.full_chain`` → ``_reconcile_chain_fields``
        # path that bypasses 2ab), 2ax would otherwise skip the hops. Cover
        # every real chain by enumerating the interactor-derived chains too;
        # ``_add_pair``'s own dedup prevents double-enumeration when Track A
        # already produced the same entry.
        _main_raw = ctx_json.get("main") or ""
        from utils.chain_view import ChainView
        for inter in ctx_json.get("interactors", []):
            if not isinstance(inter, dict):
                continue
            primary = (inter.get("primary") or "").strip()
            if not primary:
                continue
            # ChainView is the single reader — it prefers
            # chain_context.full_chain and falls back to the denormalised
            # columns only when reconstruction is possible.
            view = ChainView.from_interaction_data(
                inter, query_protein=_main_raw or None,
            )
            if view.is_empty:
                continue
            full_chain = list(view.full_chain)
            # Dedup consecutive repeats (defensive — older data
            # occasionally stored the query twice in a row).
            _dedup_chain: List[str] = []
            for s in full_chain:
                if not _dedup_chain or _dedup_chain[-1].upper() != s.upper():
                    _dedup_chain.append(s)
            full_chain = _dedup_chain
            if len(full_chain) < 2:
                continue
            for j in range(len(full_chain) - 1):
                _add_pair(
                    full_chain[j],
                    full_chain[j + 1],
                    chain=full_chain,
                    source="interactor_mediator_chain",
                )

    elif step_name == "step2az_claim_generation_hidden":
        # Primary path: enumerate from the canonical hidden-chain annotation
        # lists. Track B's _hidden_pairs_data is structured by pair-result;
        # _chain_annotations_hidden is the raw per-chain list that Track B's
        # 2ab3 LLM stage emitted. Both are authoritative, so we read each and
        # let _add_pair's dedup collapse overlaps.
        for pair_result in ctx_json.get("_hidden_pairs_data", []):
            source_entry = pair_result.get("source_chain_entry") or {}
            origin = _origin_claim_from_entry(source_entry) if source_entry else {}
            for d in pair_result.get("new_directs", []):
                _add_pair(d['pair'][0], d['pair'][1], source="hidden_direct", origin=origin)
            for ind in pair_result.get("new_indirects", []):
                chain = ind.get("chain", [])
                for j in range(len(chain) - 1):
                    _add_pair(chain[j], chain[j+1], chain=chain, source="hidden_indirect", origin=origin)

        # Also enumerate directly from _chain_annotations_hidden — catches
        # chains whose _hidden_pairs_data entry was dropped by an upstream
        # pair-extraction edge case but whose chain string is present.
        for entry in ctx_json.get("_chain_annotations_hidden", []):
            chain_h = _parse_chain_string(entry.get("chain", []))
            origin_h = _origin_claim_from_entry(entry)
            for j in range(len(chain_h) - 1):
                _add_pair(
                    chain_h[j], chain_h[j + 1],
                    chain=chain_h, source="hidden_annotation", origin=origin_h,
                )

        # Atom K (Track B parity): catch hidden chains that reached the
        # interactor via chain_context (fixes 1.2/1.3 guarantee they do)
        # but somehow did not appear in _chain_annotations_hidden or
        # _hidden_pairs_data. Same pattern as the explicit step above.
        _main_raw_h = ctx_json.get("main") or ""
        from utils.chain_view import ChainView as _ChainView_Hidden
        for inter in ctx_json.get("interactors", []):
            if not isinstance(inter, dict):
                continue
            primary = (inter.get("primary") or "").strip()
            if not primary:
                continue
            view_h = _ChainView_Hidden.from_interaction_data(
                inter, query_protein=_main_raw_h or None,
            )
            if view_h.is_empty:
                continue
            full_chain_h = list(view_h.full_chain)
            _dedup_h: List[str] = []
            for s in full_chain_h:
                if not _dedup_h or _dedup_h[-1].upper() != s.upper():
                    _dedup_h.append(s)
            full_chain_h = _dedup_h
            if len(full_chain_h) < 2:
                continue
            for j in range(len(full_chain_h) - 1):
                _add_pair(
                    full_chain_h[j], full_chain_h[j + 1],
                    chain=full_chain_h, source="interactor_mediator_chain_hidden",
                )

    # Atom E — Zero-skip invariant: EVERY hop of EVERY resolved chain
    # must be handed to 2ax/2az for claim generation. Structural rules
    # already enforced inside ``_add_pair``:
    #   - ``a.upper() == b.upper()`` → skipped (self-pair)
    # Query-at-tail hops are valid biology and must stay eligible
    # (e.g. PINK1->PARK2->ATXN3 needs a PARK2->ATXN3 claim).
    # No coverage-based filtering happens here any more. Idempotency is
    # handled downstream: the LLM sees existing claims via the
    # "EXISTING CLAIMS — DO NOT DUPLICATE" block in the batch directive,
    # and duplicate claims are filtered by name in the dedup step that
    # runs after each 2ax/2az phase (see ``_dedup_functions_locally``).
    # If an earlier chain-claim phase already generated nested functions for
    # a pair, do not burn another Flash call re-generating it in the hidden
    # phase. Missing pairs remain in the list and get retried.
    _rehydrate_chain_claims_from_db(ctx_json, pairs, phase_name=step_name)
    generated_pairs = _generated_chain_pair_keys(ctx_json)
    if generated_pairs:
        from utils.chain_resolution import canonical_pair_key as _canon_pair_key
        before_filter = len(pairs)
        filtered_pairs: List[str] = []
        for pair in pairs:
            if "->" not in pair:
                filtered_pairs.append(pair)
                continue
            src, tgt = pair.split("->", 1)
            if _canon_pair_key(src, tgt) in generated_pairs:
                continue
            filtered_pairs.append(pair)
        pairs = filtered_pairs
        skipped = before_filter - len(pairs)
        if skipped:
            print(
                f"   [CHAIN] Skipping {skipped} chain pair(s) that already "
                f"have nested chain_link_functions.",
                file=sys.stderr, flush=True,
            )

    print(
        f"   [CHAIN] Zero-skip: returning all {len(pairs)} hop pair(s) "
        f"for fresh claim generation in {step_name}",
        file=sys.stderr, flush=True,
    )
    return list(pairs)


def _rehydrate_chain_claims_from_db(
    ctx_json: Dict[str, Any],
    pairs: List[str],
    *,
    phase_name: str = "",
) -> int:
    """Attach existing DB claims before spending new chain-claim calls.

    PostgreSQL is the durable memory layer. If another query already
    generated a valid direct/chain-derived claim for a hop pair, reuse it in
    ``chain_link_functions`` and let the normal generated-pair filter skip
    the redundant Gemini call. This is best-effort and never blocks a run.
    """
    if os.getenv("CHAIN_CLAIM_DB_REHYDRATE", "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return 0

    missing_pairs = _missing_chain_claim_pairs(ctx_json, pairs)
    if not missing_pairs:
        return 0

    try:
        from models import Interaction, InteractionClaim, Pathway, Protein
        from utils.db_sync import _is_placeholder_text
    except Exception:
        return 0

    records: List[Dict[str, Any]] = []
    try:
        for pair in missing_pairs:
            if "->" not in pair:
                continue
            src, tgt = [p.strip() for p in pair.split("->", 1)]
            if not src or not tgt or src.upper() == tgt.upper():
                continue
            prot_a = Protein.query.filter_by(symbol=src.upper()).first()
            prot_b = Protein.query.filter_by(symbol=tgt.upper()).first()
            if not prot_a or not prot_b:
                continue
            a_id, b_id = min(prot_a.id, prot_b.id), max(prot_a.id, prot_b.id)
            interaction = Interaction.query.filter_by(
                protein_a_id=a_id,
                protein_b_id=b_id,
            ).first()
            if not interaction:
                continue

            claims = (
                InteractionClaim.query
                .filter_by(interaction_id=interaction.id)
                .all()
            )
            functions: List[Dict[str, Any]] = []
            for claim in claims:
                if not claim.function_name:
                    continue
                if (
                    _is_placeholder_text(claim.function_name)
                    or _is_placeholder_text(claim.mechanism)
                ):
                    continue
                pathway_name = claim.pathway_name
                if not pathway_name and claim.pathway_id:
                    pathway = Pathway.query.get(claim.pathway_id)
                    pathway_name = pathway.name if pathway else None
                functions.append({
                    "function": claim.function_name,
                    "arrow": claim.arrow or "regulates",
                    "cellular_process": claim.mechanism or "",
                    "effect_description": claim.effect_description or "",
                    "biological_consequence": claim.biological_consequences or [],
                    "specific_effects": claim.specific_effects or [],
                    "evidence": claim.evidence or [],
                    "pmids": claim.pmids or [],
                    "pathway": pathway_name or "",
                    "function_context": "chain_derived",
                    "confidence": (
                        float(claim.confidence)
                        if claim.confidence is not None
                        else None
                    ),
                    "_rehydrated_from_existing_claim": True,
                    "_source_claim_id": claim.id,
                })
            if functions:
                records.append({
                    "pair": pair,
                    "source": src,
                    "target": tgt,
                    "functions": functions,
                })
    except Exception:
        return 0

    if not records:
        return 0

    attached = _attach_chain_claim_records(
        {"ctx_json": ctx_json},
        records,
        requested_pairs=[record["pair"] for record in records],
        phase_name="db_rehydrate",
    )
    if attached and phase_name:
        print(
            f"[PARALLEL:{phase_name}] DB rehydrated chain claims for "
            f"{len(records)} pair(s), avoiding duplicate Gemini calls.",
            file=sys.stderr,
            flush=True,
        )
    return attached


def _build_chain_batch_directive(
    batch_names: List[str],
    ctx_json: Dict[str, Any],
    depth_expand: bool = False,
) -> str:
    """Build an enriched batch directive that lists existing claims per pair.

    Tells the LLM which function names already exist on each target
    interactor so it avoids regenerating them — prevents duplicates at
    the source instead of discarding them after generation.

    R1: when ``depth_expand=True``, the directive prepends a
    "fix-the-failing-rules" block that names the specific PhD-depth
    fields (cellular_process sentences, effect_description sentences,
    biological_consequence cascades, specific_effects, evidence) the
    previous generation under-shot on. Used by the chain-hop depth
    redispatch path so a single targeted retry can hit depth instead
    of regenerating the whole pair from scratch with the same prompt.
    """
    interactors_by_name = {
        i.get("primary", "").upper(): i
        for i in ctx_json.get("interactors", [])
    }

    lines = [
        f"BATCH ASSIGNMENT — Generate claims for ONLY",
        f"these {len(batch_names)} chain interaction pairs:",
        ", ".join(batch_names),
        "Do NOT process pairs outside this list.",
        "",
    ]

    if depth_expand:
        lines.extend([
            "DEPTH-EXPAND PASS (R1):",
            "  This is a targeted retry. The previous chain-claim generation",
            "  for the listed pair(s) emitted shallow output that violated the",
            "  PhD-level depth contract. REGENERATE each pair, hitting EVERY",
            "  field's minimum:",
            "    - cellular_process: ≥6 dense pair-specific sentences",
            "    - effect_description: ≥6 dense sentences",
            "    - biological_consequence: 3-5 cascades, ≥6 named steps each",
            "    - specific_effects: ≥3 entries (technique + model + result each)",
            "    - evidence: ≥3 papers (paper_title + paraphrased quote +",
            "                year + assay + species + key_finding)",
            "  Pair-scope rule still applies: no query-protein mention, no",
            "  non-adjacent chain mediator mention. Hit depth AND scope.",
            "  Do NOT emit thin claims here — the pair's biology must already",
            "  be richer than the prior pass produced; if it genuinely is not",
            "  in the literature, reduce to a single mechanism with full depth",
            "  rather than padding with generic prose.",
            "",
        ])

    lines.extend([
        "ZERO-SKIP MANDATE (MANDATORY):",
        f"  You MUST return at least one claim for EACH of the {len(batch_names)}",
        "  pair(s) listed above. No pair may be silently omitted. If the",
        "  literature contains no pair-specific biology for a given hop in",
        "  the cascade's context, emit an HONEST THIN CLAIM for it — see",
        "  THIN-CLAIM FALLBACK below. Silent omission of a pair violates the",
        "  pipeline contract and will be logged as a missing-hop error.",
        "",
        "THIN-CLAIM FALLBACK (when pair biology is genuinely undocumented):",
        "  When and only when no credible pair-specific mechanism exists in",
        "  the literature, emit a minimal function inside the pair's",
        "  chain_claims record:",
        "    {",
        "      \"function\": \"Pair biology not characterized in the cascade context\",",
        "      \"function_context\": \"chain_derived\",",
        "      \"arrow\": \"regulates\",",
        "      \"cellular_process\": \"No peer-reviewed characterization of the",
        "        <SOURCE>-<TARGET> interaction in the context of this cascade",
        "        could be retrieved. This hop appears in the cascade topology",
        "        but lacks pair-specific biochemical evidence at time of",
        "        generation.\",",
        "      \"effect_description\": \"Pair role within the cascade is",
        "        topologically inferred, not mechanistically characterized.\",",
        "      \"biological_consequence\": \"\",",
        "      \"specific_effects\": [],",
        "      \"evidence\": [],",
        "      \"pathway\": \"\",",
        "      \"_thin_claim\": true",
        "    }",
        "  Do NOT fabricate mechanisms, evidence, or quantitative data to",
        "  satisfy the zero-skip rule. A thin claim with _thin_claim=true is",
        "  always preferable to invented biology.",
        "",
    ])

    # Chain-context annotations: for each pair, describe the full cascade
    # it belongs to and its hop position. This turns isolated "generate a
    # claim for X->Y" calls into chain-coherent prompts where the LLM
    # knows the hop is hop 2 of 4 in Q → A → B → C → T. Without this
    # annotation Gemini Flash produces generic pair-level stubs that lose
    # the upstream/downstream biology of the cascade.
    pair_context_map = ctx_json.get("_chain_pair_context", {}) or {}
    chain_annotated = [p for p in batch_names if p in pair_context_map]
    if chain_annotated:
        lines.append("CHAIN CONTEXT (each pair is one hop of a longer cascade):")
        for pair in chain_annotated:
            info = pair_context_map.get(pair) or {}
            full_chain = info.get("full_chain") or []
            hop_index = info.get("hop_index")
            chain_length = info.get("chain_length") or len(full_chain)
            if full_chain:
                path = " → ".join(full_chain)
                if hop_index is not None:
                    lines.append(
                        f"  {pair}: hop {hop_index + 1} of {max(chain_length - 1, 1)} "
                        f"in cascade {path}"
                    )
                else:
                    lines.append(f"  {pair}: within cascade {path}")
        lines.extend([
            "",
            "IMPORTANT: each hop's function must describe THIS PAIR's specific",
            "biochemistry while remaining consistent with the upstream and",
            "downstream hops in the cascade above. Don't fabricate a generic",
            "'binds and regulates' stub — tie it to the cascade's mechanism.",
            "",
        ])

    # ORIGIN CLAIM block — ground each hop in the single scientific claim
    # that described this cascade upstream. This is the principle: a chain
    # is one biological mechanism documented in ONE sci-claim; per-hop
    # claims must describe the same mechanism from their pair's view, not
    # re-derive pair biology independently. When the origin is absent
    # (Track B without claim_index tracking, or Track A chains that
    # skipped claim_index), we fall through to cascade-only context.
    origin_groups: Dict[str, List[str]] = {}
    for pair in chain_annotated:
        info = pair_context_map.get(pair) or {}
        origin_key = (
            (info.get("origin_interactor") or "").strip().upper()
            + "::"
            + str(info.get("origin_claim_index"))
            if info.get("origin_interactor") is not None
            and info.get("origin_claim_index") is not None
            else ""
        )
        if not origin_key:
            continue
        origin_groups.setdefault(origin_key, []).append(pair)

    if origin_groups:
        lines.append(
            "ORIGIN CLAIM — every hop below was derived from ONE scientific"
        )
        lines.append(
            "claim that already describes this cascade as a single biological"
        )
        lines.append(
            "mechanism. Ground each hop's prose IN that same mechanism —"
        )
        lines.append(
            "the hop is one pair-scoped view of the origin claim, not an"
        )
        lines.append(
            "independent finding. Do not invent new biology; extract the"
        )
        lines.append(
            "specific biochemistry of this pair AS DESCRIBED by the origin:"
        )
        lines.append("")
        # Atom H: cap origin groups per batch at 2 and origin prose at
        # 400 chars. Origin blocks used to dominate the directive at 900
        # chars × N groups; for a 5-pair batch split across 3 origins
        # that's 2700 chars of pure prompt overhead. The cap keeps the
        # cascade-anchor pointer without ballooning the prompt.
        _ORIGIN_CAP = 2
        _ORIGIN_PROSE_CAP = 400
        _emitted_origin_groups = 0
        _skipped_origin_groups = 0
        for origin_key, pairs_for_origin in origin_groups.items():
            if _emitted_origin_groups >= _ORIGIN_CAP:
                _skipped_origin_groups += 1
                continue
            info = pair_context_map.get(pairs_for_origin[0]) or {}
            origin_name = info.get("origin_interactor") or "?"
            origin_claim = info.get("origin_claim_name") or "(unnamed claim)"
            origin_pathway = info.get("origin_claim_pathway") or ""
            origin_prose = (info.get("origin_claim_prose") or "").strip()
            if len(origin_prose) > _ORIGIN_PROSE_CAP:
                origin_prose = origin_prose[:_ORIGIN_PROSE_CAP].rstrip() + "…"
            lines.append(
                f"  • Origin interactor: {info.get('origin_interactor') or '?'}"
            )
            lines.append(
                f"    Origin claim: \"{origin_claim}\""
                + (f" (pathway: {origin_pathway})" if origin_pathway else "")
            )
            if origin_prose:
                lines.append(f"    Origin prose: {origin_prose}")
            lines.append(
                f"    Hops derived from this origin: {', '.join(pairs_for_origin)}"
            )
            lines.append("")
            _emitted_origin_groups += 1
        if _skipped_origin_groups > 0:
            lines.append(
                f"  (+{_skipped_origin_groups} additional cascade origin(s) "
                f"share context via ctx_json — see interactors' existing "
                f"functions for their biology)"
            )
            lines.append("")
        lines.append(
            "For each hop, generate a function claim that describes the"
        )
        lines.append(
            "HOP PAIR's role in the origin cascade above. If the origin"
        )
        lines.append(
            "prose does not describe this specific pair's role, state that"
        )
        lines.append(
            "explicitly rather than fabricating mechanisms. Hop claims MUST"
        )
        lines.append(
            "reference the cascade's biology, not generic binding stubs."
        )
        lines.append("")

    lines.append("EXISTING CLAIMS — DO NOT DUPLICATE:")

    has_existing = False
    seen_proteins: set = set()
    for pair in batch_names:
        parts = pair.split("->")
        if len(parts) != 2:
            continue
        for protein in parts:
            p_upper = protein.strip().upper()
            if p_upper in seen_proteins:
                continue
            seen_proteins.add(p_upper)
            interactor = interactors_by_name.get(p_upper)
            if not interactor:
                continue
            existing_fns = [
                f for f in interactor.get("functions", [])
                if f.get("function")
            ]
            if existing_fns:
                # Atom H: just list function names — the LLM already
                # sees the compacted ctx_json (Atom F) with full names
                # and arrows; repeating 120 chars of cellular_process
                # per claim here inflated the directive by 4-8k tokens
                # on typical batches. Names alone are enough for the
                # "don't duplicate" signal.
                for fn in existing_fns[:8]:
                    name = fn.get("function", "")
                    lines.append(f'  {protein}: "{name}"')
                if len(existing_fns) > 8:
                    lines.append(f"  {protein}: (+{len(existing_fns) - 8} more)")
                has_existing = True

    if not has_existing:
        lines.append("  (none — all pairs are new)")

    # Read chain claim settings from env (set by runner from UI config)
    max_claims = os.environ.get("MAX_CHAIN_CLAIMS_PER_LINK")
    claim_style = os.environ.get("CHAIN_CLAIM_STYLE", "tailored").lower()

    lines.extend([
        "",
        "Generate ONLY functions with DISTINCT mechanisms not listed above.",
        "If a mechanism is already covered, do NOT generate a variant of it.",
    ])

    if max_claims:
        lines.append(
            f"\nGenerate AT MOST {max_claims} function(s) per chain pair. "
            "Prefer exactly one compact, high-signal function unless the "
            "literature clearly supports a second distinct mechanism."
        )
    else:
        lines.append(
            "\nGenerate exactly one compact, high-signal function per chain "
            "pair unless the literature clearly supports a second distinct "
            "mechanism."
        )

    if claim_style == "identical":
        lines.extend([
            "",
            "CLAIM STYLE: IDENTICAL — For each chain link, rephrase the PARENT",
            "indirect claim for the specific binary pair. All links in the same",
            "chain should describe the same biological mechanism from different",
            "perspectives (upstream vs downstream). Do NOT independently research",
            "new mechanisms — adapt the existing indirect claim.",
        ])
    else:
        lines.extend([
            "",
            "CLAIM STYLE: TAILORED — For each chain link, independently research",
            "the specific binary interaction. Each pair should have its OWN unique",
            "biological characterization grounded in literature specific to that",
            "protein pair. Do NOT copy or rephrase claims from other links.",
        ])

    lines.extend([
        "",
        "OUTPUT CONTRACT — ABSOLUTE:",
        "Return ONLY this top-level object shape:",
        "{\"chain_claims\":[{\"pair\":\"A->B\",\"source\":\"A\","
        "\"target\":\"B\",\"functions\":[{...}]}]}",
        "Every requested pair MUST appear exactly once in chain_claims.",
        "The pair value MUST exactly match the requested pair string.",
        "Do NOT emit ctx_json, interactors, or chain_link_functions.",
        "The runner attaches these records into chain_link_functions itself.",
    ])

    return "\n".join(lines)


# ── Interactor promotion: convert cascade-discovered proteins into real interactors ──


def _promote_discovered_interactors(payload: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    """Promote cascade-discovered proteins into the main interactors array.

    Processing order (higher-quality chain data first):
    1. Scan ``_implicated_proteins`` on existing functions — these have a known
       parent protein and produce ``mediator_chain: [parent]``.
    2. Process the ``indirect_interactors`` array from step2a — these may lack
       upstream/chain data.
    3. Reclassify direct→indirect when ``_evidence_suggests_indirect`` is set.

    Returns:
        (updated_payload, list of newly promoted interactor names)
    """
    ctx = payload.get("ctx_json", {})
    interactors = ctx.get("interactors", [])
    existing_names = {i.get("primary", "").upper() for i in interactors}
    main_protein = (ctx.get("main") or "").upper()
    promoted_names: List[str] = []

    def _add_to_history(name: str) -> None:
        history = ctx.setdefault("interactor_history", [])
        if name not in history:
            history.append(name)

    # B5: every chain we accept goes through ``validate_chain_on_ingest``
    # so a malformed LLM hint can't smuggle in a chain that lacks the
    # query or has consecutive dupes.
    # Previously only the step2ab chain-resolution path validated; the
    # ``_implicated_proteins`` and ``indirect_interactors`` paths skipped it
    # and silently truncated multi-hop hints to a single ``[upstream]``.
    from utils.chain_resolution import validate_chain_on_ingest

    def _build_chain(raw_chain: list, parent_name: str) -> list:
        """Validate ``raw_chain`` (a list of protein symbols leading from
        the query toward the new interactor) and fall back to a single-
        element ``[parent_name]`` chain when nothing usable is provided.
        """
        if isinstance(raw_chain, list) and raw_chain:
            cleaned, _ = validate_chain_on_ingest(
                raw_chain, query_protein=main_protein,
            )
            if cleaned:
                return cleaned
        return [parent_name] if parent_name else []

    # ── Pass 1: Scan _implicated_proteins (highest quality — has parent chain) ──
    # Iterate over a snapshot since we append to interactors
    for interactor in list(interactors):
        for func in interactor.get("functions", []):
            implicated = func.get("_implicated_proteins", [])
            for imp in implicated:
                if isinstance(imp, dict):
                    imp_name = imp.get("name", "")
                    # B5: if the LLM provided an explicit chain on the
                    # implicated entry, honor it instead of the 1-hop
                    # default — multi-hop hints used to be silently dropped.
                    explicit_chain = imp.get("mediator_chain")
                else:
                    imp_name = imp
                    explicit_chain = None
                if not imp_name or imp_name.upper() in existing_names or imp_name.upper() == main_protein:
                    continue
                parent = interactor.get("primary", "")
                chain = _build_chain(explicit_chain, parent)
                # Use ChainView to derive depth/upstream from the canonical
                # full_chain instead of fragile manual len(chain)+1 / chain[-1].
                full = [main_protein] + chain + [imp_name] if chain else [main_protein, imp_name]
                new_interactor = {
                    "primary": imp_name,
                    "interaction_type": "indirect",
                    "support_summary": f"Discovered in {parent} function cascade",
                    "functions": [],
                }
                from utils.chain_view import ChainView as _CV
                _CV.from_full_chain(full, query_protein=main_protein).apply_to_dict(new_interactor)
                interactors.append(new_interactor)
                existing_names.add(imp_name.upper())
                promoted_names.append(imp_name)
                _add_to_history(imp_name)

    # ── Pass 2: Process indirect_interactors array (may lack chain data) ──
    discovered = ctx.get("indirect_interactors", [])
    for entry in discovered:
        name = entry.get("name", "")
        if not name or name.upper() in existing_names or name.upper() == main_protein:
            continue

        upstream = entry.get("upstream_interactor", "")
        # B5: prefer the LLM's full mediator_chain hint when it provides
        # one. The previous code took only ``upstream_interactor`` and
        # silently truncated multi-hop chains to a single mediator.
        explicit_chain = entry.get("mediator_chain")
        if isinstance(explicit_chain, list) and explicit_chain:
            chain = _build_chain(explicit_chain, upstream)
        elif upstream:
            chain = [upstream]
        else:
            chain = []

        full = [main_protein] + chain + [name] if chain else [main_protein, name]
        new_interactor = {
            "primary": name,
            "interaction_type": "indirect",
            "support_summary": entry.get("role_in_cascade", ""),
            "functions": [],
        }
        from utils.chain_view import ChainView as _CV
        _CV.from_full_chain(full, query_protein=main_protein).apply_to_dict(new_interactor)
        interactors.append(new_interactor)
        existing_names.add(name.upper())
        promoted_names.append(name)
        _add_to_history(name)

    # ── Pass 3: Reclassify direct→indirect when evidence shows intermediaries ──
    for interactor in interactors:
        reclassify = None
        for func in interactor.get("functions", []):
            if func.get("_evidence_suggests_indirect"):
                mediators = func.get("_implicated_mediators", [])
                if mediators:
                    reclassify = mediators
                    break

        if reclassify and interactor.get("interaction_type") == "direct":
            # B5: validate the reclassification chain before adopting it.
            # The previous code trusted the LLM's ``_implicated_mediators``
            # list verbatim — which let chains with consecutive duplicates
            # or the query protein missing silently land on the row.
            cleaned, errors = validate_chain_on_ingest(
                reclassify, query_protein=main_protein,
            )
            if errors:
                print(
                    f"   [PROMOTE:reclassify] Chain for {interactor.get('primary')}: "
                    f"{', '.join(errors)}",
                    file=sys.stderr, flush=True,
                )
            if not cleaned:
                continue
            interactor["interaction_type"] = "indirect"
            # Use ChainView to derive all chain fields canonically.
            full = [main_protein] + cleaned + [interactor.get("primary", "")]
            from utils.chain_view import ChainView as _CV
            _CV.from_full_chain(full, query_protein=main_protein).apply_to_dict(interactor)
            print(
                f"   [PROMOTE] Reclassified {interactor.get('primary')} as indirect "
                f"via {' → '.join(cleaned)}",
                file=sys.stderr, flush=True,
            )

    ctx["interactors"] = interactors
    payload["ctx_json"] = ctx

    if promoted_names:
        print(
            f"   [PROMOTE] Added {len(promoted_names)} new interactors from cascades: "
            f"{promoted_names[:10]}{'...' if len(promoted_names) > 10 else ''}",
            file=sys.stderr, flush=True,
        )

    return payload, promoted_names


def _reconcile_chain_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reconcile denormalised chain columns from the canonical full_chain.

    ``chain_context.full_chain`` is the authoritative cascade representation.
    The denormalised columns (``mediator_chain``, ``upstream_interactor``,
    ``depth``) remain on interactor dicts for SQL convenience and for the
    handful of consumers that still read them directly — but they are ALWAYS
    derived from ``full_chain`` via ``ChainView.apply_to_dict`` so they can
    never drift. This function is idempotent.
    """
    ctx = payload.get("ctx_json", {})
    reconciled = 0

    from utils.chain_view import ChainView

    query = (ctx.get("main") or "").strip() or None

    for interactor in ctx.get("interactors", []):
        if interactor.get("interaction_type") != "indirect":
            continue

        full_chain = (
            (interactor.get("chain_context") or {}).get("full_chain") or []
        )
        if full_chain and len(full_chain) >= 2:
            ChainView.from_full_chain(
                full_chain, query_protein=query,
            ).apply_to_dict(interactor)
            reconciled += 1
            continue

        # Indirect without chain_context.full_chain is silently skipped.
        # This is normal pre-step2ab (step2ab is what populates full_chain);
        # post-step2ab, the downstream consumer _get_chained_needing_link_
        # functions logs a [CHAIN SKIP] exactly once per consequential skip.
        # Logging here would double-fire for every pre-resolution call.
        continue

    if reconciled:
        print(
            f"   [CHAIN] Reconciled {reconciled} indirect interactor(s) "
            "from canonical full_chain.",
            file=sys.stderr, flush=True,
        )
    payload["ctx_json"] = ctx
    return payload


def _backfill_chain_context_from_mediator_chain(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Fix 1.6 — derive ``chain_context.full_chain`` from ``mediator_chain``
    for indirect interactors that no upstream pass populated.

    Three known good writers exist for ``chain_context``:
      1. Track A (``_run_track_a`` lines 786-818) — applies ChainView for
         every ``_chain_annotations_explicit`` entry.
      2. Track B (Fix 1.2 inside ``_run_track_b``) — same for hidden chains.
      3. ``_promote_chain_interactors._add_indirect`` — newly promoted indirects.

    The 4th case this function covers: step2a's broad_discovery returns an
    interactor with ``mediator_chain=["X"]`` already populated INLINE in its
    output. Track A doesn't re-resolve it (already has a chain), Track B
    doesn't pick it up (it's a first-class direct discovery, not a hidden
    candidate), and ``_promote_chain_interactors`` skips it (not new).
    Without backfill, the interactor has ``mediator_chain`` but no
    ``chain_context``; downstream ``_get_chain_claim_targets`` reads an
    empty ChainView and the chain hop never enters the 2ax/2az batch.

    Assumes query-at-head — that's the semantic step2a's prompt enforces
    for ``mediator_chain`` ("intermediates between query and primary"). A
    ``_derived_from_mediator_chain=True`` audit tag lets future passes
    detect any backfilled entry that downstream consumers re-flag.
    """
    from utils.chain_view import ChainView
    from utils.chain_resolution import validate_chain_on_ingest

    ctx = payload.get("ctx_json", {})
    main = (ctx.get("main") or "").strip()
    if not main:
        return payload

    backfilled = 0
    for interactor in ctx.get("interactors", []):
        if not isinstance(interactor, dict):
            continue
        if interactor.get("interaction_type") != "indirect":
            continue
        # Skip if chain_context.full_chain is already populated.
        existing_cc = interactor.get("chain_context") or {}
        if isinstance(existing_cc, dict):
            stored = existing_cc.get("full_chain") or []
            if isinstance(stored, list) and len(stored) >= 2:
                continue
        # Need mediator_chain to derive.
        mediators = interactor.get("mediator_chain") or []
        if not isinstance(mediators, list) or not mediators:
            continue
        primary = (interactor.get("primary") or "").strip()
        if not primary:
            continue
        # Construct query-at-head chain. validate_chain_on_ingest cleans
        # consecutive repeats and enforces minimum length.
        raw_chain = [main] + [str(m).strip() for m in mediators if m] + [primary]
        cleaned, _errors = validate_chain_on_ingest(
            raw_chain, query_protein=main,
        )
        if len(cleaned) < 2:
            continue
        ChainView.from_full_chain(
            cleaned, query_protein=main,
        ).apply_to_dict(interactor)
        # Audit tag — downstream can detect backfilled entries if a future
        # query-at-tail discovery surfaces and the assumption breaks.
        cc = interactor.get("chain_context")
        if isinstance(cc, dict):
            cc["_derived_from_mediator_chain"] = True
        backfilled += 1

    if backfilled:
        print(
            f"   [CHAIN] Backfilled chain_context from mediator_chain for "
            f"{backfilled} indirect interactor(s) (Fix 1.6 — step2a inline "
            f"chain orphans).",
            file=sys.stderr, flush=True,
        )
    payload["ctx_json"] = ctx
    return payload


# Local dedup helpers moved to ``utils/dedup_local.py`` so ``runner.py``
# and the iterative merge path share one implementation of
# word-overlap + mechanism-overlap dedup. Re-export under the legacy
# private names so the rest of runner.py stays untouched.
from utils.dedup_local import (  # noqa: E402
    dedup_words as _dedup_words,
    word_overlap as _word_overlap,
    is_mechanism_duplicate as _is_mechanism_duplicate,
    strip_empty_functions as _strip_empty_functions,
    deduplicate_functions_local as _dedup_functions_locally,
    NAME_OVERLAP_GATE,
    NAME_OVERLAP_FUZZY,
    PROC_OVERLAP_FUZZY,
    PROC_OVERLAP_ALONE,
    PROC_OVERLAP_SAME_ARROW,
    NAME_OVERLAP_SAME_ARROW,
    MECHANISM_OVERLAP_THRESHOLD,
    MIN_WORDS_FOR_MECHANISM_DEDUP,
    MIN_MECHANISM_CHARS,
)


_BANNED_NAME_SUFFIXES = re.compile(
    r'\s+(regulation|suppression|activation|inhibition|promotion|induction|'
    r'stimulation|enhancement|modulation)\s*$', re.IGNORECASE
)

_DIRECT_EVIDENCE_RE_RUNNER = re.compile(
    r'co-ip|co-immunoprecipitat\w*|direct\s+binding|physical\s+interaction'
    r'|y2h|yeast\s+two-hybrid|pull-?down|direct\s+interact',
    re.IGNORECASE
)


def _clean_function_names_in_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip banned outcome-verb suffixes from function names."""
    cleaned = 0
    for inter in payload.get("ctx_json", {}).get("interactors", []):
        for fn in inter.get("functions", []):
            name = fn.get("function", "")
            new_name = _BANNED_NAME_SUFFIXES.sub('', name).strip()
            if new_name != name:
                fn["function"] = new_name
                cleaned += 1
    if cleaned:
        print(f"   [NAME-CLEAN] Stripped banned suffixes from {cleaned} function name(s)",
              file=sys.stderr, flush=True)
    return payload


def _reclassify_indirect_to_direct(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reclassify indirect interactors to direct when function evidence shows direct binding."""
    reclassified = 0
    for inter in payload.get("ctx_json", {}).get("interactors", []):
        if inter.get("interaction_type") != "indirect":
            continue
        for fn in inter.get("functions", []):
            text = (fn.get("cellular_process", "") or "") + " " + (fn.get("effect_description", "") or "")
            if _DIRECT_EVIDENCE_RE_RUNNER.search(text):
                inter["interaction_type"] = "direct"
                inter.pop("mediator_chain", None)
                inter.pop("upstream_interactor", None)
                inter.pop("depth", None)
                reclassified += 1
                break
    if reclassified:
        print(f"   [RECLASSIFY] {reclassified} interactor(s) indirect→direct based on function evidence",
              file=sys.stderr, flush=True)
    return payload


def _tag_shallow_functions(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Tag functions that fail PhD-level depth via the strict validator.

    Delegates to ``utils.quality_validator.validate_payload_depth`` so the
    same thresholds (6-10 sentences in cellular_process, 3-5 cascades in
    biological_consequence) are used here AND by the post-processor's
    ``quality_validation`` stage. The previous loose detector
    (``cp.count('.') < 2``, ``len(bc) < 1``) almost never fired and let
    100% of recent runs ship with 0% pass-rate.

    The strict validator stamps ``_depth_issues = ['min_sentences', ...]``
    on every flagged function. Downstream:
      • ``_shallow_interactor_names`` reads the tag to build the
        redispatch target list.
      • ``make_depth_expand_batch_directive`` references the tag in the
        re-prompt so the model targets exactly the failing rule.
    Idempotent — non-violating functions get the tag cleared so re-runs
    don't loop on already-fixed entries.
    """
    report = validate_payload_depth(payload, tag_in_place=True)

    # Idempotency: validate_payload_depth only ADDS the tag on violators;
    # we also need to CLEAR it on functions that passed this round so a
    # second redispatch loop doesn't re-target a fixed function.
    flagged_pairs = {
        (v.interactor, v.function_name) for v in report.violations
    }
    cleared = 0
    for inter in payload.get("ctx_json", {}).get("interactors", []) or []:
        if not isinstance(inter, dict):
            continue
        primary = inter.get("primary") or "?"
        for fn in inter.get("functions", []) or []:
            if not isinstance(fn, dict):
                continue
            fn_name = fn.get("function") or "?"
            if (primary, fn_name) not in flagged_pairs and "_depth_issues" in fn:
                fn.pop("_depth_issues", None)
                cleared += 1

    if report.flagged_functions:
        print(
            f"   [DEPTH-CHECK] {report.flagged_functions}/{report.total_functions} "
            f"function(s) flagged ({len(report.violations)} violations) — "
            f"pass_rate={report.pass_rate:.1%}",
            file=sys.stderr, flush=True,
        )
    if cleared:
        print(
            f"   [DEPTH-CHECK] Cleared {cleared} stale `_depth_issues` tag(s) "
            f"from now-passing functions",
            file=sys.stderr, flush=True,
        )
    return payload


def _shallow_interactor_names(current_payload: Dict[str, Any]) -> List[str]:
    """Return names of interactors whose flat functions still have depth issues.

    Consumed by the DEPTH-CHECK re-dispatch pass downstream of
    ``_tag_shallow_functions``. An interactor counts as "shallow" when any
    of its flat ``functions[]`` entries still has a ``_depth_issues`` tag
    after the initial generation. Chain-link functions are excluded — they
    have their own completeness guarantees via 2ax/2az and db_sync.
    """
    if not current_payload or "ctx_json" not in current_payload:
        return []
    names: List[str] = []
    seen: set = set()
    for inter in current_payload.get("ctx_json", {}).get("interactors", []):
        if not isinstance(inter, dict):
            continue
        has_shallow = False
        for fn in inter.get("functions", []) or []:
            if isinstance(fn, dict) and fn.get("_depth_issues"):
                has_shallow = True
                break
        if has_shallow:
            n = (inter.get("primary") or "").strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    return names


# ``_strip_empty_functions`` and ``_dedup_functions_locally`` now live in
# ``utils/dedup_local.py`` and are imported above via aliases. The
# previous in-file definitions were removed as part of the
# deduplication-path consolidation.


_DIRECTIVE_CHAIN_LINKS = (
    "\n\n{'='*60}\n"
    "BATCH ASSIGNMENT — Generate chain link functions for ONLY\n"
    "these {count} indirect interactors:\n"
    "{batch_names}\n"
    "Do NOT process interactors outside this list.\n"
    "{'='*60}\n"
)


def _run_parallel_batched_phase(
    phase_name: str,
    current_payload: Dict[str, Any],
    user_query: str,
    targets: List[str],
    step_factory: Any,
    batch_directive_template: str,
    *,
    batch_size: int = 3,
    max_workers: int = 999,
    cancel_event=None,
    request_mode: Optional[str] = None,
    batch_poll_seconds: Optional[int] = None,
    batch_max_wait_seconds: Optional[int] = None,
    known_interactions: Optional[List[Dict[str, Any]]] = None,
    update_status=None,
    step_idx: int = 0,
    total_steps: int = 0,
    batch_directive_fn: Optional[Any] = None,
    rate_limit_group_size: Optional[int] = None,
    retry_max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Run a pipeline phase in parallel batches.

    Splits *targets* into batches of ~batch_size, creates one LLM call per
    batch with an explicit batch directive appended to the prompt, fires all
    calls concurrently via ThreadPoolExecutor, and merges results sequentially.

    Args:
        phase_name: Human-readable label for logging (e.g. "function_mapping").
        targets: List of interactor names to process.
        step_factory: Callable that returns a StepConfig.  Accepted signatures:
            - step_factory()            — no args (step2b2, step2b4)
            - step_factory(round_num=N) — for step2a
        batch_directive_template: f-string-style template with {count} and
            {batch_names} placeholders.

    Returns:
        Updated payload with all batch results merged in.
    """
    import math, inspect

    if not targets:
        print(
            f"[PARALLEL:{phase_name}] No targets — skipping",
            file=sys.stderr, flush=True,
        )
        # Return tuple to match the success path at the bottom of this
        # function. Callers unpack ``payload, stats = _run_parallel_...()``;
        # returning a bare dict here caused unpacking to iterate dict keys
        # and hand the caller two strings, which crashed the token
        # accumulator with ``AttributeError: 'str' object has no attribute
        # 'get'``.
        return current_payload, {}

    _is_chain_claim_phase = (
        "claim_generation" in phase_name
        or "ax_claim" in phase_name
        or "az_claim" in phase_name
    )
    _is_function_mapping_phase = phase_name.startswith("function_mapping")
    if _is_function_mapping_phase:
        batch_size = max(1, FUNCTION_MAPPING_BATCH_SIZE)
        max_workers = max(1, FUNCTION_MAPPING_MAX_WORKERS)

    # ── Plan batches ──────────────────────────────────────────────
    num_batches = max(1, math.ceil(len(targets) / batch_size))
    even_size = math.ceil(len(targets) / num_batches)
    batches: List[List[str]] = []
    for i in range(num_batches):
        batches.append(targets[i * even_size : (i + 1) * even_size])

    print(
        f"\n{'='*70}\n"
        f"[PARALLEL:{phase_name}] {len(targets)} targets → "
        f"{num_batches} batch(es) (~{even_size}/batch, {min(max_workers, num_batches)} workers)\n"
        f"[PARALLEL:{phase_name}] Targets: {targets}\n"
        f"{'='*70}",
        file=sys.stderr, flush=True,
    )

    if update_status:
        update_status(
            text=f"{phase_name}: {num_batches} parallel batches for {len(targets)} interactors...",
            current_step=step_idx,
            total_steps=total_steps,
        )

    # ── Detect step_factory signature ─────────────────────────────
    _sig = inspect.signature(step_factory)
    _accepts_round = "round_num" in _sig.parameters

    _needs_batch_filter = (
        "citation" in phase_name
        or "verification" in phase_name
        or _is_function_mapping_phase
        or _is_chain_claim_phase
    )

    def _make_prompt_payload(batch_names_for_prompt: List[str]) -> Dict[str, Any]:
        """Return a batch-scoped payload for prompt construction."""
        if not _needs_batch_filter:
            return current_payload
        from copy import deepcopy
        prompt_payload = deepcopy(current_payload)
        ctx = prompt_payload.get("ctx_json", {})
        batch_set: set = set()
        for name in batch_names_for_prompt:
            if "->" in name:
                src, tgt = name.split("->", 1)
                batch_set.add(src.strip().lower())
                batch_set.add(tgt.strip().lower())
            else:
                batch_set.add(str(name).lower())
        main = (ctx.get("main") or "").lower()
        if main:
            batch_set.add(main)
        ctx["interactors"] = [
            i for i in ctx.get("interactors", [])
            if (i.get("primary") or "").lower() in batch_set
        ]
        # Chain claim prompts need pair context even when the endpoint
        # interactors are filtered. Keep only relevant pairs to reduce prompt
        # pressure and prevent the model from servicing non-batch hops.
        if _is_chain_claim_phase:
            pair_ctx = ctx.get("_chain_pair_context") or {}
            if isinstance(pair_ctx, dict):
                ctx["_chain_pair_context"] = {
                    p: v for p, v in pair_ctx.items()
                    if p in set(batch_names_for_prompt)
                }
        return prompt_payload

    # ── Build call list ───────────────────────────────────────────
    call_list: List[Dict[str, Any]] = []
    for batch_idx, batch_names in enumerate(batches):
        step = (
            step_factory(round_num=batch_idx + 1)
            if _accepts_round
            else step_factory()
        )

        _prompt_payload = _make_prompt_payload(batch_names)

        base_prompt = build_prompt(
            step, _prompt_payload, user_query, False,
            known_interactions=known_interactions,
        )

        if batch_directive_fn is not None:
            directive_text = batch_directive_fn(batch_names)
        else:
            directive_text = batch_directive_template.format(
                count=len(batch_names),
                batch_names=", ".join(batch_names),
            )
        directive = f"\n\n{'='*60}\n" + directive_text + f"\n{'='*60}\n"

        call_list.append(dict(
            step=step,
            prompt=base_prompt + directive,
            batch_idx=batch_idx,
            batch_names=batch_names,
        ))

    # ── Fire in parallel ──────────────────────────────────────────
    def _worker(args: dict) -> dict:
        _reserved = int(args.get("_tpm_reserved", 0) or 0)
        try:
            raw, stats = call_gemini_model(
                args["step"], args["prompt"],
                cancel_event=cancel_event,
                request_mode=request_mode,
                batch_poll_seconds=batch_poll_seconds,
                batch_max_wait_seconds=batch_max_wait_seconds,
                previous_interaction_id=None,
            )
            # Feed the shared TPM tracker with the ACTUAL usage. Ordering is
            # important: record first, then release the reservation, so the
            # rolling window already reflects real tokens before the gate
            # shrinks. Otherwise a fast response could briefly drop load to
            # zero and let the next group race ahead.
            if isinstance(stats, dict):
                total = (
                    _coerce_token_count(stats.get("total_tokens"))
                    or (
                        _coerce_token_count(stats.get("prompt_tokens"))
                        + _coerce_token_count(stats.get("thinking_tokens"))
                        + _coerce_token_count(stats.get("output_tokens"))
                    )
                )
                _tpm_record_tokens(total)
            return {**args, "raw_output": raw, "stats": stats}
        finally:
            # Release the pre-dispatch reservation whether or not the call
            # succeeded — failures still consumed some server-side budget
            # and the point of the reservation is just to prevent over-
            # dispatch, not to account exactly.
            if _reserved > 0:
                _tpm_release(_reserved)

    results: Dict[int, dict] = {}
    # Group dispatch is gated by BOTH a hard concurrency cap AND a rolling
    # token-per-minute budget (see _tpm_wait_for_budget). The 10-s wall-
    # clock pause that used to sit between groups is gone — it stalled the
    # pipeline even when the real budget was free, and did nothing useful
    # when a single burst exhausted TPM. The pause env is retained as a
    # floor (default 0) for anyone who needs a blind sleep.
    _RL_GROUP_SIZE = int(
        rate_limit_group_size
        if rate_limit_group_size is not None
        else os.environ.get("PARALLEL_RATE_LIMIT_GROUP", "10")
    )
    _RL_PAUSE_SECONDS = float(os.environ.get("PARALLEL_RATE_LIMIT_PAUSE", "0"))
    eff_workers = min(max_workers, _RL_GROUP_SIZE, len(call_list))

    _rolling_dispatch_enabled = (
        (_is_chain_claim_phase and CHAIN_CLAIM_ROLLING_DISPATCH)
        or (_is_function_mapping_phase and FUNCTION_MAPPING_ROLLING_DISPATCH)
    )
    if _rolling_dispatch_enabled:
        print(
            f"[PARALLEL:{phase_name}] Rolling dispatch enabled — "
            f"{len(call_list)} batch(es), concurrency={eff_workers}. "
            "Finished slots immediately launch the next batch.",
            file=sys.stderr, flush=True,
        )
        next_call_idx = 0
        active: Dict[Any, int] = {}
        completed_count = 0
        rolling_started_at = time.time()
        target_workers = eff_workers
        adaptive_floor = (
            max(1, min(CHAIN_CLAIM_ADAPTIVE_WORKERS, eff_workers))
            if _is_chain_claim_phase
            else eff_workers
        )

        def _looks_like_deadline_pressure(exc: BaseException) -> bool:
            msg = str(exc).lower()
            if "job cancelled by user" in msg:
                return False
            pressure_markers = (
                "504",
                "deadline_exceeded",
                "499",
                "cancelled",
                "readtimeout",
                "read operation timed out",
                "timed out",
                "timeout",
            )
            return any(marker in msg for marker in pressure_markers)

        def _submit_next_call(executor: ThreadPoolExecutor) -> bool:
            nonlocal next_call_idx
            if next_call_idx >= len(call_list):
                return False
            item = call_list[next_call_idx]
            per_call_headroom = _estimate_tokens_per_call(item["step"])
            _tpm_wait_for_budget(
                GEMINI_TPM_BUDGET,
                phase_name,
                needed_headroom=per_call_headroom,
            )
            item["_tpm_reserved"] = per_call_headroom
            _tpm_reserve(per_call_headroom)
            future = executor.submit(_worker, item)
            active[future] = item["batch_idx"]
            next_call_idx += 1
            return True

        with ThreadPoolExecutor(max_workers=eff_workers) as executor:
            while len(active) < target_workers and _submit_next_call(executor):
                pass

            while active:
                done, _pending = futures_wait(
                    set(active),
                    timeout=PARALLEL_GROUP_HEARTBEAT_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    pending_batches = [active[f] + 1 for f in active]
                    print(
                        f"[PARALLEL:{phase_name}] Rolling dispatch waiting on "
                        f"{len(active)} active batch(es) after "
                        f"{time.time() - rolling_started_at:.0f}s: "
                        f"{pending_batches[:8]}"
                        f"{'...' if len(pending_batches) > 8 else ''}; "
                        f"{next_call_idx}/{len(call_list)} submitted",
                        file=sys.stderr, flush=True,
                    )
                    continue

                for future in done:
                    idx = active.pop(future)
                    try:
                        r = future.result()
                        results[idx] = r
                        completed_count += 1
                        print(
                            f"[PARALLEL:{phase_name}] Batch {idx + 1}/{num_batches} "
                            f"done — {r['batch_names'][:4]}"
                            f"{'...' if len(r['batch_names']) > 4 else ''} "
                            f"({completed_count}/{num_batches} complete)",
                            file=sys.stderr, flush=True,
                        )
                    except Exception as exc:
                        completed_count += 1
                        print(
                            f"[PARALLEL:{phase_name}] Batch {idx + 1}/{num_batches} "
                            f"failed: {exc} ({completed_count}/{num_batches} complete)",
                            file=sys.stderr, flush=True,
                        )
                        if (
                            _is_chain_claim_phase
                            and CHAIN_CLAIM_ADAPTIVE_DISPATCH
                            and target_workers > adaptive_floor
                            and _looks_like_deadline_pressure(exc)
                        ):
                            old_target = target_workers
                            target_workers = adaptive_floor
                            print(
                                f"[PARALLEL:{phase_name}] Adaptive throttle — "
                                f"deadline/timeout pressure detected; reducing "
                                f"remaining rolling concurrency from {old_target} "
                                f"to {target_workers}.",
                                file=sys.stderr, flush=True,
                            )

                    while len(active) < target_workers and _submit_next_call(executor):
                        pass
    else:
        _num_groups = math.ceil(len(call_list) / _RL_GROUP_SIZE)
        for _grp_idx in range(_num_groups):
            _grp_start = _grp_idx * _RL_GROUP_SIZE
            _grp_end = _grp_start + _RL_GROUP_SIZE
            _group = call_list[_grp_start:_grp_end]
            if not _group:
                continue
            # Pre-flight: compute the expected token burst for this group so the
            # throttle can reserve headroom before dispatch. Each item's step
            # knows its own ``max_output_tokens``; combining that with a prompt
            # estimate gives a conservative upper bound. The reservation is
            # released by ``_worker`` when each call completes.
            _group_headroom = sum(
                _estimate_tokens_per_call(_item["step"]) for _item in _group
            )
            # Token-aware throttle: wait until (observed + reserved + group_headroom)
            # fits under GEMINI_TPM_BUDGET. This is stricter than the old
            # observed-only gate and prevents the 861k/600k overshoot we saw
            # when 10 workers dispatched before any of them returned to record.
            _tpm_wait_for_budget(GEMINI_TPM_BUDGET, phase_name, needed_headroom=_group_headroom)
            # Reserve the group's estimated cost now. Every item carries a
            # per-call reservation under ``_tpm_reserved`` that ``_worker``
            # releases after the call finishes.
            for _item in _group:
                _per = _estimate_tokens_per_call(_item["step"])
                _item["_tpm_reserved"] = _per
                _tpm_reserve(_per)
            _tpm_before = _tpm_current_usage()
            print(
                f"[PARALLEL:{phase_name}] Group "
                f"{_grp_idx + 1}/{_num_groups} — dispatching {len(_group)} "
                f"batch(es) in parallel "
                f"(TPM window: {_tpm_before}/{GEMINI_TPM_BUDGET}, "
                f"reserved headroom: {_group_headroom})...",
                file=sys.stderr, flush=True,
            )
            with ThreadPoolExecutor(
                max_workers=min(eff_workers, len(_group))
            ) as executor:
                futures = {
                    executor.submit(_worker, a): a["batch_idx"]
                    for a in _group
                }
                pending = set(futures)
                group_started_at = time.time()
                while pending:
                    done, pending = futures_wait(
                        pending,
                        timeout=PARALLEL_GROUP_HEARTBEAT_SECONDS,
                        return_when=FIRST_COMPLETED,
                    )
                    if not done:
                        pending_batches = [futures[f] + 1 for f in pending]
                        print(
                            f"[PARALLEL:{phase_name}] Still waiting on "
                            f"{len(pending)} batch(es) after "
                            f"{time.time() - group_started_at:.0f}s: "
                            f"{pending_batches[:8]}"
                            f"{'...' if len(pending_batches) > 8 else ''}",
                            file=sys.stderr, flush=True,
                        )
                        continue
                    for future in done:
                        idx = futures[future]
                        try:
                            r = future.result()
                            results[idx] = r
                            print(
                                f"[PARALLEL:{phase_name}] Batch {idx + 1}/{num_batches} done — "
                                f"{r['batch_names'][:4]}{'...' if len(r['batch_names']) > 4 else ''}",
                                file=sys.stderr, flush=True,
                            )
                        except Exception as exc:
                            print(
                                f"[PARALLEL:{phase_name}] Batch {idx + 1}/{num_batches} "
                                f"failed: {exc}",
                                file=sys.stderr, flush=True,
                            )
            # Optional fixed floor between groups. Default 0 — the TPM
            # throttle at the top of the next iteration is the real gate.
            if _grp_idx < _num_groups - 1 and _RL_PAUSE_SECONDS > 0:
                print(
                    f"[PARALLEL:{phase_name}] Rate-limit pause: "
                    f"sleeping {_RL_PAUSE_SECONDS}s before next group "
                    f"({_grp_idx + 2}/{_num_groups})...",
                    file=sys.stderr, flush=True,
                )
                time.sleep(_RL_PAUSE_SECONDS)

    if not results:
        print(
            f"[PARALLEL:{phase_name}] All batches failed — continuing with partial data",
            file=sys.stderr, flush=True,
        )
        # Tuple-shape for the same reason as the no-targets early return
        # above — callers unpack into (payload, stats).
        return current_payload, {}

    # ── Detect truncation helper ─────────────────────────────────
    def _is_truncated(raw: str) -> bool:
        return raw.count("{") - raw.count("}") > 0

    def _merge_phase_output(
        raw: str,
        step_obj: StepConfig,
        previous_payload: Dict[str, Any],
        batch_names_for_output: List[str],
    ) -> Dict[str, Any]:
        if _is_chain_claim_phase:
            return _merge_chain_claim_output(
                raw,
                previous_payload,
                batch_names_for_output,
                phase_name,
            )
        return parse_json_output(
            raw,
            list(step_obj.expected_columns),
            previous_payload=previous_payload,
        )

    # ── Retry completely failed batches ──────────────────────────
    failed_batch_indices = [i for i in range(num_batches) if i not in results]
    failed_batch_names = []
    for idx in failed_batch_indices:
        failed_batch_names.extend(batches[idx])

    # Retry failed batches with exponential backoff. Chain-claim calls already
    # have their own missing-pair recovery pass, so keep failed-call retries
    # deliberately short; otherwise one flaky 503 can stall the whole AX/AZ
    # phase for many minutes.
    if failed_batch_names:
        max_failed_retries = (
            CHAIN_CLAIM_FAILED_BATCH_RETRIES
            if _is_chain_claim_phase
            else (
                FUNCTION_MAPPING_FAILED_BATCH_RETRIES
                if _is_function_mapping_phase
                else 3
            )
        )
        max_failed_retries = max(0, int(max_failed_retries))
        for retry_attempt in range(max_failed_retries):
            if not failed_batch_names:
                break
            wait = 2 ** retry_attempt
            print(
                f"[PARALLEL:{phase_name}] Retrying {len(failed_batch_names)} failed "
                f"interactors (attempt {retry_attempt + 1}/{max_failed_retries}, "
                f"wait {wait}s)...",
                file=sys.stderr, flush=True,
            )
            time.sleep(wait)

            retry_calls = []
            for name in failed_batch_names:
                step = step_factory(round_num=200 + retry_attempt) if _accepts_round else step_factory()
                retry_payload = _make_prompt_payload([name])
                base_prompt = build_prompt(step, retry_payload, user_query, False,
                                           known_interactions=known_interactions)
                if batch_directive_fn is not None:
                    directive_text = batch_directive_fn([name])
                else:
                    directive_text = batch_directive_template.format(count=1, batch_names=name)
                if _is_chain_claim_phase:
                    directive_text += (
                        "\n\nFAILED-CALL RETRY — previous call for this pair "
                        "returned no usable text, often because output ran too "
                        "long. Return exactly ONE chain_claims record for this "
                        "pair and exactly ONE function. cellular_process max 2 "
                        "sentences; effect_description max 1 sentence; "
                        "biological_consequence exactly 1 array item with 1 "
                        "short sentence; evidence max 1 item. No background prose."
                    )
                elif _is_function_mapping_phase:
                    directive_text += (
                        "\n\nFAILED-CALL COMPACT RETRY — the previous function-mapping "
                        "batch was too slow or failed. Process exactly this ONE "
                        "interactor. Return compact JSON only. Emit at most TWO "
                        "best-supported functions; cellular_process max 3 sentences; "
                        "effect_description max 1 sentence; biological_consequence "
                        "exactly 1 array item with 1 short sentence; specific_effects "
                        "max 2; evidence max 1 item per function. Do not add "
                        "background prose."
                    )
                directive = f"\n\n{'='*60}\n{directive_text}\n{'='*60}\n"
                retry_calls.append(dict(step=step, prompt=base_prompt + directive,
                                        batch_idx=3000 + retry_attempt * 100, batch_names=[name]))

            still_failed = []
            retry_workers = retry_max_workers or max_workers
            with ThreadPoolExecutor(max_workers=min(retry_workers, len(retry_calls))) as executor:
                futures_retry = {executor.submit(_worker, a): a["batch_names"][0] for a in retry_calls}
                for future in as_completed(futures_retry):
                    interactor_name = futures_retry[future]
                    try:
                        r = future.result()
                        raw = r["raw_output"]
                        if not _is_truncated(raw):
                            current_payload = _merge_phase_output(
                                raw,
                                r["step"],
                                current_payload,
                                r["batch_names"],
                            )
                        else:
                            still_failed.append(interactor_name)
                    except Exception as retry_exc:
                        print(
                            f"[PARALLEL:{phase_name}] Retry worker for "
                            f"interactor '{interactor_name}' raised "
                            f"{type(retry_exc).__name__}: {retry_exc}",
                            file=sys.stderr, flush=True,
                        )
                        still_failed.append(interactor_name)
            failed_batch_names = still_failed

        # Record any permanently failed interactors in metadata
        if failed_batch_names:
            print(
                f"[PARALLEL:{phase_name}] {len(failed_batch_names)} interactors "
                f"failed after all retries: {failed_batch_names}",
                file=sys.stderr, flush=True,
            )
            current_payload.setdefault("_pipeline_metadata", {}).setdefault(
                "failed_batches", []
            ).append({"phase": phase_name, "failed_interactors": failed_batch_names})

    # ── First pass: split-retry truncated batches, then repair as fallback ──
    truncated_indices: List[int] = []
    for idx in sorted(results.keys()):
        r = results[idx]
        raw = r["raw_output"]
        if _is_truncated(raw):
            unclosed = raw.count("{") - raw.count("}")
            batch_names = r["batch_names"]
            # Single-item batch already truncated against max_output_tokens —
            # splitting further would just re-dispatch the same item with
            # the same budget. Try repair_truncated_json FIRST to recover
            # whatever JSON made it through (the model usually wrote most
            # of the response before running out of budget), and only flag
            # for retry if repair fails completely.
            if len(batch_names) <= 1:
                _repaired_raw = repair_truncated_json(raw)
                _recovered = False
                if _repaired_raw and _repaired_raw != raw:
                    try:
                        current_payload = _merge_phase_output(
                            _repaired_raw,
                            r["step"],
                            current_payload,
                            batch_names,
                        )
                        _recovered = True
                        print(
                            f"[PARALLEL:{phase_name}] Batch {idx + 1} truncated "
                            f"({unclosed} unclosed, 1 item) — recovered via "
                            f"repair_truncated_json (no retry dispatched).",
                            file=sys.stderr, flush=True,
                        )
                    except PipelineError:
                        _recovered = False
                if not _recovered:
                    print(
                        f"[PARALLEL:{phase_name}] Batch {idx + 1} truncated "
                        f"({unclosed} unclosed, 1 item) — repair failed, "
                        "flagging for reduced-scope retry.",
                        file=sys.stderr, flush=True,
                    )
                    r["_missing_names"] = list(batch_names)
                    r["_needs_bigger_budget"] = True
                    truncated_indices.append(idx)
                continue
            print(
                f"[PARALLEL:{phase_name}] Batch {idx + 1} truncated "
                f"({unclosed} unclosed, {len(batch_names)} items) — splitting into sub-batches...",
                file=sys.stderr, flush=True,
            )

            # Split into sub-batches of ~ceil(N/2) and run in parallel
            import math as _math
            sub_size = max(1, _math.ceil(len(batch_names) / 2))
            sub_batches = [batch_names[i:i + sub_size] for i in range(0, len(batch_names), sub_size)]
            sub_calls = []
            for sb in sub_batches:
                step = step_factory(round_num=300) if _accepts_round else step_factory()
                sub_payload = _make_prompt_payload(sb)
                base_prompt = build_prompt(step, sub_payload, user_query, False,
                                           known_interactions=known_interactions)
                if batch_directive_fn is not None:
                    directive_text = batch_directive_fn(sb)
                else:
                    directive_text = batch_directive_template.format(
                        count=len(sb), batch_names=", ".join(sb))
                directive = f"\n\n{'='*60}\n{directive_text}\n{'='*60}\n"
                sub_calls.append(dict(step=step, prompt=base_prompt + directive,
                                      batch_idx=5000 + idx * 10, batch_names=sb))

            sub_results = {}
            sub_workers = retry_max_workers or max_workers
            with ThreadPoolExecutor(max_workers=min(sub_workers, len(sub_calls))) as sub_exec:
                sub_futures = {sub_exec.submit(_worker, a): si for si, a in enumerate(sub_calls)}
                for fut in as_completed(sub_futures):
                    si = sub_futures[fut]
                    try:
                        sub_results[si] = fut.result()
                    except Exception as sub_exc:
                        print(f"[PARALLEL:{phase_name}] Sub-batch {si + 1} failed: {sub_exc}",
                              file=sys.stderr, flush=True)

            # Merge successful sub-batches, collect still-missing names
            still_missing = []
            for si, sb_names in enumerate(sub_batches):
                sr = sub_results.get(si)
                if sr and not _is_truncated(sr["raw_output"]):
                    try:
                        current_payload = _merge_phase_output(
                            sr["raw_output"],
                            sr["step"],
                            current_payload,
                            sr["batch_names"],
                        )
                        print(f"[PARALLEL:{phase_name}] Sub-batch {si + 1}/{len(sub_batches)} OK ({sb_names})",
                              file=sys.stderr, flush=True)
                    except PipelineError:
                        still_missing.extend(sb_names)
                elif sr:
                    # Sub-batch still truncated — try repair
                    repaired = repair_truncated_json(sr["raw_output"])
                    try:
                        current_payload = _merge_phase_output(
                            repaired,
                            sr["step"],
                            current_payload,
                            sr["batch_names"],
                        )
                        print(f"[PARALLEL:{phase_name}] Sub-batch {si + 1} repaired ({sb_names})",
                              file=sys.stderr, flush=True)
                    except PipelineError:
                        still_missing.extend(sb_names)
                else:
                    still_missing.extend(sb_names)

            if still_missing:
                r["_missing_names"] = still_missing
                truncated_indices.append(idx)
                print(f"[PARALLEL:{phase_name}] {len(still_missing)} proteins still missing after split-retry",
                      file=sys.stderr, flush=True)
            else:
                print(f"[PARALLEL:{phase_name}] Batch {idx + 1} fully recovered via split-retry",
                      file=sys.stderr, flush=True)
            continue  # Already merged via split-retry path
        else:
            try:
                current_payload = _merge_phase_output(
                    raw,
                    r["step"],
                    current_payload,
                    r["batch_names"],
                )
            except PipelineError as exc:
                print(
                    f"[PARALLEL:{phase_name}] Parse failed for batch {idx + 1}: {exc}",
                    file=sys.stderr, flush=True,
                )

    # ── Retry ONLY missing proteins from truncated batches ─────
    if truncated_indices:
        retry_calls: List[Dict[str, Any]] = []
        for idx in truncated_indices:
            # Only retry the proteins that were actually missing after repair
            names = results[idx].get("_missing_names", results[idx]["batch_names"])
            # Retry each missing protein individually
            sub_name_groups = [[n] for n in names]
            # When the original batch was already size-1 and truncated, we
            # flagged ``_needs_bigger_budget`` so the retry step gets a
            # bumped max_output_tokens — otherwise we'd just re-dispatch
            # the exact same call and truncate again.
            _needs_bigger = bool(results[idx].get("_needs_bigger_budget"))
            for sub_idx, sub_names in enumerate(sub_name_groups):
                if not sub_names:
                    continue
                step = (
                    step_factory(round_num=100 + idx * 10 + sub_idx)
                    if _accepts_round
                    else step_factory()
                )
                # The previous "bump max_output_tokens to 131072" trick crashed
                # against Gemini 3 Flash's hard cap of 65537 (400 INVALID_ARGUMENT).
                # Flash genuinely cannot emit more than 65536 output tokens, so
                # bumping the cap is impossible. Instead, for single-item retries
                # that flagged _needs_bigger, retry with a *reduced-content*
                # directive that asks the LLM for fewer cascades / shorter
                # cellular_process — fitting within the same cap. The directive
                # is appended to the per-call directive_text below.
                _reduced_scope = bool(_needs_bigger)
                if _reduced_scope:
                    print(
                        f"[PARALLEL:{phase_name}] Retry for {sub_names}: "
                        f"reduced-scope (asking LLM for ≤2 cascades, ≤4 sentences "
                        f"per cellular_process to fit Flash's 65k output cap).",
                        file=sys.stderr, flush=True,
                    )
                retry_payload = _make_prompt_payload(sub_names)
                base_prompt = build_prompt(
                    step, retry_payload, user_query, False,
                    known_interactions=known_interactions,
                )
                if batch_directive_fn is not None:
                    retry_text = batch_directive_fn(sub_names)
                else:
                    retry_text = batch_directive_template.format(
                        count=len(sub_names),
                        batch_names=", ".join(sub_names),
                    )
                # Reduced-scope directive for the prior-truncation case:
                # tells the LLM to compress its response so it fits within
                # Flash's 65k output cap (which the previous full-depth
                # attempt overflowed).
                if _reduced_scope:
                    if _is_chain_claim_phase:
                        retry_text += (
                            "\n\nULTRA-COMPACT CHAIN RETRY — the previous "
                            "attempt exceeded the output cap. Return exactly "
                            "ONE chain_claims record and exactly ONE function. "
                            "cellular_process max 2 sentences; "
                            "effect_description max 1 sentence; "
                            "biological_consequence exactly 1 array item with "
                            "1 short sentence; "
                            "specific_effects max 1; evidence max 1. No "
                            "background prose."
                        )
                    else:
                        retry_text += (
                            "\n\nREDUCED SCOPE — the previous attempt for this "
                            "pair exceeded the 65k output cap. Cap your response: "
                            "AT MOST 2 cascades per function, AT MOST 4 sentences "
                            "per cellular_process, AT MOST 3 functions per pair, "
                            "AT MOST 2 evidence entries per function. Skip "
                            "background/methodology preambles. Aim for a tight, "
                            "self-contained JSON under 30k tokens."
                        )
                directive = f"\n\n{'='*60}\n" + retry_text + f"\n{'='*60}\n"
                retry_calls.append(dict(
                    step=step,
                    prompt=base_prompt + directive,
                    batch_idx=1000 + idx * 10 + sub_idx,
                    batch_names=sub_names,
                ))

        print(
            f"[PARALLEL:{phase_name}] Retrying {len(retry_calls)} sub-batches "
            f"from {len(truncated_indices)} truncated batch(es)...",
            file=sys.stderr, flush=True,
        )

        retry_results: Dict[int, dict] = {}
        retry_workers = min(retry_max_workers or max_workers, len(retry_calls))
        with ThreadPoolExecutor(max_workers=retry_workers) as executor:
            futures = {
                executor.submit(_worker, a): a["batch_idx"]
                for a in retry_calls
            }
            for future in as_completed(futures):
                ridx = futures[future]
                try:
                    r = future.result()
                    retry_results[ridx] = r
                    print(
                        f"[PARALLEL:{phase_name}] Retry sub-batch done — "
                        f"{r['batch_names']}",
                        file=sys.stderr, flush=True,
                    )
                except Exception as exc:
                    print(
                        f"[PARALLEL:{phase_name}] Retry sub-batch failed: {exc}",
                        file=sys.stderr, flush=True,
                    )

        # Merge retry results — skip any that are still truncated
        retry_ok = 0
        for ridx in sorted(retry_results.keys()):
            r = retry_results[ridx]
            raw = r["raw_output"]
            if _is_truncated(raw):
                print(
                    f"[PARALLEL:{phase_name}] Retry sub-batch {r['batch_names']} "
                    f"still truncated — skipping (data will be incomplete for these targets)",
                    file=sys.stderr, flush=True,
                )
                continue
            try:
                current_payload = _merge_phase_output(
                    raw,
                    r["step"],
                    current_payload,
                    r["batch_names"],
                )
                retry_ok += 1
            except PipelineError as exc:
                print(
                    f"[PARALLEL:{phase_name}] Retry parse failed for {r['batch_names']}: {exc}",
                    file=sys.stderr, flush=True,
                )

    total_ok = len(results) - len(truncated_indices) + (
        retry_ok if truncated_indices else 0
    )

    # Aggregate token stats from all batches (original + retries)
    phase_token_stats = {"prompt_tokens": 0, "thinking_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for idx in sorted(results.keys()):
        s = results[idx].get("stats") or {}
        for k in phase_token_stats:
            phase_token_stats[k] += int(s.get(k) or 0)
    # Include retry batch stats (split-retry and final-retry results)
    if truncated_indices and retry_results:
        for ridx in sorted(retry_results.keys()):
            s = retry_results[ridx].get("stats") or {}
            for k in phase_token_stats:
                phase_token_stats[k] += int(s.get(k) or 0)

    print(
        f"[PARALLEL:{phase_name}] Completed — "
        f"{total_ok}/{num_batches} batches succeeded"
        + (f" ({len(truncated_indices)} retried, {retry_ok} recovered)" if truncated_indices else ""),
        file=sys.stderr, flush=True,
    )
    return current_payload, phase_token_stats


# Retry-After helper moved to utils/gemini_runtime.extract_retry_after_seconds
# so every retry loop (main dispatcher + arrow validator) shares it.
# Re-export under the old underscore name for backwards compat.
from utils.gemini_runtime import extract_retry_after_seconds as _extract_retry_after_seconds  # noqa: E402


def call_gemini_model(
    step: StepConfig,
    prompt: str,
    cancel_event=None,
    request_mode: Optional[str] = None,
    batch_poll_seconds: Optional[int] = None,
    batch_max_wait_seconds: Optional[int] = None,
    previous_interaction_id: Optional[str] = None,
) -> tuple[str, Dict[str, Any]]:
    """Execute a single Gemini model call with Gemini 3 defaults.

    Args:
        step: Step configuration
        prompt: Prompt text
        cancel_event: Optional threading.Event to check for cancellation
        request_mode: Optional transport mode override ('standard' or 'batch')
        batch_poll_seconds: Optional polling interval for batch jobs
        batch_max_wait_seconds: Optional max wait time for batch jobs
        previous_interaction_id: Optional interaction ID for Interactions API chaining

    Returns:
        tuple: (response_text, token_stats_dict)
            token_stats_dict contains: {
                'prompt_tokens': int,
                'thinking_tokens': int,
                'output_tokens': int,
                'total_tokens': int,
                'request_mode': str,
                'interaction_id': str (only for interaction mode)
            }

    Raises:
        PipelineError: If cancellation is requested
    """
    # Check for cancellation before making expensive API call
    if cancel_event and cancel_event.is_set():
        raise PipelineError("Job cancelled by user")

    is_function_mapping_call = step.name.startswith("step2a_functions")
    is_chain_claim_call = step.name in (
        "step2ax_claim_generation_explicit",
        "step2az_claim_generation_hidden",
    )
    # Reuse a cached Vertex AI client. Function mapping uses a bounded request
    # timeout so one pathological 5-protein batch cannot hold a whole phase for
    # many minutes; the outer dispatcher retries failed batches as compact
    # single-interactor calls.
    api_key = None  # Vertex AI uses ADC; kept for downstream signature compat
    client_timeout_ms = (
        FUNCTION_MAPPING_REQUEST_TIMEOUT_MS
        if is_function_mapping_call and FUNCTION_MAPPING_REQUEST_TIMEOUT_MS > 0
        else (
            CHAIN_CLAIM_REQUEST_TIMEOUT_MS
            if is_chain_claim_call and CHAIN_CLAIM_REQUEST_TIMEOUT_MS > 0
            else None
        )
    )
    client_retry_attempts = (
        FUNCTION_MAPPING_HTTP_RETRY_ATTEMPTS
        if is_function_mapping_call
        else (
            CHAIN_CLAIM_HTTP_RETRY_ATTEMPTS
            if is_chain_claim_call
            else None
        )
    )
    if client_timeout_ms or client_retry_attempts is not None:
        try:
            client = get_client(
                timeout_ms=client_timeout_ms,
                retry_attempts=client_retry_attempts,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            # Some tests monkeypatch get_client with the legacy signature.
            client = get_client()
    else:
        client = get_client()
    try:
        effective_request_mode = (
            parse_request_mode(request_mode)
            if request_mode is not None
            else get_request_mode()
        )
        effective_batch_poll_seconds = resolve_batch_poll_seconds(batch_poll_seconds)
        effective_batch_max_wait_seconds = resolve_batch_max_wait_seconds(batch_max_wait_seconds)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc

    model_name = step.model or get_core_model()
    _raw_thinking = getattr(step, "thinking_level", None)
    thinking_level = _raw_thinking if _raw_thinking is not None else DEFAULT_THINKING_LEVEL
    output_token_limit = getattr(step, "max_output_tokens", None) or DEFAULT_MAX_OUTPUT_TOKENS
    step_temperature = getattr(step, "temperature", None)
    # 2026-05-03: Batch API now applies to BOTH Pro and Flash. Vertex AI's
    # Batch API supports gemini-3-flash-preview with the same 50%
    # cost-saving and quota-exempt characteristics it gives Pro. The
    # earlier Pro-only gate was a leftover from when Batch only accepted
    # Pro models — function-mapping and chain-claim batches on Flash
    # never reached the Batch transport, leaving 50% of their cost on
    # the table. Per Vertex docs (May 2026): Batch supports Gemini 3
    # Flash, batch jobs of up to 200K requests, ≤24h SLA.
    _BATCH_ELIGIBLE_MODELS = {
        "gemini-3.1-pro-preview",
        "gemini-3-flash-preview",
        "gemini-3-pro",
        "gemini-3-flash",
    }
    use_batch_transport = (
        effective_request_mode == "batch" and model_name in _BATCH_ELIGIBLE_MODELS
    )
    allow_server_output_clamp = str(os.getenv("GEMINI_ALLOW_SERVER_OUTPUT_CLAMP", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if effective_request_mode == "batch" and not use_batch_transport:
        print(
            f"   [INFO]Batch requested but model '{model_name}' is not on the "
            f"Batch-eligible list ({sorted(_BATCH_ELIGIBLE_MODELS)}); using standard mode.",
            flush=True,
        )

    def _extract_server_token_cap(error_message: str) -> int | None:
        """Parse server-enforced output cap from INVALID_ARGUMENT text."""
        match = re.search(r"maximum token limit of\s*([0-9,]+)", error_message, flags=re.IGNORECASE)
        if not match:
            return None
        raw = match.group(1).replace(",", "").strip()
        try:
            cap = int(raw)
        except ValueError:
            return None
        return cap if cap > 0 else None

    def _batch_state_name(batch_job: Any) -> str:
        state = getattr(batch_job, "state", None)
        if hasattr(state, "name"):
            return str(state.name)
        return str(state or "UNKNOWN")

    step_response_schema = getattr(step, "response_schema", None)

    def build_generation_config() -> types.GenerateContentConfig:
        return build_generate_content_config(
            thinking_level=thinking_level,
            max_output_tokens=output_token_limit,
            system_instruction=step.system_prompt,
            temperature=step_temperature,
            response_mime_type="application/json" if step_response_schema else None,
            response_json_schema=step_response_schema,
            use_google_search=bool(step.use_google_search),
            use_url_context=bool(getattr(step, "use_url_context", False)),
            use_code_execution=bool(getattr(step, "use_code_execution", False)),
            include_thoughts=False,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    # ── API mode dispatch ───────────────────────────────────────────────
    api_mode = getattr(step, "api_mode", "generate") or "generate"

    if api_mode == "interaction":
        return _call_interaction_mode(
            step, prompt, model_name, thinking_level, output_token_limit,
            step_temperature, previous_interaction_id, cancel_event, api_key,
        )

    if api_mode == "iterative_research":
        return _call_iterative_research_mode(
            step, prompt, model_name, thinking_level, output_token_limit,
            step_temperature, cancel_event, api_key,
        )

    if api_mode == "deep_research":
        return _call_deep_research_mode(step, prompt, cancel_event, api_key)

    # api_mode == "generate" falls through to existing logic below
    # ──────────────────────────────────────────────────────────────────

    def run_single_batch_call(config: types.GenerateContentConfig) -> tuple[str, Dict[str, int]]:
        """Create one batch job, block until completion, and return first inline response."""
        display_name = f"{step.name[:50]}-{int(time.time())}"
        inline_request = types.InlinedRequest(contents=prompt, config=config)
        batch_job = client.batches.create(
            model=model_name,
            src=[inline_request],
            config=types.CreateBatchJobConfig(display_name=display_name),
        )
        batch_name = getattr(batch_job, "name", None)
        if not batch_name:
            raise PipelineError("Batch create succeeded but no batch job name was returned.")

        terminal_failure_states = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
        poll_started_at = time.time()

        while True:
            if cancel_event and cancel_event.is_set():
                try:
                    client.batches.cancel(name=batch_name)
                except Exception as cancel_exc:
                    print(
                        f"[WARN] Batch cancel failed for {batch_name} during "
                        f"user-cancel path: {type(cancel_exc).__name__}: {cancel_exc}",
                        file=sys.stderr,
                    )
                raise PipelineError("Job cancelled by user")

            current_job = client.batches.get(name=batch_name)
            state_name = _batch_state_name(current_job)

            if state_name in {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}:
                destination = getattr(current_job, "dest", None)
                inline_responses = getattr(destination, "inlined_responses", None) or []
                if not inline_responses:
                    raise PipelineError(
                        f"Batch job {batch_name} completed with {state_name} but returned no inline responses."
                    )

                first_response = inline_responses[0]
                response_error = getattr(first_response, "error", None)
                if response_error:
                    raise PipelineError(
                        f"Batch job {batch_name} returned an inline error: {response_error}"
                    )

                response_obj = getattr(first_response, "response", None)
                if response_obj is None:
                    raise PipelineError(f"Batch job {batch_name} returned an empty response object.")

                output_text = extract_text_from_generate_response(response_obj)
                if not output_text:
                    raise PipelineError(
                        f"No text in response ({describe_empty_response(response_obj)})"
                    )

                return output_text, extract_usage_token_stats(response_obj)

            if state_name in terminal_failure_states:
                job_error = getattr(current_job, "error", None)
                raise PipelineError(f"Batch job {batch_name} ended with {state_name}: {job_error}")

            if (time.time() - poll_started_at) > effective_batch_max_wait_seconds:
                try:
                    client.batches.cancel(name=batch_name)
                except Exception as cancel_exc:
                    print(
                        f"[WARN] Batch cancel failed for {batch_name} during "
                        f"max-wait-exceeded path: {type(cancel_exc).__name__}: {cancel_exc}",
                        file=sys.stderr,
                    )
                raise PipelineError(
                    f"Batch job {batch_name} exceeded max wait of {effective_batch_max_wait_seconds}s."
                )

            time.sleep(float(effective_batch_poll_seconds))

    max_retries = (
        max(1, CHAIN_CLAIM_MAX_RETRIES)
        if is_chain_claim_call
        else (
            max(1, FUNCTION_MAPPING_MAX_RETRIES)
            if is_function_mapping_call
            else int(os.getenv("GEMINI_MAX_RETRIES", "8"))
        )
    )
    base_delay = (
        max(0.5, CHAIN_CLAIM_RETRY_BASE_DELAY)
        if is_chain_claim_call
        else (
            max(0.5, FUNCTION_MAPPING_RETRY_BASE_DELAY)
            if is_function_mapping_call
            else float(os.getenv("GEMINI_RETRY_BASE_DELAY", "5.0"))
        )
    )

    for attempt in range(1, max_retries + 1):
        config = build_generation_config()

        try:
            if attempt == 1:
                temp_label = "default" if step_temperature is None else str(step_temperature)
                reasoning_label = f"thinking_level={thinking_level}"
                transport_label = "batch" if use_batch_transport else "standard"
                print(
                    f"   Calling {model_name} via {transport_label} "
                    f"({reasoning_label}, max_output_tokens={output_token_limit}, temperature={temp_label})",
                    flush=True,
                )

            if model_name == "gemini-3.1-pro-preview":
                _increment_run_request_metric("core_calls_3pro", 1)

            if use_batch_transport:
                output_text, token_stats = run_single_batch_call(config)
                token_stats["request_mode"] = "batch"
            else:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
                output_text = extract_text_from_generate_response(response)
                if not output_text:
                    raise PipelineError(
                        f"No text in response ({describe_empty_response(response)})"
                    )
                token_stats = extract_usage_token_stats(response)
                token_stats["request_mode"] = "standard"

            token_stats["prompt_tokens"] = _coerce_token_count(token_stats.get("prompt_tokens", 0))
            token_stats["thinking_tokens"] = _coerce_token_count(token_stats.get("thinking_tokens", 0))
            token_stats["output_tokens"] = _coerce_token_count(token_stats.get("output_tokens", 0))
            token_stats["total_tokens"] = _coerce_token_count(token_stats.get("total_tokens", 0))
            if token_stats["total_tokens"] <= 0:
                token_stats["total_tokens"] = (
                    token_stats["prompt_tokens"]
                    + token_stats["thinking_tokens"]
                    + token_stats["output_tokens"]
                )
            return output_text, token_stats

        except Exception as e:
            if isinstance(e, PipelineError):
                raise

            error_str = str(e)
            error_lower = error_str.lower()
            is_quota_error = "429" in error_str or "resource_exhausted" in error_lower
            is_service_unavailable = (
                "503" in error_str
                or "unavailable" in error_lower
                or "service is currently unavailable" in error_lower
            )
            is_deadline_pressure = (
                "504" in error_str
                or "deadline_exceeded" in error_lower
                or "499" in error_str
                or "cancelled" in error_lower
                or "deadline expired" in error_lower
            )
            # Transient network failures (TCP resets, broken pipes, timeouts)
            # share the same "retry with backoff" disposition as transient
            # quota errors. Without this, a single ConnectionResetError
            # during chain-claim generation would escape the main retry
            # gate and kill the entire step.
            is_transient_net = is_transient_network_error(e)
            is_invalid_argument = "invalid_argument" in error_lower or "invalid_request" in error_lower

            if is_daily_model_quota_exhausted(e):
                raise PipelineError(
                    f"Core model daily quota exhausted for {model_name}; "
                    f"no fallback is allowed for core stages: {e}"
                )

            if is_invalid_argument:
                server_cap = _extract_server_token_cap(error_str)
                if server_cap and output_token_limit > server_cap:
                    if allow_server_output_clamp and attempt < max_retries:
                        print(
                            f"   [WARN]Server cap for {model_name} is {server_cap} tokens; "
                            f"clamping requested max_output_tokens from {output_token_limit} to {server_cap} and retrying.",
                            flush=True,
                        )
                        output_token_limit = server_cap
                        time.sleep(1.0)
                        continue
                    raise PipelineError(
                        f"Server cap for {model_name} is {server_cap} tokens, requested {output_token_limit}. "
                        f"Auto-clamping is disabled (GEMINI_ALLOW_SERVER_OUTPUT_CLAMP=false)."
                    )
                raise PipelineError(f"Non-retryable request/config error for {model_name}: {e}")

            if is_quota_error:
                # Honour server's Retry-After when present; otherwise
                # exponential (5, 10, 20, 40, 80, capped at 120) + jitter.
                # Linear 30*attempt was both too patient early (wasting
                # 30s on a transient) and too impatient late (60/90/120s
                # when the quota needed more).
                retry_after = _extract_retry_after_seconds(e)
                if retry_after is not None:
                    delay = float(retry_after) + random.uniform(0.0, 2.0)
                    source_note = " (honoring Retry-After)"
                else:
                    delay = min(120.0, 5.0 * (2 ** (attempt - 1))) + random.uniform(0.0, 2.0)
                    source_note = ""
                print(
                    f"   [WARN]Quota exceeded (429). Pausing for {delay:.1f}s{source_note}...",
                    flush=True,
                )
            elif is_service_unavailable or is_transient_net or is_deadline_pressure:
                # Same backoff shape as quota, different log prefix so
                # post-run triage can tell them apart.
                cap = 12.0 if is_chain_claim_call else 120.0
                delay = min(cap, base_delay * (2 ** (attempt - 1))) + random.uniform(0.0, 1.0)
                if is_service_unavailable:
                    label = "Service unavailable (503)"
                elif is_deadline_pressure:
                    label = "Deadline/cancel pressure"
                else:
                    label = f"Transient network error ({type(e).__name__})"
                if attempt < max_retries:
                    print(
                        f"   [WARN]{label}: {e}. Pausing for {delay:.1f}s "
                        f"(attempt {attempt}/{max_retries})...",
                        flush=True,
                    )
                else:
                    print(
                        f"   [WARN]{label}: {e}. No retry budget left "
                        f"(attempt {attempt}/{max_retries}).",
                        flush=True,
                    )
            else:
                # Standard exponential backoff for other errors
                delay = base_delay * (2 ** (attempt - 1))

            if attempt < max_retries:
                if (
                    not is_quota_error
                    and not is_transient_net
                    and not is_service_unavailable
                    and not is_deadline_pressure
                ):
                    print(f"   [WARN]Error: {e}. Retrying in {delay:.1f}s...", flush=True)
                time.sleep(delay)
            else:
                raise PipelineError(f"Failed after {max_retries} attempts: {e}")


# ═══════════════════════════════════════════════════════════════
# INTERACTION & DEEP RESEARCH MODE HELPERS
# ═══════════════════════════════════════════════════════════════

def _call_interaction_mode(
    step: StepConfig,
    prompt: str,
    model_name: str,
    thinking_level: str,
    max_output_tokens: int,
    temperature: Optional[float],
    previous_interaction_id: Optional[str],
    cancel_event,
    api_key: str,
) -> tuple[str, Dict[str, Any]]:
    """Execute a pipeline step via the Interactions API with optional chaining."""
    from dataclasses import replace as dc_replace

    if cancel_event and cancel_event.is_set():
        raise PipelineError("Job cancelled by user")

    tools = build_interaction_tools(
        use_google_search=bool(step.use_google_search),
        use_url_context=bool(getattr(step, "use_url_context", False)),
    )
    gen_config = build_interaction_generation_config(
        thinking_level=thinking_level,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )
    step_response_schema = getattr(step, "response_schema", None)

    system_instruction = step.system_prompt

    # Optionally cache system prompt
    if getattr(step, "cache_system_prompt", False) and system_instruction:
        try:
            create_or_get_system_cache(
                system_text=system_instruction,
                model=model_name,
                api_key=api_key,
            )
            # Note: Interactions API doesn't directly accept cached_content,
            # but sending the same system prompt benefits from implicit caching
        except Exception as e:
            print(f"[WARN] Cache creation failed: {e}", file=sys.stderr)

    try:
        interaction = call_interaction(
            input_text=prompt,
            model=model_name,
            system_instruction=system_instruction,
            generation_config=gen_config,
            tools=tools if tools else None,
            response_format=step_response_schema,
            store=True,
            previous_interaction_id=previous_interaction_id,
            api_key=api_key,
            max_retries=8,
            base_delay=5.0,
        )
    except RuntimeError as exc:
        error_lower = str(exc).lower()

        # Stale chain fallback: retry without previous_interaction_id
        if previous_interaction_id and ("not found" in error_lower or "not_found" in error_lower):
            print(
                f"   [WARN] Stale interaction chain detected, retrying without chaining...",
                flush=True,
            )
            interaction = call_interaction(
                input_text=prompt,
                model=model_name,
                system_instruction=system_instruction,
                generation_config=gen_config,
                tools=tools if tools else None,
                response_format=step_response_schema,
                store=True,
                previous_interaction_id=None,
                api_key=api_key,
                max_retries=8,
                base_delay=5.0,
            )
        # Quota fallback: fall back to generate mode
        elif is_quota_error(exc):
            print(
                f"   [WARN] Quota hit on interaction mode, falling back to generate...",
                flush=True,
            )
            fallback_step = dc_replace(step, api_mode="generate")
            return call_gemini_model(fallback_step, prompt, cancel_event=cancel_event)
        # Transient network failures: same disposition as quota — fall
        # back to generate mode so a TCP reset on the interaction endpoint
        # doesn't kill the whole step. generate_content has its own retry
        # loop that will absorb further blips.
        elif is_transient_network_error(exc):
            print(
                f"   [WARN] Transient network error ({type(exc).__name__}) on "
                f"interaction mode, falling back to generate: {exc}",
                flush=True,
            )
            fallback_step = dc_replace(step, api_mode="generate")
            return call_gemini_model(fallback_step, prompt, cancel_event=cancel_event)
        # Copyright/recitation fallback: fall back to generate mode
        elif "copyright" in error_lower or "recitation" in error_lower:
            print(
                f"   [WARN] Copyright/recitation block on interaction mode, falling back to generate...",
                flush=True,
            )
            fallback_step = dc_replace(step, api_mode="generate")
            return call_gemini_model(fallback_step, prompt, cancel_event=cancel_event)
        else:
            raise PipelineError(f"Interaction call failed: {exc}") from exc

    text = extract_text_from_interaction(interaction)
    if not text:
        raise PipelineError("Interaction returned empty text")

    token_stats = extract_usage_token_stats(interaction)
    token_stats["request_mode"] = "interaction"
    token_stats["interaction_id"] = getattr(interaction, "id", None)

    for k in ("prompt_tokens", "thinking_tokens", "output_tokens", "total_tokens"):
        token_stats[k] = _coerce_token_count(token_stats.get(k, 0))
    if token_stats["total_tokens"] <= 0:
        token_stats["total_tokens"] = sum(
            token_stats[k] for k in ("prompt_tokens", "thinking_tokens", "output_tokens")
        )

    # Detect likely truncation (only check unclosed braces — the Interactions
    # API does not return token usage metadata, so all-zero stats are expected).
    # Do NOT repair here — let the caller (_run_parallel_batched_phase) handle
    # it via retry with smaller batches to get COMPLETE data.
    open_braces = text.count("{") - text.count("}")
    if open_braces > 0:
        print(
            f"   [WARN] Possible truncated output: {open_braces} unclosed braces",
            file=sys.stderr,
            flush=True,
        )

    return text, token_stats


def _call_deep_research_mode(
    step: StepConfig,
    prompt: str,
    cancel_event,
    api_key: str,
) -> tuple[str, Dict[str, Any]]:
    """Execute a pipeline step via the Deep Research API with polling."""
    if cancel_event and cancel_event.is_set():
        raise PipelineError("Job cancelled by user")

    agent = get_model("deep_research")
    print(f"   Calling Deep Research agent ({agent})...", flush=True)

    try:
        interaction = call_deep_research(
            input_text=prompt,
            agent=agent,
            tools=[{"type": "google_search"}],
            poll_interval_seconds=10.0,
            max_wait_seconds=600.0,
            api_key=api_key,
            cancel_event=cancel_event,
        )
    except RuntimeError as exc:
        raise PipelineError(f"Deep research failed: {exc}") from exc

    text = extract_text_from_interaction(interaction)
    if not text:
        raise PipelineError("Deep research returned empty text")

    token_stats = extract_usage_token_stats(interaction)
    token_stats["request_mode"] = "deep_research"

    for k in ("prompt_tokens", "thinking_tokens", "output_tokens", "total_tokens"):
        token_stats[k] = _coerce_token_count(token_stats.get(k, 0))
    if token_stats["total_tokens"] <= 0:
        token_stats["total_tokens"] = sum(
            token_stats[k] for k in ("prompt_tokens", "thinking_tokens", "output_tokens")
        )

    return text, token_stats


# ═══════════════════════════════════════════════════════════════
# ITERATIVE RESEARCH MODE HANDLER
# ═══════════════════════════════════════════════════════════════

# Cache probe results so we don't waste an API call on every run
import threading as _threading
_interactions_api_probe_cache: Dict[str, bool] = {}  # model -> supports_interactions
_probe_cache_lock = _threading.Lock()

def _build_iteration_prompt(
    iter_cfg,
    base_prompt: str,
    accumulated_ctx: Optional[Dict[str, Any]],
    idx: int,
    total: int,
) -> str:
    """Build the prompt for a single iteration, injecting accumulated context."""
    parts: list[str] = []
    parts.append(f"[ITERATION {idx + 1} of {total}: {iter_cfg.focus}]")
    parts.append("")

    if accumulated_ctx:
        # Inject a compact summary so the model sees what was found so far
        found_count = len(accumulated_ctx.get("interactors", []))
        history = accumulated_ctx.get("interactor_history", [])
        parts.append(f"ACCUMULATED CONTEXT FROM PREVIOUS ITERATIONS ({found_count} interactors found so far):")
        parts.append(f"KNOWN PROTEINS: {', '.join(history[:80])}")  # cap for token safety
        parts.append("")
        parts.append("BUILD ON the above. Add ONLY NEW proteins not already discovered.")
        parts.append("")

    # The base_prompt already contains known_interactions exclusion context from build_prompt()
    if idx == 0 and base_prompt:
        parts.append(base_prompt)
    else:
        parts.append(iter_cfg.prompt_template)

    if iter_cfg.search_queries_hint:
        parts.append("\nSUGGESTED SEARCH QUERIES (use Google Search grounding for these):")
        for q in iter_cfg.search_queries_hint:
            parts.append(f"  - {q}")

    return "\n".join(parts)


def _extract_json_object(
    text: str,
    require_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Extract the outermost JSON object from *text* using brace-counting.

    Handles nested braces correctly and tries multiple start positions so
    surrounding prose doesn't break parsing.

    ``require_key`` — when set, only return a parsed object that contains
    this top-level key. Fix for the "discovery_low_result" bug: when the
    outer ``{ "ctx_json": { ... } }`` root was truncated or malformed,
    the parser would fall through to the FIRST inner `{` that parsed
    successfully — usually a single interactor like
    ``{ "primary": "VCP", "interaction_type": "direct", "upstream": null }``
    — which looked like valid JSON but was a fragment, not the root.
    Downstream code then read ``.get("ctx_json", parsed)`` and walked a
    one-interactor object shape, reporting 0 total interactors.

    With ``require_key="ctx_json"`` (or ``"interactors"``), we skip any
    parse result that doesn't contain the key we actually need and keep
    searching — so truncation doesn't silently give us a single-interactor
    fragment instead of the whole response.
    """
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            start = i
            break
    if start < 0:
        return None

    # Try from every top-level '{' position until one parses AND satisfies
    # the require_key constraint (if any).
    while start >= 0:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if require_key and isinstance(parsed, dict):
                            if require_key not in parsed:
                                # Valid JSON but wrong shape — keep looking.
                                break
                        return parsed
                    except json.JSONDecodeError:
                        break  # this brace pair didn't parse — try next '{'
        # Find next '{' after current start
        next_start = text.find("{", start + 1)
        if next_start < 0:
            break
        start = next_start

    return None


def _salvage_interactors_array(text: str) -> Optional[list]:
    """Last-resort: find ``"interactors": [...]`` and collect every
    complete ``{ ... }`` object inside it.

    Fires when the outer JSON is truncated mid-array (common failure
    mode when the model hits max_output_tokens). We scan from the
    opening ``[`` and pull out each complete object; a mid-object
    truncation at the end is simply skipped, so we still recover the
    earlier entries. Returns ``None`` only if we can't find ``[`` at
    all.
    """
    m_key = text.find('"interactors"')
    if m_key < 0:
        return None
    arr_start = text.find("[", m_key)
    if arr_start < 0:
        return None

    results: list = []
    i = arr_start + 1
    n = len(text)
    while i < n:
        # Skip whitespace and separators between objects.
        while i < n and text[i] in " \n\r\t,":
            i += 1
        if i >= n:
            break
        if text[i] == "]":
            # Normal array close.
            break
        if text[i] != "{":
            # Malformed — bail.
            break
        # Brace-count one object starting here.
        depth = 0
        in_string = False
        escape_next = False
        obj_start = i
        obj_end = None
        while i < n:
            ch = text[i]
            if escape_next:
                escape_next = False
                i += 1
                continue
            if ch == "\\":
                escape_next = True
                i += 1
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                i += 1
                continue
            if in_string:
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    obj_end = i + 1
                    break
            i += 1
        if obj_end is None:
            # Truncation mid-object — ignore the partial, return what we have.
            break
        try:
            obj = json.loads(text[obj_start:obj_end])
            if isinstance(obj, dict) and obj.get("primary"):
                results.append(obj)
        except json.JSONDecodeError:
            # Skip this object, advance past it and try the next.
            pass
        i = obj_end
    return results or None


def _merge_iteration_output(
    accumulated: Optional[Dict[str, Any]],
    raw_text: str,
) -> Dict[str, Any]:
    """Parse iteration JSON and merge into accumulated context.

    Deduplicates interactors by ``primary`` name. Enriches existing entries
    non-destructively. Merges tracking arrays.
    """
    # Strip markdown fences if present
    cleaned = strip_code_fences(raw_text)

    # Strategy 1: Direct JSON parse
    parsed = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 2: Brace-counting — but REQUIRE the resulting object to
    # contain ``ctx_json`` or ``interactors`` at the top level. Without
    # this constraint a truncated response would fall through to the
    # first inner `{` that parses (often a single interactor), masquerade
    # as the root, and report 0 interactors. See _extract_json_object
    # docstring for the full bug story.
    if parsed is None:
        parsed = _extract_json_object(cleaned, require_key="ctx_json")
    if parsed is None:
        parsed = _extract_json_object(cleaned, require_key="interactors")

    # Strategy 3: Raw text (before fence stripping).
    if parsed is None:
        parsed = _extract_json_object(raw_text, require_key="ctx_json")
    if parsed is None:
        parsed = _extract_json_object(raw_text, require_key="interactors")

    # Strategy 4: SALVAGE — find the ``"interactors": [...]`` substring
    # and parse just the array. Covers truncation where the outer root
    # was cut off but the array itself is intact. Synthesize a minimal
    # ctx_json around it so downstream code sees the expected shape.
    if parsed is None or (
        isinstance(parsed, dict)
        and "ctx_json" not in parsed
        and "interactors" not in parsed
    ):
        salvaged = _salvage_interactors_array(cleaned) or _salvage_interactors_array(raw_text)
        if salvaged:
            print(
                f"   [SALVAGE] Rescued {len(salvaged)} interactors from "
                "truncated/malformed response via array-scan fallback.",
                flush=True,
            )
            parsed = {"ctx_json": {"interactors": salvaged}}

    if parsed is None:
        print(
            f"   [WARN] Could not parse JSON from iteration output ({len(raw_text)} chars)",
            flush=True,
        )
        print(f"   [WARN] Raw preview: {raw_text[:500]!r}", flush=True)
        return accumulated or {}

    # Accept either {"ctx_json": {...}} or {"interactors": [...], "main": ...}
    # shapes. Previously ``parsed.get("ctx_json", parsed)`` would silently
    # return a single-interactor fragment if the parser picked the wrong
    # `{`; the require_key filter above makes that impossible, so this
    # lookup is now safe.
    if isinstance(parsed, dict) and "ctx_json" in parsed:
        new_ctx = parsed["ctx_json"]
    elif isinstance(parsed, dict) and "interactors" in parsed:
        new_ctx = parsed
    else:
        new_ctx = {}
    if not isinstance(new_ctx, dict):
        return accumulated or {}

    if accumulated is None:
        # First iteration — initialise, but deduplicate within the output
        seen: set[str] = set()
        deduped: list[dict] = []
        for i in new_ctx.get("interactors", []):
            name = i.get("primary")
            if name and name not in seen:
                seen.add(name)
                deduped.append(i)
        if deduped:
            new_ctx["interactors"] = deduped
        # Diagnostic: when the first iteration returns 0 (or suspiciously
        # few) interactors, print enough of the raw response that we can
        # tell what actually came back. Well-studied proteins should
        # always return 20+; a low-result log line is the signal that
        # either the prompt isn't landing or the model produced empty
        # JSON. Silent when the result is normal.
        if len(deduped) < 5:
            _ctx_present = "ctx_json" in parsed if isinstance(parsed, dict) else False
            _raw_interactors = new_ctx.get("interactors") if isinstance(new_ctx, dict) else None
            from utils.observability import log_event
            log_event(
                "discovery_low_result",
                level="warn",
                tag="ITER 1 ZERO-RESULT",
                deduped=len(deduped),
                ctx_json_present=_ctx_present,
                raw_interactors_type=type(_raw_interactors).__name__,
                raw_interactors_len=(
                    len(_raw_interactors) if isinstance(_raw_interactors, list) else None
                ),
                response_preview=(raw_text or "")[:500],
            )
        return new_ctx

    # ── Merge interactors (dedup by primary, case-insensitive) ─────
    existing_names = {
        (i.get("primary") or "").upper() for i in accumulated.get("interactors", [])
    }
    for interactor in new_ctx.get("interactors", []):
        name = interactor.get("primary")
        if not name:
            continue
        if name.upper() not in existing_names:
            accumulated.setdefault("interactors", []).append(interactor)
            existing_names.add(name.upper())
        else:
            # Enrich existing entry non-destructively (e.g. reclassification).
            # ``chain_context`` is included so a later iteration that discovers
            # the query's correct mid-chain position can attach it to the
            # existing interactor without overwriting earlier data.
            for existing in accumulated["interactors"]:
                if (existing.get("primary") or "").upper() == name.upper():
                    for key in (
                        "interaction_type", "upstream_interactor",
                        "mediator_chain", "depth", "support_summary",
                        "chain_context",
                    ):
                        if interactor.get(key) and not existing.get(key):
                            existing[key] = interactor[key]
                    # Merge functions from later iterations (dedup by name)
                    new_funcs = interactor.get("functions", [])
                    if new_funcs:
                        existing_funcs = existing.setdefault("functions", [])
                        existing_fn_names = {
                            (f.get("function") or "").strip().lower()
                            for f in existing_funcs
                        }
                        # Pre-compute existing word sets for O(N) instead of O(N*M)
                        existing_proc_words = [
                            _dedup_words(ef.get("cellular_process", ""))
                            for ef in existing_funcs
                        ]
                        for nf in new_funcs:
                            nf_name = (nf.get("function") or "").strip().lower()
                            if nf_name and nf_name not in existing_fn_names:
                                nf_proc = _dedup_words(nf.get("cellular_process", ""))
                                is_mech_dup = any(
                                    _is_mechanism_duplicate(nf_proc, epw)
                                    for epw in existing_proc_words
                                )
                                if not is_mech_dup:
                                    existing_funcs.append(nf)
                                    existing_fn_names.add(nf_name)
                                    existing_proc_words.append(nf_proc)
                    break

    # ── Merge tracking arrays ──────────────────────────────────────
    # ``upstream_of_main`` is populated by the dedicated upstream-context
    # iteration and union-merged across later iterations so the full
    # upstream set is carried through the entire run.
    for array_key in (
        "interactor_history", "search_history", "function_batches",
        "upstream_of_main",
    ):
        existing_set = set(accumulated.get(array_key, []))
        for item in new_ctx.get(array_key, []):
            if item not in existing_set:
                accumulated.setdefault(array_key, []).append(item)
                existing_set.add(item)

    # ── Merge indirect_interactors with upstream + cycle validation ──
    # PR-3c: LLM-emitted indirect_interactors can carry an upstream_interactor
    # that doesn't exist in the known interactor set (orphan chain) or a
    # mediator_chain with cycles / self-loops. Log every violation; do not
    # drop rows silently — promoting with the bad hint is still better
    # than losing the protein entirely, and log visibility lets us tune.
    existing_indirect = {
        i.get("name") for i in accumulated.get("indirect_interactors", [])
    }
    if new_ctx.get("indirect_interactors"):
        try:
            from utils.upstream_interactor_validator import (
                validate_upstream_hint,
                validate_chain_shape,
            )
            known_symbols = [
                i.get("primary") for i in accumulated.get("interactors", [])
                if isinstance(i, dict) and i.get("primary")
            ] + list(accumulated.get("interactor_history", []))
            main_symbol_for_check = accumulated.get("main") or new_ctx.get("main") or ""
            for indirect in new_ctx["indirect_interactors"]:
                if not isinstance(indirect, dict):
                    continue
                # Shape validator accepts "primary" or "name" as the key.
                norm = {
                    "primary": indirect.get("name") or indirect.get("primary"),
                    "upstream_interactor": indirect.get("upstream_interactor"),
                    "mediator_chain": indirect.get("mediator_chain") or [],
                }
                u_verdict = validate_upstream_hint(
                    norm,
                    main_symbol=main_symbol_for_check,
                    known_interactors=known_symbols,
                )
                if u_verdict.reason == "orphan":
                    print(
                        f"[UPSTREAM ORPHAN] {u_verdict.interactor!r}: "
                        f"upstream={u_verdict.upstream!r} not in known "
                        "interactor set — promoting anyway but chain may dangle.",
                        file=sys.stderr, flush=True,
                    )
                elif u_verdict.reason == "self-reference":
                    print(
                        f"[UPSTREAM SELF] {u_verdict.interactor!r}: "
                        "upstream == primary (self-reference). Dropping hint.",
                        file=sys.stderr, flush=True,
                    )
                    indirect["upstream_interactor"] = None
                c_verdict = validate_chain_shape(norm)
                if c_verdict.reason != "valid":
                    print(
                        f"[CHAIN CYCLE] {c_verdict.interactor!r}: "
                        f"chain={c_verdict.chain} has "
                        f"cycles={c_verdict.cycles} "
                        f"self_loops={c_verdict.self_loops}",
                        file=sys.stderr, flush=True,
                    )
        except Exception as _upstream_exc:
            print(
                f"[UPSTREAM VALIDATOR] failed: "
                f"{type(_upstream_exc).__name__}: {_upstream_exc}",
                file=sys.stderr, flush=True,
            )

    for indirect in new_ctx.get("indirect_interactors", []):
        if indirect.get("name") not in existing_indirect:
            accumulated.setdefault("indirect_interactors", []).append(indirect)
            existing_indirect.add(indirect.get("name"))

    # Ensure main is set
    if not accumulated.get("main") and new_ctx.get("main"):
        accumulated["main"] = new_ctx["main"]

    return accumulated


def _call_iterative_research_mode(
    step: StepConfig,
    prompt: str,
    model_name: str,
    thinking_level: str,
    max_output_tokens: int,
    temperature: Optional[float],
    cancel_event,
    api_key: str,
    user_query: str = "",
) -> tuple[str, Dict[str, Any]]:
    """Execute iterative discovery via N chained Interactions API calls.

    Each iteration:
    1. Builds iteration-specific prompt (appending prior context)
    2. Calls Interactions API with google_search + url_context tools
    3. Parses incremental JSON output
    4. Merges into running ctx_json accumulator
    5. Chains to next iteration via previous_interaction_id

    Returns consolidated text (final merged JSON) and aggregated token stats.
    """
    from pipeline.prompts.iterative_research_steps import get_iterative_system_prompt

    iteration_configs = getattr(step, "iteration_configs", None)
    if not iteration_configs:
        raise PipelineError(
            "iterative_research mode requires non-empty iteration_configs on StepConfig"
        )

    if cancel_event and cancel_event.is_set():
        raise PipelineError("Job cancelled by user")

    system_instruction = get_iterative_system_prompt()
    tools = build_interaction_tools(use_google_search=True, use_url_context=True)
    gen_config = build_interaction_generation_config(
        thinking_level=thinking_level,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )
    step_response_schema = getattr(step, "response_schema", None)

    # Best-effort cache the system prompt across iterations
    if getattr(step, "cache_system_prompt", False) and system_instruction:
        try:
            create_or_get_system_cache(
                system_text=system_instruction,
                model=model_name,
                api_key=api_key,
            )
        except Exception as e:
            print(f"[WARN] Cache creation failed: {e}", file=sys.stderr)

    inter_iteration_delay = float(os.getenv("ITERATIVE_DELAY_SECONDS", "2.0"))
    accumulated_ctx: Optional[Dict[str, Any]] = None
    previous_interaction_id: Optional[str] = None
    aggregated_stats: Dict[str, Any] = {
        "prompt_tokens": 0,
        "thinking_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "iteration_count": 0,
        "interaction_ids": [],
    }

    total_iters = len(iteration_configs)

    # Probe: test if Interactions API supports this model.
    # Use cached result; first call per model tries the real API (no wasteful ping).
    _use_generate_content_fallback = False
    with _probe_cache_lock:
        _cached_probe = _interactions_api_probe_cache.get(model_name)
    if _cached_probe is not None:
        _use_generate_content_fallback = not _cached_probe
        if _use_generate_content_fallback:
            print(f"   [PROBE] Cached: using multi-turn generate_content for {model_name}", flush=True)
    else:
        # For known Vertex AI models that don't support Interactions API,
        # pre-populate the cache to avoid even one failed call.
        _vertex_models = {"gemini-3-flash-preview", "gemini-2.5-flash-preview-05-20",
                          "gemini-2.0-flash", "gemini-2.0-flash-001"}
        if any(v in model_name for v in _vertex_models):
            _use_generate_content_fallback = True
            with _probe_cache_lock:
                _interactions_api_probe_cache[model_name] = False
            print(f"   [PROBE] Known Vertex model {model_name} — using generate_content (no probe delay)", flush=True)
        else:
            print(f"   [PROBE] Will try Interactions API for {model_name} on first iteration", flush=True)

    # ── Pre-build generate_content resources (reused across all iterations) ──
    _fb_client = None
    _fb_config = None
    _conversation_history: list = []   # multi-turn Content list for generate_content

    if _use_generate_content_fallback:
        from utils.gemini_runtime import get_client, build_generate_content_config
        from google.genai import types as _genai_types
        _fb_client = get_client(api_key)
        _fb_config = build_generate_content_config(
            thinking_level=thinking_level,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
            temperature=temperature,
            response_mime_type="application/json",
            response_json_schema=step_response_schema,
            use_google_search=True,
            include_thoughts=False,
        )

    for idx, iter_cfg in enumerate(iteration_configs):
        if cancel_event and cancel_event.is_set():
            raise PipelineError("Job cancelled by user")

        iter_prompt = _build_iteration_prompt(
            iter_cfg, prompt, accumulated_ctx, idx, total_iters,
        )

        print(
            f"   [ITER {idx + 1}/{total_iters}] {iter_cfg.name}: {iter_cfg.focus}",
            flush=True,
        )
        # Wake SSE listeners so frontend shows progress during discovery
        _sse_protein = user_query or (accumulated_ctx or {}).get("main", "")
        if _sse_protein:
            try:
                from services.state import notify_job_update
                notify_job_update(_sse_protein)
            except Exception:
                pass

        # If Interactions API was already rejected, use multi-turn generate_content
        if _use_generate_content_fallback:
            # Append user turn to conversation history (model sees full prior context)
            _conversation_history.append(
                _genai_types.Content(
                    role="user",
                    parts=[_genai_types.Part(text=iter_prompt)],
                )
            )
            _fb_response = _fb_client.models.generate_content(
                model=model_name,
                contents=_conversation_history,
                config=_fb_config,
            )
            text = (_fb_response.text or "").strip()
            if text:
                # Append model turn so next iteration sees this response
                _conversation_history.append(
                    _genai_types.Content(
                        role="model",
                        parts=[_genai_types.Part(text=text)],
                    )
                )
                accumulated_ctx = _merge_iteration_output(accumulated_ctx, text)
                interactors_found = len(accumulated_ctx.get("interactors", []))
                print(f"   [ITER {idx + 1}] (generate_content) Total interactors: {interactors_found}", flush=True)
            else:
                print(f"   [WARN] Iteration {idx + 1} returned empty (generate_content)", flush=True)
                # Must append a model turn to avoid consecutive user turns
                _conversation_history.append(
                    _genai_types.Content(
                        role="model",
                        parts=[_genai_types.Part(text="No new proteins found in this iteration.")],
                    )
                )

            # Track token stats from generate_content response
            iter_stats = extract_usage_token_stats(_fb_response)
            for k in ("prompt_tokens", "thinking_tokens", "output_tokens", "total_tokens"):
                aggregated_stats[k] += _coerce_token_count(iter_stats.get(k, 0))
            aggregated_stats["iteration_count"] += 1
            continue

        try:
            interaction = call_interaction(
                input_text=iter_prompt,
                model=model_name,
                system_instruction=system_instruction,
                generation_config=gen_config,
                tools=tools if tools else None,
                response_format=step_response_schema,
                store=True,
                previous_interaction_id=previous_interaction_id,
                api_key=api_key,
                max_retries=8,
                base_delay=5.0,
            )
        except RuntimeError as exc:
            error_lower = str(exc).lower()

            # Stale chain fallback
            if previous_interaction_id and (
                "not found" in error_lower or "not_found" in error_lower
            ):
                print(
                    f"   [WARN] Stale chain at iteration {idx + 1}, retrying without chaining...",
                    flush=True,
                )
                interaction = call_interaction(
                    input_text=iter_prompt,
                    model=model_name,
                    system_instruction=system_instruction,
                    generation_config=gen_config,
                    tools=tools if tools else None,
                    response_format=step_response_schema,
                    store=True,
                    previous_interaction_id=None,
                    api_key=api_key,
                    max_retries=8,
                    base_delay=5.0,
                )
                previous_interaction_id = None
            elif "unsupported model" in error_lower or "unsupported" in error_lower:
                # Vertex AI may not support Interactions API for this model —
                # switch to multi-turn generate_content for ALL remaining iterations
                _use_generate_content_fallback = True
                print(
                    f"   [FALLBACK] Interactions API unsupported for {model_name} on Vertex AI — "
                    f"switching to multi-turn generate_content",
                    flush=True,
                )
                if _fb_client is None:
                    from utils.gemini_runtime import get_client, build_generate_content_config
                    from google.genai import types as _genai_types
                    _fb_client = get_client(api_key)
                    _fb_config = build_generate_content_config(
                        thinking_level=thinking_level,
                        max_output_tokens=max_output_tokens,
                        system_instruction=system_instruction,
                        temperature=temperature,
                        response_mime_type="application/json",
                        response_json_schema=step_response_schema,
                        use_google_search=True,
                        include_thoughts=False,
                    )

                # Seed conversation history with prior context (must start with user turn)
                if not _conversation_history and accumulated_ctx:
                    _conversation_history.append(
                        _genai_types.Content(
                            role="user",
                            parts=[_genai_types.Part(text=(
                                "CONTEXT FROM PREVIOUS ITERATIONS:\n"
                                + json.dumps(accumulated_ctx, ensure_ascii=False)
                            ))],
                        )
                    )
                    _conversation_history.append(
                        _genai_types.Content(
                            role="model",
                            parts=[_genai_types.Part(text="Understood. I will build on this context and add only new proteins.")],
                        )
                    )

                _conversation_history.append(
                    _genai_types.Content(
                        role="user",
                        parts=[_genai_types.Part(text=iter_prompt)],
                    )
                )
                _fb_response = _fb_client.models.generate_content(
                    model=model_name,
                    contents=_conversation_history,
                    config=_fb_config,
                )
                text = (_fb_response.text or "").strip()
                previous_interaction_id = None
                if text:
                    _conversation_history.append(
                        _genai_types.Content(
                            role="model",
                            parts=[_genai_types.Part(text=text)],
                        )
                    )
                    accumulated_ctx = _merge_iteration_output(accumulated_ctx, text)
                    interactors_found = len(accumulated_ctx.get("interactors", []))
                    print(f"   [ITER {idx + 1}] (via generate_content) Total interactors: {interactors_found}", flush=True)
                else:
                    _conversation_history.append(
                        _genai_types.Content(
                            role="model",
                            parts=[_genai_types.Part(text="No new proteins found in this iteration.")],
                        )
                    )
                # Track token stats
                iter_stats = extract_usage_token_stats(_fb_response)
                for k in ("prompt_tokens", "thinking_tokens", "output_tokens", "total_tokens"):
                    aggregated_stats[k] += _coerce_token_count(iter_stats.get(k, 0))
                aggregated_stats["iteration_count"] += 1
                continue
            elif is_quota_error(exc):
                print(
                    f"   [WARN] Quota hit at iteration {idx + 1}, stopping early with partial results",
                    flush=True,
                )
                break
            elif is_transient_network_error(exc):
                # TCP reset / timeout on a single iteration is transient —
                # stop early with partial results rather than killing the
                # whole step. Matches the quota-hit disposition above.
                print(
                    f"   [WARN] Transient network error at iteration {idx + 1} "
                    f"({type(exc).__name__}): {exc} — stopping early with partial results",
                    flush=True,
                )
                break
            else:
                raise PipelineError(
                    f"Iteration '{iter_cfg.name}' failed: {exc}"
                ) from exc

        text = extract_text_from_interaction(interaction)

        # Debug: log raw response structure so we can diagnose parsing issues
        _dbg_outputs = getattr(interaction, "outputs", []) or []
        _dbg_types = [getattr(o, "type", "?") for o in _dbg_outputs]
        print(f"   [DEBUG] Interaction outputs ({len(_dbg_outputs)}): {_dbg_types}", flush=True)
        print(
            f"   [DEBUG] Extracted text length: {len(text)}, preview: {text[:500]!r}",
            flush=True,
        )

        if not text:
            # Fallback: try concatenating ALL text-like outputs (grounding may split them)
            text_parts = []
            for _out in _dbg_outputs:
                _t = getattr(_out, "text", None)
                if _t:
                    text_parts.append(str(_t))
            if text_parts:
                text = "\n".join(text_parts)
                print(
                    f"   [DEBUG] Fallback: concatenated {len(text_parts)} text outputs ({len(text)} chars)",
                    flush=True,
                )

        if not text:
            print(
                f"   [WARN] Iteration {idx + 1} returned empty, continuing...",
                flush=True,
            )
            # Still update chain ID so next iteration can build on the conversation
            new_id = getattr(interaction, "id", None)
            if new_id:
                previous_interaction_id = new_id
            continue

        # Update chain
        new_id = getattr(interaction, "id", None)
        if new_id:
            previous_interaction_id = new_id
            aggregated_stats["interaction_ids"].append(new_id)

        # Aggregate token stats
        iter_stats = extract_usage_token_stats(interaction)
        for k in ("prompt_tokens", "thinking_tokens", "output_tokens", "total_tokens"):
            aggregated_stats[k] += _coerce_token_count(iter_stats.get(k, 0))
        aggregated_stats["iteration_count"] += 1

        # Merge incremental output into accumulated context
        accumulated_ctx = _merge_iteration_output(accumulated_ctx, text)

        found_count = len((accumulated_ctx or {}).get("interactors", []))
        print(f"   [ITER {idx + 1}/{total_iters}] Done — {found_count} total interactors", flush=True)

        # Rate-limit safety delay between iterations (skip after last)
        if idx < total_iters - 1 and inter_iteration_delay > 0:
            time.sleep(inter_iteration_delay)

    if accumulated_ctx is None:
        raise PipelineError("All iterations returned empty — no data discovered")

    # Ensure total_tokens is correct
    if aggregated_stats["total_tokens"] <= 0:
        aggregated_stats["total_tokens"] = sum(
            aggregated_stats[k]
            for k in ("prompt_tokens", "thinking_tokens", "output_tokens")
        )

    aggregated_stats["request_mode"] = "iterative_research"
    aggregated_stats["interaction_id"] = (
        aggregated_stats["interaction_ids"][-1]
        if aggregated_stats["interaction_ids"]
        else None
    )

    # Wrap in expected output format
    final_output = {
        "ctx_json": accumulated_ctx,
        "step_json": {
            "step": "step1_iterative_research_discovery",
            "count": len(accumulated_ctx.get("interactors", [])),
            "iterations_completed": aggregated_stats["iteration_count"],
        },
    }
    final_text = json.dumps(final_output, ensure_ascii=False)
    return final_text, aggregated_stats


def create_snapshot_from_ctx(
    ctx_json: Dict[str, Any],
    expected_fields: List[str],
    step_name: str,
) -> Dict[str, Any]:
    """Generate snapshot_json and ndjson from ctx_json."""
    main_symbol = ctx_json.get("main", "UNKNOWN")
    interactors_data = ctx_json.get("interactors", [])

    snapshot_interactors: List[Dict[str, Any]] = []
    ndjson_lines: List[str] = []

    for interactor in interactors_data:
        # Extract core fields
        primary = interactor.get("primary", "")
        direction = interactor.get("direction", "")
        arrow = interactor.get("arrow", "")
        intent = interactor.get("intent", "")
        pmids = interactor.get("pmids", [])
        confidence = interactor.get("confidence")
        evidence = interactor.get("evidence", [])
        support_summary = interactor.get("support_summary", "")
        multiple_mechanisms = interactor.get("multiple_mechanisms", False)

        # Minimal functions (without full evidence to reduce size)
        functions_full = interactor.get("functions", [])
        minimal_functions: List[Dict[str, Any]] = []
        for func in functions_full:
            minimal_func = {
                "function": func.get("function", ""),
                "arrow": func.get("arrow", ""),
                "interaction_effect": func.get("interaction_effect", func.get("arrow", "")),
                "interaction_direction": func.get("interaction_direction", func.get("direction", "")),
                "cellular_process": func.get("cellular_process", ""),
                "effect_description": func.get("effect_description", ""),
                "biological_consequence": func.get("biological_consequence", []),
                "specific_effects": func.get("specific_effects", []),
                "pmids": func.get("pmids", []),
                "confidence": func.get("confidence"),
            }
            # These fields are not cosmetic: checkpoint DB sync reads
            # snapshot_json, so stripping them here breaks claim context,
            # pathway assignment, and chain-hop ownership before
            # post-processing can rebuild a full snapshot from ctx_json.
            for opt_key in (
                "function_context",
                "pathway",
                "mechanism_id",
                "evidence",
                "normal_role",
                "note",
                "_thin_claim",
                "_chain_group",
                "_depth_issues",
            ):
                if opt_key in func:
                    minimal_func[opt_key] = deepcopy(func[opt_key])
            minimal_functions.append(minimal_func)

        # Build snapshot interactor entry
        interactor_entry: Dict[str, Any] = {}
        if primary:
            interactor_entry["primary"] = primary
        if direction:
            interactor_entry["direction"] = direction
        if arrow:
            interactor_entry["arrow"] = arrow
        if intent:
            interactor_entry["intent"] = intent
        if pmids:
            interactor_entry["pmids"] = pmids
        if confidence is not None:
            interactor_entry["confidence"] = confidence
        if evidence:
            interactor_entry["evidence"] = evidence
        if support_summary:
            interactor_entry["support_summary"] = support_summary
        if multiple_mechanisms:
            interactor_entry["multiple_mechanisms"] = multiple_mechanisms

        # Chain resolution metadata — MUST be preserved for frontend rendering
        interaction_type = interactor.get("interaction_type", "direct")
        interactor_entry["interaction_type"] = interaction_type
        upstream = interactor.get("upstream_interactor")
        if upstream:
            interactor_entry["upstream_interactor"] = upstream
        mediator_chain = interactor.get("mediator_chain")
        if mediator_chain:
            interactor_entry["mediator_chain"] = mediator_chain
        depth = interactor.get("depth")
        if depth and depth > 1:
            interactor_entry["depth"] = depth
        for chain_key in (
            "chain_context",
            "chain_link_functions",
            "chain_with_arrows",
            "_chain_pathway",
            "step3_finalized_pathway",
            "pathways",
            "function_context",
        ):
            val = interactor.get(chain_key)
            if val not in (None, "", [], {}):
                interactor_entry[chain_key] = deepcopy(val)

        interactor_entry["functions"] = minimal_functions

        # Skip interactors with zero functions (likely from truncation failures)
        chain_link_functions = interactor_entry.get("chain_link_functions") or {}
        has_chain_link_functions = isinstance(chain_link_functions, dict) and any(
            isinstance(v, list) and v for v in chain_link_functions.values()
        )
        if not minimal_functions and not has_chain_link_functions:
            print(
                f"[SNAPSHOT] Excluding {primary} — zero functions (likely truncation)",
                file=sys.stderr, flush=True,
            )
            continue

        snapshot_interactors.append(interactor_entry)

        # NDJSON line
        ndjson_obj: Dict[str, Any] = {"main": main_symbol}
        ndjson_obj.update(interactor_entry)
        ndjson_lines.append(json.dumps(ndjson_obj, ensure_ascii=False, separators=(",", ":")))

    snapshot_json = {"main": main_symbol, "interactors": snapshot_interactors}
    if ctx_json.get("_chain_claim_phase_ran"):
        snapshot_json["_chain_claim_phase_ran"] = True
    result: Dict[str, Any] = {
        "ctx_json": ctx_json,
        "snapshot_json": snapshot_json,
        "ndjson": ndjson_lines
    }
    result["step_json"] = {"step": step_name, "rows": len(ndjson_lines)}

    for field in expected_fields:
        if field not in result:
            result[field] = None

    return result


def _zero_function_interactor_names(current_payload: Dict[str, Any]) -> List[str]:
    """Atom J helper — list interactors whose ``functions`` list is empty.

    Used by the pre-snapshot recovery pass: names returned here will be
    retried by ``step2a_interaction_functions`` with ``batch_size=1`` so
    each gets its own full Flash output budget, avoiding the long-tail
    truncation failures that otherwise cause ``[SNAPSHOT] Excluding X``
    log lines downstream.

    Dedups by name while preserving order.
    """
    if not current_payload or "ctx_json" not in current_payload:
        return []
    ctx = current_payload.get("ctx_json") or {}
    interactors = ctx.get("interactors") or []
    names: List[str] = []
    seen: set = set()
    for i in interactors:
        if not isinstance(i, dict):
            continue
        if i.get("functions"):
            continue
        # Indirect interactors legitimately carry zero flat functions — their
        # per-hop claims live in ``chain_link_functions[pair_key]``. Count a
        # populated nested dict as "has functions" so SNAPSHOT-RECOVERY no
        # longer spuriously re-runs step2a for chain-only targets (which then
        # returns an entry without chain_context and hits the A5 overwrite).
        clf = i.get("chain_link_functions") or {}
        if isinstance(clf, dict) and any(
            isinstance(v, list) and v for v in clf.values()
        ):
            continue
        n = (i.get("primary") or "").strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    return names


# dumps_compact, build_known_interactions_context, build_prompt
# imported from pipeline.context_builders above



# ═══════════════════════════════════════════════════════════════
# VALIDATION HELPER FUNCTIONS FOR PHASE 2 COMPLETENESS
# ═══════════════════════════════════════════════════════════════

def find_interactors_without_functions(ctx_json: dict) -> list[dict]:
    """Find all interactors that are missing functions (deduplicated by name).

    Returns:
        List of dicts with {name, interaction_type, functions_count}, unique by name.
    """
    missing = []
    seen: set[str] = set()
    interactors = ctx_json.get("interactors", [])

    for interactor in interactors:
        name = interactor.get("primary", "Unknown")
        if name in seen:
            continue
        seen.add(name)

        interaction_type = interactor.get("interaction_type", "direct")
        functions = interactor.get("functions", [])

        if not functions or len(functions) == 0:
            missing.append({
                "name": name,
                "interaction_type": interaction_type,
                "functions_count": 0
            })

    return missing


def validate_phase2_completeness(ctx_json: dict, interactor_history: list[str]) -> tuple[bool, list[dict]]:
    """
    Validate that ALL interactors from interactor_history have functions in ctx_json.
    
    Args:
        ctx_json: The current context JSON
        interactor_history: List of all discovered interactor names
        
    Returns:
        Tuple of (is_complete: bool, missing_interactors: list[dict])
    """
    missing = find_interactors_without_functions(ctx_json)
    
    # Also check if any interactors in history are NOT in ctx_json.interactors at all
    interactors_map = {i.get("primary"): i for i in ctx_json.get("interactors", [])}
    
    for name in interactor_history:
        if name not in interactors_map:
            missing.append({
                "name": name,
                "interaction_type": "unknown",
                "functions_count": 0,
                "note": "Not found in ctx_json.interactors"
            })
    
    is_complete = len(missing) == 0
    return is_complete, missing


def log_missing_functions_diagnostic(
    ctx_json: dict,
    interactor_history: list[str],
    step_name: str = "unknown"
) -> None:
    """
    Log detailed diagnostic information about missing functions.
    
    Args:
        ctx_json: The current context JSON
        interactor_history: List of all discovered interactor names
        step_name: Name of the step where this diagnostic is run
    """
    is_complete, missing = validate_phase2_completeness(ctx_json, interactor_history)
    
    if not is_complete:
        print(f"\n{'='*70}", file=sys.stderr)
        print(f"[VALIDATION] [WARN] PHASE 2 INCOMPLETE AFTER {step_name}", file=sys.stderr)
        print(f"[VALIDATION] Found {len(missing)} interactors WITHOUT functions:", file=sys.stderr)
        print(f"{'='*70}", file=sys.stderr)
        
        # Group by interaction type
        direct_missing = [m for m in missing if m.get("interaction_type") == "direct"]
        indirect_missing = [m for m in missing if m.get("interaction_type") == "indirect"]
        unknown_missing = [m for m in missing if m.get("interaction_type") not in ["direct", "indirect"]]
        
        if direct_missing:
            print(f"\n[VALIDATION] DIRECT interactors missing functions ({len(direct_missing)}):", file=sys.stderr)
            for m in direct_missing:
                note = f" - {m['note']}" if m.get('note') else ""
                print(f"  - {m['name']}{note}", file=sys.stderr)
            print(f"  → Should have been processed by Steps 2a-2a5 or Step 2b3", file=sys.stderr)
        
        if indirect_missing:
            print(f"\n[VALIDATION] INDIRECT interactors missing functions ({len(indirect_missing)}):", file=sys.stderr)
            for m in indirect_missing:
                note = f" - {m['note']}" if m.get('note') else ""
                print(f"  - {m['name']}{note}", file=sys.stderr)
            print(f"  → Should have been processed by Step 2b2 (indirect functions)", file=sys.stderr)
        
        if unknown_missing:
            print(f"\n[VALIDATION] UNKNOWN type interactors missing functions ({len(unknown_missing)}):", file=sys.stderr)
            for m in unknown_missing:
                note = f" - {m['note']}" if m.get('note') else ""
                print(f"  - {m['name']}{note}", file=sys.stderr)
            print(f"  → Missing interaction_type classification!", file=sys.stderr)
        
        print(f"\n[VALIDATION] Total interactors in history: {len(interactor_history)}", file=sys.stderr)
        print(f"[VALIDATION] Interactors with functions: {len(interactor_history) - len(missing)}", file=sys.stderr)
        print(f"[VALIDATION] Missing functions: {len(missing)}", file=sys.stderr)
        print(f"[VALIDATION] Completion rate: {((len(interactor_history) - len(missing)) / len(interactor_history) * 100) if interactor_history else 0:.1f}%", file=sys.stderr)
        print(f"{'='*70}\n", file=sys.stderr)
    else:
        print(f"\n[VALIDATION] [OK]PHASE 2 COMPLETE: All {len(interactor_history)} interactors have functions", file=sys.stderr)


def validate_classification_preservation(
    before_payload: dict,
    after_payload: dict,
    step_name: str
) -> bool:
    """
    Ensure post-processing doesn't corrupt interaction_type classifications.
    
    Args:
        before_payload: Payload before post-processing step
        after_payload: Payload after post-processing step
        step_name: Name of the post-processing step for logging
        
    Returns:
        True if all classifications preserved, False if corruptions detected
    """
    # Extract classifications from before
    before_snap = before_payload.get('snapshot_json', {})
    before_classifications = {
        i.get('primary'): i.get('interaction_type')
        for i in before_snap.get('interactors', [])
        if i.get('primary')
    }
    
    # Extract classifications from after
    after_snap = after_payload.get('snapshot_json', {})
    after_classifications = {
        i.get('primary'): i.get('interaction_type')
        for i in after_snap.get('interactors', [])
        if i.get('primary')
    }
    
    # Find corruptions
    corrupted = []
    for protein, before_type in before_classifications.items():
        after_type = after_classifications.get(protein)
        
        # Check if classification changed
        if before_type != after_type and before_type is not None:
            corrupted.append({
                'protein': protein,
                'before': before_type,
                'after': after_type
            })
    
    # Report results
    if corrupted:
        print(f"\n{'='*70}", file=sys.stderr)
        print(f"[WARN] WARNING: {step_name} changed {len(corrupted)} classification(s)!", file=sys.stderr)
        print(f"{'='*70}", file=sys.stderr)
        for corruption in corrupted:
            print(f"  - {corruption['protein']}: {corruption['before']} → {corruption['after']}", file=sys.stderr)
        print(f"{'='*70}\n", file=sys.stderr)
        return False
    else:
        print(f"[VALIDATION] [OK]{step_name}: All classifications preserved", file=sys.stderr)
        return True


def run_pipeline(
    user_query: str,
    verbose: bool = False,
    stream: bool = True,
    num_interactor_rounds: int = 3,
    num_function_rounds: int = 3,
    request_mode: Optional[str] = None,
    batch_poll_seconds: Optional[int] = None,
    batch_max_wait_seconds: Optional[int] = None,
    cancel_event=None,
) -> Dict[str, Any]:
    """Execute the full pipeline with configurable discovery rounds.

    Args:
        user_query: Protein name to analyze
        verbose: Print detailed debugging info
        stream: Enable streaming previews
        num_interactor_rounds: Number of interactor discovery rounds (min 3, max 10)
        num_function_rounds: Number of function mapping rounds (min 3, max 10)
        request_mode: Transport mode ('standard' or 'batch')
        batch_poll_seconds: Poll interval for batch mode
        batch_max_wait_seconds: Max wait time for batch mode
    """
    # Reset per-run telemetry. ``_PARSE_FAILURE_COUNTERS`` is module-level
    # and would otherwise accumulate over the process lifetime, making the
    # ``counters=`` payload on structured log events drift away from
    # "failures in THIS run" toward "failures since server boot".
    _PARSE_FAILURE_COUNTERS.clear()

    # Generate pipeline with requested rounds
    if DYNAMIC_CONFIG_AVAILABLE:
        pipeline_steps = generate_pipeline(num_interactor_rounds, num_function_rounds)
    else:
        pipeline_steps = DEFAULT_PIPELINE_STEPS

    validated_steps = validate_steps(pipeline_steps)
    current_payload: Optional[Dict[str, Any]] = None

    try:
        effective_request_mode = (
            parse_request_mode(request_mode)
            if request_mode is not None
            else get_request_mode()
        )
        effective_batch_poll_seconds = resolve_batch_poll_seconds(batch_poll_seconds)
        effective_batch_max_wait_seconds = resolve_batch_max_wait_seconds(batch_max_wait_seconds)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc

    # Token tracking with dynamic per-step cost estimates.
    # Gemini 3 Pro rates are selected by request mode + prompt bucket (<=200k vs >200k).
    # Non-core models retain the existing legacy estimate fallback.
    pipeline_token_stats = {
        'total_input_tokens': 0,
        'total_thinking_tokens': 0,
        'total_output_tokens': 0,
        'total_tokens': 0,
        'total_input_cost': 0.0,
        'total_thinking_cost': 0.0,
        'total_output_cost': 0.0,
        'total_cost': 0.0,
        'steps': []
    }

    def _accum_batch_tokens(batch_stats: dict, phase_name: str = ""):
        """Accumulate token stats from a batched phase into pipeline totals."""
        _inp = int(batch_stats.get("prompt_tokens") or 0)
        _think = int(batch_stats.get("thinking_tokens") or 0)
        _out = int(batch_stats.get("output_tokens") or 0)
        _total = _inp + _think + _out
        # Gemini 3 Flash Preview Standard: $0.50/1M input, $3.00/1M output (incl. thinking)
        _ic = (_inp / 1_000_000) * 0.50
        _tc = (_think / 1_000_000) * 3.00
        _oc = (_out / 1_000_000) * 3.00
        pipeline_token_stats["total_input_tokens"] += _inp
        pipeline_token_stats["total_thinking_tokens"] += _think
        pipeline_token_stats["total_output_tokens"] += _out
        pipeline_token_stats["total_tokens"] += _total
        pipeline_token_stats["total_input_cost"] += _ic
        pipeline_token_stats["total_thinking_cost"] += _tc
        pipeline_token_stats["total_output_cost"] += _oc
        pipeline_token_stats["total_cost"] += _ic + _tc + _oc
        if _total > 0:
            pipeline_token_stats["steps"].append({
                "step": f"[BATCH] {phase_name}",
                "input_tokens": _inp, "thinking_tokens": _think,
                "output_tokens": _out, "total_tokens": _total,
                "input_cost": _ic, "thinking_cost": _tc, "output_cost": _oc,
                "total_cost": _ic + _tc + _oc,
                "input_rate": 0.50, "thinking_rate": 3.00, "output_rate": 3.00,
                "request_mode": "standard", "prompt_bucket": "n/a", "elapsed_time": 0,
            })

    # Track overall pipeline time
    import time as time_module
    pipeline_start_time = time_module.time()

    # Initialize step logger (only if enabled via environment)
    step_logger = None
    if STEP_LOGGER_AVAILABLE:
        step_logger = StepLogger(user_query)

    # Initialize structured pipeline log (non-critical)
    structured_log = None
    if STRUCTURED_LOG_AVAILABLE:
        try:
            structured_log = StructuredPipelineLog(user_query)
            structured_log.pipeline_start(len(validated_steps), effective_request_mode)
        except Exception as exc:
            print(
                f"[WARN] StructuredPipelineLog init failed, continuing without "
                f"structured logs: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            structured_log = None

    print(f"\n{'='*80}")
    print(f"RUNNING MAXIMIZED PIPELINE FOR: {user_query}")
    print(f"{'='*80}")
    print(f"Total steps: {len(validated_steps)}")
    print(f"Thinking level per step: high (default unless a step overrides it)")
    print(f"Max output per step: {DEFAULT_MAX_OUTPUT_TOKENS:,} tokens")
    print(f"Request mode: {effective_request_mode}")
    if effective_request_mode == "batch":
        print(f"Batch polling: every {effective_batch_poll_seconds}s, max wait {effective_batch_max_wait_seconds}s")
    print(f"{'='*80}\n")

    for step_idx, step in enumerate(validated_steps, start=1):
        # Check for cancellation before each step
        if cancel_event and cancel_event.is_set():
            raise PipelineError("Job cancelled by user")

        step_start_time = time_module.time()

        # Structured log: step start (non-critical — observability hook)
        try:
            if structured_log:
                structured_log.step_start(step.name, step_idx)
        except Exception as exc:
            print(
                f"[WARN] structured_log.step_start failed for {step.name}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

        # Log step start
        if step_logger:
            step_logger.log_step_start(
                step_name=step.name,
                input_data=current_payload,
                step_type="pipeline"
            )

        print(f"\n[Step {step_idx}/{len(validated_steps)}] {step.name}")
        print(f"{'-'*80}")

        # Capture terminal output for logging
        if step_logger:
            step_logger.log_terminal_output(f"[Step {step_idx}/{len(validated_steps)}] {step.name}")
            step_logger.log_terminal_output('-' * 80)

        is_first = (step_idx == 1)

        # Special handling for snapshot step
        if step.name == "step3_snapshot":
            if current_payload and "ctx_json" in current_payload:
                # Atom J — last-chance recovery before exclusion. Retry
                # zero-function interactors with batch_size=1 so each
                # name gets its own full Flash output budget.
                _zero = _zero_function_interactor_names(current_payload)
                if _zero:
                    print(
                        f"[SNAPSHOT-RECOVERY] {len(_zero)} interactor(s) "
                        f"with zero functions before snapshot — retrying "
                        f"with batch_size=1: {_zero}",
                        file=sys.stderr, flush=True,
                    )
                    try:
                        from pipeline.prompts.modern_steps import (
                            step2a_interaction_functions as _snap_step2a,
                        )
                        _snap_kwargs = locals().get(
                            "_cli_parallel_kwargs"
                        ) or {}
                        current_payload, _rstats = _run_parallel_batched_phase(
                            "function_mapping_snapshot_recovery",
                            current_payload, user_query, _zero,
                            step_factory=_snap_step2a,
                            batch_directive_template=make_batch_directive(
                                "SNAPSHOT-RECOVERY interactors (prior attempts truncated)"
                            ),
                            batch_size=1,
                            max_workers=PARALLEL_MAX_WORKERS,
                            **_snap_kwargs,
                        )
                        _accum_batch_tokens(_rstats, "function_mapping_snapshot_recovery")
                        current_payload = _dedup_functions_locally(current_payload)
                    except Exception as _rec_exc:
                        print(
                            f"[SNAPSHOT-RECOVERY] Recovery pass raised "
                            f"{type(_rec_exc).__name__}: {_rec_exc} — "
                            "continuing to snapshot.",
                            file=sys.stderr, flush=True,
                        )
                print("   Creating snapshot locally (no model call)...")
                current_payload = create_snapshot_from_ctx(
                    current_payload["ctx_json"],
                    list(step.expected_columns),
                    step.name,
                )
                step_elapsed = time_module.time() - step_start_time
                print(f"   [OK]Snapshot created ({step_elapsed:.1f}s)")
            continue

        # ── PARALLEL PHASE DISPATCH (CLI) ─────────────────────────────
        _cli_parallel_kwargs = dict(
            cancel_event=cancel_event,
            request_mode=effective_request_mode,
            batch_poll_seconds=effective_batch_poll_seconds,
            batch_max_wait_seconds=effective_batch_max_wait_seconds,
        )

        if step.name.startswith("step2a_functions_r"):
            if step.name != "step2a_functions_r1":
                continue
            if current_payload and "ctx_json" in current_payload:
                _targets = [
                    m["name"]
                    for m in find_interactors_without_functions(current_payload["ctx_json"])
                ]
                current_payload, _bstats = _run_parallel_batched_phase(
                    "function_mapping",
                    current_payload, user_query, _targets,
                    step_factory=step2a_interaction_functions,
                    batch_directive_template=make_batch_directive("interactors"),
                    batch_size=PARALLEL_BATCH_SIZE,
                    max_workers=PARALLEL_MAX_WORKERS,
                    **_cli_parallel_kwargs,
                )
                _accum_batch_tokens(_bstats, "function_mapping")

                # ── Promote cascade-discovered proteins → real interactors ──
                current_payload, newly_promoted = _promote_discovered_interactors(current_payload)
                current_payload = _reconcile_chain_fields(current_payload)
                # Fix 1.6 — also derive chain_context from mediator_chain for
                # any indirect interactors that step2a returned with the chain
                # already inline. Without this, those interactors stay with
                # mediator_chain set but chain_context empty, and the
                # downstream 2ax/2az enumerator reads an empty ChainView →
                # the chain hop never enters the batch directive → claim
                # missing → CHAIN HOP CLAIM MISSING in db_sync.
                current_payload = _backfill_chain_context_from_mediator_chain(current_payload)
                current_payload = _clean_function_names_in_payload(current_payload)
                current_payload = _reclassify_indirect_to_direct(current_payload)
                current_payload = _dedup_functions_locally(current_payload)
                current_payload = _tag_shallow_functions(current_payload)
                # DEPTH-CHECK acts on its tags by default: re-dispatch
                # step2a with batch_size=1 and a targeted "expand-this-rule"
                # directive for every interactor whose flat functions still
                # carry `_depth_issues`. ON by default (env
                # DEPTH_CHECK_REDISPATCH=false to disable). Recent quality
                # reports showed 0% pass rate without redispatch — the
                # ~30-50% extra LLM cost of one targeted retry is
                # negligible vs the total run and is the only way to hit
                # the 6-10 sentences / 3-5 cascades target reliably on
                # Flash. Capped at one redispatch per interactor so the
                # loop cannot run away if Flash still under-shoots on
                # round 2.
                if os.environ.get("DEPTH_CHECK_REDISPATCH", "true").lower() != "false":
                    _shallow = _shallow_interactor_names(current_payload)
                    if _shallow:
                        print(
                            f"   [DEPTH-CHECK] Re-dispatching {len(_shallow)} "
                            f"shallow interactor(s) with batch_size=1 for expansion: "
                            f"{_shallow[:6]}{'...' if len(_shallow) > 6 else ''}",
                            file=sys.stderr, flush=True,
                        )
                        try:
                            current_payload, _dstats = _run_parallel_batched_phase(
                                "function_mapping_depth_expand",
                                current_payload, user_query, _shallow,
                                step_factory=step2a_interaction_functions,
                                batch_directive_template=make_batch_directive(
                                    "DEPTH-EXPAND interactors (prior output was "
                                    "shallow — add more cellular_process detail, "
                                    "at least one cascade, and additional evidence PMIDs)"
                                ),
                                batch_size=1,
                                max_workers=PARALLEL_MAX_WORKERS,
                                **_cli_parallel_kwargs,
                            )
                            _accum_batch_tokens(_dstats, "function_mapping_depth_expand")
                            current_payload = _dedup_functions_locally(current_payload)
                            current_payload = _tag_shallow_functions(current_payload)
                        except Exception as _d_exc:
                            print(
                                f"   [DEPTH-CHECK] Re-dispatch raised "
                                f"{type(_d_exc).__name__}: {_d_exc} — continuing.",
                                file=sys.stderr, flush=True,
                            )

                # ── Filter zero-function interactors before chain resolution ──
                _fctx2 = current_payload["ctx_json"]
                _bf2 = len(_fctx2.get("interactors", []))
                # L3.2 — capture the names of zero-function interactors BEFORE
                # filtering so the API response can surface them under
                # ``_zero_function_dropped``. Otherwise these silent drops are
                # invisible to the user and they have no idea what was lost.
                _zero_dropped = [
                    i.get("primary") for i in _fctx2.get("interactors", [])
                    if not i.get("functions") and i.get("primary")
                ]
                _fctx2["interactors"] = [
                    i for i in _fctx2.get("interactors", []) if i.get("functions")
                ]
                _zd2 = _bf2 - len(_fctx2["interactors"])
                if _zd2:
                    # Stash for the API response. Merge with any prior list
                    # so multiple cleanup passes accumulate rather than
                    # overwrite each other.
                    _existing = list(_fctx2.get("_zero_function_dropped") or [])
                    _existing.extend(_zero_dropped)
                    # De-duplicate while preserving order
                    _seen = set()
                    _fctx2["_zero_function_dropped"] = [
                        x for x in _existing
                        if x and (x not in _seen and not _seen.add(x))
                    ]
                    print(
                        f"   [CLEANUP] Filtered {_zd2} zero-function interactor(s) "
                        f"before chain resolution: {_zero_dropped}",
                        file=sys.stderr, flush=True,
                    )

                # ── Diagnostic: indirect pipeline status ──
                _indirect_n = len(_get_all_indirect_interactors(current_payload["ctx_json"]))
                _chained_n = len(_get_chained_needing_link_functions(current_payload["ctx_json"]))
                print(
                    f"   [INDIRECT] {_indirect_n} indirect interactors, "
                    f"{_chained_n} with chain data",
                    file=sys.stderr, flush=True,
                )

                # ── Expansion pass: run step2a again for newly promoted interactors ──
                if newly_promoted:
                    current_payload, _bstats = _run_parallel_batched_phase(
                        "function_mapping_expansion",
                        current_payload, user_query, newly_promoted,
                        step_factory=step2a_interaction_functions,
                        batch_directive_template=make_batch_directive(
                            "NEWLY DISCOVERED interactors"
                        ),
                        batch_size=PARALLEL_BATCH_SIZE,
                        max_workers=PARALLEL_MAX_WORKERS,
                        **_cli_parallel_kwargs,
                    )
                    _accum_batch_tokens(_bstats, "function_mapping_expansion")
                    current_payload = _dedup_functions_locally(current_payload)
            continue

        # ── Phase 2b: Chain resolution (Track A + B in parallel) ──
        # When we hit the first chain step, run the entire orchestrator.
        # Subsequent lightweight chain steps are skipped (already done).
        if step.name == "step2ab_chain_determination":
            if current_payload and "ctx_json" in current_payload:
                current_payload, chain_promoted = _run_chain_resolution_phase(
                    current_payload, user_query,
                    **_cli_parallel_kwargs,
                )
                # Run function mapping for chain-promoted proteins (they start with functions=[])
                if chain_promoted:
                    current_payload, _bstats = _run_parallel_batched_phase(
                        "function_mapping_chain_promoted",
                        current_payload, user_query, chain_promoted,
                        step_factory=step2a_interaction_functions,
                        batch_directive_template=make_batch_directive(
                            "CHAIN-PROMOTED interactors"
                        ),
                        batch_size=PARALLEL_BATCH_SIZE,
                        max_workers=PARALLEL_MAX_WORKERS,
                        **_cli_parallel_kwargs,
                    )
                    _accum_batch_tokens(_bstats, "function_mapping_chain_promoted")
                    current_payload = _dedup_functions_locally(current_payload)
                # Re-run the chain reconstruction now that chain resolution has
                # promoted new mid-chain proteins (TP53, TFEB, ATG4B, etc.).
                # The first _reconcile_chain_fields pass (after step2a) couldn't
                # salvage indirects whose upstream wasn't yet in the ctx;
                # this second pass catches those now that the promotions have
                # landed. Idempotent — indirects with populated chain fields
                # are skipped.
                current_payload = _reconcile_chain_fields(current_payload)
                # Fix 1.6 — also derive chain_context from mediator_chain for
                # any indirect interactors that step2a returned with the chain
                # already inline. Without this, those interactors stay with
                # mediator_chain set but chain_context empty, and the
                # downstream 2ax/2az enumerator reads an empty ChainView →
                # the chain hop never enters the batch directive → claim
                # missing → CHAIN HOP CLAIM MISSING in db_sync.
                current_payload = _backfill_chain_context_from_mediator_chain(current_payload)
            continue

        # Skip chain steps already handled by the orchestrator above
        if step.name in (
            "step2ab2_hidden_indirect_detection",
            "step2ab3_hidden_chain_determination",
            "step2ab5_extract_pairs_explicit",
        ):
            print(f"   [SKIP] {step.name} — handled by chain resolution orchestrator", file=sys.stderr)
            continue

        # ── Phase 2b: Chain claim generation (heavy Pro, batched+parallel) ──
        if step.name in ("step2ax_claim_generation_explicit", "step2az_claim_generation_hidden"):
            if current_payload and "ctx_json" in current_payload:
                from pipeline.prompts.deep_research_steps import (
                    step2ax_claim_generation_explicit as _step2ax_f,
                    step2az_claim_generation_hidden as _step2az_f,
                )
                _factory = _step2ax_f if step.name == "step2ax_claim_generation_explicit" else _step2az_f
                _targets = _get_chain_claim_targets(current_payload["ctx_json"], step.name)
                # Atom K — mark the phase as attempted BEFORE checking
                # whether any targets were found. The flag's semantic is
                # "we entered the 2ax/2az phase in this session", which
                # is what db_sync actually cares about. Otherwise a
                # missing-annotation path (Track A batch failed / chain
                # came from step2a directly / zero indirect interactors)
                # looks identical to "pipeline was resumed from cache"
                # and db_sync would rehydrate stale prior-run claims.
                _pl_ctx = current_payload.setdefault("ctx_json", {})
                _pl_ctx["_chain_claim_phase_ran"] = True
                print(
                    f"[PARALLEL:{step.name}] Phase entered — "
                    f"{len(_targets)} target pair(s) from "
                    f"_chain_annotations_explicit, _hidden_pairs_data, and "
                    f"interactor.mediator_chain combined.",
                    file=sys.stderr, flush=True,
                )
                if _targets:
                    _ctx = current_payload.get("ctx_json", {})
                    # Route chain-claim generation through the Batch API by
                    # default. Batch jobs are exempt from the per-minute
                    # RPM/TPM quotas that 429-stall the standard endpoint,
                    # and the ~30s-per-group poll overhead is trivial next
                    # to the 30s-per-call recovery we used to pay on every
                    # quota hit. Override via CHAIN_CLAIM_REQUEST_MODE.
                    _chain_kwargs = dict(_cli_parallel_kwargs)
                    _chain_kwargs["request_mode"] = CHAIN_CLAIM_REQUEST_MODE
                    current_payload, _bstats = _run_parallel_batched_phase(
                        step.name.replace("step2", ""),
                        current_payload, user_query, _targets,
                        step_factory=_factory,
                        batch_directive_template="",  # unused when fn provided
                        batch_directive_fn=lambda names, c=_ctx: _build_chain_batch_directive(names, c),
                        # Chain-claim batch sized so 3 full-depth claims
                        # fit under the 65k output cap. Other phases use
                        # PARALLEL_BATCH_SIZE.
                        batch_size=CHAIN_CLAIM_BATCH_SIZE,
                        max_workers=CHAIN_CLAIM_MAX_WORKERS,
                        rate_limit_group_size=CHAIN_CLAIM_MAX_WORKERS,
                        retry_max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                        **_chain_kwargs,
                    )
                    _accum_batch_tokens(_bstats, "chain_claims")
                    current_payload = _dedup_functions_locally(current_payload)
                    # Belt-and-suspenders: even if the LLM (or a future
                    # prompt drift) emits chain-hop functions on the flat
                    # ``functions[]`` list instead of the nested
                    # ``chain_link_functions[pair_key]`` slot, relocate
                    # them now so db_sync can find them. Without this, a
                    # flat-emitting LLM response results in zero claims
                    # reaching the chain-hop DB rows.
                    _relocate_flat_chain_hop_functions(current_payload)
                    _missing_pairs = _missing_chain_claim_pairs(
                        current_payload.get("ctx_json", {}), _targets,
                    )
                    if _missing_pairs:
                        print(
                            f"[PARALLEL:{step.name}] Recovery pass for "
                            f"{len(_missing_pairs)} missing chain claim pair(s): "
                            f"{_missing_pairs}",
                            file=sys.stderr, flush=True,
                        )
                        _retry_ctx = current_payload.get("ctx_json", {})
                        current_payload, _rstats = _run_parallel_batched_phase(
                            step.name.replace("step2", "") + "_missing_recovery",
                            current_payload, user_query, _missing_pairs,
                            step_factory=_factory,
                            batch_directive_template="",
                            batch_directive_fn=lambda names, c=_retry_ctx: _build_chain_batch_directive(names, c),
                            batch_size=1,
                            max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                            rate_limit_group_size=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                            retry_max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                            **_chain_kwargs,
                        )
                        _accum_batch_tokens(_rstats, "chain_claims_recovery")
                        current_payload = _dedup_functions_locally(current_payload)
                        _relocate_flat_chain_hop_functions(current_payload)
                        _still_missing = _missing_chain_claim_pairs(
                            current_payload.get("ctx_json", {}), _missing_pairs,
                        )
                        if _still_missing:
                            current_payload.setdefault("_pipeline_metadata", {}).setdefault(
                                "missing_chain_claim_pairs", []
                            ).extend(_still_missing)
                            current_payload.setdefault("ctx_json", {}).setdefault(
                                "_missing_chain_claim_pairs", []
                            ).extend(_still_missing)
                            print(
                                f"[PARALLEL:{step.name}] Missing after recovery: "
                                f"{_still_missing}",
                                file=sys.stderr, flush=True,
                            )

                    # R1: chain-hop depth-expand pass. P2.2 made
                    # quality_validator audit chain_link_functions, but
                    # the existing flat depth-redispatch path only
                    # walks interactor.functions[] — chain hops with
                    # _depth_issues were detected but never repaired.
                    # Tag depth issues IN-PLACE here so the helper can
                    # find them, then re-dispatch only the shallow
                    # pairs through 2ax with a depth_expand=True
                    # directive that names the failing fields. Capped
                    # to one pass per pair: if Flash still under-shoots
                    # after a focused retry, the bad output enters
                    # post-processing where quality_validation will
                    # tag it for the user (no infinite redispatch).
                    if (
                        os.environ.get("CHAIN_DEPTH_REDISPATCH", "true").lower()
                        != "false"
                    ):
                        try:
                            from utils.quality_validator import (
                                validate_payload_depth as _validate_depth,
                            )
                            _validate_depth(current_payload, tag_in_place=True)
                            _shallow_chain_pairs = _shallow_chain_hop_pairs(
                                current_payload.get("ctx_json", {}) or {}
                            )
                        except Exception as _v_exc:
                            print(
                                f"[PARALLEL:{step.name}] Chain depth-redispatch tagging "
                                f"raised {type(_v_exc).__name__}: {_v_exc} — skipping.",
                                file=sys.stderr, flush=True,
                            )
                            _shallow_chain_pairs = []

                        if _shallow_chain_pairs:
                            print(
                                f"[PARALLEL:{step.name}] Chain depth-expand "
                                f"for {len(_shallow_chain_pairs)} shallow hop(s): "
                                f"{_shallow_chain_pairs[:6]}"
                                f"{'...' if len(_shallow_chain_pairs) > 6 else ''}",
                                file=sys.stderr, flush=True,
                            )
                            _depth_ctx = current_payload.get("ctx_json", {})
                            try:
                                current_payload, _dxstats = _run_parallel_batched_phase(
                                    step.name.replace("step2", "") + "_depth_expand",
                                    current_payload,
                                    user_query,
                                    _shallow_chain_pairs,
                                    step_factory=_factory,
                                    batch_directive_template="",
                                    batch_directive_fn=lambda names, c=_depth_ctx: _build_chain_batch_directive(
                                        names, c, depth_expand=True,
                                    ),
                                    batch_size=1,
                                    max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                                    rate_limit_group_size=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                                    retry_max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                                    **_chain_kwargs,
                                )
                                _accum_batch_tokens(_dxstats, "chain_claims_depth_expand")
                                current_payload = _dedup_functions_locally(current_payload)
                                _relocate_flat_chain_hop_functions(current_payload)
                            except Exception as _dx_exc:
                                print(
                                    f"[PARALLEL:{step.name}] Chain depth-expand raised "
                                    f"{type(_dx_exc).__name__}: {_dx_exc} — continuing.",
                                    file=sys.stderr, flush=True,
                                )

                    # Re-set the flag in case the inner phase rebuilt the
                    # payload reference (safe no-op when already True).
                    _pl_ctx = current_payload.setdefault("ctx_json", {})
                    _pl_ctx["_chain_claim_phase_ran"] = True
                else:
                    print(
                        f"[PARALLEL:{step.name}] No chain targets — skipping",
                        file=sys.stderr, flush=True,
                    )
            continue

        # ── Phase 2e: Citation verification (parallel batched, CLI) ──
        if step.name == "step2e_citation_verification":
            if current_payload and "ctx_json" in current_payload:
                from pipeline.prompts.modern_steps import step2e_citation_verification as _step2e_f
                _all_names = [
                    i.get("primary") for i in current_payload["ctx_json"].get("interactors", [])
                    if i.get("primary") and i.get("functions")
                ]
                if _all_names:
                    current_payload, _bstats = _run_parallel_batched_phase(
                        "citation_verification",
                        current_payload, user_query, _all_names,
                        step_factory=_step2e_f,
                        batch_directive_template=(
                            "BATCH ASSIGNMENT — Verify and enrich evidence for ONLY\n"
                            "these {count} interactors:\n"
                            "{batch_names}\n"
                            "Do NOT process interactors outside this list.\n\n"
                            "For EACH function in these interactors, verify paper titles\n"
                            "exist and add missing evidence entries for functions with empty evidence."
                        ),
                        batch_size=PARALLEL_BATCH_SIZE,
                        max_workers=PARALLEL_MAX_WORKERS,
                        **_cli_parallel_kwargs,
                    )
                    _accum_batch_tokens(_bstats, "citation_verification")
            continue

        # ── DEFAULT: Single-call step (CLI) ───────────────────────────
        prompt = build_prompt(step, current_payload, user_query, is_first)

        if verbose:
            print("\nPrompt:\n" + prompt + "\n")

        try:
            raw_output, token_stats = call_gemini_model(
                step,
                prompt,
                cancel_event=cancel_event,
                request_mode=effective_request_mode,
                batch_poll_seconds=effective_batch_poll_seconds,
                batch_max_wait_seconds=effective_batch_max_wait_seconds,
            )
        except PipelineError as exc:
            raise PipelineError(f"{step.name}: {exc}") from exc

        if step_logger:
            step_logger.log_ai_response(raw_output, metadata=token_stats)

        if verbose:
            print("Model output:\n" + raw_output + "\n")

        current_payload = _parse_with_retry(
            step, prompt, raw_output, current_payload,
            call_kwargs=dict(
                cancel_event=cancel_event,
                request_mode=effective_request_mode,
                batch_poll_seconds=effective_batch_poll_seconds,
                batch_max_wait_seconds=effective_batch_max_wait_seconds,
            ),
        )
        current_payload = _handle_parse_failed_flag(current_payload)

        # Track tokens and calculate costs
        actual_request_mode = str(token_stats.get('request_mode') or 'standard')
        prompt_tokens = _coerce_token_count(token_stats.get('prompt_tokens'))
        thinking_tokens = _coerce_token_count(token_stats.get('thinking_tokens'))
        output_tokens = _coerce_token_count(token_stats.get('output_tokens'))
        total_tokens = _coerce_token_count(token_stats.get('total_tokens'))

        # Calculate input tokens (prefer prompt_token_count when available)
        input_tokens = prompt_tokens if prompt_tokens > 0 else max(0, total_tokens - thinking_tokens - output_tokens)
        if total_tokens <= 0:
            total_tokens = input_tokens + thinking_tokens + output_tokens

        # Map new API modes to pricing modes
        # interaction/deep_research use same pricing as standard (same underlying model)
        pricing_mode = actual_request_mode
        if pricing_mode in ("interaction", "deep_research"):
            pricing_mode = "standard"

        prompt_bucket = "n/a"
        if model_name := (step.model or get_core_model()):
            if model_name == "gemini-3.1-pro-preview":
                pricing = get_gemini_3_pro_pricing(
                    request_mode=pricing_mode,
                    prompt_tokens=input_tokens,
                )
                input_rate = float(pricing["input_per_million"])
                output_rate = float(pricing["output_per_million"])
                prompt_bucket = str(pricing["prompt_bucket"])
                # Gemini pricing table states output includes thinking tokens.
                thinking_rate = output_rate
            elif "flash" in model_name:
                # Gemini 3 Flash Preview Standard: $0.50 input, $3.00 output (incl. thinking)
                input_rate = 0.50
                thinking_rate = 3.00
                output_rate = 3.00
            else:
                input_rate = 1.25
                thinking_rate = 5.00
                output_rate = 10.00
        else:
            input_rate = 0.50
            thinking_rate = 3.00
            output_rate = 3.00

        input_cost = (input_tokens / 1_000_000) * input_rate
        thinking_cost = (thinking_tokens / 1_000_000) * thinking_rate
        output_cost = (output_tokens / 1_000_000) * output_rate
        total_cost = input_cost + thinking_cost + output_cost

        # Calculate step elapsed time
        step_elapsed = time_module.time() - step_start_time

        step_stat = {
            'step': step.name,
            'request_mode': actual_request_mode,
            'prompt_bucket': prompt_bucket,
            'input_tokens': input_tokens,
            'thinking_tokens': thinking_tokens,
            'output_tokens': output_tokens,
            'total_tokens': total_tokens,
            'input_cost': input_cost,
            'thinking_cost': thinking_cost,
            'output_cost': output_cost,
            'total_cost': total_cost,
            'input_rate': input_rate,
            'thinking_rate': thinking_rate,
            'output_rate': output_rate,
            'elapsed_time': step_elapsed
        }
        pipeline_token_stats['steps'].append(step_stat)
        pipeline_token_stats['total_input_tokens'] += input_tokens
        pipeline_token_stats['total_thinking_tokens'] += thinking_tokens
        pipeline_token_stats['total_output_tokens'] += output_tokens
        pipeline_token_stats['total_tokens'] += total_tokens
        pipeline_token_stats['total_input_cost'] += input_cost
        pipeline_token_stats['total_thinking_cost'] += thinking_cost
        pipeline_token_stats['total_output_cost'] += output_cost
        pipeline_token_stats['total_cost'] += total_cost

        # Show progress with token info
        interactor_count = 0
        total_functions = 0
        if current_payload and "ctx_json" in current_payload:
            interactor_count = len(current_payload["ctx_json"].get("interactors", []))
            total_functions = sum(
                len(i.get("functions", []))
                for i in current_payload["ctx_json"].get("interactors", [])
            )

            print(f"  → {interactor_count} interactors, {total_functions} functions mapped")

            # Capture terminal output
            if step_logger:
                step_logger.log_terminal_output(f"  → {interactor_count} interactors, {total_functions} functions mapped")

        # Print token usage and cost for this step
        print(f"  → Mode: {actual_request_mode} (prompt bucket: {prompt_bucket})")
        print(f"  → Tokens: input={input_tokens:,}, thinking={thinking_tokens:,}, output={output_tokens:,}, total={total_tokens:,}")
        print(f"  → Cost: ${total_cost:.4f} (input: ${input_cost:.4f}, thinking: ${thinking_cost:.4f}, output: ${output_cost:.4f})")
        print(f"  → Time: {step_elapsed:.1f}s")

        # Capture terminal output
        if step_logger:
            step_logger.log_terminal_output(f"  → Mode: {actual_request_mode} (prompt bucket: {prompt_bucket})")
            step_logger.log_terminal_output(f"  → Tokens: input={input_tokens:,}, thinking={thinking_tokens:,}, output={output_tokens:,}, total={total_tokens:,}")
            step_logger.log_terminal_output(f"  → Cost: ${total_cost:.4f} (input: ${input_cost:.4f}, thinking: ${thinking_cost:.4f}, output: ${output_cost:.4f})")
            step_logger.log_terminal_output(f"  → Time: {step_elapsed:.1f}s")

        # Log step completion
        if step_logger:
            step_metadata = {
                'step_name': step.name,
                'interactor_count': interactor_count,
                'function_count': total_functions,
                'request_mode': actual_request_mode,
                'prompt_bucket': prompt_bucket,
                'input_tokens': input_tokens,
                'thinking_tokens': thinking_tokens,
                'output_tokens': output_tokens,
                'total_tokens': total_tokens,
                'input_cost': input_cost,
                'thinking_cost': thinking_cost,
                'output_cost': output_cost,
                'total_cost': total_cost
            }
            step_logger.log_step_complete(
                output_data=current_payload,
                metadata=step_metadata,
                generate_summary=True
            )

        # Structured log: step complete (non-critical)
        try:
            if structured_log:
                structured_log.step_complete(
                    step_name=step.name,
                    step_index=step_idx,
                    token_stats={
                        "input": input_tokens,
                        "thinking": thinking_tokens,
                        "output": output_tokens,
                        "total": total_tokens,
                    },
                    cost_stats={
                        "input": input_cost,
                        "thinking": thinking_cost,
                        "output": output_cost,
                        "total": total_cost,
                    },
                    interactor_count=interactor_count,
                    function_count=total_functions,
                )
        except Exception as exc:
            print(
                f"[WARN] structured_log.step_complete failed for {step.name}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    if current_payload is None:
        raise PipelineError("Pipeline completed without returning data.")

    # Calculate total pipeline time
    pipeline_elapsed = time_module.time() - pipeline_start_time
    pipeline_elapsed_min = pipeline_elapsed / 60

    # Log final pipeline output (before post-processing)
    if step_logger:
        step_logger.log_final_output(current_payload)

    # Print comprehensive token and cost summary
    print(f"\n{'='*80}")
    print("PIPELINE SUMMARY")
    print(f"{'='*80}")
    print(f"Total time: {pipeline_elapsed_min:.1f} minutes ({pipeline_elapsed:.0f}s)")
    print(f"\n{'='*80}")
    print("TOKEN USAGE & COST BREAKDOWN")
    print(f"{'='*80}")
    print(f"\nTOTAL TOKENS:")
    print(f"  Input tokens:    {pipeline_token_stats['total_input_tokens']:>12,}")
    print(f"  Thinking tokens: {pipeline_token_stats['total_thinking_tokens']:>12,}")
    print(f"  Output tokens:   {pipeline_token_stats['total_output_tokens']:>12,}")
    print(f"  {'─'*40}")
    print(f"  TOTAL:           {pipeline_token_stats['total_tokens']:>12,}")

    print(f"\nESTIMATED COST:")
    print(f"  Input:    ${pipeline_token_stats['total_input_cost']:>8.4f}")
    print(f"  Thinking: ${pipeline_token_stats['total_thinking_cost']:>8.4f}")
    print(f"  Output:   ${pipeline_token_stats['total_output_cost']:>8.4f}")
    print(f"  {'─'*24}")
    print(f"  TOTAL:    ${pipeline_token_stats['total_cost']:>8.4f}")

    print(f"\n{'='*80}")
    print("PER-STEP BREAKDOWN")
    print(f"{'='*80}")
    print(f"{'Step':<24} {'Mode':<8} {'Bucket':<8} {'Input':>8} {'Think':>8} {'Output':>8} {'Total':>10} {'Cost':>10} {'Time':>8}")
    print(f"{'-'*80}")
    for step_stat in pipeline_token_stats['steps']:
        print(f"{step_stat['step']:<24} "
              f"{step_stat['request_mode']:<8} "
              f"{step_stat['prompt_bucket']:<8} "
              f"{step_stat['input_tokens']:>8,} "
              f"{step_stat['thinking_tokens']:>8,} "
              f"{step_stat['output_tokens']:>8,} "
              f"{step_stat['total_tokens']:>10,} "
              f"${step_stat['total_cost']:>9.4f} "
              f"{step_stat['elapsed_time']:>7.1f}s")
    print(f"{'-'*80}")
    print(f"{'TOTAL':<24} "
          f"{'-':<8} "
          f"{'-':<8} "
          f"{pipeline_token_stats['total_input_tokens']:>8,} "
          f"{pipeline_token_stats['total_thinking_tokens']:>8,} "
          f"{pipeline_token_stats['total_output_tokens']:>8,} "
          f"{pipeline_token_stats['total_tokens']:>10,} "
          f"${pipeline_token_stats['total_cost']:>9.4f} "
          f"{pipeline_elapsed:>7.0f}s")
    print(f"{'='*80}\n")

    # Structured log: pipeline complete (non-critical)
    try:
        if structured_log:
            structured_log.pipeline_complete(pipeline_token_stats, _get_run_request_metrics())
    except Exception as exc:
        print(
            f"[WARN] structured_log.pipeline_complete failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    # Return both payload and step_logger for post-processing
    if STEP_LOGGER_AVAILABLE and step_logger:
        return current_payload, step_logger
    return current_payload, None


def run_pipeline_for_protein(
    protein_symbol: str,
    max_interactor_rounds: int = 1,
    max_function_rounds: int = 1,
    api_key: Optional[str] = None,  # accepted for caller-signature parity; unused
    verbose: bool = False,
) -> Dict[str, Any]:
    """Lightweight nested-pipeline shim consumed by Tier 2 direct-link extraction.

    Previously `_query_direct_pair_simple` in ``utils/arrow_effect_validator.py``
    imported a symbol by this name that did not exist — the outer
    ``except Exception`` swallowed the ``ImportError`` silently, turning
    the whole Tier 2 path into a no-op. This shim re-enables Tier 2 with
    two explicit safeguards:

    1. Flask app-context wrap: Tier 2 runs inside a ThreadPoolExecutor
       worker that does not inherit the request context. Without this,
       the nested pipeline's DB queries crash with "Working outside of
       application context". We detect an existing context first so
       callers that already hold one don't nest.

    2. Cost gate: spawning a full ``run_pipeline`` per indirect pair is
       the single most expensive call pattern in the system. Gated on
       ``ENABLE_TIER2_NESTED_PIPELINE`` so the default behavior stays
       identical to the prior stub (fall through to Tier 3). Flip the
       env var to enable the real behavior when you actually want the
       nested lookups to run.

    Returns the same shape ``run_pipeline`` returns (a dict with
    ``snapshot_json`` / ``ctx_json`` keys) so the caller in
    ``arrow_effect_validator._query_direct_pair_simple`` can iterate
    ``result['snapshot_json']['interactors']`` directly.
    """
    if os.getenv("ENABLE_TIER2_NESTED_PIPELINE", "false").strip().lower() not in (
        "1", "true", "yes", "on"
    ):
        return {}

    try:
        from utils.observability import log_event
        log_event(
            "tier2_nested_pipeline_start",
            level="info",
            tag="TIER 2",
            protein=protein_symbol,
            max_interactor_rounds=max_interactor_rounds,
            max_function_rounds=max_function_rounds,
        )
    except Exception:
        pass

    def _invoke():
        result, _step_logger = run_pipeline(
            user_query=protein_symbol,
            verbose=verbose,
            stream=False,
            num_interactor_rounds=max(1, int(max_interactor_rounds or 1)),
            num_function_rounds=max(1, int(max_function_rounds or 1)),
        )
        return result or {}

    # If we're already inside a Flask app context (called from the main
    # request thread), invoke directly. Otherwise push a fresh context
    # for the duration of the nested call — this is the ThreadPoolExecutor
    # worker case that the original bug never addressed.
    try:
        from flask import current_app as _current_app
        _current_app._get_current_object()
        _in_ctx = True
    except Exception:
        _in_ctx = False

    try:
        if _in_ctx:
            return _invoke()
        from app import app as _flask_app
        with _flask_app.app_context():
            return _invoke()
    except Exception as exc:
        try:
            from utils.observability import log_event
            log_event(
                "tier2_nested_pipeline_failed",
                level="warn",
                tag="TIER 2",
                protein=protein_symbol,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        return {}


# ============================================================================
# FLASK-COMPATIBLE WEB INTEGRATION (NEW)
# ============================================================================

def _get_user_friendly_step_name(step_name: str) -> str:
    """
    Convert internal pipeline step names to user-friendly display text.

    Args:
        step_name: Internal step identifier (e.g., "step1a_discover")

    Returns:
        User-friendly description for display
    """
    # Modern pipeline steps (Deep Research + Interactions API)
    if step_name == "step1_deep_research_discovery":
        return "Deep Research: discovering all interactors..."
    elif step_name.startswith("step2a_functions_r"):
        round_num = step_name.rsplit("r", 1)[-1]
        return f"Mapping functions (round {round_num}, stateful)..."

    # Legacy interactor discovery steps (step1*)
    elif step_name == "step1a_discover":
        return "Researching interactors..."
    elif step_name == "step1b_expand":
        return "Expanding interaction network..."
    elif step_name == "step1c_deep_mining":
        return "Deep mining literature for interactors..."
    elif "step1d" in step_name or "round2" in step_name.lower():
        return "Round 2: Discovering additional interactors..."
    elif "step1e" in step_name or "round3" in step_name.lower():
        return "Round 3: Finding more interactors..."
    elif "step1f" in step_name or "round4" in step_name.lower():
        return "Round 4: Expanding interactor search..."
    elif "step1g" in step_name or "round5" in step_name.lower():
        return "Round 5: Comprehensive interactor sweep..."
    elif step_name == "step1_iterative_research_discovery":
        return "Iterative research: discovering interactors..."
    elif step_name.startswith("step1"):
        # Catch-all for any other step1* variants
        return "Discovering protein interactors..."

    # Function mapping steps (step2*)
    elif step_name == "step2a_functions":
        return "Mapping biological functions..."
    elif step_name == "step2a2_functions_batch":
        return "Analyzing additional functions..."
    elif step_name == "step2a3_functions_exhaustive":
        return "Comprehensive function analysis..."
    elif "step2a4" in step_name or ("step2" in step_name and "round2" in step_name.lower()):
        return "Round 2: Discovering additional functions..."
    elif "step2a5" in step_name or ("step2" in step_name and "round3" in step_name.lower()):
        return "Round 3: Finding more functions..."
    elif step_name == "step2ab_chain_determination":
        return "Determining chains for explicit indirect interactions..."
    elif step_name == "step2ab2_hidden_indirect_detection":
        return "Confirming hidden indirect candidates..."
    elif step_name == "step2ab3_hidden_chain_determination":
        return "Determining chains for hidden indirect interactions..."
    elif step_name == "step2ab5_extract_pairs_explicit":
        return "Checking for duplicate claims across chain pairs..."
    elif step_name == "step2ax_claim_generation_explicit":
        return "Generating claims for explicit indirect chains..."
    elif step_name == "step2az_claim_generation_hidden":
        return "Generating claims for hidden indirect chains..."
    elif step_name.startswith("step2c_arrow_"):
        # NEW: Arrow determination steps (dynamically generated)
        # Extract interactor name from step_name (e.g., "step2c_arrow_VCP" -> "VCP")
        interactor = step_name.replace("step2c_arrow_", "")
        return f"Determining arrow/direction for {interactor}..."
    elif step_name == "step2g_final_qc":
        return "Final quality control..."
    elif step_name.startswith("step2"):
        # Catch-all for any other step2* variants
        return "Analyzing biological functions..."

    # Snapshot step (step3)
    elif step_name == "step3_snapshot":
        return "Building network snapshot..."

    # Fallback for unknown steps
    else:
        # Convert underscores to spaces and capitalize for basic readability
        return step_name.replace("_", " ").title()


def _run_main_pipeline_for_web(
    user_query: str,
    update_status_func,
    total_steps: int,
    num_interactor_rounds: int = 3,
    num_function_rounds: int = 3,
    discovery_iterations: int = 5,
    request_mode: Optional[str] = None,
    batch_poll_seconds: Optional[int] = None,
    batch_max_wait_seconds: Optional[int] = None,
    cancel_event=None,
    known_interactions: Optional[List[Dict[str, Any]]] = None,
    skip_arrow_determination: bool = False,
    skip_citation_verification: bool = False,
) -> Dict[str, Any]:
    """
    A lean, web-focused version of run_pipeline. It only runs the main data
    gathering steps and reports progress. It does NOT handle validation or file saving.

    NEW: Dynamically generates arrow determination steps (Step 2c) after function discovery.

    Args:
        user_query: Protein name
        update_status_func: Function to update progress
        total_steps: Total number of steps including post-processing (for accurate progress)
        num_interactor_rounds: Number of interactor rounds
        num_function_rounds: Number of function rounds
        discovery_iterations: Number of iterative research iterations (default 5, 1-10)
        request_mode: Transport mode ('standard' or 'batch')
        batch_poll_seconds: Poll interval for batch jobs
        batch_max_wait_seconds: Max wait for batch jobs
        cancel_event: Optional threading.Event to check for cancellation
        known_interactions: List of known interactions from database (for exclusion)
    """
    # Reset per-run telemetry (see run_pipeline for the same reset).
    _PARSE_FAILURE_COUNTERS.clear()

    # ── Pipeline selection ─────────────────────────────────────────────
    # 'standard' mode and the USE_LEGACY_PIPELINE env flag are retired —
    # zero production users since the modern/iterative configs landed.
    # Only 'modern' and 'iterative' are selectable; unknown values fall
    # through to iterative (the documented default).
    pipeline_mode = os.getenv("PIPELINE_MODE", "iterative").strip().lower()
    print(
        f"[PIPELINE BUILD] mode={pipeline_mode} "
        f"skip_citation_verification={skip_citation_verification} "
        f"skip_arrow_determination={skip_arrow_determination}",
        file=sys.stderr, flush=True,
    )

    if not DYNAMIC_CONFIG_AVAILABLE:
        # Emergency fallback: config_dynamic.py failed to import. Use the
        # baked-in PIPELINE_STEPS so the app can at least try to serve
        # queries instead of crashing at startup.
        pipeline_steps = DEFAULT_PIPELINE_STEPS
    elif pipeline_mode == "modern":
        try:
            from pipeline.config_dynamic import generate_modern_pipeline
            pipeline_steps = generate_modern_pipeline(
                num_function_rounds=num_function_rounds,
                skip_citation_verification=skip_citation_verification,
            )
        except ImportError:
            pipeline_steps = generate_pipeline(num_interactor_rounds, num_function_rounds)
    else:
        # Default: iterative pipeline (Gemini 3.1 Pro multi-iteration)
        try:
            from pipeline.config_dynamic import generate_iterative_pipeline
            pipeline_steps = generate_iterative_pipeline(
                num_function_rounds=num_function_rounds,
                discovery_iterations=discovery_iterations,
                skip_citation_verification=skip_citation_verification,
            )
        except ImportError:
            # Fallback to modern if iterative module not available
            try:
                from pipeline.config_dynamic import generate_modern_pipeline
                pipeline_steps = generate_modern_pipeline(
                    num_function_rounds=num_function_rounds,
                    skip_citation_verification=skip_citation_verification,
                )
            except ImportError:
                pipeline_steps = generate_pipeline(num_interactor_rounds, num_function_rounds)

    validated_steps = validate_steps(pipeline_steps)
    current_payload: Optional[Dict[str, Any]] = None
    arrow_steps_executed = False  # Track if we've already done arrow determination

    # ── Interaction chain state for Interactions API ────────────────
    _interaction_chain: Dict[str, Optional[str]] = {
        "discovery": None,   # Phase 1 chain (standard/deep research)
        "iterative": None,   # Phase 1 chain (iterative research)
        "functions": None,   # Phase 2 chain
        "qc": None,          # Phase 3 chain
    }

    def _get_chain_key(step_name: str) -> str:
        """Map step name to interaction chain group."""
        if step_name == "step1_iterative_research_discovery":
            return "iterative"
        if step_name.startswith("step1"):
            return "discovery"
        if step_name.startswith("step2") and step_name != "step2g_final_qc":
            return "functions"
        return "qc"

    # ── Token accounting (mirrors run_pipeline) ──────────────────────
    pipeline_token_stats = {
        'total_input_tokens': 0,
        'total_thinking_tokens': 0,
        'total_output_tokens': 0,
        'total_tokens': 0,
        'total_input_cost': 0.0,
        'total_thinking_cost': 0.0,
        'total_output_cost': 0.0,
        'total_cost': 0.0,
        'steps': []
    }

    def _accum_batch_tokens(batch_stats: dict, phase_name: str = ""):
        """Accumulate token stats from a batched phase into pipeline totals."""
        _inp = int(batch_stats.get("prompt_tokens") or 0)
        _think = int(batch_stats.get("thinking_tokens") or 0)
        _out = int(batch_stats.get("output_tokens") or 0)
        _total = _inp + _think + _out
        # Gemini 3 Flash Preview Standard: $0.50/1M input, $3.00/1M output (incl. thinking)
        _ic = (_inp / 1_000_000) * 0.50
        _tc = (_think / 1_000_000) * 3.00
        _oc = (_out / 1_000_000) * 3.00
        pipeline_token_stats["total_input_tokens"] += _inp
        pipeline_token_stats["total_thinking_tokens"] += _think
        pipeline_token_stats["total_output_tokens"] += _out
        pipeline_token_stats["total_tokens"] += _total
        pipeline_token_stats["total_input_cost"] += _ic
        pipeline_token_stats["total_thinking_cost"] += _tc
        pipeline_token_stats["total_output_cost"] += _oc
        pipeline_token_stats["total_cost"] += _ic + _tc + _oc
        if _total > 0:
            pipeline_token_stats["steps"].append({
                "step": f"[BATCH] {phase_name}",
                "input_tokens": _inp, "thinking_tokens": _think,
                "output_tokens": _out, "total_tokens": _total,
                "input_cost": _ic, "thinking_cost": _tc, "output_cost": _oc,
                "total_cost": _ic + _tc + _oc,
                "input_rate": 0.50, "thinking_rate": 3.00, "output_rate": 3.00,
                "request_mode": "standard", "prompt_bucket": "n/a", "elapsed_time": 0,
            })

    for step_idx, step in enumerate(validated_steps, start=1):
        # Check for cancellation before each step
        if cancel_event and cancel_event.is_set():
            raise PipelineError("Job cancelled by user")

        # Report progress to the web UI with user-friendly name
        friendly_name = _get_user_friendly_step_name(step.name)
        update_status_func(
            text=friendly_name,
            current_step=step_idx,
            total_steps=total_steps
        )

        if step.name == "step3_snapshot":
            if current_payload and "ctx_json" in current_payload:
                # Atom J — pre-snapshot recovery (web pipeline path).
                _zero = _zero_function_interactor_names(current_payload)
                if _zero:
                    print(
                        f"[SNAPSHOT-RECOVERY] {len(_zero)} interactor(s) "
                        f"with zero functions before snapshot — retrying "
                        f"with batch_size=1: {_zero}",
                        file=sys.stderr, flush=True,
                    )
                    try:
                        from pipeline.prompts.modern_steps import (
                            step2a_interaction_functions as _snap_step2a,
                        )
                        _snap_kwargs = locals().get(
                            "_cli_parallel_kwargs"
                        ) or {}
                        current_payload, _rstats = _run_parallel_batched_phase(
                            "function_mapping_snapshot_recovery",
                            current_payload, user_query, _zero,
                            step_factory=_snap_step2a,
                            batch_directive_template=make_batch_directive(
                                "SNAPSHOT-RECOVERY interactors (prior attempts truncated)"
                            ),
                            batch_size=1,
                            max_workers=PARALLEL_MAX_WORKERS,
                            **_snap_kwargs,
                        )
                        _accum_batch_tokens(_rstats, "function_mapping_snapshot_recovery")
                        current_payload = _dedup_functions_locally(current_payload)
                    except Exception as _rec_exc:
                        print(
                            f"[SNAPSHOT-RECOVERY] Recovery pass raised "
                            f"{type(_rec_exc).__name__}: {_rec_exc} — "
                            "continuing to snapshot.",
                            file=sys.stderr, flush=True,
                        )
                current_payload = create_snapshot_from_ctx(
                    current_payload["ctx_json"],
                    list(step.expected_columns),
                    step.name,
                )
            continue

        # ===================================================================
        # PARALLEL PHASE DISPATCH
        # ===================================================================
        # Steps that should be batched across interactors and run in parallel
        # are intercepted here. Everything else falls through to the normal
        # single-call path below.
        _parallel_kwargs = dict(
            cancel_event=cancel_event,
            request_mode=request_mode,
            batch_poll_seconds=batch_poll_seconds,
            batch_max_wait_seconds=batch_max_wait_seconds,
            known_interactions=known_interactions,
            update_status=update_status_func,
            step_idx=step_idx,
            total_steps=total_steps,
        )

        # ── Phase 2a: Function mapping (parallel for ALL interactors) ─
        if step.name.startswith("step2a_functions_r"):
            # Only run on the first step2a marker; skip subsequent ones
            if step.name != "step2a_functions_r1":
                continue
            if current_payload and "ctx_json" in current_payload:
                _targets = [
                    m["name"]
                    for m in find_interactors_without_functions(current_payload["ctx_json"])
                ]
                current_payload, _bstats = _run_parallel_batched_phase(
                    "function_mapping",
                    current_payload, user_query, _targets,
                    step_factory=step2a_interaction_functions,
                    batch_directive_template=make_batch_directive("interactors"),
                    batch_size=PARALLEL_BATCH_SIZE,
                    max_workers=PARALLEL_MAX_WORKERS,
                    **_parallel_kwargs,
                )
                _accum_batch_tokens(_bstats, "function_mapping")

                # ── Promote cascade-discovered proteins → real interactors ──
                current_payload, newly_promoted = _promote_discovered_interactors(current_payload)
                current_payload = _reconcile_chain_fields(current_payload)
                # Fix 1.6 — also derive chain_context from mediator_chain for
                # any indirect interactors that step2a returned with the chain
                # already inline. Without this, those interactors stay with
                # mediator_chain set but chain_context empty, and the
                # downstream 2ax/2az enumerator reads an empty ChainView →
                # the chain hop never enters the batch directive → claim
                # missing → CHAIN HOP CLAIM MISSING in db_sync.
                current_payload = _backfill_chain_context_from_mediator_chain(current_payload)
                current_payload = _clean_function_names_in_payload(current_payload)
                current_payload = _reclassify_indirect_to_direct(current_payload)
                current_payload = _dedup_functions_locally(current_payload)
                current_payload = _tag_shallow_functions(current_payload)
                # DEPTH-CHECK acts on its tags by default: re-dispatch
                # step2a with batch_size=1 and a targeted "expand-this-rule"
                # directive for every interactor whose flat functions still
                # carry `_depth_issues`. ON by default (env
                # DEPTH_CHECK_REDISPATCH=false to disable). Recent quality
                # reports showed 0% pass rate without redispatch — the
                # ~30-50% extra LLM cost of one targeted retry is
                # negligible vs the total run and is the only way to hit
                # the 6-10 sentences / 3-5 cascades target reliably on
                # Flash. Capped at one redispatch per interactor so the
                # loop cannot run away if Flash still under-shoots on
                # round 2.
                if os.environ.get("DEPTH_CHECK_REDISPATCH", "true").lower() != "false":
                    _shallow = _shallow_interactor_names(current_payload)
                    if _shallow:
                        print(
                            f"   [DEPTH-CHECK] Re-dispatching {len(_shallow)} "
                            f"shallow interactor(s) with batch_size=1 for expansion: "
                            f"{_shallow[:6]}{'...' if len(_shallow) > 6 else ''}",
                            file=sys.stderr, flush=True,
                        )
                        try:
                            current_payload, _dstats = _run_parallel_batched_phase(
                                "function_mapping_depth_expand",
                                current_payload, user_query, _shallow,
                                step_factory=step2a_interaction_functions,
                                batch_directive_template=make_depth_expand_batch_directive(),
                                batch_size=1,
                                max_workers=PARALLEL_MAX_WORKERS,
                                **_parallel_kwargs,
                            )
                            _accum_batch_tokens(_dstats, "function_mapping_depth_expand")
                            current_payload = _dedup_functions_locally(current_payload)
                            # One pass of redispatch — re-tag here so the
                            # post-processor's quality_validation stage
                            # sees the second-round outcome. Cap at one
                            # redispatch by NOT looping back into another
                            # if-shallow-redispatch block.
                            current_payload = _tag_shallow_functions(current_payload)
                        except Exception as _d_exc:
                            print(
                                f"   [DEPTH-CHECK] Re-dispatch raised "
                                f"{type(_d_exc).__name__}: {_d_exc} — continuing.",
                                file=sys.stderr, flush=True,
                            )

                # ── Filter zero-function interactors before chain resolution ──
                _fctx = current_payload["ctx_json"]
                _bf = len(_fctx.get("interactors", []))
                _fctx["interactors"] = [
                    i for i in _fctx.get("interactors", []) if i.get("functions")
                ]
                _zd = _bf - len(_fctx["interactors"])
                if _zd:
                    print(
                        f"   [CLEANUP] Filtered {_zd} zero-function interactor(s) "
                        f"before chain resolution",
                        file=sys.stderr, flush=True,
                    )

                # ── Diagnostic: indirect pipeline status ──
                _indirect_n = len(_get_all_indirect_interactors(current_payload["ctx_json"]))
                _chained_n = len(_get_chained_needing_link_functions(current_payload["ctx_json"]))
                print(
                    f"   [INDIRECT] {_indirect_n} indirect interactors, "
                    f"{_chained_n} with chain data",
                    file=sys.stderr, flush=True,
                )

                # ── Expansion pass: run step2a for newly promoted interactors ──
                if newly_promoted:
                    current_payload, _bstats = _run_parallel_batched_phase(
                        "function_mapping_expansion",
                        current_payload, user_query, newly_promoted,
                        step_factory=step2a_interaction_functions,
                        batch_directive_template=make_batch_directive(
                            "NEWLY DISCOVERED interactors"
                        ),
                        batch_size=PARALLEL_BATCH_SIZE,
                        max_workers=PARALLEL_MAX_WORKERS,
                        **_parallel_kwargs,
                    )
                    _accum_batch_tokens(_bstats, "function_mapping_expansion")
                    current_payload = _dedup_functions_locally(current_payload)
            continue

        # ── Phase 2b: Chain resolution (Track A + B in parallel) ─────
        if step.name == "step2ab_chain_determination":
            if current_payload and "ctx_json" in current_payload:
                current_payload, chain_promoted = _run_chain_resolution_phase(
                    current_payload, user_query,
                    **_parallel_kwargs,
                )
                # Run function mapping for chain-promoted proteins (they start with functions=[])
                if chain_promoted:
                    current_payload, _bstats = _run_parallel_batched_phase(
                        "function_mapping_chain_promoted",
                        current_payload, user_query, chain_promoted,
                        step_factory=step2a_interaction_functions,
                        batch_directive_template=make_batch_directive(
                            "CHAIN-PROMOTED interactors"
                        ),
                        batch_size=PARALLEL_BATCH_SIZE,
                        max_workers=PARALLEL_MAX_WORKERS,
                        **_parallel_kwargs,
                    )
                    _accum_batch_tokens(_bstats, "function_mapping_chain_promoted")
                    current_payload = _dedup_functions_locally(current_payload)
                # Re-run chain reconciliation so indirects whose full_chain
                # was populated by chain resolution (TP53, TFEB, ATG4B, etc.)
                # propagate into the denormalised columns. Idempotent.
                current_payload = _reconcile_chain_fields(current_payload)
                # Fix 1.6 — also derive chain_context from mediator_chain for
                # any indirect interactors that step2a returned with the chain
                # already inline. Without this, those interactors stay with
                # mediator_chain set but chain_context empty, and the
                # downstream 2ax/2az enumerator reads an empty ChainView →
                # the chain hop never enters the batch directive → claim
                # missing → CHAIN HOP CLAIM MISSING in db_sync.
                current_payload = _backfill_chain_context_from_mediator_chain(current_payload)
            continue

        if step.name in (
            "step2ab2_hidden_indirect_detection",
            "step2ab3_hidden_chain_determination",
            "step2ab5_extract_pairs_explicit",
        ):
            print(f"   [SKIP] {step.name} — handled by chain resolution orchestrator", file=sys.stderr)
            continue

        # ── Phase 2b: Chain claim generation (heavy Pro, batched+parallel) ──
        if step.name in ("step2ax_claim_generation_explicit", "step2az_claim_generation_hidden"):
            if current_payload and "ctx_json" in current_payload:
                from pipeline.prompts.deep_research_steps import (
                    step2ax_claim_generation_explicit as _step2ax_factory,
                    step2az_claim_generation_hidden as _step2az_factory,
                )
                _factory = _step2ax_factory if step.name == "step2ax_claim_generation_explicit" else _step2az_factory
                _targets = _get_chain_claim_targets(current_payload["ctx_json"], step.name)
                # Atom K (web pipeline parity): mark the phase as attempted
                # BEFORE checking targets so db_sync sees the flag even
                # when targets were empty. Same semantic as the CLI branch.
                _pl_ctx_web = current_payload.setdefault("ctx_json", {})
                _pl_ctx_web["_chain_claim_phase_ran"] = True
                print(
                    f"[PARALLEL:{step.name}] Phase entered — "
                    f"{len(_targets)} target pair(s) from "
                    f"_chain_annotations_explicit, _hidden_pairs_data, and "
                    f"interactor.mediator_chain combined.",
                    file=sys.stderr, flush=True,
                )
                if _targets:
                    _ctx = current_payload.get("ctx_json", {})
                    _chain_kwargs = dict(_parallel_kwargs)
                    _chain_kwargs["request_mode"] = CHAIN_CLAIM_REQUEST_MODE
                    current_payload, _bstats = _run_parallel_batched_phase(
                        step.name.replace("step2", ""),
                        current_payload, user_query, _targets,
                        step_factory=_factory,
                        batch_directive_template="",  # unused when fn provided
                        batch_directive_fn=lambda names, c=_ctx: _build_chain_batch_directive(names, c),
                        # Atom I (web pipeline parity): smaller batch for
                        # chain-claim phase so Flash's 65k output budget
                        # fits full-depth claims without truncation.
                        batch_size=CHAIN_CLAIM_BATCH_SIZE,
                        max_workers=CHAIN_CLAIM_MAX_WORKERS,
                        rate_limit_group_size=CHAIN_CLAIM_MAX_WORKERS,
                        retry_max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                        **_chain_kwargs,
                    )
                    _accum_batch_tokens(_bstats, "chain_claims")
                    current_payload = _dedup_functions_locally(current_payload)
                    # Atom B (web pipeline parity): rescue flat chain-hop
                    # claims into nested chain_link_functions[pair_key]
                    # slot so db_sync picks them up.
                    _relocate_flat_chain_hop_functions(current_payload)
                    _missing_pairs = _missing_chain_claim_pairs(
                        current_payload.get("ctx_json", {}), _targets,
                    )
                    if _missing_pairs:
                        print(
                            f"[PARALLEL:{step.name}] Recovery pass for "
                            f"{len(_missing_pairs)} missing chain claim pair(s): "
                            f"{_missing_pairs}",
                            file=sys.stderr, flush=True,
                        )
                        _retry_ctx = current_payload.get("ctx_json", {})
                        current_payload, _rstats = _run_parallel_batched_phase(
                            step.name.replace("step2", "") + "_missing_recovery",
                            current_payload, user_query, _missing_pairs,
                            step_factory=_factory,
                            batch_directive_template="",
                            batch_directive_fn=lambda names, c=_retry_ctx: _build_chain_batch_directive(names, c),
                            batch_size=1,
                            max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                            rate_limit_group_size=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                            retry_max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                            **_chain_kwargs,
                        )
                        _accum_batch_tokens(_rstats, "chain_claims_recovery")
                        current_payload = _dedup_functions_locally(current_payload)
                        _relocate_flat_chain_hop_functions(current_payload)
                        _still_missing = _missing_chain_claim_pairs(
                            current_payload.get("ctx_json", {}), _missing_pairs,
                        )
                        if _still_missing:
                            current_payload.setdefault("_pipeline_metadata", {}).setdefault(
                                "missing_chain_claim_pairs", []
                            ).extend(_still_missing)
                            current_payload.setdefault("ctx_json", {}).setdefault(
                                "_missing_chain_claim_pairs", []
                            ).extend(_still_missing)
                            print(
                                f"[PARALLEL:{step.name}] Missing after recovery: "
                                f"{_still_missing}",
                                file=sys.stderr, flush=True,
                            )

                    # R1 (web pipeline parity): chain-hop depth-expand
                    # pass. See the CLI block for the rationale; this
                    # mirrors it exactly so /api/query runs get the
                    # same depth-redispatch behavior as the CLI runs.
                    if (
                        os.environ.get("CHAIN_DEPTH_REDISPATCH", "true").lower()
                        != "false"
                    ):
                        try:
                            from utils.quality_validator import (
                                validate_payload_depth as _validate_depth_web,
                            )
                            _validate_depth_web(current_payload, tag_in_place=True)
                            _shallow_chain_pairs_web = _shallow_chain_hop_pairs(
                                current_payload.get("ctx_json", {}) or {}
                            )
                        except Exception as _v_exc_web:
                            print(
                                f"[PARALLEL:{step.name}] Chain depth-redispatch tagging "
                                f"raised {type(_v_exc_web).__name__}: {_v_exc_web} — skipping.",
                                file=sys.stderr, flush=True,
                            )
                            _shallow_chain_pairs_web = []

                        if _shallow_chain_pairs_web:
                            print(
                                f"[PARALLEL:{step.name}] Chain depth-expand "
                                f"for {len(_shallow_chain_pairs_web)} shallow hop(s): "
                                f"{_shallow_chain_pairs_web[:6]}"
                                f"{'...' if len(_shallow_chain_pairs_web) > 6 else ''}",
                                file=sys.stderr, flush=True,
                            )
                            _depth_ctx_web = current_payload.get("ctx_json", {})
                            try:
                                current_payload, _dxstats_web = _run_parallel_batched_phase(
                                    step.name.replace("step2", "") + "_depth_expand",
                                    current_payload,
                                    user_query,
                                    _shallow_chain_pairs_web,
                                    step_factory=_factory,
                                    batch_directive_template="",
                                    batch_directive_fn=lambda names, c=_depth_ctx_web: _build_chain_batch_directive(
                                        names, c, depth_expand=True,
                                    ),
                                    batch_size=1,
                                    max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                                    rate_limit_group_size=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                                    retry_max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS,
                                    **_chain_kwargs,
                                )
                                _accum_batch_tokens(_dxstats_web, "chain_claims_depth_expand")
                                current_payload = _dedup_functions_locally(current_payload)
                                _relocate_flat_chain_hop_functions(current_payload)
                            except Exception as _dx_exc_web:
                                print(
                                    f"[PARALLEL:{step.name}] Chain depth-expand raised "
                                    f"{type(_dx_exc_web).__name__}: {_dx_exc_web} — continuing.",
                                    file=sys.stderr, flush=True,
                                )

                    # Re-set the flag in case the inner phase rebuilt the
                    # payload reference (safe no-op when already True).
                    _pl_ctx_web = current_payload.setdefault("ctx_json", {})
                    _pl_ctx_web["_chain_claim_phase_ran"] = True
                else:
                    print(
                        f"[PARALLEL:{step.name}] No chain targets — skipping",
                        file=sys.stderr, flush=True,
                    )

            # ── Arrow heuristic (runs once after chain links) ─────────
            if not arrow_steps_executed and current_payload and "ctx_json" in current_payload:
                arrow_steps_executed = True
                ctx_json = current_payload.get("ctx_json", {})
                interactors = ctx_json.get("interactors", [])
                needs_heuristic = any(
                    (not i.get("arrow") or i.get("arrow") == "binds") and i.get("functions")
                    for i in interactors
                )
                if needs_heuristic:
                    print("[ARROW DETERMINATION] Applying fast heuristic...", file=sys.stderr)
                    from utils.interaction_metadata_generator import determine_interaction_arrow
                    from utils.direction import infer_direction_from_arrow
                    from collections import Counter
                    for interactor in interactors:
                        functions = interactor.get("functions", [])
                        if not functions:
                            continue
                        if not interactor.get("arrow") or interactor.get("arrow") == "binds":
                            interactor["arrow"] = determine_interaction_arrow(functions)
                        # Collect only REAL directions from functions — don't
                        # let missing fields silently contribute a
                        # "bidirectional" placeholder (which then won via
                        # Counter when all functions were missing direction
                        # data and polluted chain-derived records).
                        directions = [
                            d for d in (
                                f.get("interaction_direction")
                                or f.get("likely_direction")
                                or f.get("direction")
                                for f in functions
                            )
                            if d
                        ]
                        if directions:
                            interactor["direction"] = Counter(directions).most_common(1)[0][0]
                        elif not interactor.get("direction"):
                            # Infer from arrow rather than a blanket bidirectional
                            # placeholder. A claim with arrow="activates" should
                            # default to main_to_primary, not bidirectional.
                            interactor["direction"] = infer_direction_from_arrow(
                                interactor.get("arrow")
                            )
                        if not interactor.get("intent"):
                            interactor["intent"] = "regulation"
                    print("[ARROW DETERMINATION] Heuristic applied\n", file=sys.stderr)
            continue

        # ── Phase 2e: Citation verification (parallel batched) ──────────
        if step.name == "step2e_citation_verification":
            # Defensive guard: honor skip flag even if step somehow ended up
            # in the list (e.g., cached step list, legacy env overrides).
            if skip_citation_verification:
                print(
                    "[PARALLEL:citation_verification] SKIPPED via skip_citation_verification flag",
                    file=sys.stderr, flush=True,
                )
                continue
            if current_payload and "ctx_json" in current_payload:
                from pipeline.prompts.modern_steps import step2e_citation_verification as _step2e_factory
                # Batch by interactor groups for verification
                _all_names = [
                    i.get("primary") for i in current_payload["ctx_json"].get("interactors", [])
                    if i.get("primary") and i.get("functions")
                ]
                if _all_names:
                    current_payload, _bstats = _run_parallel_batched_phase(
                        "citation_verification",
                        current_payload, user_query, _all_names,
                        step_factory=_step2e_factory,
                        batch_directive_template=(
                            "BATCH ASSIGNMENT — Verify and enrich evidence for ONLY\n"
                            "these {count} interactors:\n"
                            "{batch_names}\n"
                            "Do NOT process interactors outside this list.\n\n"
                            "For EACH function in these interactors, verify paper titles\n"
                            "exist and add missing evidence entries for functions with empty evidence."
                        ),
                        batch_size=PARALLEL_BATCH_SIZE,
                        max_workers=PARALLEL_MAX_WORKERS,
                        **_parallel_kwargs,
                    )
                    _accum_batch_tokens(_bstats, "citation_verification")
            continue

        # ===================================================================
        # DEFAULT: Single-call step (discovery, step2b_combined, QC, etc.)
        # ===================================================================
        prompt = build_prompt(
            step,
            current_payload,
            user_query,
            (step_idx == 1),
            known_interactions=known_interactions
        )

        # Determine interaction chain ID for this step
        api_mode = getattr(step, "api_mode", "generate") or "generate"
        chain_key = _get_chain_key(step.name)
        prev_id = _interaction_chain.get(chain_key) if api_mode == "interaction" else None

        try:
            raw_output, token_stats = call_gemini_model(
                step,
                prompt,
                cancel_event=cancel_event,
                request_mode=request_mode,
                batch_poll_seconds=batch_poll_seconds,
                batch_max_wait_seconds=batch_max_wait_seconds,
                previous_interaction_id=prev_id,
            )
        except PipelineError as exc:
            raise PipelineError(f"{step.name}: {exc}") from exc

        # Update interaction chain if this was an interaction call
        if api_mode == "interaction" and token_stats.get("interaction_id"):
            _interaction_chain[chain_key] = token_stats["interaction_id"]

        current_payload = _parse_with_retry(
            step, prompt, raw_output, current_payload,
            call_kwargs=dict(
                cancel_event=cancel_event,
                request_mode=request_mode,
                batch_poll_seconds=batch_poll_seconds,
                batch_max_wait_seconds=batch_max_wait_seconds,
                previous_interaction_id=prev_id,
            ),
        )
        current_payload = _handle_parse_failed_flag(current_payload)

    if current_payload is None:
        raise PipelineError("Main pipeline completed without returning data.")

    return current_payload


def run_full_job(
    user_query: str,
    jobs: dict,
    lock: Lock,
    num_interactor_rounds: int = 3,
    num_function_rounds: int = 3,
    skip_validation: bool = False,
    skip_deduplicator: bool = False,
    skip_arrow_determination: bool = False,
    skip_fact_checking: bool = False,
    quick_pathway_assignment: bool = False,
    flask_app = None,
    request_mode: Optional[str] = None,
    batch_poll_seconds: Optional[int] = None,
    batch_max_wait_seconds: Optional[int] = None,
    discovery_iterations: int = 5,
    model_overrides: Optional[Dict[str, str]] = None,
    validation_max_workers: Optional[int] = None,
    validation_batch_size: Optional[int] = None,
    validation_batch_delay: Optional[float] = None,
    allow_output_clamp: bool = False,
    iterative_delay_seconds: Optional[float] = None,
    verbose_pipeline: bool = False,
    enable_step_logging: bool = False,
    max_chain_claims: Optional[int] = None,
    chain_claim_style: Optional[str] = None,
    skip_schema_validation: bool = False,
    skip_interaction_metadata: bool = False,
    skip_pmid_update: bool = False,
    skip_arrow_validation: bool = False,
    skip_normalize_function_contexts: bool = False,
    skip_clean_names: bool = False,
    skip_finalize_metadata: bool = False,
    skip_direct_links: bool = False,
    skip_citation_verification: bool = False,
):
    """
    This is the master function for the Flask background thread.
    It orchestrates the main pipeline AND the evidence validation.

    Args:
        user_query: Protein name to analyze
        jobs: Shared jobs dictionary for status tracking
        lock: Threading lock for jobs dict access
        num_interactor_rounds: Number of interactor discovery rounds (default: 3)
        num_function_rounds: Number of function mapping rounds (default: 3)
        skip_validation: Skip evidence validation step
        skip_deduplicator: Skip function deduplication step
        skip_arrow_determination: Skip LLM arrow determination, use heuristic (100× faster)
        skip_fact_checking: Skip claim fact-checking step (faster, may include unverified claims)
        discovery_iterations: Number of iterative research iterations (default 5, 1-10)
        flask_app: Flask app instance (required for database operations in background thread)
        request_mode: Transport mode ('standard' or 'batch')
        batch_poll_seconds: Poll interval for batch jobs
        batch_max_wait_seconds: Max wait for batch jobs
    """
    # PR-4: tag this worker thread with the protein so every
    # utils.observability.log_event call from inside this pipeline run
    # pushes its event to the SSE buffer for ``/api/stream/<protein>``.
    try:
        from utils.observability import set_current_job_protein
        set_current_job_protein(user_query)
    except Exception:
        pass

    # Build env overrides from advanced settings
    env_dict: Dict[str, str] = {}
    if model_overrides:
        _model_env_map = {
            'gemini_model_core': 'GEMINI_MODEL_CORE',
            'gemini_model_evidence': 'GEMINI_MODEL_EVIDENCE',
            'gemini_model_arrow': 'GEMINI_MODEL_ARROW',
            'gemini_model_flash': 'GEMINI_MODEL_FLASH',
        }
        for key, env_key in _model_env_map.items():
            if key in model_overrides:
                env_dict[env_key] = model_overrides[key]
    if validation_max_workers is not None:
        env_dict['VALIDATION_MAX_WORKERS'] = str(validation_max_workers)
    if validation_batch_size is not None:
        env_dict['VALIDATION_BATCH_SIZE'] = str(validation_batch_size)
    if validation_batch_delay is not None:
        env_dict['VALIDATION_BATCH_DELAY'] = str(validation_batch_delay)
    if allow_output_clamp:
        env_dict['GEMINI_ALLOW_SERVER_OUTPUT_CLAMP'] = '1'
    if iterative_delay_seconds is not None:
        env_dict['ITERATIVE_DELAY_SECONDS'] = str(iterative_delay_seconds)
    if verbose_pipeline:
        env_dict['VERBOSE_PIPELINE'] = '1'
    if enable_step_logging:
        env_dict['ENABLE_STEP_LOGGING'] = 'true'
    if max_chain_claims is not None:
        env_dict['MAX_CHAIN_CLAIMS_PER_LINK'] = str(max_chain_claims)
    if chain_claim_style:
        env_dict['CHAIN_CLAIM_STYLE'] = chain_claim_style
    if skip_direct_links:
        env_dict['SKIP_DIRECT_LINK_EXTRACTION'] = '1'

    if env_dict:
        print(f"[ADVANCED] Applying {len(env_dict)} env overrides: {list(env_dict.keys())}", file=sys.stderr)

    # P1-B4 (interim): serialize concurrent jobs that mutate process env.
    # Without this, two parallel jobs with different model_overrides
    # cross-contaminate each other's env reads (gemini_runtime resolves
    # GEMINI_MODEL_* every call). Full per-run config object is the
    # proper fix and lands in Phase 5; this lock at least makes the
    # bleed impossible until then. Jobs WITHOUT env_dict (the common
    # case) skip the lock and run in parallel as before.
    _using_env_lock = bool(env_dict)
    if _using_env_lock:
        _RUN_ENV_OVERRIDE_LOCK.acquire()

    # Apply env overrides for the duration of this job
    _saved_env: Dict[str, Optional[str]] = {}
    try:
        for _k, _v in env_dict.items():
            _saved_env[_k] = os.environ.get(_k)
            os.environ[_k] = str(_v)
    except Exception:
        # If something fails before the main try below acquires control,
        # release the lock immediately so we don't deadlock.
        if _using_env_lock:
            try:
                _RUN_ENV_OVERRIDE_LOCK.release()
            except RuntimeError:
                pass
        raise

    # Get the cancel_event from jobs dict
    cancel_event = None
    with lock:
        if user_query in jobs:
            cancel_event = jobs[user_query].get('cancel_event')

    def update_status(text: str, current_step: int = None, total_steps: int = None):
        with lock:
            # Only update if this is still our job (check cancel_event identity)
            if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                progress_update = {"text": text}
                if current_step and total_steps:
                    progress_update.update({"current": current_step, "total": total_steps})
                jobs[user_query]['progress'] = progress_update
        # Wake SSE listeners
        try:
            from services.state import notify_job_update
            notify_job_update(user_query)
        except Exception as e:
            print(f"[WARN] SSE notification failed: {e}", file=sys.stderr)

    # ========================================================================
    # CALCULATE TOTAL STEPS (before starting work)
    # ========================================================================
    # Generate pipeline to get accurate step count
    if DYNAMIC_CONFIG_AVAILABLE:
        pipeline_steps = generate_pipeline(num_interactor_rounds, num_function_rounds)
    else:
        pipeline_steps = DEFAULT_PIPELINE_STEPS

    # Count pipeline steps
    pipeline_step_count = len(pipeline_steps)

    # Count post-processing steps via PostProcessor
    api_key = os.getenv("GOOGLE_CLOUD_PROJECT") or ""
    post_processor = PostProcessor(skip_flags={
        "skip_validation": skip_validation,
        "skip_deduplicator": skip_deduplicator,
        "skip_fact_checking": skip_fact_checking,
        "skip_schema_validation": skip_schema_validation,
        "skip_interaction_metadata": skip_interaction_metadata,
        "skip_pmid_update": skip_pmid_update,
        "skip_arrow_validation": skip_arrow_validation,
        "skip_clean_names": skip_clean_names,
        "skip_finalize_metadata": skip_finalize_metadata,
        "skip_normalize_function_contexts": skip_normalize_function_contexts,
    })
    post_steps = post_processor.count_steps()
    post_steps += 1  # Unified storage save

    # Total steps = pipeline + post-processing
    total_steps = pipeline_step_count + post_steps
    current_step = 0
    _reset_run_request_metrics()
    run_request_metrics = _get_run_request_metrics()

    def _consume_stage_metrics(payload_obj: Any) -> None:
        if not isinstance(payload_obj, dict):
            return
        stage_metrics = payload_obj.pop("_request_metrics", None)
        if not isinstance(stage_metrics, dict):
            return
        for key in (
            "evidence_calls_2_5pro",
            "arrow_calls_2_5pro",
            "arrow_llm_calls",
            "arrow_tier1_hits",
            "arrow_fallback_to_pro",
            "quota_skipped_calls",
        ):
            if key in stage_metrics:
                _increment_run_request_metric(key, _coerce_token_count(stage_metrics.get(key)))

    print(f"[PROGRESS] Total steps calculated: {total_steps} (pipeline: {pipeline_step_count}, post: {post_steps})", file=sys.stderr)

    # Initialize step logger for post-processing (only if enabled via environment)
    step_logger = None
    if STEP_LOGGER_AVAILABLE:
        step_logger = StepLogger(user_query)

    # Track job start for metrics
    _job_start_time = time.time()
    try:
        if METRICS_AVAILABLE:
            metrics_registry.increment("jobs_started")
    except Exception as exc:
        print(
            f"[WARN] metrics_registry.increment('jobs_started') failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    try:
        # --- STAGE 0: Load known interactions from database ---
        update_status("Loading known interactions from database...")
        from utils.storage import StorageLayer
        storage = StorageLayer(flask_app=flask_app)
        current_payload = None  # Track for crash-save
        known_interactions = storage.get_known_interactions(user_query)
        # Set status to "running" AFTER loading known interactions (avoids race condition
        # where concurrent reads see "running" before data is loaded)
        _set_pipeline_status(user_query, "running", flask_app)
        if known_interactions:
            print(f"[DB] History loaded: {len(known_interactions)} known interactions", file=sys.stderr)

        # --- STAGE 1: Run the main pipeline with known interactions context ---
        try:
            pipeline_payload = _run_main_pipeline_for_web(
                user_query,
                update_status,
                total_steps=total_steps,  # Pass accurate total
                num_interactor_rounds=num_interactor_rounds,
                num_function_rounds=num_function_rounds,
                discovery_iterations=discovery_iterations,
                request_mode=request_mode,
                batch_poll_seconds=batch_poll_seconds,
                batch_max_wait_seconds=batch_max_wait_seconds,
                cancel_event=cancel_event,
                known_interactions=known_interactions,  # Pass to pipeline for exclusion
                skip_arrow_determination=skip_arrow_determination,
                skip_citation_verification=skip_citation_verification,
            )

            # Pipeline completed - update current step
            current_step = pipeline_step_count
            current_payload = pipeline_payload
            storage.save_checkpoint(user_query, pipeline_payload, "pipeline_complete")

            # --- POST-PROCESSING (Stages 2-9) via PostProcessor ---
            final_payload, current_step = post_processor.run(
                pipeline_payload,
                api_key=api_key,
                user_query=user_query,
                flask_app=flask_app,
                step_logger=step_logger,
                update_status=update_status,
                current_step=current_step,
                total_steps=total_steps,
                consume_metrics=_consume_stage_metrics,
                verbose=False,
            )
            current_payload = final_payload

            # --- Filter zero-content interactors before save ---
            # Symmetry with snapshot rule (~L5466): an interactor counts as
            # non-empty if EITHER its flat functions[] is populated OR its
            # nested chain_link_functions dict has at least one non-empty
            # list. Without the chain_link_functions branch, chain-only
            # interactors (e.g. POLB on the ATXN3→PNKP→POLB→XRCC1 chain)
            # survive the snapshot but die here, surfacing as
            # [CHAIN AUDIT] missing_chain_link_functions at final save.
            if final_payload and "ctx_json" in final_payload:
                _ctx = final_payload["ctx_json"]
                _orig_count = len(_ctx.get("interactors", []))

                def _interactor_has_content(i):
                    if i.get("functions"):
                        return True
                    clf = i.get("chain_link_functions") or {}
                    if isinstance(clf, dict) and any(
                        isinstance(v, list) and v for v in clf.values()
                    ):
                        return True
                    return False

                _ctx["interactors"] = [
                    i for i in _ctx.get("interactors", [])
                    if _interactor_has_content(i)
                ]
                _dropped = _orig_count - len(_ctx["interactors"])
                if _dropped:
                    print(
                        f"[CLEANUP] Removed {_dropped} interactor(s) with zero content before save",
                        file=sys.stderr, flush=True,
                    )

            # --- STAGE 10: Save results (unified storage) ---
            current_step += 1
            update_status(
                text="Saving results...",
                current_step=current_step,
                total_steps=total_steps,
            )

            # Persist pipeline diagnostics (silent-drop counts, parse
            # failures, depth report) to Logs/<protein>/pipeline_diagnostics.json
            # so the API response can surface them via data_builder.
            # These fields don't have DB columns; without this step they
            # vanish at save time and the user never sees that 29
            # interactors were dropped or that 20 chain pairs are missing.
            try:
                _ctx_for_diag = final_payload.get("ctx_json", {}) if isinstance(final_payload, dict) else {}
                # Recovery code stamps `_missing_chain_claim_pairs` (list
                # of "FROM->TO" strings) for hops that never produced a
                # claim despite split-retry + per-pair recovery. Surface
                # these as `chain_pair_unrecoverable` for the UI banner.
                _unrecoverable = list(
                    _ctx_for_diag.get("_chain_pair_unrecoverable")
                    or _ctx_for_diag.get("_missing_chain_claim_pairs")
                    or []
                )
                # De-dupe while preserving order.
                _seen_pairs = set()
                _unrecoverable = [
                    p for p in _unrecoverable
                    if p and (p not in _seen_pairs and not _seen_pairs.add(p))
                ]
                _diag = {
                    "zero_function_dropped": list(_ctx_for_diag.get("_zero_function_dropped") or []),
                    "chain_pair_unrecoverable": _unrecoverable,
                    "pipeline_metadata": dict(final_payload.get("_pipeline_metadata") or {}),
                    "chain_incomplete_hops": [
                        {
                            "interactor": i.get("primary"),
                            "missing_hops": list(i.get("_chain_incomplete_hops") or []),
                        }
                        for i in (_ctx_for_diag.get("interactors") or [])
                        if isinstance(i, dict) and i.get("_chain_incomplete_hops")
                    ],
                }
                _diag_dir = os.path.join("Logs", user_query)
                os.makedirs(_diag_dir, exist_ok=True)
                _diag_path = os.path.join(_diag_dir, "pipeline_diagnostics.json")
                with open(_diag_path, "w", encoding="utf-8") as _diag_fp:
                    json.dump(_diag, _diag_fp, indent=2, ensure_ascii=False)
                print(
                    f"[DIAGNOSTICS] Wrote {_diag_path} "
                    f"(dropped={len(_diag['zero_function_dropped'])}, "
                    f"unrecoverable={len(_diag['chain_pair_unrecoverable'])}, "
                    f"incomplete_chains={len(_diag['chain_incomplete_hops'])})",
                    file=sys.stderr, flush=True,
                )
            except Exception as _diag_exc:
                # Diagnostics write is non-critical — never block the save.
                print(
                    f"[WARN] Failed to write pipeline_diagnostics.json: {_diag_exc}",
                    file=sys.stderr, flush=True,
                )

            save_stats = storage.save_pipeline_results(
                protein_symbol=user_query,
                final_payload=final_payload,
            )

            print(f"\n{'='*60}", file=sys.stderr)
            print(
                f"[STORAGE] protein={user_query} "
                f"db_synced={save_stats['db_synced']} "
                f"file_cached={save_stats['file_cached']} "
                f"created={save_stats.get('interactions_created', 0)} "
                f"updated={save_stats.get('interactions_updated', 0)}",
                file=sys.stderr,
            )
            print(f"{'='*60}\n", file=sys.stderr)

            # --- STAGE 11: Run Unified Pathway Pipeline (AFTER DB sync so interactions exist) ---
            if PATHWAY_PIPELINE_AVAILABLE and flask_app is not None:
                current_step += 1
                update_status(
                    text="Building pathway hierarchy...",
                    current_step=current_step,
                    total_steps=total_steps
                )
                try:
                    from scripts.pathway_v2.run_pipeline import run_pathway_pipeline

                    # Collect interaction IDs for quick-assign scoping
                    pipeline_kwargs = {}
                    if quick_pathway_assignment:
                        try:
                            from models import Interaction as _Interaction
                            with flask_app.app_context():
                                _query_ids = [
                                    i.id for i in _Interaction.query.filter_by(
                                        discovered_in_query=user_query
                                    ).all()
                                ]
                            pipeline_kwargs = {"quick_assign": True, "interaction_ids": _query_ids}
                            print(f">> Quick pathway assignment for {len(_query_ids)} interactions", file=sys.stderr)
                        except Exception as _e:
                            print(f"[WARN] Could not collect interaction IDs for quick assign: {_e}", file=sys.stderr)
                            # Still use quick_assign mode even without scoped IDs
                            pipeline_kwargs = {"quick_assign": True}

                    result = run_pathway_pipeline(**pipeline_kwargs)

                    # Enforce chain pathway consistency AFTER quick_assign
                    # Chain pathway enforcement removed — claims get independent pathways

                    if result.get('passed'):
                        fixes = result.get('verification', {}).get('issues_fixed', 0)
                        print(f">> Verification PASSED ({fixes} auto-fixes applied)", file=sys.stderr)
                    else:
                        blocking = result.get('verification', {}).get('blocking_issues', 0)
                        print(f">> Verification FAILED - {blocking} blocking issues", file=sys.stderr)

                    # P3.1 surface: append write-time pathway drift entries
                    # to the existing Logs/<protein>/pipeline_diagnostics.json
                    # so the frontend (cv_diagnostics.applyPathwayDriftBadges
                    # + the diagnostics banner) can show users which
                    # claims were rehomed at write time vs still drifting.
                    try:
                        _drift_entries = list(
                            (result.get("quick_assign") or {}).get("pathway_drifts") or []
                        )
                        if _drift_entries:
                            import json as _json_drift
                            _diag_path = os.path.join(
                                "Logs", user_query, "pipeline_diagnostics.json"
                            )
                            _existing_diag: Dict[str, Any] = {}
                            if os.path.isfile(_diag_path):
                                try:
                                    with open(_diag_path, "r", encoding="utf-8") as _df:
                                        _existing_diag = _json_drift.load(_df) or {}
                                except Exception:
                                    _existing_diag = {}
                            _existing_diag["pathway_drifts"] = _drift_entries
                            os.makedirs(os.path.dirname(_diag_path), exist_ok=True)
                            with open(_diag_path, "w", encoding="utf-8") as _df:
                                _json_drift.dump(_existing_diag, _df, indent=2, default=str)
                            _corrected = sum(1 for d in _drift_entries if d.get("action") == "corrected")
                            print(
                                f"[DIAGNOSTICS] Pathway drift surface: "
                                f"{_corrected} corrected, "
                                f"{len(_drift_entries) - _corrected} report-only "
                                f"→ written to {_diag_path}",
                                file=sys.stderr,
                            )
                    except Exception as _drift_exc:
                        # Non-critical: failure to persist drifts shouldn't
                        # break the pathway pipeline's success status.
                        print(
                            f"[WARN] Failed to persist pathway_drifts to "
                            f"pipeline_diagnostics.json: "
                            f"{type(_drift_exc).__name__}: {_drift_exc}",
                            file=sys.stderr,
                        )
                except ImportError as e:
                    print(f"[WARN] Failed to import pathway pipeline: {e}", file=sys.stderr)
                except Exception as e:
                    print(f"[ERROR] Pathway pipeline failed: {e}", file=sys.stderr)
                    import traceback
                    traceback.print_exc(file=sys.stderr)
                    # Continue - pathway assignment is non-critical for base functionality

            print(
                "[REQUEST METRICS] "
                f"core_calls_3pro={run_request_metrics.get('core_calls_3pro', 0)} | "
                f"evidence_calls_2_5pro={run_request_metrics.get('evidence_calls_2_5pro', 0)} | "
                # F5: arrow metrics. arrow_llm_calls is the real per-pair
                # LLM count (was hidden on gemini-3-flash-preview because
                # the legacy counter only matched "2.5" model names).
                # arrow_tier1_hits surfaces the speed win when DB has
                # already-validated arrows.
                f"arrow_llm_calls={run_request_metrics.get('arrow_llm_calls', 0)} | "
                f"arrow_tier1_hits={run_request_metrics.get('arrow_tier1_hits', 0)} | "
                f"arrow_fallback_to_pro={run_request_metrics.get('arrow_fallback_to_pro', 0)} | "
                f"quota_skipped_calls={run_request_metrics.get('quota_skipped_calls', 0)}",
                file=sys.stderr,
            )
        except Exception as exc:
            # Guaranteed save: persist whatever we have
            if current_payload is not None:
                try:
                    storage.save_checkpoint(user_query, current_payload, "crashed")
                except Exception as save_exc:
                    print(
                        f"[WARN] Crash-path save_checkpoint failed for "
                        f"{user_query}: {type(save_exc).__name__}: {save_exc}",
                        file=sys.stderr,
                    )
            _set_pipeline_status(user_query, "partial", flask_app, phase="crashed")
            with lock:
                if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                    jobs[user_query]['status'] = 'error'
                    jobs[user_query]['progress'] = f'Pipeline error: {exc}'
                    jobs[user_query]['_finished_at'] = time.time()
            try:
                from services.state import notify_job_update
                notify_job_update(user_query)
            except Exception as notify_exc:
                print(
                    f"[WARN] notify_job_update failed during crash handling "
                    f"for {user_query}: "
                    f"{type(notify_exc).__name__}: {notify_exc}",
                    file=sys.stderr,
                )
            raise

        # --- STAGE 12: Mark job as complete ---
        with lock:
            # Only update if this is still our job (check cancel_event identity)
            if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                jobs[user_query]['status'] = 'complete'
                jobs[user_query]['progress'] = 'Done'
                jobs[user_query]['_finished_at'] = time.time()
        try:
            from services.state import notify_job_update
            notify_job_update(user_query)
        except Exception as exc:
            print(
                f"[WARN] notify_job_update on complete failed for "
                f"{user_query}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

        # Record completion metrics (non-critical)
        try:
            if METRICS_AVAILABLE:
                _elapsed = time.time() - _job_start_time
                metrics_registry.increment("jobs_completed")
                metrics_registry.record_pipeline_complete({
                    "protein": user_query,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_job_start_time)),
                    "elapsed_seconds": _elapsed,
                    "step_count": total_steps,
                    **run_request_metrics,
                })
        except Exception as exc:
            print(
                f"[WARN] Completion metrics recording failed for "
                f"{user_query}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    except Exception as e:
        error_message = f"Error: {str(e)}"

        # Check if this was a cancellation
        is_cancelled = "cancelled by user" in error_message.lower()

        if is_cancelled:
            print(f"PIPELINE CANCELLED for '{user_query}'", file=sys.stderr)
            with lock:
                # Only update if this is still our job (check cancel_event identity)
                if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                    jobs[user_query]['status'] = 'cancelled'
                    jobs[user_query]['progress'] = {"text": "Cancelled by user"}
            try:
                from services.state import notify_job_update
                notify_job_update(user_query)
            except Exception as notify_exc:
                print(
                    f"[WARN] notify_job_update on cancel failed for "
                    f"{user_query}: "
                    f"{type(notify_exc).__name__}: {notify_exc}",
                    file=sys.stderr,
                )
            try:
                if METRICS_AVAILABLE:
                    metrics_registry.increment("jobs_cancelled")
            except Exception as metrics_exc:
                print(
                    f"[WARN] metrics_registry.increment('jobs_cancelled') "
                    f"failed: {type(metrics_exc).__name__}: {metrics_exc}",
                    file=sys.stderr,
                )
        else:
            print(f"PIPELINE ERROR for '{user_query}': {error_message}", file=sys.stderr)
            # Also print the full traceback for detailed debugging
            import traceback
            traceback.print_exc(file=sys.stderr)

            with lock:
                # Only update if this is still our job (check cancel_event identity)
                if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                    jobs[user_query]['status'] = 'error'
                    jobs[user_query]['progress'] = {"text": error_message}
                    jobs[user_query]['_finished_at'] = time.time()
                    print(f"Successfully updated jobs dictionary for '{user_query}' to error state.", file=sys.stderr)
            try:
                from services.state import notify_job_update
                notify_job_update(user_query)
            except Exception as notify_exc:
                print(
                    f"[WARN] notify_job_update on error failed for "
                    f"{user_query}: "
                    f"{type(notify_exc).__name__}: {notify_exc}",
                    file=sys.stderr,
                )
            try:
                if METRICS_AVAILABLE:
                    metrics_registry.increment("jobs_errored")
            except Exception as metrics_exc:
                print(
                    f"[WARN] metrics_registry.increment('jobs_errored') "
                    f"failed: {type(metrics_exc).__name__}: {metrics_exc}",
                    file=sys.stderr,
                )
    finally:
        # Restore env overrides
        for _k, _orig in _saved_env.items():
            if _orig is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _orig
        # Release the per-run env lock acquired above (P1-B4 interim).
        if _using_env_lock:
            try:
                _RUN_ENV_OVERRIDE_LOCK.release()
            except RuntimeError:
                # Already released (e.g. exception path released it
                # early). Nothing to do — finally runs anyway.
                pass


def run_requery_job(
    user_query: str,
    jobs: dict,
    lock: Lock,
    num_interactor_rounds: int = 3,
    num_function_rounds: int = 3,
    skip_deduplicator: bool = False,
    skip_fact_checking: bool = False,
    flask_app = None,
    request_mode: Optional[str] = None,
    batch_poll_seconds: Optional[int] = None,
    batch_max_wait_seconds: Optional[int] = None,
):
    """
    Re-query pipeline that finds ONLY NEW interactors and adds them to existing data.

    This function:
    1. Loads existing cached results
    2. Runs FRESH pipeline with context of what to avoid
    3. Validates ONLY new data
    4. Fact-checks ONLY new data (if not skipped)
    5. Merges new validated data with existing
    6. Saves merged results

    Args:
        user_query: Protein name to re-query
        jobs: Shared jobs dictionary for status tracking
        lock: Threading lock for jobs dict access
        num_interactor_rounds: Number of interactor discovery rounds (default: 3, min: 1)
        num_function_rounds: Number of function mapping rounds (default: 3, min: 1)
        skip_deduplicator: Skip function deduplication step
        flask_app: Flask app instance (required for database operations in background thread)
        request_mode: Transport mode ('standard' or 'batch')
        batch_poll_seconds: Poll interval for batch jobs
        batch_max_wait_seconds: Max wait for batch jobs
    """
    # Get the cancel_event from jobs dict
    cancel_event = None
    with lock:
        if user_query in jobs:
            cancel_event = jobs[user_query].get('cancel_event')

    def update_status(text: str, current_step: int = None, total_steps: int = None):
        with lock:
            # Only update if this is still our job (check cancel_event identity)
            if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                progress_update = {"text": text}
                if current_step and total_steps:
                    progress_update.update({"current": current_step, "total": total_steps})
                jobs[user_query]['progress'] = progress_update
        # Wake SSE listeners
        try:
            from services.state import notify_job_update
            notify_job_update(user_query)
        except Exception as e:
            print(f"[WARN] SSE notification failed: {e}", file=sys.stderr)

    # ========================================================================
    # CALCULATE TOTAL STEPS (before starting work)
    # ========================================================================
    # Generate pipeline to get accurate step count
    if DYNAMIC_CONFIG_AVAILABLE:
        # Allow 1-8 rounds for re-queries
        num_interactor_rounds = max(1, min(8, num_interactor_rounds))
        num_function_rounds = max(1, min(8, num_function_rounds))
        pipeline_steps = generate_pipeline(num_interactor_rounds, num_function_rounds)
    else:
        pipeline_steps = DEFAULT_PIPELINE_STEPS

    validated_steps = validate_steps(pipeline_steps)
    pipeline_step_count = len(validated_steps)

    # Count post-processing steps via PostProcessor (requery subset)
    api_key = os.getenv("GOOGLE_CLOUD_PROJECT") or ""
    requery_processor = PostProcessor(
        stages=PostProcessor.requery_stages(),
        skip_flags={"skip_deduplicator": skip_deduplicator},
    )
    post_steps = requery_processor.count_steps()
    post_steps += 2  # Merge + Save

    # Total steps = pipeline + post-processing
    total_steps = pipeline_step_count + post_steps
    current_step = 0

    print(f"[RE-QUERY PROGRESS] Total steps calculated: {total_steps} (pipeline: {pipeline_step_count}, post: {post_steps})", file=sys.stderr)

    try:
        # --- STAGE 0: Load existing cache (with backward compatibility) ---
        cache_path = os.path.join(CACHE_DIR, f"{user_query}.json")
        metadata_path = os.path.join(CACHE_DIR, f"{user_query}_metadata.json")

        if not os.path.exists(cache_path):
            raise PipelineError(f"No existing cache found for {user_query}. Run initial query first.")

        update_status("Loading existing results...")

        # Try to load from split files (new format) or single file (old format)
        if os.path.exists(metadata_path):
            # NEW FORMAT: Load from both files (snapshot + metadata)
            print(f"Re-query: Loading from new split-file format", file=sys.stderr)

            with open(cache_path, 'r', encoding='utf-8') as f:
                snapshot_data = json.load(f)

            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata_data = json.load(f)

            # Combine both for compatibility with existing logic
            existing_payload = {
                "snapshot_json": snapshot_data.get("snapshot_json", {}),
                "ctx_json": metadata_data.get("ctx_json", {})
            }
        else:
            # OLD FORMAT: Load from single file (backward compatibility)
            print(f"Re-query: Loading from old single-file format", file=sys.stderr)

            with open(cache_path, 'r', encoding='utf-8') as f:
                combined_data = json.load(f)

            # Extract both parts from the combined file
            existing_payload = {
                "snapshot_json": combined_data.get("snapshot_json", {}),
                "ctx_json": combined_data.get("ctx_json", {})
            }

            # If ctx_json is missing, try to extract it from the root level (very old format)
            if not existing_payload["ctx_json"] and "interactors" in combined_data:
                print(f"Re-query: WARNING - Very old format detected, attempting migration", file=sys.stderr)
                existing_payload["ctx_json"] = {
                    "main": combined_data.get("main", user_query),
                    "interactors": combined_data.get("interactors", []),
                    "interactor_history": [],
                    "function_history": {},
                    "function_batches": []
                }
                if not existing_payload["snapshot_json"]:
                    existing_payload["snapshot_json"] = {
                        "main": combined_data.get("main", user_query),
                        "interactors": combined_data.get("interactors", [])
                    }

        # Extract existing interactors and functions to provide context
        existing_ctx = existing_payload.get("ctx_json", {})
        existing_interactors = existing_ctx.get("interactors", [])
        existing_symbols = [i.get("primary", "") for i in existing_interactors if i.get("primary")]
        existing_function_history = existing_ctx.get("function_history", {})

        print(f"Re-query: Found {len(existing_symbols)} existing interactors", file=sys.stderr)
        print(f"Re-query: Function history for {len(existing_function_history)} proteins", file=sys.stderr)

        # --- STAGE 1: Run FRESH pipeline with context ---
        # (validated_steps and total_steps already calculated above)

        # Initialize payload with existing context (interactor_history and function_history)
        # This allows the AI to see what has already been found
        current_payload: Optional[Dict[str, Any]] = {
            "ctx_json": {
                "main": user_query,
                "interactor_history": existing_ctx.get("interactor_history", []),
                "function_history": existing_function_history,
                "function_batches": existing_ctx.get("function_batches", [])
            }
        }

        # Build context instruction for interactor discovery
        interactor_context_text = f"\n\n**RE-QUERY CONTEXT:**\n"
        interactor_context_text += f"You have previously found these interactors for {user_query}:\n"
        interactor_context_text += f"{', '.join(existing_symbols)}\n\n"
        interactor_context_text += f"**PRIORITY: Find COMPLETELY NEW interactors that are NOT in the above list.**\n"
        interactor_context_text += f"**If you can't find new interactors, you may research the existing ones for NEW FUNCTIONS.**\n"
        interactor_context_text += f"But focus primarily on discovering new interactor proteins first.\n\n"

        # Build detailed context for function discovery with triplet-based avoidance
        function_context_text = f"\n\n**RE-QUERY FUNCTION CONTEXT - TRIPLET-BASED DUPLICATE AVOIDANCE:**\n\n"

        function_context_text += f"**CRITICAL: UNDERSTAND THE TRIPLET MODEL**\n"
        function_context_text += f"A function is ONLY a duplicate if ALL THREE elements match:\n"
        function_context_text += f"  1. Main protein: {user_query}\n"
        function_context_text += f"  2. The SPECIFIC interactor protein\n"
        function_context_text += f"  3. The SPECIFIC function name\n\n"

        function_context_text += f"This means:\n"
        function_context_text += f"[OK]ALLOWED: Same interactor + DIFFERENT function (e.g., VCP already has 'DNA Repair', but 'Cell Cycle' is NEW)\n"
        function_context_text += f"[OK]ALLOWED: Different interactor + SAME function (e.g., VCP has 'DNA Repair', but UBQLN2 + 'DNA Repair' is NEW)\n"
        function_context_text += f"✗ BLOCKED: Same interactor + SAME function (e.g., VCP already has 'DNA Repair', so VCP + 'DNA Repair' again is duplicate)\n\n"

        function_context_text += f"**EXISTING FUNCTION TRIPLETS TO AVOID:**\n"
        function_context_text += f"Below is what each interactor ALREADY does with {user_query}. Only avoid the EXACT combinations listed.\n\n"

        for protein, funcs in existing_function_history.items():
            if funcs:
                function_context_text += f"━━━ {user_query} + {protein} ━━━\n"
                function_context_text += f"This specific interaction already covers {len(funcs)} function(s):\n"
                for func_name in funcs:
                    function_context_text += f"  ✗ AVOID: ({user_query}, {protein}, \"{func_name}\")\n"
                function_context_text += f"  [OK]BUT: ({user_query}, {protein}, <any NEW function>) is ALLOWED\n"
                function_context_text += "\n"

        function_context_text += f"**YOUR MISSION:**\n"
        function_context_text += f"1. For EXISTING interactors above: Find NEW functions they perform with {user_query}\n"
        function_context_text += f"2. For NEW interactors (not listed above): Find ALL their functions with {user_query}\n"
        function_context_text += f"3. ONLY avoid the exact triplets marked with ✗ above\n"
        function_context_text += f"4. If you find a function name in the list, check WHICH INTERACTOR it's paired with - if it's a different interactor, it's NEW!\n\n"

        function_context_text += f"**EXAMPLES - WHAT TO ADD:**\n"
        function_context_text += f"If ({user_query}, VCP, 'DNA Repair') exists:\n"
        function_context_text += f"  [OK]ADD: ({user_query}, VCP, 'Telomere Maintenance') - different function, same interactor\n"
        function_context_text += f"  [OK]ADD: ({user_query}, VCP, 'Cell Cycle Regulation') - different function, same interactor\n"
        function_context_text += f"  [OK]ADD: ({user_query}, UBQLN2, 'DNA Repair') - same function, different interactor\n"
        function_context_text += f"  ✗ SKIP: ({user_query}, VCP, 'DNA Repair') - exact duplicate\n"
        function_context_text += f"  ✗ SKIP: ({user_query}, VCP, 'DNA Damage Repair') - semantic duplicate of 'DNA Repair'\n\n"

        for step_idx, step in enumerate(validated_steps, start=1):
            # Check for cancellation before each step
            if cancel_event and cancel_event.is_set():
                raise PipelineError("Job cancelled by user")

            # Report progress with user-friendly name
            friendly_name = _get_user_friendly_step_name(step.name)
            update_status(
                text=f"Re-query: {friendly_name}",
                current_step=step_idx,
                total_steps=total_steps
            )

            if step.name == "step3_snapshot":
                if current_payload and "ctx_json" in current_payload:
                    # Atom J — pre-snapshot recovery (re-query path).
                    _zero = _zero_function_interactor_names(current_payload)
                    if _zero:
                        print(
                            f"[SNAPSHOT-RECOVERY] {len(_zero)} interactor(s) "
                            f"with zero functions before snapshot — retrying "
                            f"with batch_size=1: {_zero}",
                            file=sys.stderr, flush=True,
                        )
                        try:
                            from pipeline.prompts.modern_steps import (
                                step2a_interaction_functions as _snap_step2a,
                            )
                            _snap_kwargs = locals().get(
                                "_cli_parallel_kwargs"
                            ) or {}
                            current_payload, _rstats = _run_parallel_batched_phase(
                                "function_mapping_snapshot_recovery",
                                current_payload, user_query, _zero,
                                step_factory=_snap_step2a,
                                batch_directive_template=make_batch_directive(
                                    "SNAPSHOT-RECOVERY interactors (prior attempts truncated)"
                                ),
                                batch_size=1,
                                max_workers=PARALLEL_MAX_WORKERS,
                                **_snap_kwargs,
                            )
                        except Exception as _rec_exc:
                            print(
                                f"[SNAPSHOT-RECOVERY] Recovery pass raised "
                                f"{type(_rec_exc).__name__}: {_rec_exc} — "
                                "continuing to snapshot.",
                                file=sys.stderr, flush=True,
                            )
                    current_payload = create_snapshot_from_ctx(
                        current_payload["ctx_json"],
                        list(step.expected_columns),
                        step.name,
                    )
                continue

            # Build prompt
            prompt = build_prompt(step, current_payload, user_query, (step_idx == 1))

            # Add appropriate context based on step type
            if "discover" in step.name.lower() or "step1" in step.name:
                # Interactor discovery step - add interactor context
                prompt += interactor_context_text
            elif "function" in step.name.lower() or "step2a" in step.name:
                # Function mapping step - add function context
                prompt += function_context_text

            try:
                raw_output, _ = call_gemini_model(
                    step,
                    prompt,
                    cancel_event=cancel_event,
                    request_mode=request_mode,
                    batch_poll_seconds=batch_poll_seconds,
                    batch_max_wait_seconds=batch_max_wait_seconds,
                )
            except PipelineError as exc:
                raise PipelineError(f"{step.name}: {exc}") from exc

            current_payload = _parse_with_retry(
                step, prompt, raw_output, current_payload,
                call_kwargs=dict(
                    cancel_event=cancel_event,
                    request_mode=request_mode,
                    batch_poll_seconds=batch_poll_seconds,
                    batch_max_wait_seconds=batch_max_wait_seconds,
                ),
            )
            current_payload = _handle_parse_failed_flag(current_payload)

        if current_payload is None:
            raise PipelineError("Re-query pipeline completed without returning data.")

        new_pipeline_payload = current_payload

        # Pipeline completed - update current step
        current_step = pipeline_step_count

        # Extract NEW interactors and updates to existing ones
        new_ctx = new_pipeline_payload.get("ctx_json", {})
        new_interactors = new_ctx.get("interactors", [])
        new_symbols = [i.get("primary", "") for i in new_interactors if i.get("primary")]

        # Separate truly new interactors from updates to existing ones
        truly_new_interactors = []
        updated_existing_interactors = []

        for interactor in new_interactors:
            primary = interactor.get("primary")
            if not primary:
                continue

            if primary in existing_symbols:
                # This is an update to an existing interactor (likely new functions)
                updated_existing_interactors.append(interactor)
            else:
                # This is a completely new interactor
                truly_new_interactors.append(interactor)

        print(f"Re-query: Pipeline found {len(new_interactors)} interactors", file=sys.stderr)
        print(f"Re-query: {len(truly_new_interactors)} are truly new, {len(updated_existing_interactors)} are updates to existing", file=sys.stderr)

        # Combine for validation (both new and updates need validation)
        interactors_to_validate = truly_new_interactors + updated_existing_interactors

        if not interactors_to_validate:
            # No new data found — reset pipeline_status and increment query_count
            _set_pipeline_status(user_query, "complete", flask_app, phase="complete")
            try:
                with flask_app.app_context():
                    from models import Protein, db
                    p = Protein.query.filter_by(symbol=user_query).first()
                    if p:
                        p.query_count += 1
                        db.session.commit()
            except Exception as exc:
                print(
                    f"[WARN] Failed to increment query_count for "
                    f"{user_query} on no-new-data requery: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            update_status("No new data found. Search complete.")
            with lock:
                if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                    jobs[user_query]['status'] = 'complete'
                    jobs[user_query]['progress'] = 'No new data found'
                    jobs[user_query]['_finished_at'] = time.time()
            try:
                from services.state import notify_job_update
                notify_job_update(user_query)
            except Exception as exc:
                print(
                    f"[WARN] notify_job_update on no-new-data requery "
                    f"failed for {user_query}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            return

        # Create payload with data to validate (both new interactors and updates)
        new_only_payload = deepcopy(new_pipeline_payload)
        new_only_payload["ctx_json"]["interactors"] = interactors_to_validate

        # --- POST-PROCESSING (requery stages) via PostProcessor ---
        validated_new_payload, current_step = requery_processor.run(
            new_only_payload,
            api_key=api_key,
            user_query=user_query,
            update_status=update_status,
            current_step=current_step,
            total_steps=total_steps,
            verbose=False,
        )

        # --- STAGE 3: Merge validated new data with existing ---
        current_step += 1
        update_status(
            text="Merging new results with existing data...",
            current_step=current_step,
            total_steps=total_steps
        )

        # Get validated new interactors
        validated_new_interactors = validated_new_payload.get("ctx_json", {}).get("interactors", [])

        # Post-processing: Remove duplicate functions before merging
        print(f"Re-query: Checking for duplicate functions before merge...", file=sys.stderr)
        deduplicated_new_interactors = []

        for new_int in validated_new_interactors:
            primary = new_int.get("primary")
            new_functions = new_int.get("functions", [])

            if not primary or not new_functions:
                deduplicated_new_interactors.append(new_int)
                continue

            # Get existing functions for this protein
            existing_funcs = existing_function_history.get(primary, [])

            # Filter out duplicate functions
            unique_functions = []
            duplicates_found = 0

            for func in new_functions:
                func_name = func.get("function", "").strip().lower()

                # Check if this function name already exists (case-insensitive)
                is_duplicate = any(
                    func_name == existing_func.strip().lower()
                    for existing_func in existing_funcs
                )

                if not is_duplicate:
                    unique_functions.append(func)
                else:
                    duplicates_found += 1
                    print(f"Re-query: Removed duplicate function '{func.get('function')}' for {primary}", file=sys.stderr)

            # Update interactor with only unique functions
            new_int_copy = deepcopy(new_int)
            new_int_copy["functions"] = unique_functions

            if unique_functions or primary not in existing_symbols:
                # Keep this interactor if it has unique functions OR is a new interactor
                deduplicated_new_interactors.append(new_int_copy)
            else:
                print(f"Re-query: Skipping {primary} - all functions were duplicates", file=sys.stderr)

        print(f"Re-query: Deduplication complete. Kept {len(deduplicated_new_interactors)} interactors", file=sys.stderr)

        # Merge with existing using deep merge
        merged_interactors = deep_merge_interactors(existing_interactors, deduplicated_new_interactors)

        # Update existing payload with merged data
        existing_ctx["interactors"] = merged_interactors

        # Update tracking lists
        existing_interactor_history = existing_ctx.get("interactor_history", [])
        new_interactor_history = validated_new_payload.get("ctx_json", {}).get("interactor_history", [])
        existing_ctx["interactor_history"] = existing_interactor_history + [
            x for x in new_interactor_history if x not in existing_interactor_history
        ]

        existing_function_batches = existing_ctx.get("function_batches", [])
        new_function_batches = validated_new_payload.get("ctx_json", {}).get("function_batches", [])
        existing_ctx["function_batches"] = existing_function_batches + [
            x for x in new_function_batches if x not in existing_function_batches
        ]

        # Merge function_history
        existing_func_hist = existing_ctx.get("function_history", {})
        new_func_hist = validated_new_payload.get("ctx_json", {}).get("function_history", {})
        for protein, funcs in new_func_hist.items():
            if protein in existing_func_hist:
                existing_func_hist[protein].extend(funcs)
            else:
                existing_func_hist[protein] = funcs
        existing_ctx["function_history"] = existing_func_hist

        # Rebuild snapshot with merged data
        merged_payload = deepcopy(existing_payload)
        merged_payload["ctx_json"] = existing_ctx

        # Regenerate snapshot_json from merged ctx_json
        # Include ALL fields to preserve pathway data
        merged_payload["snapshot_json"] = {
            "main": existing_ctx.get("main", user_query),
            "interactors": merged_interactors  # Use full interactors to preserve pathways and other fields
        }

        # --- STAGE 4: Save merged results (unified storage) ---
        current_step += 1
        update_status(
            text="Saving merged results...",
            current_step=current_step,
            total_steps=total_steps,
        )

        from utils.storage import StorageLayer
        requery_storage = StorageLayer(flask_app=flask_app)
        save_stats = requery_storage.save_pipeline_results(
            protein_symbol=user_query,
            final_payload=merged_payload,
        )

        print(f"\n{'='*60}", file=sys.stderr)
        print(
            f"[RE-QUERY STORAGE] protein={user_query} "
            f"db_synced={save_stats['db_synced']} "
            f"file_cached={save_stats['file_cached']} "
            f"created={save_stats.get('interactions_created', 0)} "
            f"updated={save_stats.get('interactions_updated', 0)}",
            file=sys.stderr,
        )
        print(f"{'='*60}\n", file=sys.stderr)

        # Build detailed completion message with list of new items
        result_parts = []
        detailed_new_items = []

        if truly_new_interactors:
            result_parts.append(f"{len(truly_new_interactors)} new interactor{'s' if len(truly_new_interactors) != 1 else ''}")
            new_interactor_names = [i.get("primary", "Unknown") for i in truly_new_interactors]
            detailed_new_items.append(f"New interactors: {', '.join(new_interactor_names)}")

        if updated_existing_interactors:
            result_parts.append(f"{len(updated_existing_interactors)} updated interactor{'s' if len(updated_existing_interactors) != 1 else ''}")
            # Count new functions added to existing interactors
            for interactor in updated_existing_interactors:
                primary = interactor.get("primary", "Unknown")
                new_funcs = interactor.get("functions", [])
                if new_funcs:
                    func_names = [f.get("function", "Unknown") for f in new_funcs]
                    detailed_new_items.append(f"New functions for {primary}: {', '.join(func_names)}")

        result_message = "Added: " + ", ".join(result_parts) if result_parts else "No new data found"

        # Add detailed breakdown
        if detailed_new_items:
            result_message += " || " + " | ".join(detailed_new_items)

        print(f"Re-query: {result_message}", file=sys.stderr)
        print(f"Re-query: Saved to {cache_path}", file=sys.stderr)

        # --- STAGE 5: Mark job as complete ---
        print(f"Re-query: Marking job as complete for {user_query}", file=sys.stderr)
        with lock:
            if user_query in jobs:
                if jobs[user_query].get('cancel_event') is cancel_event:
                    jobs[user_query]['status'] = 'complete'
                    jobs[user_query]['progress'] = result_message
                    jobs[user_query]['_finished_at'] = time.time()
                    print(f"Re-query: Successfully set status to 'complete'", file=sys.stderr)
                else:
                    print(f"Re-query: Cancel event mismatch, not updating status", file=sys.stderr)
            else:
                print(f"Re-query: Job {user_query} not found in jobs dict", file=sys.stderr)
        try:
            from services.state import notify_job_update
            notify_job_update(user_query)
        except Exception as e:
            print(f"[WARN] Re-query SSE notification failed: {e}", file=sys.stderr)

    except Exception as e:
        error_message = f"Error: {str(e)}"
        is_cancelled = "cancelled by user" in error_message.lower()

        if is_cancelled:
            print(f"RE-QUERY CANCELLED for '{user_query}'", file=sys.stderr)
            with lock:
                if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                    jobs[user_query]['status'] = 'cancelled'
                    jobs[user_query]['progress'] = {"text": "Cancelled by user"}
        else:
            print(f"RE-QUERY ERROR for '{user_query}': {error_message}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)

            with lock:
                if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                    jobs[user_query]['status'] = 'error'
                    jobs[user_query]['progress'] = {"text": error_message}
                    jobs[user_query]['_finished_at'] = time.time()
        try:
            from services.state import notify_job_update
            notify_job_update(user_query)
        except Exception as e:
            print(f"[WARN] Re-query error SSE notification failed: {e}", file=sys.stderr)


# ============================================================================
# CLI INTERFACE (ORIGINAL FUNCTIONALITY PRESERVED)
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enhanced pipeline runner with evidence validation"
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Protein to analyze"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed debugging info"
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming previews"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output JSON path (default: <query>_pipeline.json)"
    )
    parser.add_argument(
        "--no-viz",
        action="store_true",
        help="Skip visualization generation"
    )
    parser.add_argument(
        "--viz-only",
        type=str,
        help="Only generate visualization from existing JSON"
    )
    parser.add_argument(
        "--validate-evidence",
        action="store_true",
        help="Run evidence validator after pipeline (RECOMMENDED)"
    )
    parser.add_argument(
        "--validation-batch-size",
        type=int,
        default=3,
        help="Batch size for evidence validation (default: 3)"
    )
    parser.add_argument(
        "--interactor-rounds",
        type=int,
        help="Number of interactor discovery rounds (default: 3, min: 3, max: 10)"
    )
    parser.add_argument(
        "--function-rounds",
        type=int,
        help="Number of function mapping rounds (default: 3, min: 3, max: 10)"
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for number of discovery rounds interactively"
    )
    parser.add_argument(
        "--request-mode",
        choices=["standard", "batch"],
        help="Core-model request transport mode (default from GEMINI_REQUEST_MODE or 'standard')."
    )
    parser.add_argument(
        "--batch-poll-seconds",
        type=int,
        help="Batch-mode polling interval in seconds (default from GEMINI_BATCH_POLL_SECONDS or 15)."
    )
    parser.add_argument(
        "--batch-max-wait-seconds",
        type=int,
        help="Batch-mode max wait in seconds (default from GEMINI_BATCH_MAX_WAIT_SECONDS or 86400)."
    )

    args = parser.parse_args()

    # Viz-only mode
    if args.viz_only:
        json_file = Path(args.viz_only)
        if not json_file.exists():
            parser.error(f"JSON file not found: {json_file}")

        print(f"Creating visualization from: {json_file}")
        html_file = create_visualization(json_file)
        print("Visualization created!")
        return

    # Normal pipeline mode
    if not args.query:
        try:
            args.query = input("Enter protein name: ").strip()
        except EOFError:
            args.query = ""

    if not args.query:
        parser.error("query is required")

    ensure_env()

    # Resolve request mode defaults early for user-facing summary.
    resolved_request_mode = args.request_mode
    if resolved_request_mode is None:
        try:
            resolved_request_mode = get_request_mode()
        except ValueError as exc:
            parser.error(str(exc))

    # Determine number of rounds (interactive or from args)
    num_interactor_rounds = 3  # Default
    num_function_rounds = 3    # Default

    if args.interactive or (not args.interactor_rounds and not args.function_rounds):
        # Interactive mode - prompt user
        print(f"\n{'='*80}")
        print("PIPELINE CONFIGURATION")
        print(f"{'='*80}")
        print("\nDefault configuration:")
        print("  - Interactor discovery rounds: 3 (1a, 1b, 1c)")
        print("  - Function mapping rounds: 3 (2a, 2a2, 2a3)")
        print("\nYou can customize the number of rounds for more comprehensive results.")
        print("More rounds = more interactors and functions discovered (but longer runtime)")
        print(f"{'='*80}\n")

        try:
            interactor_input = input("Number of interactor discovery rounds (3-10, default 3): ").strip()
            if interactor_input:
                num_interactor_rounds = int(interactor_input)
                num_interactor_rounds = max(3, min(10, num_interactor_rounds))

            function_input = input("Number of function mapping rounds (3-10, default 3): ").strip()
            if function_input:
                num_function_rounds = int(function_input)
                num_function_rounds = max(3, min(10, num_function_rounds))
        except (ValueError, EOFError):
            print("\nUsing defaults (3 rounds each)")

    # Override with command-line args if provided
    if args.interactor_rounds:
        num_interactor_rounds = max(3, min(10, args.interactor_rounds))
    if args.function_rounds:
        num_function_rounds = max(3, min(10, args.function_rounds))

    # Show configuration
    print(f"\n{'='*80}")
    print("RUNNING PIPELINE WITH:")
    print(f"{'='*80}")
    print(f"  Protein: {args.query}")
    print(f"  Interactor discovery rounds: {num_interactor_rounds}")
    print(f"  Function mapping rounds: {num_function_rounds}")
    print(f"  Request mode: {resolved_request_mode}")
    if resolved_request_mode == "batch":
        try:
            resolved_poll = resolve_batch_poll_seconds(args.batch_poll_seconds)
            resolved_wait = resolve_batch_max_wait_seconds(args.batch_max_wait_seconds)
            print(f"  Batch polling: {resolved_poll}s (max wait {resolved_wait}s)")
        except ValueError as exc:
            parser.error(str(exc))
    if DYNAMIC_CONFIG_AVAILABLE:
        print(f"  Dynamic configuration: ENABLED")
    else:
        print(f"  Dynamic configuration: NOT AVAILABLE (using defaults)")
    print(f"{'='*80}\n")

    # Run main pipeline
    final_payload, step_logger = run_pipeline(
        user_query=args.query,
        verbose=args.verbose,
        stream=not args.no_stream,
        num_interactor_rounds=num_interactor_rounds,
        num_function_rounds=num_function_rounds,
        request_mode=args.request_mode,
        batch_poll_seconds=args.batch_poll_seconds,
        batch_max_wait_seconds=args.batch_max_wait_seconds,
    )

    # Save initial output
    output_path = Path(args.output) if args.output else Path(f"{args.query}_pipeline.json")
    output_path.write_text(
        json.dumps(final_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n[OK]Saved pipeline output to: {output_path}")

    # Save NDJSON if present
    ndjson_content = final_payload.get("ndjson")
    if ndjson_content:
        ndjson_path = output_path.with_suffix(".ndjson")
        if isinstance(ndjson_content, list):
            ndjson_text = "\n".join(
                item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                for item in ndjson_content
            )
        else:
            ndjson_text = str(ndjson_content)
        ndjson_path.write_text(ndjson_text.rstrip() + "\n", encoding="utf-8")
        print(f"[OK]Saved NDJSON to: {ndjson_path}")

    # Evidence validation (if requested)
    if args.validate_evidence:
        if not VALIDATOR_AVAILABLE:
            print("\n[WARN]Evidence validator not available. Skipping validation.")
        else:
            print(f"\n{'='*80}")
            print("RUNNING EVIDENCE VALIDATION")
            print(f"{'='*80}")

            api_key = os.getenv("GOOGLE_CLOUD_PROJECT")
            validated_payload = validate_and_enrich_evidence(
                final_payload,
                api_key,
                verbose=args.verbose,
                batch_size=args.validation_batch_size,
                step_logger=step_logger
            )

            # Save validated output
            validated_path = output_path.parent / f"{output_path.stem}_validated{output_path.suffix}"
            validated_path.write_text(
                json.dumps(validated_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8"
            )
            print(f"\n[OK]Saved validated output to: {validated_path}")

            # Use validated output for visualization
            output_path = validated_path
            final_payload = validated_payload

    # Summary
    print(f"\n{'='*80}")
    print("PIPELINE COMPLETE")
    print(f"{'='*80}")

    if "ctx_json" in final_payload:
        interactors = final_payload["ctx_json"].get("interactors", [])
        total_functions = sum(len(i.get("functions", [])) for i in interactors)
        total_pmids = sum(len(i.get("pmids", [])) for i in interactors)

        print(f"[OK]Found {len(interactors)} interactors")
        print(f"[OK]Mapped {total_functions} biological functions")
        print(f"[OK]Collected {total_pmids} citations")

    # Generate visualization
    if not args.no_viz:
        print(f"\n{'='*80}")
        print("GENERATING VISUALIZATION")
        print(f"{'='*80}")
        html_path = output_path.with_suffix(".html")
        viz_file = create_visualization(output_path, html_path)
        print(f"[OK]Visualization saved to: {viz_file}")


if __name__ == "__main__":
    main()
