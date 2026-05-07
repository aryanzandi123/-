"""Shared Gemini 3 runtime helpers.

Centralized Gemini SDK interface: client management, model registry,
explicit caching, Interactions API, Batch API, and request builders.
All callsites use consistent Gemini 3 settings through this module.
"""

from __future__ import annotations

import hashlib
import os
import re
import time as _time_mod
from threading import Lock
from typing import Any, Dict, Optional

from google.genai import types

DEFAULT_MODEL = "gemini-3-flash-preview" #WAS GEMINI-3.1-PRO
DEFAULT_FLASH_MODEL = "gemini-3-flash-preview"
# Vertex AI Gemini 3 free-form generation accepts up to 65536 output
# tokens. The 8192 server cap that triggered batch failures on the
# 2026-04-29 ULK1 run applied ONLY to structured-output calls
# (those passing response_json_schema) — chain-claim generation
# step2ax_*/step2az_*. For free-form calls (function mapping, discovery,
# QC, citation, arrow), 65536 is the right ceiling. Per-step factories
# may set a lower value when their structured-output path is engaged.
DEFAULT_MAX_OUTPUT_TOKENS = 65536
DEFAULT_THINKING_LEVEL = "high"
DEFAULT_THINKING_SUMMARIES = "auto"
DEFAULT_EVIDENCE_MODEL = "gemini-3-flash-preview"
DEFAULT_ARROW_MODEL = "gemini-3-flash-preview"
DEFAULT_REQUEST_MODE = "standard"
DEFAULT_BATCH_POLL_SECONDS = 15
DEFAULT_BATCH_MAX_WAIT_SECONDS = 86400
GEMINI_3_PRO_PROMPT_BUCKET_THRESHOLD = 200000

# ---------------------------------------------------------------------------
# Model registry (env-overridable defaults for every pipeline role)
# ---------------------------------------------------------------------------
MODEL_REGISTRY: Dict[str, str] = {
    "core": DEFAULT_MODEL,
    "evidence": DEFAULT_EVIDENCE_MODEL,
    "arrow": DEFAULT_ARROW_MODEL,
    "flash": DEFAULT_FLASH_MODEL,
    "deep_research": "deep-research-pro-preview-12-2025",
    "iterative": "gemini-3-flash-preview",  # Falls back to generate_content if Interactions API unsupported on Vertex
}

_MODEL_ENV_KEYS: Dict[str, str] = {
    "core": "GEMINI_MODEL_CORE",
    "evidence": "GEMINI_MODEL_EVIDENCE",
    "arrow": "GEMINI_MODEL_ARROW",
    "flash": "GEMINI_MODEL_FLASH",
    "deep_research": "GEMINI_MODEL_DEEP_RESEARCH",
    "iterative": "GEMINI_MODEL_ITERATIVE",
}

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SEARCH_HINT_RE = re.compile(
    r"\b(latest|today|current|recent|news|who won|when did|price|stock|weather)\b",
    re.IGNORECASE,
)
_DAILY_QUOTA_RE = re.compile(
    r"generate_requests_per_model_per_day|quota exceeded.*per.*day|quota.*limit:\s*0",
    re.IGNORECASE | re.DOTALL,
)


def validate_vertex_config() -> tuple[str, str]:
    """Validate Vertex AI configuration from environment. Returns (project, location)."""
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "").strip()
    if not project:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT is not set. Add it to your .env file or environment."
        )
    if not location:
        raise RuntimeError(
            "GOOGLE_CLOUD_LOCATION is not set. Add it to your .env file or environment."
        )
    return project, location


