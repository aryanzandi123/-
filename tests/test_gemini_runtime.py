#!/usr/bin/env python3
"""Tests for shared Gemini runtime helpers."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import hashlib

from utils.gemini_runtime import (
    MODEL_REGISTRY,
    build_generate_content_config,
    build_interaction_generation_config,
    build_generate_tools,
    build_interaction_tools,
    contains_url,
    enforce_thinking_mode,
    extract_usage_token_stats,
    validate_vertex_config,
    get_gemini_3_pro_pricing,
    get_model,
    get_request_mode,
    get_arrow_model,
    get_core_model,
    get_evidence_model,
    is_daily_model_quota_exhausted,
    is_quota_error,
    _system_cache_key,
    parse_request_mode,
    resolve_batch_max_wait_seconds,
    resolve_batch_poll_seconds,
    should_enable_google_search,
)


def test_generate_content_config_uses_nested_thinking_level():
    cfg = build_generate_content_config(thinking_level="high", max_output_tokens=1234)
    dumped = cfg.model_dump(exclude_none=True)

    assert dumped["max_output_tokens"] == 1234
    assert dumped["thinking_config"]["thinking_level"] == "HIGH"
    assert "thinking_level" not in dumped


def test_interaction_generation_config_uses_top_level_thinking_fields():
    cfg = build_interaction_generation_config(
        thinking_level="high",
        thinking_summaries="auto",
        max_output_tokens=2048,
    )

    assert cfg["thinking_level"] == "high"
    assert cfg["thinking_summaries"] == "auto"
    assert cfg["max_output_tokens"] == 2048
    assert "thinking_config" not in cfg


def test_tool_builders_are_opt_in():
    generate_tools = build_generate_tools(use_google_search=True, use_url_context=True)
    interaction_tools = build_interaction_tools(use_google_search=True, use_url_context=True)

    assert len(generate_tools) == 2
    assert interaction_tools == [{"type": "google_search"}, {"type": "url_context"}]


def test_url_and_search_heuristics():
    assert contains_url("Summarize https://example.com")
    assert not contains_url("No url here")
    assert should_enable_google_search("Who won the latest super bowl today?")
    assert not should_enable_google_search("Explain this cached graph state")


def test_enforce_thinking_mode_defaults():
    assert enforce_thinking_mode() == "high"
    assert enforce_thinking_mode(thinking_level="medium") == "medium"
    assert enforce_thinking_mode(thinking_level="off") is None
    assert enforce_thinking_mode(thinking_level="OFF") is None
    assert enforce_thinking_mode(thinking_level="low") == "low"


def test_citation_delta_schema_structure():
    from pipeline.types import CITATION_DELTA_SCHEMA

    assert CITATION_DELTA_SCHEMA["required"] == ["ctx_json"]
    ctx = CITATION_DELTA_SCHEMA["properties"]["ctx_json"]
    assert "interactors" in ctx["properties"]

    interactor = ctx["properties"]["interactors"]["items"]
    assert interactor["required"] == ["primary", "functions"]

    fn = interactor["properties"]["functions"]["items"]
    # Must include merge-key fields + evidence/pmids
    assert "function" in fn["properties"]
    assert "cellular_process" in fn["properties"]
    assert "interaction_direction" in fn["properties"]
    assert "evidence" in fn["properties"]
    assert "pmids" in fn["properties"]
    # Must NOT include heavy fields
    assert "effect_description" not in fn["properties"]
    assert "biological_consequence" not in fn["properties"]
    assert "specific_effects" not in fn["properties"]


def test_model_resolvers_use_defaults_and_env(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL_CORE", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_EVIDENCE", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_ARROW", raising=False)
    assert get_core_model() == "gemini-3-flash-preview"
    assert get_evidence_model() == "gemini-3-flash-preview"
    assert get_arrow_model() == "gemini-3-flash-preview"

    monkeypatch.setenv("GEMINI_MODEL_CORE", "gemini-3.1-pro-preview")
    monkeypatch.setenv("GEMINI_MODEL_EVIDENCE", "gemini-3.1-pro-preview")
    monkeypatch.setenv("GEMINI_MODEL_ARROW", "gemini-3.1-pro-preview")
    assert get_core_model() == "gemini-3.1-pro-preview"
    assert get_evidence_model() == "gemini-3.1-pro-preview"
    assert get_arrow_model() == "gemini-3.1-pro-preview"


def test_quota_error_helpers():
    daily_quota_msg = (
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_requests_per_model_per_day, limit: 0"
    )
    transient_quota_msg = "429 RESOURCE_EXHAUSTED"
    invalid_arg_msg = "400 INVALID_ARGUMENT"

    assert is_quota_error(daily_quota_msg)
    assert is_quota_error(transient_quota_msg)
    assert not is_quota_error(invalid_arg_msg)

    assert is_daily_model_quota_exhausted(daily_quota_msg)
    assert not is_daily_model_quota_exhausted(transient_quota_msg)


def test_request_mode_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("GEMINI_REQUEST_MODE", raising=False)
    assert get_request_mode() == "standard"

    monkeypatch.setenv("GEMINI_REQUEST_MODE", "batch")
    assert get_request_mode() == "batch"
    assert parse_request_mode("STANDARD") == "standard"

    monkeypatch.setenv("GEMINI_REQUEST_MODE", "invalid-mode")
    try:
        get_request_mode()
        assert False, "Expected ValueError for invalid GEMINI_REQUEST_MODE"
    except ValueError as exc:
        assert "Invalid request mode" in str(exc)


def test_batch_wait_env_parsing(monkeypatch):
    monkeypatch.delenv("GEMINI_BATCH_POLL_SECONDS", raising=False)
    monkeypatch.delenv("GEMINI_BATCH_MAX_WAIT_SECONDS", raising=False)
    assert resolve_batch_poll_seconds() == 15
    assert resolve_batch_max_wait_seconds() == 86400

    monkeypatch.setenv("GEMINI_BATCH_POLL_SECONDS", "7")
    monkeypatch.setenv("GEMINI_BATCH_MAX_WAIT_SECONDS", "123")
    assert resolve_batch_poll_seconds() == 7
    assert resolve_batch_max_wait_seconds() == 123

    assert resolve_batch_poll_seconds(9) == 9
    assert resolve_batch_max_wait_seconds(456) == 456

    monkeypatch.setenv("GEMINI_BATCH_POLL_SECONDS", "0")
    try:
        resolve_batch_poll_seconds()
        assert False, "Expected ValueError for non-positive poll seconds"
    except ValueError as exc:
        assert "must be > 0" in str(exc)


def test_gemini_3_pro_pricing_buckets():
    standard_low = get_gemini_3_pro_pricing(request_mode="standard", prompt_tokens=1000)
    standard_high = get_gemini_3_pro_pricing(request_mode="standard", prompt_tokens=300000)
    batch_low = get_gemini_3_pro_pricing(request_mode="batch", prompt_tokens=1000)
    batch_high = get_gemini_3_pro_pricing(request_mode="batch", prompt_tokens=300000)

    assert standard_low["prompt_bucket"] == "<=200k"
    assert standard_low["input_per_million"] == 2.0
    assert standard_low["output_per_million"] == 12.0

    assert standard_high["prompt_bucket"] == ">200k"
    assert standard_high["input_per_million"] == 4.0
    assert standard_high["output_per_million"] == 18.0

    assert batch_low["input_per_million"] == 1.0
    assert batch_low["output_per_million"] == 6.0
    assert batch_high["input_per_million"] == 2.0
    assert batch_high["output_per_million"] == 9.0


def test_extract_usage_token_stats_prefers_thoughts_count():
    class _Usage:
        prompt_token_count = 20
        candidates_token_count = 10
        thoughts_token_count = 7
        cached_content_token_count = 999  # Should not win over thoughts_token_count.
        total_token_count = 37

    class _Resp:
        usage_metadata = _Usage()

    stats = extract_usage_token_stats(_Resp())
    assert stats["prompt_tokens"] == 20
    assert stats["output_tokens"] == 10
    assert stats["thinking_tokens"] == 7
    assert stats["total_tokens"] == 37


# ---------------------------------------------------------------------------
# validate_vertex_config tests
# ---------------------------------------------------------------------------


def test_validate_vertex_config_returns_project_and_location(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    assert validate_vertex_config() == ("my-project", "us-central1")


def test_validate_vertex_config_strips_whitespace(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "  my-project  ")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "  us-east1  ")
    assert validate_vertex_config() == ("my-project", "us-east1")


def test_validate_vertex_config_raises_when_project_missing(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    try:
        validate_vertex_config()
        assert False, "Expected RuntimeError"
    except RuntimeError as exc:
        assert "GOOGLE_CLOUD_PROJECT" in str(exc)


def test_validate_vertex_config_raises_when_location_missing(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    try:
        validate_vertex_config()
        assert False, "Expected RuntimeError"
    except RuntimeError as exc:
        assert "GOOGLE_CLOUD_LOCATION" in str(exc)


# ---------------------------------------------------------------------------
# get_client tests
# ---------------------------------------------------------------------------


def test_get_client_caches_by_vertex_config(monkeypatch):
    """get_client returns same object for same project:location, creates Vertex AI client."""
    import utils.gemini_runtime as rt

    _init_calls = []

    class _FakeClient:
        def __init__(self, **kwargs):
            _init_calls.append(kwargs)

    # Patch the google.genai module inside gemini_runtime's deferred import
    import types as stdlib_types
    fake_genai = stdlib_types.ModuleType("google.genai")
    fake_genai.Client = _FakeClient
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google", stdlib_types.ModuleType("google"))
    sys.modules["google"].genai = fake_genai

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    # Clear cache between tests
    rt._client_cache.clear()

    c1 = rt.get_client()
    c2 = rt.get_client()
    assert c1 is c2
    assert len(_init_calls) == 1
    assert _init_calls[0]["vertexai"] is True
    assert _init_calls[0]["project"] == "test-project"
    assert _init_calls[0]["location"] == "us-central1"

    rt._client_cache.clear()


def test_get_client_applies_http_pool_timeout_and_retry(monkeypatch):
    """Vertex client gets explicit HTTP options for high-concurrency runs."""
    import utils.gemini_runtime as rt

    _init_calls = []

    class _FakeClient:
        def __init__(self, **kwargs):
            _init_calls.append(kwargs)

    import types as stdlib_types
    fake_genai = stdlib_types.ModuleType("google.genai")
    fake_genai.Client = _FakeClient
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google", stdlib_types.ModuleType("google"))
    sys.modules["google"].genai = fake_genai

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_HTTP_POOL_MAX_CONNECTIONS", "12")
    monkeypatch.setenv("GEMINI_HTTP_POOL_MAX_KEEPALIVE", "6")

    rt._client_cache.clear()
    rt.get_client(timeout_ms=12345, retry_attempts=1)

    http_options = _init_calls[0]["http_options"]
    dumped = http_options.model_dump(exclude_none=True)
    assert dumped["timeout"] == 12345
    assert dumped["retry_options"]["attempts"] == 1
    assert "client_args" in dumped

    rt._client_cache.clear()


# ---------------------------------------------------------------------------
# Model registry tests
# ---------------------------------------------------------------------------


def test_model_registry_has_all_roles():
    assert "core" in MODEL_REGISTRY
    assert "evidence" in MODEL_REGISTRY
    assert "arrow" in MODEL_REGISTRY
    assert "flash" in MODEL_REGISTRY
    assert "deep_research" in MODEL_REGISTRY


def test_get_model_returns_defaults(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL_CORE", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_EVIDENCE", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_FLASH", raising=False)
    assert get_model("core") == "gemini-3-flash-preview"
    assert get_model("evidence") == "gemini-3-flash-preview"
    assert get_model("flash") == "gemini-3-flash-preview"


def test_get_model_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_CORE", "custom-model-x")
    assert get_model("core") == "custom-model-x"


def test_get_model_unknown_role_raises():
    try:
        get_model("nonexistent")
        assert False, "Expected KeyError"
    except KeyError as exc:
        assert "nonexistent" in str(exc)


def test_get_model_matches_legacy_helpers(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL_CORE", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_EVIDENCE", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_ARROW", raising=False)
    assert get_model("core") == get_core_model()
    assert get_model("evidence") == get_evidence_model()
    assert get_model("arrow") == get_arrow_model()


# ---------------------------------------------------------------------------
# Cache key generation tests
# ---------------------------------------------------------------------------


def test_cache_key_deterministic():
    """Same inputs produce same hash; different inputs produce different hash."""
    h1 = _system_cache_key("gemini-3.1-pro-preview", "system prompt A")
    h2 = _system_cache_key("gemini-3.1-pro-preview", "system prompt A")
    h3 = _system_cache_key("gemini-3.1-pro-preview", "system prompt B")
    h4 = _system_cache_key("gemini-2.5-pro", "system prompt A")

    assert h1 == h2
    assert h1 != h3
    assert h1 != h4
    assert len(h1) == 16


# ---------------------------------------------------------------------------
# build_generate_content_config cached_content tests
# ---------------------------------------------------------------------------


def test_build_config_cached_content_param():
    cfg = build_generate_content_config(
        thinking_level="high",
        max_output_tokens=1000,
        cached_content="caches/abc123",
    )
    dumped = cfg.model_dump(exclude_none=True)
    assert dumped["cached_content"] == "caches/abc123"


def test_build_config_cached_content_default_none():
    cfg = build_generate_content_config(thinking_level="high", max_output_tokens=1000)
    dumped = cfg.model_dump(exclude_none=True)
    assert "cached_content" not in dumped


# ---------------------------------------------------------------------------
# extract_text_from_generate_response tests
# ---------------------------------------------------------------------------

from utils.gemini_runtime import (
    extract_text_from_generate_response,
    extract_text_from_interaction,
    get_prompt_token_bucket,
)


def test_extract_text_from_generate_response_with_text_attr():
    """Mock _Resp with .text='hello', verify returns 'hello'."""
    class _Resp:
        text = "hello"
    assert extract_text_from_generate_response(_Resp()) == "hello"


def test_extract_text_from_generate_response_with_candidates_fallback():
    """Mock with text=None, candidates with parts."""
    class _Part:
        text = "from-candidate"

    class _Content:
        parts = [_Part()]

    class _Candidate:
        content = _Content()

    class _Resp:
        text = None
        candidates = [_Candidate()]

    assert extract_text_from_generate_response(_Resp()) == "from-candidate"


def test_extract_text_from_generate_response_empty():
    """Mock with text=None, candidates=[], verify returns ''."""
    class _Resp:
        text = None
        candidates = []
    assert extract_text_from_generate_response(_Resp()) == ""


# ---------------------------------------------------------------------------
# extract_text_from_interaction tests
# ---------------------------------------------------------------------------


def test_extract_text_from_interaction_with_outputs():
    """Mock interaction with outputs containing text type."""
    class _Output:
        type = "text"
        text = "output-text"

    class _Interaction:
        outputs = [_Output()]
        output_text = None

    assert extract_text_from_interaction(_Interaction()) == "output-text"


def test_extract_text_from_interaction_with_output_text_fallback():
    """Mock with outputs=[], output_text='fallback'."""
    class _Interaction:
        outputs = []
        output_text = "fallback"

    assert extract_text_from_interaction(_Interaction()) == "fallback"


def test_extract_text_from_interaction_empty():
    """Mock with no outputs or output_text."""
    class _Interaction:
        outputs = []
        output_text = None

    assert extract_text_from_interaction(_Interaction()) == ""


# ---------------------------------------------------------------------------
# extract_usage_token_stats edge cases
# ---------------------------------------------------------------------------


def test_extract_usage_token_stats_no_usage_metadata():
    """Mock _Resp with usage_metadata=None, verify zeros."""
    class _Resp:
        usage_metadata = None

    stats = extract_usage_token_stats(_Resp())
    assert stats["prompt_tokens"] == 0
    assert stats["thinking_tokens"] == 0
    assert stats["output_tokens"] == 0
    assert stats["total_tokens"] == 0


def test_extract_usage_token_stats_uses_response_fallback():
    """Mock with candidates_token_count=0, response_token_count=42."""
    class _Usage:
        prompt_token_count = 10
        candidates_token_count = 0
        response_token_count = 42
        thoughts_token_count = 0
        cached_content_token_count = 0
        total_token_count = 52

    class _Resp:
        usage_metadata = _Usage()

    stats = extract_usage_token_stats(_Resp())
    assert stats["output_tokens"] == 42
    assert stats["prompt_tokens"] == 10


# ---------------------------------------------------------------------------
# get_prompt_token_bucket boundary tests
# ---------------------------------------------------------------------------


def test_get_prompt_token_bucket_boundary():
    """Test 200000 returns '<=200k', 200001 returns '>200k'."""
    assert get_prompt_token_bucket(200000) == "<=200k"
    assert get_prompt_token_bucket(200001) == ">200k"


# ---------------------------------------------------------------------------
# contains_url edge cases
# ---------------------------------------------------------------------------


def test_contains_url_edge_cases():
    """Test ftp://, empty, None."""
    assert not contains_url("ftp://example.com")  # regex only matches http(s)
    assert not contains_url("")
    assert not contains_url(None)


