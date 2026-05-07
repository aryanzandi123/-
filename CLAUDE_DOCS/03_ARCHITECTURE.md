# Architecture — File-by-File Map

## One-screen overview

```
┌─ runner.py (9007L) ───────────────────────────────────┐
│  run_full_job(protein, config) — main entry           │
│  ├─ STAGE 1-2: Discovery (parallel batched LLM)       │
│  ├─ STAGE 2a: Function mapping (parallel)             │
│  ├─ STAGE 2b: Chain resolution (Track A + B parallel) │
│  ├─ STAGE 2c: Chain claim gen (step2ax/step2az)       │
│  ├─ STAGE 2e: Citation verification                   │
│  ├─ STAGE 2g: QC + Snapshot                           │
│  ├─ POST-PROCESSING: 12 stages via PostProcessor      │
│  ├─ DB SYNC: StorageLayer.save_pipeline_results()     │
│  └─ STAGE 11: Pathway pipeline (quick_assign default) │
└────────────────────────────────────────────────────────┘
                        │ writes
                        ▼
┌─ PostgreSQL (models.py 905L) ─────────────────────────┐
│  proteins, protein_aliases, interactions,             │
│  interaction_claims, indirect_chains,                 │
│  chain_participants, pathways, pathway_interactions,  │
│  pathway_parents                                      │
└────────────────────────────────────────────────────────┘
                        │ read by
                        ▼
┌─ services/data_builder.py (2210L) ────────────────────┐
│  build_full_json_from_db(protein) →                   │
│    { snapshot_json, ctx_json, _diagnostics, ... }     │
└────────────────────────────────────────────────────────┘
                        │ served by
                        ▼
┌─ Flask routes/ ────────────────────────────────────────┐
│  /api/visualize/<p>  → HTML with embedded JSON        │
│  /api/results/<p>    → JSON                           │
│  /api/chain/<id>     → IndirectChain row              │
│  /api/claims/<p>     → all claims for a protein       │
│  /api/protein/<s>/interactions  → modal data          │
│  /api/databased-interactors/<p> → cross-query data    │
│  /api/stream/<p>     → SSE progress                   │
│  /api/query, /api/cancel, /api/status, /api/requery   │
└────────────────────────────────────────────────────────┘
                        │ rendered by
                        ▼
┌─ Frontend — DEFAULT = SPA (since 2026-05-04 cutover-flip) ─┐
│  templates/visualize.html        thin SPA shell             │
│  react-app/src/app/main.tsx      SPA entry                  │
│  react-app/src/app/views/card/   CardView, PathwayExplorer  │
│  react-app/src/app/modal/        ModalShell + ClaimRenderer │
│  react-app/src/app/lib/          colors, claims, diagnostics│
│                                                             │
│  ESCAPE HATCH (?spa=0) — archived legacy:                  │
│  templates/visualize_legacy.html (former visualize.html)    │
│  static/_legacy/{card_view,modal,visualizer,…}.js (frozen) │
└─────────────────────────────────────────────────────────────┘
```

## Top-level files

| File | Lines | Purpose |
|------|-------|---------|
| `runner.py` | 9007 | Master pipeline orchestrator. `run_full_job()` is the entry point for every query. Contains the parallel-batched dispatcher (`_run_parallel_batched_phase`), chain-resolution wrapper (`_run_chain_resolution_phase`), parse-with-retry, all step factories' wiring. |
| `app.py` | 235 | Flask app factory + blueprint registration + DB init |
| `models.py` | 905 | SQLAlchemy ORM models. ALL DB shape lives here. See `05_DATABASE_SCHEMA.md`. |
| `visualizer.py` | 265 | Server-side HTML rendering for `/api/visualize/<protein>` |
| `schema.sql` | — | Raw schema dump (in sync with `models.py`) |
| `requirements.txt` | — | Python deps. Includes `google-genai`, `flask`, `flask-sqlalchemy`, `sqlalchemy`, `psycopg2-binary`, `alembic` |
| `.env` | ~250 | Runtime configuration. Read it before changing limits/models. |
| `CLAUDE.md` | — | Top-level project conventions (functional core / imperative shell, single responsibility, etc.) |
| `ARCHITECTURE.md` | — | Older architecture doc (predates this handoff) |