# ---------------------------------------------------------------------------
# Singleton client (thread-safe, cached by API key)
# ---------------------------------------------------------------------------
_client_cache: Dict[str, Any] = {}
_client_lock = Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def get_client(
    api_key: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    retry_attempts: Optional[int] = None,
) -> Any:
    """Return a cached, thread-safe Vertex AI Gemini Client.

    The api_key parameter is ignored (kept for call-site compatibility).
    Authentication uses Application Default Credentials (ADC).
    """
    from google import genai as google_genai

    project, location = validate_vertex_config()
    timeout_part = int(timeout_ms or _env_int("GEMINI_REQUEST_TIMEOUT_MS", 0) or 0)
    retry_part = int(
        retry_attempts
        if retry_attempts is not None
        else _env_int("GEMINI_HTTP_RETRY_ATTEMPTS", 1)
    )
    retry_initial = float(os.getenv("GEMINI_HTTP_RETRY_INITIAL_DELAY", "0.5"))
    retry_max_delay = float(os.getenv("GEMINI_HTTP_RETRY_MAX_DELAY", "4.0"))
    retry_exp_base = float(os.getenv("GEMINI_HTTP_RETRY_EXP_BASE", "1.6"))
    retry_jitter = float(os.getenv("GEMINI_HTTP_RETRY_JITTER", "0.25"))
    pool_max = max(1, _env_int("GEMINI_HTTP_POOL_MAX_CONNECTIONS", 64))
    pool_keepalive = max(1, _env_int("GEMINI_HTTP_POOL_MAX_KEEPALIVE", min(pool_max, 32)))
    cache_key = (
        f"{project}:{location}:timeout={timeout_part}:retry={retry_part}:"
        f"pool={pool_max}/{pool_keepalive}"
    )
    with _client_lock:
        if cache_key not in _client_cache:
            import httpx

            retry_options = None
            if retry_part >= 0:
                retry_options = types.HttpRetryOptions(
                    attempts=retry_part,
                    initial_delay=retry_initial,
                    max_delay=retry_max_delay,
                    exp_base=retry_exp_base,
                    jitter=retry_jitter,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                )
            client_args = {
                "limits": httpx.Limits(
                    max_connections=pool_max,
                    max_keepalive_connections=min(pool_keepalive, pool_max),
                )
            }
            http_options = types.HttpOptions(
                timeout=timeout_part if timeout_part > 0 else None,
                retry_options=retry_options,
                client_args=client_args,
            )
            _client_cache[cache_key] = google_genai.Client(
                vertexai=True,
                project=project,
                location=location,
                http_options=http_options,
            )
        return _client_cache[cache_key]


def contains_url(text: str) -> bool:
    return bool(_URL_RE.search(text or ""))


def should_enable_google_search(text: str) -> bool:
    return bool(_SEARCH_HINT_RE.search(text or ""))


def get_core_model() -> str:
    """Resolve model for core discovery/chat stages."""
    return os.getenv("GEMINI_MODEL_CORE", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def get_evidence_model() -> str:
    """Resolve model for evidence validation stages."""
    default_model = os.getenv("GEMINI_MODEL_EVIDENCE", DEFAULT_EVIDENCE_MODEL)
    return default_model.strip() or DEFAULT_EVIDENCE_MODEL


def get_arrow_model() -> str:
    """Resolve model for arrow/effect validation stages."""
    default_model = os.getenv("GEMINI_MODEL_ARROW", DEFAULT_ARROW_MODEL)
    return default_model.strip() or DEFAULT_ARROW_MODEL


def get_fallback_model(role: str) -> Optional[str]:
    """Return fallback model for a role when primary model quota is exhausted."""
    _FALLBACK_ENV_KEYS = {
        "arrow": "GEMINI_MODEL_ARROW_FALLBACK",
        "evidence": "GEMINI_MODEL_EVIDENCE_FALLBACK",
    }
    env_key = _FALLBACK_ENV_KEYS.get(role)
    if env_key:
        val = (os.getenv(env_key) or "").strip()
        if val:
            return val
    # Default fallback: flash model (cheaper, higher quota)
    return DEFAULT_FLASH_MODEL


def get_model(role: str) -> str:
    """Resolve model name for a pipeline role via env override or registry default."""
    if role not in MODEL_REGISTRY:
        raise KeyError(f"Unknown model role '{role}'. Valid: {sorted(MODEL_REGISTRY)}")
    env_key = _MODEL_ENV_KEYS.get(role, "")
    override = (os.getenv(env_key, "") or "").strip() if env_key else ""
    return override or MODEL_REGISTRY[role]


def parse_request_mode(request_mode: Optional[str], *, default: str = DEFAULT_REQUEST_MODE) -> str:
    """Validate and normalize request mode."""
    mode = str(request_mode or "").strip().lower()
    if not mode:
        mode = str(default or "").strip().lower()
    if mode not in {"standard", "batch"}:
        raise ValueError(
            f"Invalid request mode '{request_mode}'. Expected one of: standard, batch."
        )
    return mode


def get_request_mode() -> str:
    """Resolve request mode from env with strict validation."""
    return parse_request_mode(os.getenv("GEMINI_REQUEST_MODE"), default=DEFAULT_REQUEST_MODE)


def _parse_positive_int(raw_value: Any, *, name: str, default: int) -> int:
    raw = str(raw_value or "").strip()
    if not raw:
        return int(default)
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got '{raw_value}'.") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be > 0, got {parsed}.")
    return parsed