# ---------------------------------------------------------------------------
# should_enable_google_search hint tests
# ---------------------------------------------------------------------------


def test_should_enable_google_search_hints():
    """Test 'LATEST news' returns True, 'no hints' returns False."""
    assert should_enable_google_search("LATEST news") is True
    assert should_enable_google_search("no hints") is False


# ---------------------------------------------------------------------------
# call_interaction response_format passthrough tests
# ---------------------------------------------------------------------------

from utils.gemini_runtime import call_interaction


def test_call_interaction_passes_response_format(monkeypatch):
    """When response_format is provided, it must be included in kwargs to interactions.create()."""
    captured_kwargs = {}

    class FakeInteractions:
        def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            # Return a minimal mock interaction
            class _Interaction:
                outputs = []
                id = "test-id"
            return _Interaction()

    class FakeClient:
        interactions = FakeInteractions()

    monkeypatch.setattr("utils.gemini_runtime.get_client", lambda api_key=None: FakeClient())

    schema = {"type": "object", "properties": {"ctx_json": {"type": "object"}}}
    call_interaction(
        input_text="test prompt",
        model="gemini-3.1-pro-preview",
        response_format=schema,
        api_key="fake-key",
    )

    assert "response_format" in captured_kwargs
    assert captured_kwargs["response_format"] == schema


def test_call_interaction_omits_response_format_when_none(monkeypatch):
    """When response_format is None, it must NOT appear in kwargs."""
    captured_kwargs = {}

    class FakeInteractions:
        def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            class _Interaction:
                outputs = []
                id = "test-id"
            return _Interaction()

    class FakeClient:
        interactions = FakeInteractions()

    monkeypatch.setattr("utils.gemini_runtime.get_client", lambda api_key=None: FakeClient())

    call_interaction(
        input_text="test prompt",
        model="gemini-3.1-pro-preview",
        api_key="fake-key",
    )

    assert "response_format" not in captured_kwargs