## `pipeline/` — Pipeline configuration + prompts + types

| File | Lines | Purpose |
|------|-------|---------|
| `pipeline/types.py` | 568 | `StepConfig` dataclass and related types. All step properties (model, thinking_level, max_output_tokens, temperature, response_schema, api_mode, cache_system_prompt, etc.) |
| `pipeline/config_dynamic.py` | 233 | `generate_iterative_pipeline()`, `generate_modern_pipeline()`, `generate_pipeline()` — build the ordered list of `StepConfig`s for a run |
| `pipeline/context_builders.py` | 377 | Functions that construct prompt context (interactor batch directives, snapshot summaries) |
| `pipeline/pipeline.py` | 390 | Pipeline class wrapper |
| `pipeline/prompts/__init__.py` | — | Re-exports |
| `pipeline/prompts/shared_blocks.py` | — | Common prompt fragments (depth requirements, output format) |
| `pipeline/prompts/deep_research_steps.py` | — | step1 deep-research discovery prompt |
| `pipeline/prompts/iterative_research_steps.py` | — | iterative-research mode prompt + chain claim factories |
| `pipeline/prompts/modern_steps.py` | — | step2a function-mapping, step2e citation, step2g QC |
| `pipeline/prompts/function_mapping.py` | — | function mapping prompt body |
| `pipeline/prompts/qc_and_snapshot.py` | — | step2g QC + step3 snapshot |
| `pipeline/prompts/arrow_determination.py` | — | step2c arrow heuristic prompt (rarely fires; runner has fast heuristic) |
| `pipeline/prompts/interactor_discovery.py` | — | step1 broad-discovery prompt |

## `utils/` — Post-processing, validators, helpers (~30 files)

| File | Lines | Purpose |
|------|-------|---------|
| `utils/post_processor.py` | 749 | The 12-stage post-processing chain. `PostProcessor.run()` + `_build_default_stages()`. **Read this before touching post-processing.** |
| `utils/db_sync.py` | 2637 | `DatabaseSyncLayer.sync_query_results()` — writes everything to Postgres. Symbol classifier, pseudo whitelist, chain participant writer, locus router invocation. |
| `utils/storage.py` | 578 | `StorageLayer.save_pipeline_results()`, `save_checkpoint()` (now metadata-only), `get_known_interactions()`, file cache management |
| `utils/gemini_runtime.py` | 980 | `get_client()`, `build_generate_content_config()`, `submit_batch_job()`, `create_or_get_system_cache()`, model registry, thinking config helpers |
| `utils/llm_response_parser.py` | 195 | `extract_json_from_llm_response()` — 6-strategy JSON extraction with truncated-JSON repair |
| `utils/arrow_effect_validator.py` | 1796 | LLM-based arrow validation. `validate_arrows_for_payload`, `validate_arrows_and_effects`, `apply_corrections` (dual-arrow logic), `_preflight_tier1_arrows`, `_apply_tier1_normalization_to_payload` |
| `utils/arrow_content_validator.py` | 169 | Pure verb-family regex matcher. `classify_arrow()`, `validate_arrows()`. Auto-correct off by default (observe + log). |
| `utils/direction.py` | 167 | Semantic ↔ canonical direction translation. `infer_direction_from_arrow()`. `bidirectional` is dead (S1). |
| `utils/direction_content_validator.py` | 222 | Direction inference from prose |
| `utils/upstream_interactor_validator.py` | 189 | Validates `upstream_interactor` field on indirect interactors |
| `utils/chain_resolution.py` | 731 | `canonical_pair_key()`, `canonicalize_chain_link_functions()`, `validate_chain_on_ingest()`, chain helper utilities. **`canonical_pair_key` was case-sensitive; fixed in this session.** |
| `utils/chain_view.py` | 409 | `ChainView` class — single source of truth for chain state on an Interaction. Reads from linked IndirectChain (chain_id FK) with fallback to JSONB. |
| `utils/claim_locus_router.py` | 410 | "Locus router" — routes claims that mention the query AND a hop to the parent indirect interaction instead of the hop |
| `utils/evidence_validator.py` | 674 | LLM-based evidence validation against external sources |
| `utils/citation_finder.py` | 613 | NCBI verification of PubMed IDs |
| `utils/pubmed_match.py` | 215 | PubMed PMID matching helpers |
| `utils/quality_validator.py` | 452 | Pure runtime depth check (6-10 sentences / 3-5 cascades / 3+ evidence). Writes `quality_report.json`. |
| `utils/schema_validator.py` | 794 | `validate_schema_consistency()`, `finalize_interaction_metadata()` |
| `utils/interaction_metadata_generator.py` | 823 | `generate_interaction_metadata`, `determine_interaction_arrow` (function-arrow majority vote heuristic — used by runner.py:7445) |
| `utils/deduplicate_functions.py` | 524 | LLM-based semantic dedup |
| `utils/dedup_local.py` | 230 | Pre-LLM local dedup (exact + fuzzy + mechanism-overlap) |
| `utils/clean_function_names.py` | 205 | Standardize names (strip banned suffixes) |
| `utils/aggregation.py` | 113 | Aggregate stats helpers |
| `utils/pruner.py` | 681 | Subgraph pruning for the expand UI |
| `utils/pathway_content_validator.py` | 269 | Keyword-based pathway-vs-prose drift detector. `classify_pathway()`, `validate_pathways()` |
| `utils/protein_aliases.py` | 328 | `normalize_symbol()`, `canonicalize_protein_name()`. Greek letter / variant normalization. |
| `utils/protein_database.py` | 452 | Persistent KG fallback for `_write_to_protein_db` |
| `utils/json_helpers.py` | 569 | Generic JSON helpers (truncated-JSON repair, schema flattening) |
| `utils/observability.py` | 402 | `log_event` + structured stderr emission with telemetry hooks |
| `utils/structured_log.py` | 96 | Stderr formatter |
| `utils/step_logger.py` | 404 | Per-step file logging to `Logs/<protein>/<timestamp>/` |

