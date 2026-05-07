#!/usr/bin/env python3
"""
LLM Utilities for Pathway V2 Pipeline
=====================================
Shared functions for calling Gemini 3 Pro and parsing JSON responses.
"""

import os
import time
import logging
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from utils.gemini_runtime import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    build_generate_content_config,
    extract_text_from_generate_response,
    get_client,
    minimal_json_object_schema,
    submit_multi_batch_job,
)

# Setup logging
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent


class JsonParseError(RuntimeError):
    """Raised when all JSON extraction strategies fail."""
    pass

_LONG_DIGIT_RUN_RE = re.compile(r"\d{80,}")
_KEY_CONCAT_RE = re.compile(r'"function_pathways(?!")([^"]{1,120})"\s*:', re.IGNORECASE)
_PRIMITIVE_FUNCTION_PATHWAYS_RE = re.compile(
    r'"function_pathways"\s*:\s*\[\s*(?:"?function_index"?|\d+)\s*\]',
    re.IGNORECASE,
)
_MALFORMED_FUNCTION_PATHWAYS_TEXT_RE = re.compile(
    r'"function_pathways"\s*:\s*\[\s*"function_index"\s*\]',
    re.IGNORECASE,
)

def _ensure_vertex_config() -> None:
    """Ensure Vertex AI configuration is present in environment."""
    if not os.environ.get('GOOGLE_CLOUD_PROJECT'):
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / '.env')

    if not os.environ.get('GOOGLE_CLOUD_PROJECT'):
        raise RuntimeError("GOOGLE_CLOUD_PROJECT not found in environment")
    if not os.environ.get('GOOGLE_CLOUD_LOCATION'):
        raise RuntimeError("GOOGLE_CLOUD_LOCATION not found in environment")

def _find_balanced_json(text: str, start_pos: int) -> str | None:
    """Find a balanced JSON object starting at start_pos."""
    if start_pos >= len(text) or text[start_pos] != '{':
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start_pos, len(text)):
        char = text[i]

        # Handle string escapes
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        # Track string boundaries
        if char == '"':
            in_string = not in_string
            continue

        # Only count brackets outside strings
        if not in_string:
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    return text[start_pos:i+1]

    return None

def _extract_partial_assignments(text: str) -> list:
    """
    Extract individual assignments from malformed JSON using regex.
    This salvages whatever assignments we can even if the overall JSON is broken.
    Returns list of dicts with interaction_id and specific_pathway.
    """
    assignments = []
    # Match patterns like: "interaction_id": "123", ... "specific_pathway": "Some Pathway"
    # Handle both quoted and unquoted IDs
    pattern = r'"interaction_id"\s*:\s*"?(\d+)"?\s*,\s*"specific_pathway"\s*:\s*"([^"]+)"'
    matches = re.findall(pattern, text, re.IGNORECASE)
    for interaction_id, pathway in matches:
        assignments.append({
            'interaction_id': interaction_id,
            'specific_pathway': pathway
        })

    if assignments:
        logger.info(f"  Partial extraction recovered {len(assignments)} assignments from malformed JSON")

    return assignments


def _fix_truncated_json(text: str) -> str:
    """
    Attempt to fix truncated JSON by closing unclosed brackets and braces.
    """
    # Count open brackets/braces
    open_braces = text.count('{') - text.count('}')
    open_brackets = text.count('[') - text.count(']')

    # Check if we're inside a string (unclosed quote)
    in_string = False
    escape_next = False
    for char in text:
        if escape_next:
            escape_next = False
            continue
        if char == '\\':
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string

    fixed = text

    # Close string if needed
    if in_string:
        fixed += '"'

    # Close brackets first, then braces
    fixed += ']' * open_brackets
    fixed += '}' * open_braces

    return fixed