def resolve_batch_poll_seconds(value: Optional[int] = None) -> int:
    """Resolve batch poll interval with explicit override > env > default."""
    if value is not None:
        return _parse_positive_int(value, name="batch_poll_seconds", default=DEFAULT_BATCH_POLL_SECONDS)
    return _parse_positive_int(
        os.getenv("GEMINI_BATCH_POLL_SECONDS"),
        name="GEMINI_BATCH_POLL_SECONDS",
        default=DEFAULT_BATCH_POLL_SECONDS,
    )


def resolve_batch_max_wait_seconds(value: Optional[int] = None) -> int:
    """Resolve max wait for batch jobs with explicit override > env > default."""
    if value is not None:
        return _parse_positive_int(value, name="batch_max_wait_seconds", default=DEFAULT_BATCH_MAX_WAIT_SECONDS)
    return _parse_positive_int(
        os.getenv("GEMINI_BATCH_MAX_WAIT_SECONDS"),
        name="GEMINI_BATCH_MAX_WAIT_SECONDS",
        default=DEFAULT_BATCH_MAX_WAIT_SECONDS,
    )


def get_prompt_token_bucket(
    prompt_tokens: int,
    *,
    threshold: int = GEMINI_3_PRO_PROMPT_BUCKET_THRESHOLD,
) -> str:
    """Return Gemini pricing bucket based on prompt tokens."""
    tokens = max(0, int(prompt_tokens or 0))
    return "<=200k" if tokens <= threshold else ">200k"


def get_gemini_3_pro_pricing(*, request_mode: str, prompt_tokens: int) -> Dict[str, Any]:
    """Return Gemini 3 Pro token pricing for the given mode and prompt bucket."""
    mode = parse_request_mode(request_mode)
    prompt_bucket = get_prompt_token_bucket(prompt_tokens)

    if mode == "batch":
        input_per_million = 1.0 if prompt_bucket == "<=200k" else 2.0
        output_per_million = 6.0 if prompt_bucket == "<=200k" else 9.0
    else:
        input_per_million = 2.0 if prompt_bucket == "<=200k" else 4.0
        output_per_million = 12.0 if prompt_bucket == "<=200k" else 18.0

    return {
        "request_mode": mode,
        "prompt_bucket": prompt_bucket,
        "input_per_million": input_per_million,
        "output_per_million": output_per_million,
    }