## `routes/` — Flask blueprints

| File | Lines | Endpoint(s) |
|------|-------|-------------|
| `routes/query.py` | 609 | `/api/search/<p>` (pseudo-gate), `/api/query` (start job), `/api/requery/<job_id>`, `/api/status/<job_id>`, `/api/stream/<protein>` (SSE), `/api/cancel/<job_id>` |
| `routes/results.py` | 517 | `/api/results/<p>`, `/api/chain/<id>`, `/api/pathway/<id>/interactors`, `/api/claims/<p>`, `/api/databased-interactors/<p>`, `/api/protein/<s>/interactions`, `/api/expand/*` (pruner) |
| `routes/visualization.py` | 175 | `/`, `/api/visualize/<p>` (HTML page with embedded JSON, LRU cache 32 entries) |
| `routes/chat.py` | 104 | `/api/chat` (chat over network with LLM) |
| `routes/pipeline.py` | 320 | `/api/pipeline/*` — clear pathways, repair pathways, backfill claims, status |
| `routes/health.py` | 172 | Health checks |
| `routes/__init__.py` | 13 | Blueprint registration |

## `services/` — Backend services

| File | Lines | Purpose |
|------|-------|---------|
| `services/data_builder.py` | 2210 | `build_full_json_from_db(protein)`, `build_protein_detail_json(symbol)`, `build_expansion_json_from_db(protein, visible)`, `_chain_fields_for(interaction)` (the canonical chain-field emitter), `_reconstruct_chain_links` |
| `services/state.py` | 160 | `jobs` dict + `jobs_lock`, `Job` class, SSE event broadcaster |
| `services/chat_service.py` | 585 | Chat-over-network LLM dispatch |
| `services/error_helpers.py` | 18 | `error_response`, `ErrorCode` enum |
| `services/metrics.py` | 85 | Run metric accumulators |

## `scripts/` — Pathway pipeline + migrations + audits

### `scripts/pathway_v2/` — The pathway assignment pipeline