def _corruption_flags(text: str, expected_root_key: str | None = None) -> Dict[str, Any]:
    primitive_block_count = len(_PRIMITIVE_FUNCTION_PATHWAYS_RE.findall(text or ""))
    flags: Dict[str, Any] = {
        "has_long_digit_run": bool(_LONG_DIGIT_RUN_RE.search(text or "")),
        "has_key_concat": bool(_KEY_CONCAT_RE.search(text or "")),
        "has_malformed_function_pathways_text": bool(_MALFORMED_FUNCTION_PATHWAYS_TEXT_RE.search(text or "")),
        "primitive_function_pathways_count": primitive_block_count,
        "has_excess_primitive_function_pathways": primitive_block_count >= 3,
        "missing_expected_root_key": bool(expected_root_key) and (f'"{expected_root_key}"' not in (text or "")),
    }
    flags["is_corrupted"] = any(
        bool(flags[k])
        for k in (
            "has_long_digit_run",
            "has_key_concat",
            "has_malformed_function_pathways_text",
            "has_excess_primitive_function_pathways",
            "missing_expected_root_key",
        )
    )
    return flags


def is_corrupted_json_text(text: str, expected_root_key: str | None = None) -> bool:
    """Return True when the response text matches known JSON corruption signatures."""
    return bool(_corruption_flags(text, expected_root_key).get("is_corrupted"))


def safe_extract_json(text: str, expected_root_key: str | None = None) -> dict:
    """Best-effort JSON extraction with corruption rejection and root-key checks."""
    flags = _corruption_flags(text, expected_root_key)
    if flags.get("is_corrupted"):
        logger.warning(
            "Rejected corrupted JSON text before parsing (len=%s, flags=%s)",
            len(text or ""),
            {k: v for k, v in flags.items() if k != "is_corrupted" and v},
        )
        return {}

    try:
        parsed = _extract_json_from_text(text)
    except JsonParseError as exc:
        logger.error("JSON extraction failed: %s", exc)
        return {}

    if parsed.get("_partial_extraction"):
        logger.warning(
            "Partial extraction detected — result may be incomplete (recovered %d assignments)",
            len(parsed.get("assignments", [])),
        )

    if expected_root_key and expected_root_key not in parsed:
        logger.warning(
            "Parsed JSON missing expected root key '%s' (keys=%s)",
            expected_root_key,
            list(parsed.keys())[:10] if isinstance(parsed, dict) else type(parsed).__name__,
        )
        return {}
    return parsed


_PARSE_FAILURE_LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB per file
_PARSE_FAILURE_LOG_KEEP = 3                      # keep .1 .2 .3 archives


def _append_parse_failure_log(text: str, response_len: int) -> None:
    """Append a parse-failure entry to logs/json_parse_failures.log with rotation.

    When the current log file exceeds _PARSE_FAILURE_LOG_MAX_BYTES, the file is
    rotated: .log → .log.1 → .log.2 → .log.3 (oldest dropped). This prevents
    unbounded growth (the previous implementation grew to 750KB+ unchecked).
    """
    debug_file = PROJECT_ROOT / 'logs' / 'json_parse_failures.log'
    debug_file.parent.mkdir(exist_ok=True)

    # Rotate if needed
    try:
        if debug_file.exists() and debug_file.stat().st_size > _PARSE_FAILURE_LOG_MAX_BYTES:
            # Shift existing archives: drop oldest, then shift down
            oldest = debug_file.with_suffix(f'.log.{_PARSE_FAILURE_LOG_KEEP}')
            if oldest.exists():
                oldest.unlink()
            for i in range(_PARSE_FAILURE_LOG_KEEP - 1, 0, -1):
                src = debug_file.with_suffix(f'.log.{i}')
                dst = debug_file.with_suffix(f'.log.{i + 1}')
                if src.exists():
                    src.rename(dst)
            # Current .log → .log.1
            debug_file.rename(debug_file.with_suffix('.log.1'))
    except OSError:
        # Rotation failure is non-fatal — keep writing to whatever file exists
        pass

    with open(debug_file, 'a', encoding='utf-8') as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"TIMESTAMP: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"RESPONSE LENGTH: {response_len}\n")
        f.write(f"FULL RESPONSE:\n{text}\n")


