# Session A: Full Audit & Overhaul Changes

## Overview
This session performed a 3-pass deep audit of the entire ProPath bioinformatics pipeline, covering: deduplication bugs, token efficiency, scientific accuracy, database optimization, UI cleanup, and settings simplification.

---

## Pass 1: Deduplication & Token Efficiency (16 changes)

### A1. Evidence blind concatenation fix — `utils/json_helpers.py`
- **Bug**: `deep_merge_interactors` did raw `existing_evidence + value` with zero dedup
- **Fix**: Added `_evidence_dedup_key()` helper using PMID + content-hash fallback for PMID-less items
- **Lines changed**: Added function at line 14, modified evidence merge at line 212

### A2. PMID-less evidence DB filter fix — `utils/db_sync.py`
- **Bug**: `ev.get("pmid") not in existing_pmids` always True when pmid is None
- **Fix**: Applied same `_evidence_dedup_key()` at both interaction-level (line 574) and claim-level (line 847) evidence merges

### A3. Iteration merge drops new functions — `runner.py:2107`
- **Bug**: When interactor re-discovered across iterations, only metadata backfilled, functions silently dropped
- **Fix**: Added function merge logic (dedup by name) for the collision path

### A4/D6. Bidirectional split — `utils/aggregation.py:171`
- **Bug**: Functions tagged `direction=bidirectional` were deepcopy'd into both groups, doubling claims
- **Fix**: Keep in BOTH groups (for complete display) but add `_bidirectional=True` flag so downstream dedup knows not to double-count

### A5. Evidence validator appends without dedup — `utils/evidence_validator.py:267`
- **Bug**: Validator rephrases function names, both original and "corrected" coexist
- **Fix**: Added fuzzy name matching (60% word overlap) before appending "new" functions

### A6. Local dedup Pass 2 missing name guard — `runner.py:1094`
- **Bug**: `proc_overlap >= 0.6 and same arrow` had NO minimum `name_overlap` check
- **Fix**: Added `and name_overlap >= 0.3` guard

### B1. Reduced CONTENT_DEPTH_REQUIREMENTS — `pipeline/prompts/shared_blocks.py`
- cellular_process: 6-10 sentences → 3-5
- biological_consequence: 3-5 cascades with 6-10 steps → 1-2 cascades with 4-6 steps
- specific_effects: 5-8 → 3-5
- evidence: 3-5 papers → 2-3
- Removed all GOOD/BAD example blocks (~600 words of prompt tokens)
- **Savings**: ~35K output tokens/run

### B2. Removed redundant SCHEMA_HELP — 5 files
- `pipeline/prompts/function_mapping.py` — removed from `_function_step()`
- `pipeline/prompts/deep_research_steps.py` — removed from `_heavy_claim_step()`
- `pipeline/prompts/qc_and_snapshot.py` — removed from QC step
- `pipeline/prompts/modern_steps.py` — removed from 2 step factories
- **Savings**: ~534 tokens × ~20 calls = ~10K input tokens/run

### B3. Slimmed FUNCTION_NAMING_RULES — `pipeline/prompts/shared_blocks.py`
- Cut from ~170 lines to ~25 lines
- Kept 2 core rules + 4 examples each
- **Savings**: ~700 tokens × ~20 calls = ~14K input tokens/run

### B4. Lowered context size guard — `pipeline/context_builders.py`
- Hard guard threshold: 20,000 → 12,000 chars
- Removed `support_summary` from `_slim_interactor_for_function_step`

### B5. Batched citation verification — `runner.py`
- Citation verification batch_size: 1 → 3 (both CLI and web pipelines)
- **Savings**: ~17 fewer LLM calls per run

### B6. Dedup temperature — `utils/deduplicate_functions.py`
- Changed temperature from 1.0 → 0.0 for deterministic yes/no classification

### B7. Search history enforcement — `pipeline/prompts/interactor_discovery.py` + `pipeline/context_builders.py`
- Added `_SEARCH_DEDUP_RULE` to discovery preamble
- Added `{ctx_json.search_history}` template substitution in context_builders.py

### C1. Chain link arrow inheritance — `utils/db_sync.py:1132`
- **Bug**: Fallback used parent chain's net-effect arrow for intermediate links
- **Fix**: Use neutral "regulates" instead of inheriting parent arrow

### C2. Post-processor stage ordering — `utils/post_processor.py`
- Arrow validation moved BEFORE evidence validation
- Removed duplicate arrow_validation stage that was at old position
- Updated test: `tests/test_post_processor.py` active count 10→9