| File | Purpose |
|------|---------|
| `run_pipeline.py` | `run_pathway_pipeline(quick_assign=, interaction_ids=)` entry point |
| `quick_assign.py` | `quick_assign_pathways` — DB-first matching with hierarchy LLM batching. Includes `_check_pathway_drift_at_write` (P3.1, this session) and the chain-pathway unification pass |
| `step1_init_roots.py` | Initialize the 7 canonical biological-process root pathways |
| `step2_assign_initial_terms.py` | Tier-1 exact name matching |
| `step3_refine_pathways.py` | Tier-2 fuzzy/synonym matching |
| `step4_build_hierarchy_backwards.py` | Build parent-child edges from leaves up |
| `step5_discover_siblings.py` | Find sibling pathways at each level |
| `step6_reorganize_pathways.py` | Flatten duplicate branches, dedup |
| `step7_checks.py` | Verification checks (interactions, chains, pathways, claims) |
| `step7_repairs.py` | Auto-fixers for common issues |
| `verify_pipeline.py` | Step 7 entry — runs all checks + repairs |
| `ontology_mappings.py` | KEGG/Reactome/GO mapping |
| `llm_utils.py` | Pathway-pipeline-specific Gemini call helpers |
| `async_utils.py` | Async batch dispatch helpers |
| `cache.py` | Hierarchy cache lookups |
| `step6_utils.py` | Step 6 helpers |
| `cleanup_all_pathways.py` | Manual cleanup script |

### `scripts/` (top-level)

| File | Purpose |
|------|---------|
| `audit_chain_completeness.py` | Audit chain hop completeness |
| `cleanup_safety_net_claims.py` | Remove safety-net synthetic claims |
| `cleanup_placeholder_claims.py` | Remove placeholder claims |
| `cleanup_fallback_pathways.py` | Remove fallback pathways |
| `db_health_check.py` | DB health check script |
| `diagnose_chain_data.py` | Diagnose chain inconsistencies |
| `validate_existing_arrows.py` | Validate arrows against DB |
| `fix_direct_link_arrows.py` | Manual fixer for direct-link arrows |
| `clear_pathway_tables.py` | Truncate pathway tables (destructive) |
| `migrate_*.py` | Various migration scripts |
| `run_quick_assign_atxn3.py` | Standalone quick-assign trigger |

## `static/` — Frontend assets

The legacy vanilla-JS frontend was archived to `static/_legacy/` on 2026-05-04 (cutover-flip). It still serves at `?spa=0` as an emergency escape hatch; new work goes into `react-app/src/app/`. Plan: delete `static/_legacy/` after a few weeks of daily SPA use.

| File | Status |
|------|--------|
| `static/_legacy/card_view.js` | ARCHIVED. Replaced by `react-app/src/app/views/card/`. |
| `static/_legacy/modal.js` | ARCHIVED. Replaced by `react-app/src/app/modal/`. |
| `static/_legacy/visualizer.js` | ARCHIVED. Graph view; out of scope of the SPA rewrite. |
| `static/_legacy/cv_diagnostics.js` | ARCHIVED. Replaced by `react-app/src/app/lib/diagnostics.ts` + `views/card/DiagnosticsBanner.tsx`. |
| `static/_legacy/script.js` | ARCHIVED. SPA pipeline events live at `views/card/PipelineEventsDrawer.tsx`. |
| `static/_legacy/{network_topology,force_config,shared_utils}.js` | ARCHIVED. |
| `static/neural_particles.js` | KEPT. Decorative; legacy template references it. |
| `static/pipeline_controls.js` | KEPT. Used by `templates/index.html`. |
| `static/styles.css`, `viz-styles.css`, `pathway_explorer_v2.css` | KEPT. Legacy CSS — referenced by `visualize_legacy.html`. SPA does not use them; design tokens live in `react-app/src/app/styles/tokens.css`. |
| `static/react/app.js` | SPA bundle (Vite output, ~80 KB). |
| `static/react/pipeline-events.js` | Legacy island bundle, still used at `?spa=0`. |
| `static/react/assets/app.css` | SPA stylesheet (ReactFlow + tokens). |

## `templates/`

