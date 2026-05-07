# ProPaths Complete Application Revamp Plan

## Context

ProPaths is a protein interaction research system (~50K lines) that uses Gemini AI to discover, validate, and visualize protein-protein interactions. The application suffers from:
- **Wasteful pipeline**: 7 interactor discovery rounds + 6 function rounds using `models.generate_content()` re-sending full context every call, when Deep Research could do discovery in 1-2 calls and Interactions API could maintain state across turns
- **Monolithic files**: app.py (2,959 lines), runner.py (3,305 lines), config (2,124 lines)
- **Dead/duplicate code**: deprecated fact checker (2,286 lines), duplicate arrow validators, orphan debug scripts
- **No modern Gemini features**: No Interactions API (except chat), no Deep Research, no explicit caching, no Batch API
- **Database chaos**: Dual-write to file cache + PostgreSQL, non-atomic saves
- **Poor organization**: Everything mixed together, no service layer, no blueprints

The goal is a **complete revamp across 11 Claude Code sessions**, each with an exact prompt and context files, requiring zero guidance from the user. Each session's Claude Code operates as an autonomous co-founder.

**Session 11** was added after discovering that Deep Research Pro Preview has a 1 RPM rate limit, making it impractical as the default discovery engine. Session 11 builds a Gemini 3.1 Pro iterative fallback using the Interactions API + Google Search + URL Context that matches Deep Research quality through multi-angle targeted iterations.

---

## Session Dependency Graph

```
S1 (Gemini Runtime) --> S2 (Pipeline Config) --> S3 (Runner Core)
                                                       |
                                              +--------+--------+
                                              |                 |
                                              v                 v
                                     S4 (Post-Processing)  S5 (Database)
                                              |                 |
                                              +--------+--------+
                                                       |
                                                       v
                                              S6 (App.py Decomp)
                                                       |
                                              +--------+--------+
                                              |                 |
                                              v                 v
                                     S7 (Frontend)     S8 (Pathways)
                                              |                 |
                                              +--------+--------+
                                                       |
                                                       v
                                              S9 (Testing)
                                                       |
                                                       v
                                              S10 (Cleanup)
                                                       |
                                                       v
                                         S11 (Iterative Discovery Fallback)
                                         [depends on: S1, S2, S3 being stable]
```

---

## SESSION 1: Gemini Runtime Foundation

### What to do
Open a new Claude Code session in `/Users/aryan/Documents/nEW/`

### Files to attach as context
Drag these into the chat:
- `API Docs/Gemini 3 Developer Guide.md`
- `API Docs/Gemini Caching.md`
- `API Docs/Gemini Text Generation Docs.md`
- `API Docs/Interactions Docs Gemini [Genreal].md`
- `API Docs/Batch API Docs.md`

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself with full confidence. Do not ask me for guidance -- I trust your judgment completely. When you see something wrong, fix it. When you see something that could be better, improve it. You have full creative freedom and full authority to create, delete, rewrite, or restructure any file.

## YOUR TASK: Rebuild utils/gemini_runtime.py as the centralized Gemini SDK interface

This file (utils/gemini_runtime.py, 372 lines) is imported by runner.py, app.py, evidence_validator.py, arrow_effect_validator.py, deduplicate_functions.py, pathway_assigner.py, and interaction_metadata_generator.py. Every downstream module depends on it.