def _coerce_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def extract_usage_token_stats(response: Any) -> Dict[str, int]:
    """Extract normalized token stats from Gemini usage metadata."""
    stats = {
        "prompt_tokens": 0,
        "thinking_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return stats

    prompt_tokens = _coerce_int(getattr(usage, "prompt_token_count", 0))
    output_tokens = _coerce_int(getattr(usage, "candidates_token_count", 0))
    if output_tokens <= 0:
        output_tokens = _coerce_int(getattr(usage, "response_token_count", 0))

    thinking_tokens = _coerce_int(getattr(usage, "thoughts_token_count", 0))
    if thinking_tokens <= 0:
        # Backward compatibility: try extended_thinking_tokens before giving up.
        thinking_tokens = _coerce_int(getattr(usage, "extended_thinking_tokens", 0))

    total_tokens = _coerce_int(getattr(usage, "total_token_count", 0))
    if total_tokens > 0 and thinking_tokens <= 0:
        thinking_tokens = max(0, total_tokens - prompt_tokens - output_tokens)

    stats["prompt_tokens"] = prompt_tokens
    stats["thinking_tokens"] = thinking_tokens
    stats["output_tokens"] = output_tokens
    stats["total_tokens"] = total_tokens
    return stats


def is_quota_error(exc: Any) -> bool:
    """Detect generic quota/resource exhaustion errors from SDK exceptions."""
    text = str(exc or "")
    lowered = text.lower()
    return "429" in lowered or "resource_exhausted" in lowered or "quota exceeded" in lowered


def is_transient_network_error(exc: Any) -> bool:
    """Detect socket-level transient failures that deserve a retry.

    Distinct from ``is_quota_error``: covers TCP resets (macOS errno 54 /
    Linux ECONNRESET), broken pipes, connection aborts, and read-side
    timeouts. Before this helper existed, the arrow validator and the
    main chain-claim dispatcher gated their retry loops only on
    ``is_quota_error``, so a single ``[Errno 54] Connection reset by
    peer`` during arrow validation would silently drop that partner's
    validation — 12 such drops appeared in one ATXN3 run. Returning
    ``True`` here causes those callers to back off + retry instead.
    """
    import errno as _errno
    import socket as _socket
    if isinstance(exc, (
        ConnectionResetError,
        ConnectionAbortedError,
        ConnectionError,
        TimeoutError,
        _socket.timeout,
    )):
        return True
    if isinstance(exc, OSError):
        _en = getattr(exc, "errno", None)
        if _en in (
            _errno.ECONNRESET, _errno.EPIPE,
            _errno.ETIMEDOUT, _errno.ECONNABORTED,
        ):
            return True
    text = str(exc or "").lower()
    return any(token in text for token in (
        "connection reset", "connection aborted", "broken pipe",
        "timed out", "temporary failure", "errno 54",
    ))


def is_daily_model_quota_exhausted(exc: Any) -> bool:
    """Detect per-model/day quota exhaustion that should not be retried aggressively."""
    text = str(exc or "")
    return bool(_DAILY_QUOTA_RE.search(text))


# Patterns for extracting a server-suggested retry delay from exception
# metadata or free-text error messages. Used by ``extract_retry_after_seconds``
# below so every retry loop (main dispatcher + arrow validator + anywhere
# else) honours the same backoff semantics.
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)", re.IGNORECASE),
    re.compile(r"try\s+again\s+in\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)", re.IGNORECASE),
    re.compile(r'"?retry[_-]?delay"?\s*[:=]\s*"?(\d+(?:\.\d+)?)\s*s"?', re.IGNORECASE),
)