| File | Lines | Purpose |
|------|-------|---------|
| `templates/index.html` | 143 | Query submission page |
| `templates/visualize.html` | ~40 | Thin SPA shell. Renders `<div id="root">` + loads `/static/react/app.js`. Server injects `window.__PROPATHS_BOOTSTRAP__`. |
| `templates/visualize_legacy.html` | 626 | Archived legacy shell. Loads JS from `/static/_legacy/`. Reached via `?spa=0`. |

## `react-app/src/app/` — Canonical SPA

| Path | Purpose |
|------|---------|
| `main.tsx` | Entry; bootstrap hydration; `EXPECTED_SCHEMA_VERSION = "2026-05-04"` paired with backend `services.data_builder.SCHEMA_VERSION`. |
| `App.tsx` | Router shell + ErrorBoundary. |
| `routes/Visualize.tsx` | Single-protein view (header → diagnostics → chips → breadcrumb → sidebar+canvas). |
| `routes/Workspace.tsx` | Multi-protein workspace skeleton (route reserved). |
| `views/card/CardView.tsx` | ReactFlow + elkjs orchestration. |
| `views/card/PathwayExplorer.tsx` | Stat-rich left sidebar. |
| `views/card/PathwayBreadcrumb.tsx` | Selected + ancestor chips above canvas. |
| `views/card/PipelineEventsDrawer.tsx` | SPA-native SSE drawer. |
| `views/card/{Legend,EmptyState,FilterChips,DiagnosticsBanner}.tsx` | Surrounding affordances. |
| `views/card/{ProteinCard,ChainEdge,DuplicateCrossLink,buildCardGraph,layoutEngine}.tsx/ts` | Custom node/edge + graph build + elkjs lazy-import. |
| `modal/ModalShell.tsx` | Focus trap + escape + ←/→/j/k keyboard nav. |
| `modal/{Interaction,Aggregated}Modal.tsx` | Two modal kinds. |
| `modal/ChainContextBanner.tsx` | Per-chain banner with chip drill. |
| `modal/ClaimRenderer.tsx` | Per-claim PhD-depth render with D/C/E rubric + `function_context` badge. |
| `lib/{colors,pseudo,normalize,claims,diagnostics,pathwayStats}.ts` | Pure libs; vitest-tested. |
| `store/{useSnapStore,useViewStore,useModalStore}.ts` | Zustand stores (Map-keyed for multi-protein readiness). |
| `api/{client,queries,sse}.ts` | TanStack Query + SSE hooks. |
| `types/api.ts` | Hand-typed contract. Drift caught by schema_version warn. |
| `styles/tokens.css` | Design tokens — color, spacing, radius, typography, transitions. |

## `react-app/src/islands/` — legacy island (one remaining)

| File | Status |
|------|--------|
| `pipeline-events/` | KEPT. Mounts into legacy DOM hooks created by `static/_legacy/script.js`. Still used at `?spa=0`. SPA-native equivalent lives at `views/card/PipelineEventsDrawer.tsx`. |
| `cardview-badges/` | DELETED on 2026-05-04. Replaced by SPA's DiagnosticsBanner + lib/diagnostics. |

## `tests/`

696 pytest pass + 62 vitest pass as of 2026-05-04. Run via `bash scripts/check.sh` (covers typecheck → vitest → vite build → pytest in one shot). Key files:

- `test_chain_handling.py`, `test_chain_orientation.py`, `test_chain_view.py` — chain logic
- `test_arrow_effect_validator.py`, `test_arrow_content_validator.py` — arrow validation
- `test_post_processor.py` — post-processing stages
- `test_data_builder_chain_links.py` — frontend payload shape
- `test_card_view_chain_contract.py` — frontend ↔ backend contract; reads `static/_legacy/{card_view,visualizer}.js` since the cutover-flip
- `test_storage.py`, `test_pathway_llm_utils.py` — DB + pathway pipeline
- `test_routes_*.py` — API endpoints
- `tests/manual/` — excluded from `scripts/check.sh`; for manual smoke tests
- `react-app/src/app/lib/*.test.ts` — 62 vitest unit tests (colors / normalize / pseudo / claims / pathwayStats)

## `scripts/check.sh`

One-shot quality gate: typecheck → vitest → vite build → pytest. Use before committing.