### Read these files first (in order):
1. utils/gemini_runtime.py (the file you're rebuilding)
2. runner.py lines 1-175 (see how it uses gemini_runtime, and the _get_gemini_client caching pattern at lines 160-175)
3. app.py lines 1-15 (hardcoded API key fallback) and lines 2103-2200 (chat endpoint already uses Interactions API)
4. .env (current environment config)
5. requirements.txt

### What to build:

1. **SECURITY**: Remove the hardcoded API key fallback in app.py (lines 8-10). All API key access must go through a single `get_api_key()` function in gemini_runtime.py. Create `.env.example` with placeholder keys. Ensure `.env` is in `.gitignore`.

2. **SINGLETON CLIENT**: Move the cached client pattern from runner.py (`_get_gemini_client`) into gemini_runtime.py as `get_client()`. All modules should use this single cached client.

3. **EXPLICIT CACHING**: Add `create_or_get_system_cache(system_prompt_text, model, ttl_seconds=7200)` that creates explicit Gemini caches for system prompts. The pipeline's shared text blocks (DIFFERENTIAL_OUTPUT_RULES + STRICT_GUARDRAILS + SCHEMA_HELP from config_gemini_MAXIMIZED.py) are ~3K tokens repeated on every call -- they should be cached once with a 2-hour TTL. Follow the caching patterns from the attached Gemini Caching docs.

4. **INTERACTIONS API HELPERS**: Add `call_interaction(input, model, previous_interaction_id=None, tools=None, system_instruction=None, **kwargs)` that wraps `client.interactions.create()`. Add `call_deep_research(input, tools=None)` that wraps `client.interactions.create(agent='deep-research-pro-preview-12-2025', background=True)` with polling until completion. The chat endpoint in app.py already proves the SDK supports Interactions API -- build on that pattern.

5. **BATCH API HELPER**: Extract the batch call pattern from runner.py (look for `run_single_batch_call`) into gemini_runtime.py as a reusable `submit_batch_job()` function.

6. **MODEL REGISTRY**: Create a dict-based registry: `{'core': 'gemini-3.1-pro-preview', 'evidence': 'gemini-2.5-pro', 'arrow': 'gemini-2.5-pro', 'flash': 'gemini-3-flash-preview', 'deep_research': 'deep-research-pro-preview-12-2025'}`. Each reads from env vars with these defaults. Replace `get_core_model()`, `get_evidence_model()`, `get_arrow_model()` with `get_model(role)` but keep the old functions as aliases for backward compatibility.

7. **TYPE HINTS + DOCSTRINGS**: Full type hints and one-line docstrings on all public functions.

### Constraints:
- Keep ALL existing public function signatures working (add new ones, don't remove old ones)
- runner.py and all utils must continue working with zero changes
- Write unit tests in tests/test_gemini_runtime.py for pure functions (cache key generation, model registry, pricing)
```

### Expected outcome
A centralized Gemini runtime with client caching, explicit content caching, Interactions API helpers, Deep Research helpers, Batch API helpers, and a model registry. Security holes closed. Zero downstream breakage.

---

## SESSION 2: Pipeline Types and Prompt Architecture

### Files to attach as context
- `API Docs/Prompt Design docs.md`
- `API Docs/Gemini Caching.md`
- `API Docs/Deep Research Gemini Docs.md`

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself with full confidence. Do not ask me for guidance. You have full authority to create, delete, rewrite, or restructure any file.

## YOUR TASK: Decompose the monolithic pipeline configuration into a modular prompt architecture

### Read these files first (in order):
1. pipeline/types.py (69 lines -- the StepConfig dataclass)
2. pipeline/config_gemini_MAXIMIZED.py (2,124 lines -- the monolith you're breaking up)
3. pipeline/config_dynamic.py (350 lines -- dynamic step generator)
4. runner.py lines 900-1000 (see how steps are consumed in the pipeline)
5. utils/gemini_runtime.py (the updated runtime from Session 1 -- check for caching helpers)

### What to build:

1. **EXTEND StepConfig** in pipeline/types.py:
   - Add `api_mode: str = 'generate'` (values: 'generate', 'interaction', 'deep_research', 'batch')
   - Add `cache_system_prompt: bool = True`
   - Add `depends_on: Optional[str] = None`
   - Add `parallel_group: Optional[str] = None`
   - Add `retry_strategy: str = 'exponential'`
   - ALL existing StepConfig instantiations must continue working unchanged (new fields have defaults).

2. **DECOMPOSE PROMPTS**: Create `pipeline/prompts/` directory:
   - `shared_blocks.py`: DIFFERENTIAL_OUTPUT_RULES, STRICT_GUARDRAILS, SCHEMA_HELP, INTERACTOR_TYPES, FUNCTION_NAMING_RULES, CONTENT_DEPTH_REQUIREMENTS as named constants. Export `get_system_prompt_text()` that concatenates them. Export `get_cached_system_prompt(model)` that uses gemini_runtime's caching helper.
   - `interactor_discovery.py`: Prompt templates for discovery rounds. Export `create_discovery_step(round_num, total_rounds) -> StepConfig`.
   - `function_mapping.py`: Prompt templates for function rounds. Export `create_function_step(round_num, total_rounds) -> StepConfig`.
   - `arrow_determination.py`: Arrow step config and prompt.
   - `snapshot.py`: Snapshot step config.
   - `__init__.py` for the package.

3. **SIMPLIFY config_dynamic.py**: Rewrite `generate_pipeline()` to compose steps from the new prompt modules. The function signature `generate_pipeline(num_interactor_rounds, num_function_rounds, max_depth) -> list[StepConfig]` stays the same.

4. **DEPRECATE config_gemini_MAXIMIZED.py**: Make it a thin wrapper that imports from pipeline/prompts/ and re-exports PIPELINE_STEPS and all shared text blocks. Add a deprecation comment at the top.

5. **TESTS**: Unit tests for step creation functions, prompt composition, StepConfig validation.

### Constraints:
- runner.py must work with ZERO changes after this session
- generate_pipeline() keeps its exact signature and returns equivalent StepConfig objects
- All text constants remain accessible at their current import paths
```

### Expected outcome
Clean prompt library under `pipeline/prompts/`, extended StepConfig with api_mode support, backward-compatible config modules.

---

## SESSION 3: Runner Core Modernization (Deep Research + Interactions API)

### Files to attach as context
- `API Docs/Interactions Docs Gemini [Genreal].md`
- `API Docs/Interactions Docs Gemini [Deep Research].md`
- `API Docs/Deep Research Gemini Docs.md`
- `API Docs/Batch API Docs.md`
- `API Docs/Gemini Caching.md`

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself with full confidence. Do not ask me for guidance. You have full authority to create, delete, rewrite, or restructure any file.

## YOUR TASK: Modernize runner.py to use Deep Research for discovery and Interactions API for stateful function mapping

This is the HIGHEST IMPACT session. The current runner.py makes 15-25 sequential generate_content() calls, re-sending the full accumulated context every time. Deep Research can replace 7 discovery rounds with 1-2 calls. Interactions API can maintain state across function mapping rounds.

### Read these files COMPLETELY:
1. runner.py (all 3,305 lines -- understand every function)
2. utils/gemini_runtime.py (updated in Session 1 -- has Interactions + Deep Research + caching helpers)
3. pipeline/config_dynamic.py (updated in Session 2 -- StepConfig now has api_mode)
4. pipeline/types.py (updated in Session 2)
5. app.py lines 2103-2300 (chat endpoint already uses client.interactions.create -- proven pattern)

### What to build:

1. **MODERNIZE call_gemini_model()** (currently at line ~714):
   Read the `api_mode` field from StepConfig:
   - `api_mode='generate'`: Keep current client.models.generate_content() path (backward compat)
   - `api_mode='interaction'`: Use gemini_runtime.call_interaction() with previous_interaction_id chaining. Store interaction IDs in pipeline state dict.
   - `api_mode='deep_research'`: Use gemini_runtime.call_deep_research(). This replaces 7 sequential discovery rounds with 1-2 autonomous research calls.
   - `api_mode='batch'`: Use gemini_runtime.submit_batch_job() for non-time-critical steps.
   - When `cache_system_prompt=True`, use gemini_runtime.create_or_get_system_cache() before making the call.

2. **REDESIGN THE PIPELINE FLOW in _run_main_pipeline_for_web()**:

   **Phase 1 - DISCOVERY (Deep Research)**:
   Send ONE deep research request: "Systematically discover all protein interactors for {protein}. For each: classify as direct (physical: Co-IP, Y2H, BioID evidence) or indirect (pathway/cascade). Track full mediator chains. Provide brief support summary. Output as JSON: {ctx_json: {main, interactors: [{primary, interaction_type, upstream_interactor, mediator_chain, depth, support_summary}]}}."
   This REPLACES steps 1a through 1g (all 7 discovery rounds) with 1 call.
   Parse the deep research output into the existing ctx_json format.

   **Phase 2 - FUNCTION MAPPING (Interactions API, stateful)**:
   Use client.interactions.create() for the first function mapping step.
   Chain subsequent rounds using previous_interaction_id -- the model retains context automatically without re-sending.
   This replaces steps 2a through 2a5 (5+ rounds) but still requires multiple turns since each round focuses on different interactor batches.

   **Phase 3 - ARROW DETERMINATION (Batch or Interaction)**:
   Continue the interaction chain for arrow determination, OR use batch mode for parallel per-interactor arrow calls.

   **Phase 4 - SNAPSHOT**: Local JSON assembly (no model call, unchanged).

3. **BACKWARD COMPATIBILITY**: Add `use_legacy_pipeline: bool = False` parameter to run_pipeline(). When True, use the old generate_content path. Default is the new path.

4. **EXTRACT HELPERS** into proper modules:
   - `strip_code_fences()`, `parse_json_output()` -> `utils/json_helpers.py`
   - `deep_merge_interactors()` stays in runner.py (core logic)
   - `build_known_interactions_context()` -> `pipeline/context_builders.py`
   - Keep backward-compatible imports in runner.py

5. **ERROR HANDLING**: New interaction-based calls need handling for:
   - Deep research polling timeouts (max 10 minutes)
   - Stale interaction references
   - Quota exhaustion with automatic fallback to generate_content
   - Partial deep research results (still usable)

6. **TOKEN TRACKING**: Update pipeline_token_stats to work with Interactions API responses.

### Constraints:
- run_full_job() and run_requery_job() signatures MUST NOT change
- The web app must continue working -- same /api/query, /api/status, /api/results flow
- Deep merge logic must remain identical (it's battle-tested)
```

### Expected outcome
Pipeline goes from 15-25 sequential calls to ~3-6 (1-2 deep research + 3-4 stateful interactions). Estimated 60-80% token reduction. Full backward compatibility via legacy flag.

---

## SESSION 4: Post-Processing Pipeline Consolidation

### Files to attach as context
- `API Docs/Batch API Docs.md`
- `API Docs/Google Search Gemini Docs.md`

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself with full confidence. Do not ask me for guidance.

## YOUR TASK: Consolidate the 10+ post-processing stages into a clean PostProcessor pipeline with batch mode

### Read these files first:
1. runner.py lines 1930-2375 (all post-processing stages in run_full_job)
2. utils/schema_validator.py
3. utils/evidence_validator.py
4. utils/arrow_effect_validator.py
5. utils/arrow_validator_integrated.py
6. utils/claim_fact_checker.py (2,286 lines -- mostly deprecated, DEFAULT_SKIP=true)
7. utils/deduplicate_functions.py
8. utils/clean_function_names.py
9. utils/interaction_metadata_generator.py
10. utils/update_cache_pmids.py
11. utils/aggregation.py

### What to build:

1. **CREATE utils/post_processor.py**: A PostProcessor class with a configurable stage chain:
   ```python
   class PostProcessor:
       def __init__(self, skip_validation=False, skip_dedup=False, ...):
           self.stages = self._build_stage_chain(...)

       def run(self, payload, api_key, step_logger=None) -> dict:
           for stage in self.stages:
               payload = stage(payload, api_key=api_key)
           return payload
   ```

   Categorize stages:
   - PURE (no LLM, fast): schema_validator, aggregation, clean_function_names, finalize_metadata
   - LLM (can batch): evidence_validator, deduplicate_functions, arrow_effect_validator, interaction_metadata_generator
   - EXTERNAL API: update_cache_pmids (NCBI API)
   - DEPRECATED: claim_fact_checker

2. **MERGE ARROW VALIDATORS**: Move the unique parts of arrow_validator_integrated.py (like extract_direct_mediator_links_from_json) into arrow_effect_validator.py. Delete arrow_validator_integrated.py.

3. **BATCH MODE FOR LLM STAGES**: Evidence validation, dedup, and arrow validation each make per-interactor LLM calls. These are independent -- submit them as batch jobs (50% cost savings) for non-time-critical runs.

4. **KILL DEAD CODE**:
   - claim_fact_checker.py is 2,286 lines and DEFAULT_SKIP_FACT_CHECKING=true. Mark deprecated, remove from default chain.
   - Remove the second dedup pass that only existed because fact checker created dupes.

5. **REWRITE run_full_job() POST-PROCESSING** (lines 1930-2375): Replace the 400+ lines of sequential if/else with:
   ```python
   processor = PostProcessor(skip_validation=skip_validation, ...)
   final_payload = processor.run(pipeline_payload, api_key=api_key)
   ```

6. **TESTS**: tests/test_post_processor.py with mock LLM calls.
```

### Expected outcome
Post-processing consolidated into pluggable pipeline. Dead code removed. Batch mode for LLM validation. ~50% cost reduction on validation steps.

---

## SESSION 5: Database Layer Unification

### Files to attach as context
None needed (no Gemini API changes).

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself with full confidence. Do not ask me for guidance.

## YOUR TASK: Unify the dual storage system into PostgreSQL primary with file cache write-through

### Read these files first:
1. models.py (330 lines -- SQLAlchemy ORM)
2. utils/protein_database.py (448 lines -- file-based cache)
3. utils/db_sync.py (796 lines -- PostgreSQL sync)
4. utils/db_cleanup.py
5. runner.py lines 2148-2268 (save stages in run_full_job)
6. app.py lines 490-550 (build_full_json_from_db)

### What to build:

1. **CREATE utils/storage.py**: Unified storage facade:
   - `save_pipeline_results(protein, payload)` -> atomic PostgreSQL write + best-effort file cache write-through
   - `load_protein_data(protein)` -> PostgreSQL first, file cache fallback
   - `get_known_interactions(protein)` -> for pipeline exclusion context

2. **ATOMIC WRITES**: Wrap db_sync.py's sync_query_results() in proper `db.session.begin()` / `commit()` / `rollback()`.

3. **SIMPLIFY runner.py SAVE**: Replace the three separate save stages with one `StorageLayer.save_pipeline_results()` call.

4. **IMPROVE models.py**: Add compound index on (protein_a_id, protein_b_id, function_context). Add index on discovered_in_query. Ensure cascade behavior on Pathway relationships.

5. **DEMOTE FILE CACHE**: protein_database.py becomes read-only from pipeline perspective. Only StorageLayer writes to it.

6. **TESTS**: Atomic save tests, fallback read tests, round-trip consistency.
```

### Expected outcome
Single storage layer, atomic writes, no dual-write inconsistencies.

---

## SESSION 6: App.py Decomposition

### Files to attach as context
- `API Docs/Interactions Docs Gemini [Genreal].md`
- `API Docs/Google Search Gemini Docs.md`
- `API Docs/URL Context Gemini Docs.md`

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself with full confidence. Do not ask me for guidance.

## YOUR TASK: Decompose app.py (2,959 lines) into Flask Blueprints with a service layer

### Read the entire app.py first, then:
1. templates/index.html
2. utils/gemini_runtime.py (for Interactions API helpers)

### What to build:

1. **BLUEPRINT STRUCTURE**:
   - `routes/query.py` -- /api/query, /api/status, /api/cancel
   - `routes/results.py` -- /api/results, /api/databased, /api/expand
   - `routes/chat.py` -- /api/chat
   - `routes/pipeline.py` -- /api/pipeline/* V2 pathway routes
   - `routes/visualization.py` -- /visualize
   - `routes/__init__.py` -- register all blueprints

2. **SERVICE LAYER**:
   - `services/chat_service.py` -- Extract _build_chat_system_prompt, _build_compact_rich_context, _call_chat_llm. Make chat STATEFUL: maintain (protein, session_id) -> interaction_id mapping so multi-turn uses previous_interaction_id instead of re-sending full history.
   - `services/query_service.py` -- Job tracking (jobs dict, jobs_lock, eviction, cleanup)

3. **THIN app.py**: After extraction, app.py should be ~200 lines: Flask creation, DB init, blueprint registration, startup.

4. **DELETE /api/requery**: It's deprecated. /api/query handles everything.

5. **STANDARDIZE ERRORS**: Consistent JSON error format: `{"error": "message", "code": "ERROR_CODE"}`.

6. **TESTS**: Integration tests for each blueprint.
```

### Expected outcome
app.py drops from 2,959 to ~200 lines. Chat uses stateful Interactions API.

---

## SESSION 7: Frontend Modernization

### Files to attach as context
None (no Gemini API changes).

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself with full confidence. Do not ask me for guidance.

## YOUR TASK: Modernize the visualization layer. CRITICAL: Preserve exact visual appearance.

### Read ALL of these:
1. visualizer.py (819 lines)
2. static/visualizer.js (12,535 lines)
3. static/card_view.js (4,486 lines)
4. static/script.js
5. static/viz-styles.css (6,202 lines)
6. static/styles.css
7. static/network_topology.js
8. static/pipeline_controls.js
9. static/neural_particles.js
10. templates/index.html

### What to build:

1. **JINJA2 TEMPLATES**: Convert visualizer.py from string interpolation to Jinja2:
   - Create templates/visualize.html
   - visualizer.py becomes a thin render function
   - JSON data passed as Jinja2 variable, not string replacement

2. **OPTIMIZE VISUALIZER.JS**: Focus on D3 patterns:
   - Use data.join(enter, update, exit) pattern
   - Use Map/Set for O(1) lookups in tick handlers
   - Extract force simulation config into module
   - Extract modal rendering into module

3. **DEDUPLICATE CSS**: viz-styles.css at 6,202 lines likely has duplicates. Remove dead selectors and merge duplicates.

4. **SHARED JS UTILS**: Extract common formatters/builders from visualizer.js and card_view.js into a shared module.

5. **KEEP neural_particles.js and network_topology.js** as-is (small, self-contained).

### Constraint: ZERO visual changes. Users must see identical UI.
```

### Expected outcome
Jinja2-based visualization, optimized D3, deduplicated CSS. Zero visual change.

---

## SESSION 8: Pathway Pipeline Consolidation

### Files to attach as context
- `API Docs/Gemini Text Generation Docs.md`
- `API Docs/Batch API Docs.md`

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself with full confidence. Do not ask me for guidance.

## YOUR TASK: Consolidate the three pathway systems into one efficient pipeline

### Read ALL pathway-related files:
1. utils/pathway_assigner.py (633 lines)
2. scripts/pathway_v2/ (all 8+ files)
3. scripts/pathway_hierarchy/ (all 9 files)
4. models.py (Pathway, PathwayInteraction, PathwayParent models)
5. runner.py lines 2271-2332 (V2 pipeline invocation)
6. app.py pathway routes

### Investigate and decide:
- Which pathway system is more complete?
- Which scripts are dead code?
- What's the relationship between V2 and hierarchy?

### What to build:

1. **AUDIT**: Determine which scripts are actually called vs dead code.

2. **UNIFY**: Single pathway pipeline entry point that uses the best parts of each system. Keep pathway_assigner.py's ontology mappings (they're valuable).

3. **BATCH MODE**: Pathway assignment calls Gemini per-interaction. Use Batch API for 50% savings.

4. **SIMPLIFY RUNNER**: Replace the 7-function import + sequential call pattern with one function call.

5. **DEPRECATE** whichever system is superseded.
```

### Expected outcome
Single pathway system with batch-mode LLM calls.

---

## SESSION 9: Testing and Observability

### Files to attach as context
None.

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself.

## YOUR TASK: Build comprehensive tests and observability for the modernized codebase

### Read:
1. All files in tests/ directory (18 files)
2. utils/step_logger.py (404 lines)
3. All new modules created in Sessions 1-8

### What to build:

1. **AUDIT TESTS**: Identify which existing tests are still valid and which need rewriting.

2. **NEW UNIT TESTS**:
   - tests/test_gemini_runtime.py (extend)
   - tests/test_post_processor.py
   - tests/test_storage_layer.py
   - tests/test_pipeline_orchestration.py

3. **INTEGRATION TESTS**: Full pipeline with mocked Gemini client. Post-processing chain end-to-end. DB round-trip.

4. **MODERNIZE step_logger.py**: Structured JSON logging. Pipeline-level timing. Cost tracking.

5. **HEALTH CHECK**: Add /health and /metrics endpoints.
```

### Expected outcome
Comprehensive test suite, structured logging, health checks.

---

## SESSION 10: Dead Code Removal and Final Polish

### Files to attach as context
None.

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself.

## YOUR TASK: Final cleanup sweep

### What to do:

1. **DEAD CODE AUDIT**: grep for unused imports. Find uncalled functions. Remove deprecated modules (claim_fact_checker, arrow_validator_integrated, old config_gemini_MAXIMIZED).

2. **DELETE ORPHAN FILES**:
   - Empty files: GEMINI.md, DELETE, 1.55.0, 1.55.0'
   - Archives: .serena.7z, .serena (2).7z
   - Old handoff docs: SESSION_HANDOFF*.md, SESSION2_COMPLETE.md, HANDOFF_UI_BEAUTIFICATION.md
   - Debug scripts: debug_ancestors.py, debug_data_content.py, debug_hierarchy.py, debug_interaction_data.py, debug_interaction.py, reproduce_issue.py, verify_app_logic.py

3. **DOCUMENTATION**: Update README. Create ARCHITECTURE.md. Create API_MIGRATION.md.

4. **FINAL TEST**: Start Flask. Verify all routes. Run full test suite. Verify DB connectivity.

5. **.GITIGNORE**: Ensure .env, cache/, __pycache__/, *.pyc, .DS_Store are all listed.

6. **REQUIREMENTS.TXT**: Pin all dependencies.
```

### Expected outcome
Clean codebase, no dead code, proper documentation, all tests passing.

---

## SESSION 11: Gemini 3.1 Pro Iterative Discovery Fallback (Deep Research Alternative)

### Context & Problem

Deep Research Pro Preview (`deep-research-pro-preview-12-2025`) has a rate limit of **1 RPM** (request per minute), making it impractical as the default discovery engine. The current codebase has:

- `generate_modern_pipeline()` in `config_dynamic.py` (line 110) that uses `api_mode="deep_research"` for Phase 1 discovery
- `_call_deep_research_mode()` in `runner.py` (line ~606) that calls `call_deep_research()` from `gemini_runtime.py`
- Pipeline selection at `runner.py` line ~1454: non-legacy mode defaults to `generate_modern_pipeline()` which hits the 1 RPM wall
- The standard `generate_pipeline()` (line 37) still works with multi-round `api_mode="generate"` steps but lacks the quality/thoroughness of Deep Research

**Goal**: Build a Gemini 3.1 Pro-based multi-iteration discovery system that matches Deep Research quality by using:
- **Interactions API** with `previous_interaction_id` for stateful multi-turn research
- **Google Search grounding** on every iteration for real-time literature
- **URL Context** for PubMed/UniProt deep-dives
- **thinking_level="high"** for maximum reasoning depth
- **Explicit caching** for the large system prompt
- **Smart iteration strategy**: each round targets a different search angle to avoid missing proteins

### What to do
Open a new Claude Code session in `/Users/aryan/Documents/nEW/`

### Files to attach as context
- `API Docs/Gemini 3 Developer Guide.md`
- `API Docs/Interactions Docs Gemini [Genreal].md`
- `API Docs/Google Search Gemini Docs.md`
- `API Docs/URL Context Gemini Docs.md`
- `API Docs/Gemini Caching.md`

### Prompt to paste

```
You are the co-founder and lead engineer of ProPaths. This is YOUR codebase. Make every decision yourself with full confidence. Do not ask me for guidance -- I trust your judgment completely. You have full creative freedom and full authority to create, delete, rewrite, or restructure any file.

## THE PROBLEM

Deep Research Pro Preview has a rate limit of 1 RPM (request per minute). Our modern pipeline (generate_modern_pipeline in pipeline/config_dynamic.py) uses api_mode="deep_research" for Phase 1 interactor discovery, which means we can only run 1 query per minute. This is unacceptable for production.

We need a Gemini 3.1 Pro-based alternative that matches Deep Research quality through smart multi-iteration discovery using the Interactions API + Google Search grounding + URL Context.

## READ THESE FILES COMPLETELY (in order):

1. **utils/gemini_runtime.py** -- Understand the full runtime: get_client(), call_interaction(), call_deep_research(), build_generate_content_config(), the model registry, explicit caching helpers
2. **runner.py** -- Focus on:
   - Lines 299-328: api_mode dispatch (generate, interaction, deep_research, batch)
   - Lines 501-646: _call_interaction_mode() (501-603) and _call_deep_research_mode() (606-646) handlers
   - Lines 1454-1533: Pipeline selection logic (USE_LEGACY_PIPELINE env var, generate_modern_pipeline vs generate_pipeline)
   - Lines 1468-1480: Interaction chain state (_interaction_chain dict, _get_chain_key)
3. **pipeline/config_dynamic.py** -- Both generate_pipeline() (standard) and generate_modern_pipeline() (modern/deep research)
4. **pipeline/prompts/modern_steps.py** -- Current Deep Research discovery prompt (DEEP_RESEARCH_DISCOVERY_PROMPT), function mapping via Interactions API
5. **pipeline/prompts/interactor_discovery.py** -- Standard multi-round discovery prompts (how the old system works)
6. **pipeline/prompts/shared_blocks.py** -- Shared text blocks (DIFFERENTIAL_OUTPUT_RULES, STRICT_GUARDRAILS, INTERACTOR_TYPES, SCHEMA_HELP, etc.)
7. **pipeline/types.py** -- StepConfig dataclass with api_mode field
8. **routes/query.py** -- How the web API invokes run_full_job with parameters
9. **.env** -- Current model configuration

## WHAT TO BUILD

### 1. NEW RUNTIME FUNCTION: `call_iterative_research()` in utils/gemini_runtime.py

This is the core engine. It replaces a single Deep Research call with multiple stateful Interactions API calls to Gemini 3.1 Pro, each with Google Search + URL Context grounding:

```python
def call_iterative_research(
    *,
    iterations: list[dict],  # [{input: str, tools: list, thinking_level: str}, ...]
    model: str = "gemini-3.1-pro-preview",
    system_instruction: str = None,
    cache_name: str = None,  # Explicit cache for system prompt
    api_key: str = None,
    cancel_event = None,
    inter_iteration_delay: float = 2.0,  # Rate limit safety
) -> tuple[list[Any], str]:
    """Execute multi-iteration research via Interactions API with chaining.

    Each iteration builds on the previous via previous_interaction_id.
    Returns (list_of_interactions, final_text).
    """
```

Key behaviors:
- Chains iterations via `previous_interaction_id` -- the model sees ALL previous research context automatically
- Each iteration gets Google Search + URL Context tools (must re-send per Interactions API spec)
- System instruction re-sent each turn (per Interactions API spec) -- use explicit cache if >4096 tokens
- Configurable thinking_level per iteration (e.g., "medium" for broad search, "high" for synthesis)
- Respects cancel_event between iterations
- Returns all interactions + final consolidated text
- Retry logic with exponential backoff on quota/rate errors

### 2. NEW PIPELINE MODE: `generate_iterative_pipeline()` in pipeline/config_dynamic.py

A new pipeline generator that uses `api_mode="iterative_research"` for discovery instead of `api_mode="deep_research"`:

```python
def generate_iterative_pipeline(
    num_function_rounds: int = 2,
    max_depth: int = 3,
    discovery_iterations: int = 5,  # How many search angles for discovery
) -> list[StepConfig]:
```

This should create:
- Phase 1: A SINGLE StepConfig with `api_mode="iterative_research"` that internally runs 4-6 targeted iterations
- Phase 2a: Function mapping via Interactions API (same as modern pipeline)
- Phase 2b: Combined deep functions (same as modern pipeline)
- Phase 3: QC + Snapshot (unchanged)

### 3. DISCOVERY ITERATION STRATEGY in pipeline/prompts/iterative_discovery.py

Create a new prompt module that defines the multi-iteration discovery strategy. Each iteration must target a DIFFERENT search angle to maximize coverage and avoid redundancy:

**Iteration 1 - BROAD DISCOVERY** (thinking: high):
"Find ALL known protein interactors for {protein}. Search broadly: '{protein} protein interactions', '{protein} binding partners', '{protein} interactome'. Focus on well-established interactors from major databases (BioGRID, STRING, IntAct). Classify each as direct/indirect. Target: 15-25 proteins."

**Iteration 2 - PATHWAY & SIGNALING** (thinking: high):
"Building on previous findings, search for pathway and signaling cascade partners of {protein} NOT yet found. Search: '{protein} signaling pathway', '{protein} downstream targets', '{protein} upstream regulators'. Look for proteins connected through KEGG, Reactome, and GO pathways. Focus on indirect interactors with chain tracking. Target: 10-15 NEW proteins."

**Iteration 3 - DISEASE & FUNCTIONAL CONTEXT** (thinking: high):
"Search for interactors of {protein} in disease contexts and functional studies NOT yet found. Search: '{protein} disease mechanism', '{protein} mutation effects', '{protein} knockout proteomics', '{protein} patient samples'. Many important interactors are only discovered in disease models. Target: 5-10 NEW proteins."

**Iteration 4 - DEEP LITERATURE MINING** (thinking: high):
"Search for obscure or recently discovered interactors of {protein} NOT yet found. Search: recent reviews (2023-2025), BioID/APEX2 proximity labeling studies, mass spectrometry interactome studies, preprints. These are cutting-edge interactions not in standard databases. Target: 5-10 NEW proteins."

**Iteration 5 - CONSOLIDATION & GAP-FILL** (thinking: high):
"Review ALL interactors found across previous iterations. Check for: (1) Missing chain connections between indirect interactors, (2) Proteins that should be reclassified direct↔indirect based on new evidence, (3) Any well-known interactors that were somehow missed. Consolidate into final comprehensive JSON output with ctx_json format."

Each iteration prompt must:
- Reference what was found so far (the model sees this via interaction chain)
- Emphasize finding NEW proteins not yet discovered
- Use the same output schema as DEEP_RESEARCH_DISCOVERY_PROMPT
- Include STRICT_GUARDRAILS and INTERACTOR_TYPES

### 4. DISPATCH IN runner.py

Add a new handler `_call_iterative_research_mode()` alongside the existing handlers:

```python
if api_mode == "iterative_research":
    return _call_iterative_research_mode(
        step, prompt, model_name, thinking_level,
        cancel_event, api_key, step_logger,
    )
```

The handler should:
- Parse the discovery iteration configs from the step (or from the prompt module)
- Call `call_iterative_research()` from gemini_runtime
- Parse the final consolidated output into ctx_json format
- Return (text, token_stats) same as other handlers
- Aggregate token stats across all iterations

### 5. PIPELINE SELECTION TOGGLE

Update runner.py pipeline selection (line ~1454) to support three modes:

```python
# .env: PIPELINE_MODE=standard|modern|iterative (default: iterative)
pipeline_mode = os.getenv("PIPELINE_MODE", "iterative").strip().lower()

if pipeline_mode == "standard" or use_legacy:
    pipeline_steps = generate_pipeline(...)
elif pipeline_mode == "modern":
    pipeline_steps = generate_modern_pipeline(...)  # Deep Research (1 RPM limited)
elif pipeline_mode == "iterative":
    pipeline_steps = generate_iterative_pipeline(...)  # Gemini 3.1 Pro multi-iteration
```

Also consider adding the toggle to the web API (routes/query.py) so the user can choose from the UI.

### 6. MODEL REGISTRY UPDATE

Add `gemini-3.1-pro-preview` to the model registry in gemini_runtime.py:

```python
MODEL_REGISTRY = {
    "core": "gemini-3.1-pro-preview",
    "iterative": "gemini-3.1-pro-preview",  # NEW
    "evidence": "gemini-2.5-pro",
    ...
}
```

Make configurable via `GEMINI_MODEL_ITERATIVE` env var.

### 7. GOOGLE SEARCH + URL CONTEXT TOOL CONFIGURATION

For each iteration, configure tools as:
```python
tools = [
    types.Tool(google_search=types.GoogleSearch()),
    types.Tool(url_context=types.UrlContext()),  # For PubMed deep-dives
]
```

The model can use both: Google Search for broad discovery, URL Context when it finds a promising PubMed/UniProt URL to analyze in depth.

### 8. EXPLICIT CACHING FOR SYSTEM PROMPT

The system prompt (STRICT_GUARDRAILS + INTERACTOR_TYPES + SCHEMA_HELP + iteration instructions) is ~4-6K tokens and identical across all 5 iterations. Cache it:

```python
cache_name = create_or_get_system_cache(
    system_prompt_text=full_system_prompt,
    model="gemini-3.1-pro-preview",
    ttl_seconds=3600,
)
```

### 9. TESTS

- tests/test_iterative_research.py:
  - Unit test for iteration prompt generation
  - Mock test for call_iterative_research() with fake interactions
  - Integration test verifying the pipeline produces valid ctx_json
  - Test that cancel_event stops iteration mid-chain
  - Test that rate limit errors trigger retry

### CONSTRAINTS:
- The existing Deep Research mode (api_mode="deep_research") must continue working unchanged
- The standard pipeline (generate_pipeline) must continue working unchanged
- run_full_job() signature MUST NOT change
- The web app must continue working -- toggle via .env or API parameter
- Gemini 3.1 Pro Preview is the model to use (model ID: "gemini-3.1-pro-preview")
- Each iteration MUST re-send system_instruction, tools, and generation_config (Interactions API requirement)
- Use thinking_level="high" for all iterations to maximize reasoning quality
- Add inter_iteration_delay (default 2s) to avoid hitting rate limits

### QUALITY TARGET:
The 5-iteration strategy should discover 30-50 interactors -- matching or exceeding what Deep Research finds in a single call. The key advantage: we control EXACTLY what angles to search and can add more iterations if coverage is thin. Deep Research is a black box; this is a white box that we can tune.
```

### Expected outcome
A complete Gemini 3.1 Pro iterative discovery system that:
- Replaces Deep Research's 1 RPM limit with unlimited-rate multi-iteration Interactions API calls
- Uses 5 targeted search angles (broad → pathway → disease → deep lit → consolidation)
- Maintains full stateful context via interaction chaining (no re-sending full history)
- Matches or exceeds Deep Research quality through controlled, angle-specific iterations
- Toggleable via `PIPELINE_MODE=iterative` env var (or API parameter)
- Fully backward compatible with existing Deep Research and standard pipelines

### Critical files modified
- `utils/gemini_runtime.py` -- Add `call_iterative_research()`
- `runner.py` -- Add `_call_iterative_research_mode()` handler + pipeline selection update
- `pipeline/config_dynamic.py` -- Add `generate_iterative_pipeline()`
- `pipeline/prompts/iterative_discovery.py` -- NEW: iteration strategy + prompts
- `pipeline/types.py` -- Possibly extend StepConfig with iteration config
- `routes/query.py` -- Optional: add pipeline_mode parameter
- `.env` -- Add `PIPELINE_MODE=iterative`, `GEMINI_MODEL_ITERATIVE=gemini-3.1-pro-preview`

---

## Estimated Impact

| Metric | Before | After |
|--------|--------|-------|
| Pipeline API calls per query | 15-25 sequential | 3-6 (Deep Research + Interactions) |
| Token cost per query | 100% baseline | ~30-40% (caching + stateful + batch) |
| app.py | 2,959 lines | ~200 lines |
| runner.py | 3,305 lines | ~1,200 lines |
| Pipeline config | 2,474 lines (monolithic) | ~800 lines (modular) |
| Dead code removed | 0 | ~5,000+ lines |
| Post-processing stages | 10+ sequential | 6 (batch + parallel) |
| Storage systems | 2 (dual-write) | 1 unified (PostgreSQL primary) |
| Test coverage | Partial | Comprehensive |

---

## How to Execute

1. Complete sessions IN ORDER (1 through 10), then Session 11
2. After each session, verify the app still starts: `python app.py`
3. Sessions 4 and 5 CAN run in parallel after Session 3
4. Session 7 is independent of Session 8
5. **Session 11** can technically run anytime after Sessions 1-3 are stable, but is placed last since it was added after the original 10 sessions were completed
6. Each session is self-contained -- paste the prompt, attach the docs, let Claude Code work
7. If a session takes more than one Claude Code context window, continue in a new session with "Continue the work from the previous session. Read all files that were modified and pick up where we left off."

## Verification

After all 11 sessions:
1. `python app.py` starts without errors
2. Query a protein (e.g., "ATXN3") with `PIPELINE_MODE=iterative` and verify full pipeline completes
3. Query a protein with `PIPELINE_MODE=standard` and verify the legacy path still works
4. Check that results appear in both PostgreSQL and file cache
5. Verify the D3 visualization renders correctly
6. Test the chat endpoint with multi-turn conversation
7. Run `python -m pytest tests/` -- all tests pass
8. Verify iterative discovery finds 30-50 interactors (matching Deep Research quality)