### C3. Indirect→direct auto-correction guard — `services/data_builder.py`
- **Bug**: Regex detected "co-IP" etc. without checking which protein pair evidence referred to
- **Fix**: Added check that both protein names must appear in the summary

---

## Pass 2: Architecture & Quality (12 changes)

### D1. Filter zero-function interactors — `runner.py` (2 sites: web + CLI)
- Added filtering immediately after function mapping, before chain resolution
- Prevents wasting LLM calls on proteins with no biological data

### D2. Case-insensitive protein name matching — `runner.py:2097`
- Changed `existing_names` set to use `.upper()` normalization
- Fixed enrichment loop comparison to case-insensitive

### D3. Circuit breaker for post-processor — `utils/post_processor.py`
- Added `critical: bool = False` field to `StageDescriptor`
- Set `critical=True` on `arrow_validation` stage
- Added abort logic: if critical stage fails, pipeline stops with `_pipeline_aborted` metadata

### D4. Evidence validator hallucination guard — `utils/evidence_validator.py`
- Added `original_names` set check before processing validator results
- Made matching case-insensitive
- Skips any interactor not in original batch

### D5. Upsert richness metric fix — `utils/db_sync.py`
- Changed richness comparison from `len(evidence)` to `sum(len(str(e)))` (content length)
- Added `arrow` to `_fn_key` dedup key: `f"{name}||{pw}||{arrow}"`

### E1. Differentiated discovery rounds — `pipeline/prompts/interactor_discovery.py`
- 1b: Adaptor/scaffold proteins and regulated substrates
- 1c: Pathway partners and signaling cascade regulators
- 1d: Disease-associated and stress-responsive interactors
- 1e: Tissue-specific and recently published interactors
- 1f: Rare/obscure interactors from specialized databases
- 1g: Cross-species and computational interactors

### E2. System prompt caching — `pipeline/prompts/function_mapping.py` + `interactor_discovery.py`
- Moved preamble from `prompt_template` to `system_prompt` field
- Set `cache_system_prompt=True`
- **Savings**: ~2,500 tokens × ~15 calls = ~37,500 input tokens (cached = free/discounted)

### E3. Indirect→direct reclassification — `runner.py`
- Added `_reclassify_indirect_to_direct()` function
- Scans function evidence for direct binding keywords (co-IP, Y2H, pull-down, etc.)
- Reclassifies and removes chain metadata when evidence contradicts "indirect" classification

### F1. Automated function name validation — `runner.py`
- Added `_BANNED_NAME_SUFFIXES` regex and `_clean_function_names_in_payload()` function
- Strips: regulation, suppression, activation, inhibition, promotion, induction, stimulation, enhancement, modulation
- Wired into both CLI and web pipelines after function mapping

### F2. Depth validation tagging — `runner.py`
- Added `_tag_shallow_functions()` function
- Tags functions failing depth requirements with `_depth_issues` field
- Checks: cellular_process sentence count, evidence count, cascade count

### F3. Claim dedup at generation — already implemented
- `_build_chain_batch_directive` already lists existing claims in batch directives

---

## Pass 3: Database + UI + Settings (10 changes)

### G1. N+1 query fix — `services/data_builder.py:357`
- Added `joinedload(Interaction.protein_a), joinedload(Interaction.protein_b)` to main query
- Eliminates ~50 extra SQL queries per results page load

### G2. Composite index — `models.py`
- Added `db.Index('idx_interaction_pair_lookup', 'protein_a_id', 'protein_b_id')`

### G3. Removed cascade deletes — `models.py`
- Removed `cascade='all, delete-orphan'` from `Protein.interactions_as_a` and `interactions_as_b`

### G4. CHECK constraints — `models.py`
- Added `valid_function_context` CHECK: NULL or IN ('direct', 'net', 'chain_derived', 'mixed')
- Added `valid_interaction_type` CHECK: NULL or IN ('direct', 'indirect')

### H1. Stripped settings UI — `templates/index.html`
- Removed: All advanced settings, post-processing skips, model overrides, Pipeline V2 controls, thinking_budget (dead setting), validation tuning, logging toggles
- Kept: Mode selector (iterative/modern/standard), 3 preset buttons (Quick/Standard/Thorough), Discovery iterations slider, hidden fields for rounds/depth (set by presets)

### H2. Simplified JavaScript — `static/script.js`
- `readConfigFromInputs()`: Now returns only 5 fields (pipeline_mode, discovery_iterations, interactor_rounds, function_rounds, max_depth)
- `saveConfigToLocalStorage()`: Now saves only those 5 fields
- Removed all localStorage reads for advanced settings

