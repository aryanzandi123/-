# Citation Verification Redesign: Interactions API + Evidence-Only Delta

## Context

After switching from `gemini-3.1-pro-preview` to `gemini-3-flash-preview`, the citation verification step hits a server-enforced 8192 output token cap when `thinking_level=high` is active. This causes JSON truncation, retry loops, and data loss. The root cause is twofold:

1. **Bug**: `thinking_level=None` in the step config was silently overridden to `"high"` by runner.py (fixed: introduced `"off"` sentinel)
2. **Structural**: The step outputs the entire `ctx_json` (~50 interactors with full function data), producing massive JSON that exceeds output limits even without thinking overhead

This redesign addresses both issues by switching to the Interactions API and reducing output to evidence-only deltas.

## Changes

### 1. Step Config (`pipeline/prompts/modern_steps.py`)

| Field | Before | After |
|-------|--------|-------|
| `api_mode` | `"generate"` | `"interaction"` |
| `thinking_level` | `None` (forced to `"high"`) | `"medium"` |
| `response_schema` | `FUNCTION_MAPPING_OUTPUT_SCHEMA` | `CITATION_DELTA_SCHEMA` (new) |
| `prompt_template` | Full ctx_json output | Evidence-only delta output |

### 2. New Response Schema (`pipeline/types.py`)

```python
CITATION_DELTA_SCHEMA = {
    "type": "object",
    "properties": {
        "ctx_json": {
            "type": "object",
            "properties": {
                "main": {"type": "string"},
                "interactors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "primary": {"type": "string"},
                            "functions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "function": {"type": "string"},
                                        "cellular_process": {"type": "string"},
                                        "arrow": {"type": "string"},
                                        "evidence": {
                                            "type": "array",
                                            "items": _EVIDENCE_ENTRY,
                                        },
                                        "pmids": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": ["function", "cellular_process", "evidence"],
                                },
                            },
                        },
                        "required": ["primary", "functions"],
                    },
                },
            },
            "required": ["main", "interactors"],
        },
    },
    "required": ["ctx_json"],
}
```

Key differences from `FUNCTION_MAPPING_OUTPUT_SCHEMA`:
- Function items only include matching keys (`function`, `cellular_process`, `arrow`) + evidence/pmids
- No `mechanism`, `effect_description`, `biological_consequence`, `specific_effects`, `cascades`
- Estimated ~80% reduction in output size per interactor

### 3. Prompt Redesign (`pipeline/prompts/modern_steps.py`)

The new prompt instructs:
- Process ONLY the assigned interactors (batch directive, unchanged)
- For each function, verify paper titles via Google Search
- Output ONLY: `{primary, functions: [{function, cellular_process, arrow, evidence: [...], pmids: [...]}]}`
- Do NOT include mechanism, cascades, biological_consequence, or other fields
- Explicitly state these are **merge keys** for matching, not fields to modify

### 4. Runner Config (`runner.py`)

- `batch_size=6` (was 8) for citation_verification
- No other runner changes needed

### 5. Merge Logic (NO changes needed)

The existing `deep_merge_interactors()` in `utils/json_helpers.py` already handles this:
- Matches interactors by `primary`
- Matches functions by signature (`function` + `cellular_process` + direction)
- Unions evidence entries by PMID
- Unions pmids arrays
- Leaves unmentioned fields (mechanism, cascades, etc.) untouched

### 6. `enforce_thinking_mode` fix (`utils/gemini_runtime.py`)

Already applied:
- `"off"` sentinel returns `None`, causing thinking config to be omitted entirely
- `build_generate_content_config` skips `thinking_config` when `None`
- `build_interaction_generation_config` skips `thinking_level`/`thinking_summaries` when `None`

## Files Modified

| File | Change |
|------|--------|
| `pipeline/prompts/modern_steps.py` | Rewrite `step2e_citation_verification()`: new prompt, api_mode, thinking_level, schema |
| `pipeline/types.py` | Add `CITATION_DELTA_SCHEMA` |
| `runner.py` | Change `batch_size=6` for citation_verification |
| `utils/gemini_runtime.py` | Already fixed: `"off"` sentinel + interaction config guard |

## Verification

After implementation, run a query and confirm:
1. Log shows `api_mode=interaction` and `thinking_level=medium` for citation_verification batches
2. No `[WARN]Server cap` messages
3. No `truncated (N unclosed braces)` messages
4. Evidence arrays are properly enriched with verified paper titles
5. Non-evidence fields (mechanism, cascades, etc.) are preserved unchanged in final output
