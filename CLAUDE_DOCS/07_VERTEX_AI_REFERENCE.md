# Vertex AI / Gemini 3 Reference

**Confirmed via the official Vertex AI docs at `docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-flash` and the user's own pushback (which forced the prior session to verify).** Last refreshed 2026-05-03.

## Why this doc exists

In an earlier session prior-Claude claimed the Flash output cap was 8192 tokens. **That was wrong.** The user pushed back, verification confirmed via Vertex AI docs the real cap is 65,536. This doc captures the verified specs so the next session doesn't make the same mistake.

**Status (2026-05-03):** all stale "8192 cap" comments in `.env`, `gemini_runtime.py` docstring, and the per-step factories have been corrected as part of fix § 1.7 in `09_FIXES_HISTORY.md`. Current chain-claim cap is `CHAIN_CLAIM_MAX_OUTPUT_TOKENS=24000`.

## Models in use

| Role | Default | Env override | Notes |
|------|---------|--------------|-------|
| `core` | `gemini-3-flash-preview` | `GEMINI_MODEL_CORE` | Used for most pipeline steps |
| `evidence` | `gemini-3-flash-preview` | `GEMINI_MODEL_EVIDENCE` | Evidence validation |
| `arrow` | `gemini-3-flash-preview` | `GEMINI_MODEL_ARROW` | Arrow validation (complex branch) |
| `flash` | `gemini-3-flash-preview` | `GEMINI_MODEL_FLASH` | Generic Flash |
| `deep_research` | `deep-research-pro-preview-12-2025` | `GEMINI_MODEL_DEEP_RESEARCH` | Deep research mode (rare) |
| `iterative` | `gemini-3-flash-preview` | `GEMINI_MODEL_ITERATIVE` | Iterative-research mode |

**The user's stance:** "FLASH ONLY IS INTENTIONAL". Don't propose Pro 3 unless Flash is genuinely incapable.