def _extract_json_from_text(text: str) -> dict:
    """
    Extract JSON object from text (handles markdown code blocks, malformed responses,
    truncation, and single quotes).
    """
    if not text:
        logger.warning("Empty response text")
        return {}

    # Strategy 1: Try direct parsing
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: Extract from ```json ... ``` blocks
    match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 3: Replace single quotes with double quotes (Python dict notation)
    try:
        fixed_quotes = text.replace("'", '"')
        return json.loads(fixed_quotes)
    except json.JSONDecodeError:
        pass

    # Strategy 4: Balanced bracket search - find properly balanced JSON object
    start = text.find('{')
    if start != -1:
        json_str = _find_balanced_json(text, start)
        if json_str:
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass

    # Strategy 5: Try to fix truncated JSON by closing brackets
    start = text.find('{')
    if start != -1:
        truncated_json = text[start:]
        fixed_json = _fix_truncated_json(truncated_json)
        try:
            return json.loads(fixed_json)
        except json.JSONDecodeError:
            pass

    # Strategy 6: Extract partial assignments using regex (last resort)
    partial = _extract_partial_assignments(text)
    if partial:
        return {'assignments': partial, '_partial_extraction': True}

    # Failed all strategies - provide detailed error context
    response_len = len(text)
    preview_head = text[:300] if len(text) > 300 else text
    preview_tail = text[-300:] if len(text) > 300 else ""

    logger.warning(
        f"Failed to extract JSON from response (length: {response_len}):\n"
        f"  HEAD: {preview_head}\n"
        f"  TAIL: {preview_tail if preview_tail else '(same as head)'}"
    )

    # Log full response to file for debugging (with size-based rotation)
    try:
        _append_parse_failure_log(text, response_len)
    except Exception as e:
        logger.debug(f"Could not write debug log: {e}")

    raise JsonParseError(f"All 6 JSON extraction strategies failed (response length: {response_len})")

# 2026-05-03: aligned to gemini-3-flash-preview's actual server cap (8192).
# Was 20000 with the comment "60K is wasteful" — but 20000 was already
# above the Flash ceiling, so every call paid a "planning tax" while the
# server reasoned about the unreachable budget before clamping. Pathway
# responses average ~2K tokens; 8192 leaves comfortable headroom for the
# largest batched assignment without forcing the model to plan against
# a budget it can't use. Per Vertex AI docs (May 2026): gemini-3-flash-
# preview structured output cap is 8192 tokens. Pro 3 supports 65536 if
# the model parameter is changed (but pathway calls run on Flash).
_PATHWAY_MAX_OUTPUT_TOKENS = 8192  # Flash hard cap; pathway responses ~2K

def _call_gemini_json(
    prompt: str,
    api_key: str = None,
    max_retries: int = 3,
    temperature: Optional[float] = None,
    max_output_tokens: int = _PATHWAY_MAX_OUTPUT_TOKENS,
    response_json_schema: Optional[Dict[str, Any]] = None,
    model: str = "gemini-3-flash-preview",
    thinking_level: str = "high",
    disable_afc: bool = False,
    expected_root_key: Optional[str] = None,
    cached_content: Optional[str] = None,
) -> dict:
    """
    Call Gemini 3 Pro and parse JSON response.
    """
    try:
        from google import genai
        from google.genai import types as google_types
    except ImportError:
        logger.error("google-genai SDK not installed. Please run `pip install google-genai`.")
        return {}

    if api_key is None:
        try:
            _ensure_vertex_config()
        except RuntimeError as e:
            logger.error(str(e))
            return {}

    client = get_client()
    afc_config = google_types.AutomaticFunctionCallingConfig(disable=True) if disable_afc else None
    config = build_generate_content_config(
        thinking_level=thinking_level,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        response_mime_type="application/json",
        response_json_schema=response_json_schema or minimal_json_object_schema(),
        include_thoughts=False,
        automatic_function_calling=afc_config,
        cached_content=cached_content,
    )

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )

            text = extract_text_from_generate_response(resp)
            if text:
                parsed = safe_extract_json(text, expected_root_key=expected_root_key)
                if parsed:
                    parsed_count = 0
                    if expected_root_key and isinstance(parsed.get(expected_root_key), list):
                        parsed_count = len(parsed.get(expected_root_key, []))
                    logger.info(
                        "LLM JSON accepted (model=%s, len=%s, root=%s, items=%s)",
                        model,
                        len(text),
                        expected_root_key or "<none>",
                        parsed_count,
                    )
                    return parsed
                raise RuntimeError("Corrupted/invalid JSON payload from model")
                
            raise RuntimeError("Empty response from model")
            
        except Exception as e:
            last_err = e
            logger.warning("Attempt %s failed (model=%s): %s", attempt, model, e)
            time.sleep(2 * attempt)

    logger.error(f"LLM call failed after {max_retries} attempts: {last_err}")
    return {}