### H3. Backend defaults — `routes/query.py`
- No changes needed — backend already falls back to env vars for all removed settings

### I2. Console.log cleanup — `static/visualizer.js`, `static/card_view.js`
- Removed 38 emoji-prefixed debug console.log statements (29 from visualizer.js, 9 from card_view.js)

### Migration script — `scripts/migrate_schema.py`
- Python script that uses existing app.py/db to apply schema changes
- Adds index, fixes invalid data, adds CHECK constraints
- Safe to re-run (idempotent)
- Run: `python3 scripts/migrate_schema.py`

---

## Files Modified (Complete List)

### Pipeline Core
- `runner.py` — A3, A6, B5, D1, D2, E3, F1, F2 (iteration merge, dedup guard, batch citations, zero-function filter, case-insensitive merge, reclassification, name validation, depth tagging)

### Pipeline Prompts
- `pipeline/prompts/shared_blocks.py` — B1, B3 (content depth reduction, naming rules slimmed)
- `pipeline/prompts/function_mapping.py` — B2, E2 (SCHEMA_HELP removed, system prompt caching)
- `pipeline/prompts/deep_research_steps.py` — B2 (SCHEMA_HELP removed)
- `pipeline/prompts/qc_and_snapshot.py` — B2 (SCHEMA_HELP removed)
- `pipeline/prompts/modern_steps.py` — B2 (SCHEMA_HELP removed from 2 steps)
- `pipeline/prompts/interactor_discovery.py` — B7, E1, E2 (search history, round differentiation, system prompt caching)
- `pipeline/context_builders.py` — B4, B7 (context guard, search_history substitution)

### Utils
- `utils/json_helpers.py` — A1 (_evidence_dedup_key helper + evidence merge fix)
- `utils/db_sync.py` — A2, C1, D5 (PMID filter, chain arrow, upsert richness, fn_key with arrow)
- `utils/aggregation.py` — A4/D6 (bidirectional split with _bidirectional flag)
- `utils/evidence_validator.py` — A5, D4 (fuzzy dedup, hallucination guard)
- `utils/deduplicate_functions.py` — B6 (temperature 1.0→0.0)
- `utils/post_processor.py` — C2, D3 (stage reorder, circuit breaker)

### Services
- `services/data_builder.py` — C3, G1 (auto-correction guard, N+1 fix with joinedload)

### Models & Database
- `models.py` — G2, G3, G4 (composite index, cascade removal, CHECK constraints)
- `scripts/migrate_schema.py` — NEW (Python migration script for existing databases)
- `scripts/migrate_schema.sql` — NEW (raw SQL alternative, not needed if using Python script)

### Frontend
- `templates/index.html` — H1 (stripped to minimal settings)
- `static/script.js` — H2 (simplified readConfigFromInputs/saveConfigToLocalStorage)
- `static/visualizer.js` — I2 (29 debug console.logs removed)
- `static/card_view.js` — I2 (9 debug console.logs removed)

### Tests
- `tests/test_post_processor.py` — Updated stage count assertion (active 10→9)
- `tests/test_prompt_architecture.py` — Updated preamble check to search both prompt_template and system_prompt

---

## Estimated Impact

### Token Savings Per Pipeline Run
- **Input tokens**: ~120-180K reduction (~50-60%)
- **Output tokens**: ~35-50K reduction (~40-50%)
- **LLM calls**: ~20 fewer per run

### Duplicate Claims
- ~60-80% reduction in duplicative scientific output

### Database Performance
- ~50x fewer SQL queries on results page (N+1 fix)
- Faster pair lookups via composite index
- Data integrity enforced via CHECK constraints

### UX
- Settings form: 30+ fields → 5 (mode + presets + iterations)
- No debug spam in browser console

---

## How to Apply

```bash
# 1. Run migration for existing database
python3 scripts/migrate_schema.py

# 2. Start app normally
python3 app.py
```

---

## Known Issues NOT Fixed (Deferred)
1. **cvState in card_view.js**: Dual state management with PathwayState — too tightly integrated to remove without major refactor
2. **No Alembic migrations**: Project uses db.create_all() — should set up proper migration framework for production
3. **upstream_interactor has no FK constraint**: Stores raw string instead of protein_id reference
4. **Deprecated `arrow` column**: Both `arrow` (legacy string) and `arrows` (new JSONB) coexist — needs migration
5. **PathwayParent always loads entire table**: Should use recursive CTE or filtered query
6. **Chain link reconstruction loads all mediator interactions**: Should filter to only needed pairs