Available Pro models if absolutely needed (don't use without explicit user confirmation):
- `gemini-3-pro-preview`
- `gemini-3.1-pro-preview`

## Real specs for `gemini-3-flash-preview`

- **Input context:** 1,048,576 tokens (1M)
- **Output max:** **65,536 tokens** (per official docs — NOT 8192)
- **Multimodal:** text, images, audio, video, PDFs
- **Pricing:** $0.50 per 1M input tokens, $3 per 1M output tokens (as of May 2026)

## Real specs for `gemini-3-pro-preview` / `gemini-3.1-pro-preview`

- **Input context:** 2,000,000 tokens
- **Output max:** 65,536 tokens (some sources say 8,192 for older Pro variants — verify before use)
- **Pricing:** ~5-10× Flash

## Thinking levels

For Gemini 3 (Flash and Pro), `thinking_level` replaces `thinking_budget`. Values:

| Level | Tokens spent on thinking | Use case |
|-------|--------------------------|----------|
| `off` (None) | 0 | Pure JSON formatting where no reasoning is needed |
| `minimal` | very few | Low-complexity tasks |
| `low` | ~25% of `max_output_tokens` budget | Chain-claim gen, citation verification (constrained tasks) |
| `medium` | ~40% | Function mapping, discovery (default) |
| `high` | ~60% | Arrow validation, disagreement resolution. **Vertex's own default.** |

**CRITICAL:** `thoughts_token_count + output_token_count <= max_output_tokens`. If thinking eats most of the budget, output gets squeezed and `MAX_TOKENS` finish_reason fires. Always size `max_output_tokens` to comfortably exceed (thinking + output).

For chain-claim with low thinking (~1500 tokens) + PhD-depth output (~7000-15000 tokens for cofactor-rich proteins like REST or ATXN3) → need ~20,000 token budget MINIMUM. The 8192 setting in the prior `.env` was the bug; current setting is `CHAIN_CLAIM_MAX_OUTPUT_TOKENS=24000`.

## Structured output (`response_json_schema`)

- Same output token limit as free-form (65,536 for Flash 3).
- Schema itself counts toward INPUT token limit.
- `MAX_TOKENS` finish_reason can leave response text empty when thinking + structured output overflows budget.
- All callers must pass `response_mime_type="application/json"` alongside `response_json_schema`.

## Context caching

- Gemini 3 Flash includes context caching standard.
- ~90% cost reduction for repeated prompt prefixes over caching threshold.
- Enabled via `cached_content` parameter on `generate_content`.
- Already used in `scripts/pathway_v2/quick_assign.py` (hierarchy cache).
- **Not yet used in chain claim gen** (would save ~90% of system prompt tokens × 30-60 calls). See A2 in `10_OPEN_ISSUES.md`.
- Batch API does NOT support context caching.

## Batch API

- 50% cost savings vs synchronous API.
- Quota-exempt (does not count against per-minute RPM).
- Up to 200,000 requests per batch job.
- Up to 24h SLA.
- File size limit 1GB if using Cloud Storage input.
- **Now supports gemini-3-flash-preview** (was Pro-only at one point; this session enabled Flash via `runner.py` `_BATCH_ELIGIBLE_MODELS`).

## TPM (tokens-per-minute) budget

`.env GEMINI_TPM_BUDGET=600000` (Tier-1 paid-preview estimate). The runner's parallel dispatcher tracks rolling 60-s observed tokens and gates next group dispatch. Raise to 1,500,000 on Tier 2 / 4,000,000 on Tier 3.

## How `gemini_runtime.py` builds requests

`build_generate_content_config()` (line 500-556):

```python
config_dict = {
    "max_output_tokens": max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
}
if effective_thinking_level:
    config_dict["thinking_config"] = ThinkingConfig(
        thinking_level=effective_thinking_level,
        include_thoughts=False,
    )
if system_instruction: ...
if temperature is not None: ...
if response_mime_type: ...
if response_json_schema is not None: ...
if cached_content: ...
return GenerateContentConfig(**config_dict)
```

Tools (Google Search, URL Context, Code Execution) are constructed separately via `build_generate_tools()`.

## Constants in `gemini_runtime.py` (relevant)

```python
DEFAULT_MODEL = "gemini-3-flash-preview"
DEFAULT_FLASH_MODEL = "gemini-3-flash-preview"
DEFAULT_MAX_OUTPUT_TOKENS = 65536       # Correct
DEFAULT_THINKING_LEVEL = "high"          # Vertex's own default
DEFAULT_THINKING_SUMMARIES = "auto"
DEFAULT_EVIDENCE_MODEL = "gemini-3-flash-preview"
DEFAULT_ARROW_MODEL = "gemini-3-flash-preview"
DEFAULT_REQUEST_MODE = "standard"
DEFAULT_BATCH_POLL_SECONDS = 15
DEFAULT_BATCH_MAX_WAIT_SECONDS = 86400
GEMINI_3_PRO_PROMPT_BUCKET_THRESHOLD = 200000
```

## `.env` keys for chain claim gen

| Key | Current value | Recommended |
|-----|---------------|-------------|
| `CHAIN_CLAIM_BATCH_SIZE` | 1 (was 2) | 2 or 3 once max_output is fixed |
| `CHAIN_CLAIM_TEMPERATURE` | 0.45 | keep |
| `CHAIN_CLAIM_THINKING_LEVEL` | low | keep |
| `CHAIN_CLAIM_MAX_OUTPUT_TOKENS` | **24000** (was 8192 → 10000 → 24000) | If empirical truncation persists: 32768 or 65536 |
| `CHAIN_CLAIM_MAX_WORKERS` | 3 | keep |
| `CHAIN_CLAIM_RECOVERY_MAX_WORKERS` | 2 | keep |
| `CHAIN_CLAIM_MAX_RETRIES` | 2 | keep |
| `CHAIN_CLAIM_REQUEST_MODE` | standard | keep |

## Authentication

Uses Application Default Credentials (ADC) via `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` env vars. NO explicit api_key needed for Vertex. The `api_key` arg in older code is ignored on Vertex.

## Common errors

| Error | Meaning | Fix |
|-------|---------|-----|
| `finish_reason=FinishReason.MAX_TOKENS` + empty text | thinking + output exceeded `max_output_tokens` | Raise `max_output_tokens`, lower thinking, or split prompt |
| `400 INVALID_ARGUMENT: answer candidate length is too long with N tokens, exceeds limit of M` | Model produced N > our requested M (we set `max_output_tokens=M`) | Raise our `max_output_tokens` |
| `429 RESOURCE_EXHAUSTED` (RPM) | Hit per-minute request limit | Lower `MAX_WORKERS`, increase backoff |
| `429 RESOURCE_EXHAUSTED` (daily quota) | Hit daily quota | Wait for reset, or fall back to alternate model |
| `503 UNAVAILABLE` / `504 DEADLINE_EXCEEDED` | Transient | Retry with exponential backoff |
| `INTERNAL` / generic 500 | Transient | Retry |
| `PERMISSION_DENIED` | ADC misconfigured | Check `GOOGLE_CLOUD_PROJECT` |

## Verification approach for an unfamiliar limit

Before believing ANY claim about a Gemini limit:

1. **Check the official Vertex AI docs:** `docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-flash`. WebFetch this page.
2. **Cross-check the codebase:** what does `DEFAULT_MAX_OUTPUT_TOKENS` say in `gemini_runtime.py`? What does `.env` say?
3. **Inspect the actual error message in stderr.** Is the limit value in the message OURS (we passed it) or THEIRS (server enforced)?
4. **Test with a request near the boundary** if uncertain. The user runs queries; ask for one to verify.

The 8192 claim survived in this codebase because nobody verified it. Don't repeat that.

## Useful Vertex AI doc links

- Gemini 3 Flash: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-flash`
- Gemini 3 Pro: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-pro`
- Thinking config: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thinking`
- Structured output: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/control-generated-output`
- Batch inference: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/batch-prediction-gemini`
- Quotas: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/quotas`
- Get started with Gemini 3: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/get-started-with-gemini-3`
