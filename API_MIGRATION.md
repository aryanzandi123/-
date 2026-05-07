# API Migration Guide: google-generativeai to google-genai

This document covers the migration from the legacy `google-generativeai` SDK
to the Gemini 3 `google-genai` SDK used throughout ProPaths.

---

## Overview

ProPaths originally used the `google-generativeai` package (Gemini 2 era).
The codebase now targets **Gemini 3** exclusively via the `google-genai` SDK.

| Attribute | Before | After |
|-----------|--------|-------|
| Package | `google-generativeai` | `google-genai>=1.62.0,<2` |
| Import | `import google.generativeai as genai` | `from google import genai` |
| Client | Implicit module-level config | Explicit `genai.Client(api_key=...)` |
| Runtime | Scattered per-file setup | Centralized in `utils/gemini_runtime.py` |

---

## SDK Change Details

### Dependency Policy

```
# requirements.txt
google-genai>=1.62.0,<2

# Removed
google-generativeai      # legacy SDK — do not install
```

### Client Initialization

The old SDK used module-level configuration. The new SDK uses an explicit,
thread-safe singleton client managed by `utils/gemini_runtime.py`:
SO 
```python
from utils.gemini_runtime import get_client

client = get_client()            # cached by API key
client = get_client(api_key=k)   # override key
```

---

## API Surface Changes

ProPaths uses two Gemini 3 API surfaces:

### 1. `client.models.generate_content` (Pipeline / Batch)

Used by the research pipeline, evidence validation, and all non-chat LLM calls.

```python
response = client.models.generate_content(
    model="gemini-3.1-pro-preview",
    contents=prompt,
    config={
        "thinking_config": {"thinking_level": "high"},
        "max_output_tokens": 60000,
    },
)
```

- Thinking config is **nested** inside `config.thinking_config`.
- Do **not** pass `thinking_level` as a top-level key.

### 2. `client.interactions.create` (Chat)

Used by the `/api/chat` endpoint for stateful multi-turn conversations.

```python
response = client.interactions.create(
    model="gemini-3.1-pro-preview",
    config={
        "thinking_level": "high",
        "thinking_summaries": "auto",
        "max_output_tokens": 60000,
    },
    tools=[{"type": "google_search"}, {"type": "url_context"}],
)
```

- Thinking config keys are **top-level** in `config`.
- Tools use `{"type": "tool_name"}` format.

### Chat Continuation

The Interactions API supports multi-turn via interaction IDs:

- `/api/chat` accepts an optional `previous_interaction_id` in the request body.
- `/api/chat` returns `interaction_id` in the response for the next turn.

---

## Parameter Mapping

| Legacy (`google-generativeai`) | Gemini 3 (`google-genai`) | Notes |
|-------------------------------|--------------------------|-------|
| `thinking_budget` | `thinking_level` | String: `"low"`, `"medium"`, `"high"` |
| `reasoning_effort` | `thinking_level` | Same as above |
| `system_instructions` | `system_instruction` | Singular form |
| `temperature` (forced low) | Unset | Gemini 3 defaults are preferred |
| `top_p` (custom defaults) | Unset | Only set when explicitly needed |

---

## Model Registry

`utils/gemini_runtime.py` defines a role-based model registry with
environment-variable overrides:

| Role | Default Model | Env Override |
|------|--------------|--------------|
| `core` | `gemini-3.1-pro-preview` | `GEMINI_MODEL_CORE` |
| `evidence` | `gemini-2.5-pro` | `GEMINI_MODEL_EVIDENCE` |
| `arrow` | `gemini-2.5-pro` | `GEMINI_MODEL_ARROW` |
| `flash` | `gemini-3-flash-preview` | `GEMINI_MODEL_FLASH` |
| `deep_research` | `deep-research-pro-preview-12-2025` | `GEMINI_MODEL_DEEP_RESEARCH` |

---

## Structured JSON Hardening

High-value JSON callsites now request structured output:

```python
config = {
    "response_mime_type": "application/json",
    "response_json_schema": {"type": "object"},
}
```

Existing parser fallbacks in `utils/llm_response_parser.py` are retained for
resilience against malformed responses.

---

## Batch Mode

ProPaths supports an optional batch mode for pipeline calls:

- **Env var**: `GEMINI_REQUEST_MODE=batch` (default: `standard`)
- **Polling interval**: `GEMINI_BATCH_POLL_SECONDS` (default: `15`)
- **Max wait**: `GEMINI_BATCH_MAX_WAIT_SECONDS` (default: `86400` / 24 hours)
- **Scope**: Batch mode applies to `client.models.generate_content` calls only.
- **Pricing**: Changes pricing and latency, not model capability.

---

## Tool Policy

Gemini tools are **opt-in** per callsite:

| Tool | Purpose | Usage |
|------|---------|-------|
| `google_search` | Web search grounding | Enabled when query text contains search hints or URLs |
| `url_context` | URL content extraction | Enabled when query text contains URLs |

Auto-detection helpers in `utils/gemini_runtime.py`:

- `should_enable_google_search(text)` -- checks for keywords like "latest", "current", "news"
- `contains_url(text)` -- checks for `http://` or `https://` patterns

---

## Shared Runtime Layer

`utils/gemini_runtime.py` is the single source of truth for:

- Default constants (model, output cap, thinking level)
- Config builders for both `models.generate_content` and `interactions.create`
- Tool payload builders
- Response text extraction helpers
- Singleton client management (thread-safe, cached by API key)

All LLM callsites import from this module rather than configuring the SDK directly.