def extract_retry_after_seconds(exc: Any) -> Optional[float]:
    """Best-effort extraction of a server-provided retry delay.

    Checks, in order:
      1. HTTP ``Retry-After`` header on the exception's response object.
      2. A numeric ``retry_after`` attribute on the exception itself.
      3. Free-text patterns embedded in ``str(exc)``.

    Returns ``None`` when no explicit delay is available — callers should
    then apply their own exponential-backoff default.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers:
        try:
            value = headers.get("Retry-After") or headers.get("retry-after")
        except Exception:
            value = None
        if value:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                pass

    attr_value = getattr(exc, "retry_after", None)
    if attr_value is not None:
        try:
            return max(0.0, float(attr_value))
        except (TypeError, ValueError):
            pass

    text = str(exc or "")
    if not text:
        return None
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return max(0.0, float(match.group(1)))
            except (TypeError, ValueError):
                continue
    return None


def enforce_thinking_mode(
    *,
    thinking_level: Optional[str] = None,
) -> Optional[str]:
    """Return the effective thinking level, falling back to the default.

    Use ``"off"`` to explicitly disable thinking (returns None so that
    build_generate_content_config omits the thinking_config block entirely).

    Thinking levels for Gemini 3 (per Vertex AI docs at
    docs.cloud.google.com/vertex-ai/generative-ai/docs/thinking):
      • thinking_level="off" — 0 tokens for internal reasoning. Use for
        pure JSON-formatting calls where the model just stamps out a
        known shape (no reconciliation, no choice).
      • thinking_level="low" — minimal reasoning. Use for chain-claim
        generation, citation verification, dedup — narrow, constrained
        tasks with one fixed input pair.
      • thinking_level="medium" — balanced, suitable for function
        mapping and discovery. The default for most pipeline steps.
      • thinking_level="high" — extensive reasoning. Reserve for arrow
        determination and disagreement resolution where the model must
        reconcile multiple competing constraints.

    Note on budget interaction: ``thinking_token_count + output_token_count``
    is bounded by ``max_output_tokens`` (per Google python-genai issue
    #782 and Vertex error semantics). Whether thinking-token usage
    scales proportionally with the budget or is approximately flat per
    level is NOT documented authoritatively as of May 2026 — Vertex
    docs describe ``thinking_level`` qualitatively. Empirically, low
    on Flash 3 chain-claim work consumes single-digit-K thinking tokens.
    Real Flash 3 output cap is 65,536 (NOT the 8,192 some older comments
    in this codebase still claim). When sizing a step's ``max_output_tokens``,
    target: (expected output tokens) + (~3-6K headroom for thinking at
    the requested level).
    """
    if thinking_level is None:
        return DEFAULT_THINKING_LEVEL
    if thinking_level.lower() == "off":
        return None
    return thinking_level


def build_generate_tools(
    *,
    use_google_search: bool = False,
    use_url_context: bool = False,
    use_code_execution: bool = False,
) -> list[types.Tool]:
    tools: list[types.Tool] = []
    if use_google_search:
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    if use_url_context:
        tools.append(types.Tool(url_context=types.UrlContext()))
    if use_code_execution:
        tools.append(types.Tool(code_execution=types.CodeExecution()))
    return tools


def build_interaction_tools(
    *,
    use_google_search: bool = False,
    use_url_context: bool = False,
) -> list[dict[str, str]]:
    tools: list[dict[str, str]] = []
    if use_google_search:
        tools.append({"type": "google_search"})
    if use_url_context:
        tools.append({"type": "url_context"})
    return tools


def build_generate_content_config(
    *,
    thinking_level: Optional[str] = DEFAULT_THINKING_LEVEL,
    max_output_tokens: Optional[int] = 65000,
    system_instruction: Optional[str] = None,
    temperature: Optional[float] = None,
    response_mime_type: Optional[str] = None,
    response_json_schema: Optional[Dict[str, Any]] = None,
    use_google_search: bool = False,
    use_url_context: bool = False,
    use_code_execution: bool = False,
    include_thoughts: bool = False,
    automatic_function_calling: Optional[types.AutomaticFunctionCallingConfig] = None,
    cached_content: Optional[str] = None,
) -> types.GenerateContentConfig:
    """Build GenerateContentConfig for models.generate_content."""
    effective_thinking_level = enforce_thinking_mode(
        thinking_level=thinking_level,
    )

    config_dict: dict[str, Any] = {
        "max_output_tokens": max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
    }

    if effective_thinking_level:
        config_dict["thinking_config"] = types.ThinkingConfig(
            thinking_level=effective_thinking_level,
            include_thoughts=include_thoughts,
        )

    if system_instruction:
        config_dict["system_instruction"] = system_instruction

    if temperature is not None:
        config_dict["temperature"] = temperature

    if response_mime_type:
        config_dict["response_mime_type"] = response_mime_type

    if response_json_schema is not None:
        config_dict["response_json_schema"] = response_json_schema

    if automatic_function_calling is not None:
        config_dict["automatic_function_calling"] = automatic_function_calling

    tools = build_generate_tools(
        use_google_search=use_google_search,
        use_url_context=use_url_context,
        use_code_execution=use_code_execution,
    )
    if tools:
        config_dict["tools"] = tools

    if cached_content:
        config_dict["cached_content"] = cached_content

    return types.GenerateContentConfig(**config_dict)


def build_interaction_generation_config(
    *,
    thinking_level: str = DEFAULT_THINKING_LEVEL,
    thinking_summaries: str = DEFAULT_THINKING_SUMMARIES,
    max_output_tokens: Optional[int] = 65000,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """Build generation_config payload for interactions.create."""
    effective_thinking_level = enforce_thinking_mode(
        thinking_level=thinking_level,
    )

    cfg: Dict[str, Any] = {
        "max_output_tokens": max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
    }
    if effective_thinking_level:
        cfg["thinking_level"] = effective_thinking_level
        cfg["thinking_summaries"] = thinking_summaries
    if temperature is not None:
        cfg["temperature"] = temperature
    return cfg


def extract_text_from_generate_response(response: Any) -> str:
    if hasattr(response, "text") and response.text:
        return str(response.text)
    if hasattr(response, "candidates") and response.candidates:
        text_chunks: list[str] = []
        for candidate in response.candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                chunk = getattr(part, "text", None)
                if chunk:
                    text_chunks.append(str(chunk))
        if text_chunks:
            return "".join(text_chunks)
    return ""


def describe_empty_response(response: Any) -> str:
    """Return a human-readable reason when ``generate_content`` returned
    no text.

    Inspects ``response.candidates[0]`` for ``finish_reason`` and
    ``safety_ratings`` and formats them so ``"No text in response"``
    errors surface a diagnosable cause (MAX_TOKENS, SAFETY, RECITATION,
    OTHER, …) instead of being a blind failure.
    """
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            prompt_feedback = getattr(response, "prompt_feedback", None)
            if prompt_feedback is not None:
                block_reason = getattr(prompt_feedback, "block_reason", None)
                if block_reason:
                    return f"prompt_blocked={block_reason!s}"
            return "no candidates"
        candidate = candidates[0]
        finish = getattr(candidate, "finish_reason", None)
        finish_str = str(finish) if finish is not None else "unknown"
        parts: list[str] = [f"finish_reason={finish_str}"]
        safety = getattr(candidate, "safety_ratings", None) or []
        blocked_categories = []
        for rating in safety:
            if getattr(rating, "blocked", False):
                category = getattr(rating, "category", None)
                if category is not None:
                    blocked_categories.append(str(category))
        if blocked_categories:
            parts.append("safety_blocked=[" + ", ".join(blocked_categories) + "]")
        finish_message = getattr(candidate, "finish_message", None)
        if finish_message:
            parts.append(f"finish_message={finish_message!s}")
        return "; ".join(parts)
    except Exception as exc:  # never let diagnostics break the real error
        return f"diagnostic_failed:{type(exc).__name__}"


def extract_text_from_interaction(interaction: Any) -> str:
    outputs = getattr(interaction, "outputs", []) or []
    for output in outputs:
        if getattr(output, "type", None) == "text":
            return str(getattr(output, "text", "") or "")
    if hasattr(interaction, "output_text") and interaction.output_text:
        return str(interaction.output_text)
    return ""


def minimal_json_object_schema() -> Dict[str, Any]:
    """Small permissive schema that forces object JSON mode."""
    return {"type": "object"}


# ---------------------------------------------------------------------------
# Explicit caching (system prompt dedup via content hash)
# ---------------------------------------------------------------------------
_cache_registry: Dict[str, str] = {}
_cache_lock = Lock()


def _system_cache_key(model: str, system_text: str) -> str:
    """Deterministic short hash for cache dedup."""
    return hashlib.sha256(f"{model}:{system_text}".encode("utf-8")).hexdigest()[:16]


def create_or_get_system_cache(
    *,
    system_text: str,
    model: Optional[str] = None,
    ttl_seconds: int = 7200,
    display_name: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Create or retrieve an explicit Gemini cache for a system prompt.

    Returns the cache name suitable for GenerateContentConfig(cached_content=...).
    """
    effective_model = model or get_model("core")
    content_hash = _system_cache_key(effective_model, system_text)

    with _cache_lock:
        if content_hash in _cache_registry:
            return _cache_registry[content_hash]

    client = get_client(api_key)
    # Gemini caching API requires at least one Content object in contents.
    # Use the system instruction text as a seed content entry.
    seed_content = types.Content(
        role="user",
        parts=[types.Part(text="Initialize context.")],
    )
    cache = client.caches.create(
        model=effective_model,
        config=types.CreateCachedContentConfig(
            display_name=display_name or f"sys-cache-{content_hash}",
            system_instruction=system_text,
            contents=[seed_content],
            ttl=f"{ttl_seconds}s",
        ),
    )
    cache_name = cache.name

    with _cache_lock:
        _cache_registry[content_hash] = cache_name

    return cache_name


# ---------------------------------------------------------------------------
# Interactions API helpers
# ---------------------------------------------------------------------------


def call_interaction(
    *,
    input_text: str,
    model: Optional[str] = None,
    system_instruction: Optional[str] = None,
    generation_config: Optional[Dict[str, Any]] = None,
    tools: Optional[list] = None,
    response_format: Optional[Dict[str, Any]] = None,
    store: bool = True,
    previous_interaction_id: Optional[str] = None,
    api_key: Optional[str] = None,
    max_retries: int = 3,
    base_delay: float = 1.5,
) -> Any:
    """Call the Gemini Interactions API with retry logic."""
    client = get_client(api_key)
    effective_model = model or get_model("core")

    for attempt in range(1, max_retries + 1):
        try:
            kwargs: Dict[str, Any] = {
                "model": effective_model,
                "input": input_text,
                "store": store,
            }
            if system_instruction:
                kwargs["system_instruction"] = system_instruction
            if generation_config:
                kwargs["generation_config"] = generation_config
            if tools:
                kwargs["tools"] = tools
            if response_format:
                kwargs["response_format"] = response_format
            if previous_interaction_id:
                kwargs["previous_interaction_id"] = previous_interaction_id

            return client.interactions.create(**kwargs)

        except Exception as exc:
            error_lower = str(exc).lower()
            if "invalid_argument" in error_lower or "invalid_request" in error_lower:
                raise RuntimeError(f"Non-retryable interaction error: {exc}") from exc
            if "copyright" in error_lower or "recitation" in error_lower:
                raise RuntimeError(f"Copyright/recitation block (non-retryable): {exc}") from exc
            if attempt == max_retries:
                raise RuntimeError(
                    f"Interaction call failed after {max_retries} attempts: {exc}"
                ) from exc
            _time_mod.sleep(base_delay * (attempt ** 1.5))

    raise RuntimeError("Unexpected error in call_interaction")  # pragma: no cover


def call_deep_research(
    *,
    input_text: str,
    agent: str = "deep-research-pro-preview-12-2025",
    tools: Optional[list] = None,
    poll_interval_seconds: float = 10.0,
    max_wait_seconds: float = 600.0,
    api_key: Optional[str] = None,
    cancel_event: Optional[Any] = None,
) -> Any:
    """Start a deep research task and poll until completion."""
    client = get_client(api_key)

    kwargs: Dict[str, Any] = {
        "input": input_text,
        "agent": agent,
        "background": True,
    }
    if tools:
        kwargs["tools"] = tools

    interaction = client.interactions.create(**kwargs)
    started_at = _time_mod.time()

    while True:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Deep research cancelled by user")

        interaction = client.interactions.get(interaction.id)

        if interaction.status == "completed":
            return interaction
        if interaction.status in ("failed", "cancelled"):
            error = getattr(interaction, "error", "unknown error")
            raise RuntimeError(f"Deep research failed: {error}")

        if (_time_mod.time() - started_at) > max_wait_seconds:
            raise RuntimeError(
                f"Deep research timed out after {max_wait_seconds}s"
            )

        _time_mod.sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Batch API helper
# ---------------------------------------------------------------------------


def submit_batch_job(
    *,
    model: str,
    contents: Any,
    config: types.GenerateContentConfig,
    display_name: Optional[str] = None,
    poll_seconds: Optional[int] = None,
    max_wait_seconds: Optional[int] = None,
    cancel_event: Optional[Any] = None,
    api_key: Optional[str] = None,
) -> tuple:
    """Submit an inline batch job and poll until completion.

    Returns (output_text, token_stats_dict).
    """
    client = get_client(api_key)
    effective_poll = resolve_batch_poll_seconds(poll_seconds)
    effective_max_wait = resolve_batch_max_wait_seconds(max_wait_seconds)

    job_display_name = display_name or f"batch-{int(_time_mod.time())}"
    inline_request = types.InlinedRequest(contents=contents, config=config)

    batch_job = client.batches.create(
        model=model,
        src=[inline_request],
        config=types.CreateBatchJobConfig(display_name=job_display_name),
    )
    batch_name = getattr(batch_job, "name", None)
    if not batch_name:
        raise RuntimeError("Batch create succeeded but no job name returned.")

    terminal_failures = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
    poll_start = _time_mod.time()

    while True:
        if cancel_event and cancel_event.is_set():
            try:
                client.batches.cancel(name=batch_name)
            except Exception:
                pass
            raise RuntimeError("Batch job cancelled by user")

        current = client.batches.get(name=batch_name)
        state = getattr(current, "state", None)
        state_name = str(getattr(state, "name", None) or state or "UNKNOWN")

        if state_name in {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}:
            dest = getattr(current, "dest", None)
            responses = getattr(dest, "inlined_responses", None) or []
            if not responses:
                raise RuntimeError(
                    f"Batch {batch_name}: {state_name} but no inline responses."
                )
            first = responses[0]
            if getattr(first, "error", None):
                raise RuntimeError(f"Batch {batch_name} inline error: {first.error}")
            resp_obj = getattr(first, "response", None)
            if resp_obj is None:
                raise RuntimeError(f"Batch {batch_name}: empty response object.")
            text = extract_text_from_generate_response(resp_obj)
            if not text:
                raise RuntimeError(f"Batch {batch_name}: no text in response.")
            return text, extract_usage_token_stats(resp_obj)

        if state_name in terminal_failures:
            err = getattr(current, "error", None)
            raise RuntimeError(f"Batch {batch_name} ended {state_name}: {err}")

        if (_time_mod.time() - poll_start) > effective_max_wait:
            try:
                client.batches.cancel(name=batch_name)
            except Exception:
                pass
            raise RuntimeError(
                f"Batch {batch_name} exceeded max wait of {effective_max_wait}s."
            )

        _time_mod.sleep(float(effective_poll))


def submit_multi_batch_job(
    *,
    model: str,
    requests: list,
    display_name: Optional[str] = None,
    poll_seconds: Optional[int] = None,
    max_wait_seconds: Optional[int] = None,
    cancel_event: Optional[Any] = None,
    api_key: Optional[str] = None,
) -> list:
    """Submit a batch job with multiple inline requests and poll until completion.

    Each element of *requests* must be a ``types.InlinedRequest``.
    Returns a list of ``(output_text, token_stats_dict, error_message)`` tuples
    aligned by index with the input *requests* list.
    """
    if not requests:
        return []

    client = get_client(api_key)
    effective_poll = resolve_batch_poll_seconds(poll_seconds)
    effective_max_wait = resolve_batch_max_wait_seconds(max_wait_seconds)

    job_display_name = display_name or f"multi-batch-{int(_time_mod.time())}"

    batch_job = client.batches.create(
        model=model,
        src=requests,
        config=types.CreateBatchJobConfig(display_name=job_display_name),
    )
    batch_name = getattr(batch_job, "name", None)
    if not batch_name:
        raise RuntimeError("Batch create succeeded but no job name returned.")

    terminal_failures = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
    poll_start = _time_mod.time()

    while True:
        if cancel_event and cancel_event.is_set():
            try:
                client.batches.cancel(name=batch_name)
            except Exception:
                pass
            raise RuntimeError("Batch job cancelled by user")

        current = client.batches.get(name=batch_name)
        state = getattr(current, "state", None)
        state_name = str(getattr(state, "name", None) or state or "UNKNOWN")

        if state_name in {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}:
            dest = getattr(current, "dest", None)
            responses = getattr(dest, "inlined_responses", None) or []
            results: list = []
            for idx in range(len(requests)):
                if idx < len(responses):
                    entry = responses[idx]
                    err = getattr(entry, "error", None)
                    if err:
                        results.append((None, None, str(err)))
                        continue
                    resp_obj = getattr(entry, "response", None)
                    if resp_obj is None:
                        results.append((None, None, "Empty response object"))
                        continue
                    text = extract_text_from_generate_response(resp_obj)
                    stats = extract_usage_token_stats(resp_obj)
                    results.append((text, stats, None))
                else:
                    results.append((None, None, "No response at this index"))
            return results

        if state_name in terminal_failures:
            err = getattr(current, "error", None)
            raise RuntimeError(f"Batch {batch_name} ended {state_name}: {err}")

        if (_time_mod.time() - poll_start) > effective_max_wait:
            try:
                client.batches.cancel(name=batch_name)
            except Exception:
                pass
            raise RuntimeError(
                f"Batch {batch_name} exceeded max wait of {effective_max_wait}s."
            )

        _time_mod.sleep(float(effective_poll))