# ==============================================================================
# CACHED LLM CALLS
# ==============================================================================

def _call_gemini_json_cached(
    prompt: str,
    cache_key: str = None,
    cache_type: str = "parent",  # "parent" or "siblings"
    api_key: str = None,
    max_retries: int = 3,
    temperature: Optional[float] = None,
    max_output_tokens: int = _PATHWAY_MAX_OUTPUT_TOKENS,
    response_json_schema: Optional[Dict[str, Any]] = None,
    model: str = "gemini-3-flash-preview",
    thinking_level: str = "high",
    disable_afc: bool = False,
    expected_root_key: Optional[str] = None,
) -> dict:
    """
    Call Gemini with optional caching.

    If cache_key is provided and cache_type is "parent", checks PathwayCache first.
    """
    if cache_key:
        from scripts.pathway_v2.cache import get_pathway_cache
        cache = get_pathway_cache()

        if cache_type == "parent":
            cached = cache.get_parent(cache_key)
            if cached:
                logger.info(f"  Cache hit for parent of '{cache_key}'")
                return {"child": cache_key, "parent": cached, "_cached": True}
        elif cache_type == "siblings":
            cached = cache.get_siblings(cache_key)
            if cached:
                logger.info(f"  Cache hit for siblings of '{cache_key}'")
                return {"siblings": cached, "_cached": True}

    # Call LLM
    result = _call_gemini_json(
        prompt=prompt,
        api_key=api_key,
        max_retries=max_retries,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_json_schema=response_json_schema,
        model=model,
        thinking_level=thinking_level,
        disable_afc=disable_afc,
        expected_root_key=expected_root_key,
    )

    # Cache result if successful
    if cache_key and result:
        from scripts.pathway_v2.cache import get_pathway_cache
        cache = get_pathway_cache()

        if cache_type == "parent" and result.get("parent"):
            cache.set_parent(cache_key, result["parent"])
        elif cache_type == "siblings" and result.get("siblings"):
            cache.set_siblings(cache_key, result["siblings"])

    return result


# ==============================================================================
# BATCH API LLM CALLS
# ==============================================================================

def _call_gemini_json_batch(
    prompt_items: list,
    *,
    model: str = "gemini-3-flash-preview",
    thinking_level: str = "high",
    max_output_tokens: int = _PATHWAY_MAX_OUTPUT_TOKENS,
    response_json_schema: Optional[Dict[str, Any]] = None,
    expected_root_keys: Optional[list] = None,
    display_name: Optional[str] = None,
    api_key: str = None,
) -> list:
    """Submit multiple prompts as a single Batch API job and parse JSON results.

    *prompt_items* is a list of prompt strings.
    Returns a list of parsed dicts (aligned by index).  Empty dict on failure.
    """
    if not prompt_items:
        return []

    try:
        from google.genai import types as google_types
    except ImportError:
        logger.error("google-genai SDK not installed.")
        return [{} for _ in prompt_items]

    config = build_generate_content_config(
        thinking_level=thinking_level,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        response_json_schema=response_json_schema or minimal_json_object_schema(),
        include_thoughts=False,
    )

    inline_requests = [
        google_types.InlinedRequest(contents=prompt, config=config)
        for prompt in prompt_items
    ]

    try:
        raw_results = submit_multi_batch_job(
            model=model,
            requests=inline_requests,
            display_name=display_name or f"pathway-batch-{int(time.time())}",
            api_key=api_key,
        )
    except Exception as exc:
        logger.error("Batch API job failed: %s", exc)
        return [{} for _ in prompt_items]

    parsed_results: list = []
    for idx, (text, _stats, err) in enumerate(raw_results):
        if err or not text:
            logger.warning("Batch response %d failed: %s", idx, err or "empty text")
            parsed_results.append({})
            continue

        expected_key = None
        if expected_root_keys and idx < len(expected_root_keys):
            expected_key = expected_root_keys[idx]

        parsed = safe_extract_json(text, expected_root_key=expected_key)
        parsed_results.append(parsed)

    logger.info(
        "Batch API: %d/%d prompts returned valid JSON",
        sum(1 for p in parsed_results if p),
        len(prompt_items),
    )
    return parsed_results
