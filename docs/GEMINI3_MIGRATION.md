# Gemini 3 Migration Guide

## Scope
This repo uses a hybrid Gemini 3 integration:
- Pipeline and batch jobs: `client.models.generate_content(...)`
- Chat endpoint: `client.interactions.create(...)`

## Dependency Policy
- Keep: `google-genai>=1.62.0,<2`
- Remove: `google-generativeai`
- Remove invalid requirement entry: `google.genai`

## Parameter Mapping
| Legacy Pattern | Gemini 3 Pattern |
|---|---|
| `thinking_budget` | `thinking_level` |
| `reasoning_effort` | `thinking_level` |
| `system_instructions` | `system_instruction` |
| forced low `temperature` | leave unset (Gemini 3 default) |
| custom `top_p` defaults | unset unless explicitly needed |

## API Differences
### `models.generate_content`
- Use nested thinking config:
```python
config = {
  "thinking_config": {"thinking_level": "high"},
  "max_output_tokens": 65536,
}
```
- Do not pass top-level `thinking_level` here.

### `interactions.create`
- Use top-level generation config keys:
```python
generation_config = {
  "thinking_level": "high",
  "thinking_summaries": "auto",
  "max_output_tokens": 65536,
}
```
- Tools payload uses `type`:
```python
tools=[{"type": "google_search"}, {"type": "url_context"}]
```

## Chat Continuation
- `/api/chat` accepts optional `previous_interaction_id`.
- `/api/chat` returns `interaction_id` for the next turn.

## Shared Runtime Layer
- `utils/gemini_runtime.py` is the single source of truth for:
  - defaults (model, output cap, thinking level)
  - config builders for both API surfaces
  - tool builders
  - response text extraction helpers

## Structured JSON Hardening
High-value JSON callsites now request:
- `response_mime_type="application/json"`
- `response_json_schema={"type":"object"}`

Existing parser fallbacks are retained for resilience.
