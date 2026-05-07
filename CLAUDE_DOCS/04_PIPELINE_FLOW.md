# Pipeline Flow — End to End

The pipeline is a single function: `runner.py:run_full_job()` (line 7571–8160). Everything below happens in one Python thread (the Flask background worker) per query. The only durable side effect is the Postgres write at the end.

## Top-level structure

```
run_full_job(protein, config)
├── pre: load known interactions, build PostProcessor, count steps
├── _run_main_pipeline_for_web(protein, config)   ← stages 1-9 (data gathering)
├── filter zero-content interactors
├── post_processor.run(payload)                    ← 10-12 stages (validation + cleanup)
├── storage.save_pipeline_results(protein, payload)  ← writes to Postgres
└── stage 11: run_pathway_pipeline(quick_assign=True, interaction_ids=...)
```

## Stages 1-9 (Data gathering, runner.py:6000-7568)

All stages are dispatched by iterating over the list of `StepConfig`s built by `pipeline/config_dynamic.py:generate_iterative_pipeline()` (default mode) or `generate_modern_pipeline()`.

### Stage 1: Discovery (broad cast)
**Step name:** `step1_iterative_discovery_iter1` (iterative) or `step1_deep_research_discovery` (modern)
**Function:** Single LLM call with broad-discovery prompt → list of N interactor primary symbols.
**Output:** `ctx_json.interactors[]` populated with primary names (no functions yet).
**Logging:** `[ITER 1/1] broad_discovery: ...`, `[ITER 1] Total interactors: N`.
**Model:** `gemini-3-flash-preview`, thinking_level=medium.

### Stage 2a: Function mapping (parallel batched)
**Step name:** `step2a_interaction_functions` factory
**Function:** `_run_parallel_batched_phase()` (runner.py:3129) dispatches ~5 interactors per batch with 5–10 workers. Each batch calls `step2a_interaction_functions` factory which produces functions per interactor.
**Output:** Each interactor gets `functions[]` populated with PhD-depth claims (function_name, arrow, mechanism, effect_description, biological_consequence, evidence, pmids, pathway, etc.).
**Logging:** `[PARALLEL:function_mapping] N targets → M batch(es) (~K/batch, W workers)`, `[PARALLEL:function_mapping] Batch K/M done — [...]`, `[PARALLEL:function_mapping] Completed — M/M batches succeeded`.
**Post-batch reconciliation (runner.py):**
- `_promote_discovered_interactors()` — convert indirect→direct cascades
- `_reconcile_chain_fields()` — sync mediator_chain ↔ chain_context
- `_backfill_chain_context_from_mediator_chain()` — fill chain_context.full_chain
- `_clean_function_names_in_payload()` — strip banned suffixes
- `_reclassify_indirect_to_direct()` — re-tag interaction_types
- `_dedup_functions_locally()` — pre-LLM dedup
- `_tag_shallow_functions()` — flag <6 sentences / <3 cascades
**Depth-check redispatch:** if `pass_rate < threshold`, re-run shallow interactors with `batch_size=1`. **Capped at one redispatch.**

### Stage 2b: Chain resolution (Track A + Track B in parallel)
**Function:** `_run_chain_resolution_phase()` (runner.py:1316).
**Track A:** `step2ab_chain_determination` (LLM) — for each indirect interactor, produce explicit mediator chain.
**Track B:** `step2ab2_hidden_indirect_detection` (code+LLM) — find non-pair proteins mentioned in claims (e.g. claim says "ATXN3 → SOD2 via FOXO4 transcription"; FOXO4 isn't a direct interactor but appears in evidence). Confirms via LLM.
**Output:** `_chain_annotations_explicit`, `_hidden_pairs_data`, `_chain_link_functions` populated with mediator pairs and chain context. Promotes new interactors via `[CHAIN:promote]`.
**Logging:** `[TRACK-A:2ab] Batch N: ...`, `[CHAIN:step2ab_chain_determination] Running...`, `[CHAIN:promote] Promoted N new interactors: [...]`.

### Stage 2a (re-fire for promoted interactors)
After chain resolution promotes new interactors (typically 5–15), function mapping re-fires for those.
**Logging:** `[PARALLEL:function_mapping_chain_promoted] N targets → M batch(es)`.

### Stage 2c: Chain claim generation
**Step names:** `step2ax_claim_generation_explicit`, `step2az_claim_generation_hidden`
**Function:** Per chain hop pair (`SRC->TGT`), generate per-claim biology with arrow + mechanism + evidence. The `chain_link_functions` dict on each interactor gets populated.
**Pair count:** typically 30-60 pairs per run. Often 30%+ of pairs are densely studied (REST, MAPK1, GSK3B) and produce 6500-8500 token outputs.
**Model:** `gemini-3-flash-preview`, thinking_level=low.
**`max_output_tokens`:** Currently `.env CHAIN_CLAIM_MAX_OUTPUT_TOKENS=24000` (was 8192 → 10000 → 24000 over the 2026-05-03 sessions). Real Flash 3 cap per Vertex docs is 65,536; 24000 is the chosen middle ground (3× the original 8K baseline, comfortable for any PhD-depth chain output, low enough to bound any proportional thinking-budget overhead). See `09_FIXES_HISTORY.md` § 1.7.
**Recovery:**
- Per-batch: split-retry (sub-divide and retry) on truncation
- Missing recovery: dedicated phase for hops that didn't get claims attached
- Depth-expand: dedicated phase for shallow hops
**Logging:** `[PARALLEL:ax_claim_generation_explicit] N targets → M batch(es)`, `Batch K/M failed: No text in response (finish_reason=FinishReason.MAX_TOKENS)`, `Attached chain claims for X/Y requested pair(s)`, `Batch X truncated (Y unclosed, Z items) — recovered via repair_truncated_json`.

### Stage 2c.5: Arrow heuristic (fast, code-only)
**Function:** runner.py:7445-7480. After chain claim gen, if any interactor has empty/`binds` arrow, derive from majority vote of its function arrows via `determine_interaction_arrow()` (`utils/interaction_metadata_generator.py:46`).
**Logging:** `[ARROW DETERMINATION] Applying fast heuristic...`, `[ARROW DETERMINATION] Heuristic applied`.
**Note:** This is NOT the post-processor's `arrow_validation` stage. This is a quick code-only pass.

### Stage 2e: Citation verification (skippable)
**Step name:** `step2e_citation_verification`
**Function:** Per interactor batch, verify PMID titles match claims, fill empty evidence.
**Skip flag:** `skip_citation_verification` (often True via env override).

### Stage 2g: Final QC + Stage 3: Snapshot
**Step names:** `step2g_final_qc`, `step3_snapshot`
**Function:** Validate interactor list shape, then create immutable `snapshot_json` from `ctx_json`. Drops zero-content interactors at this point (re-checked again pre-save).
**Logging:** `[SNAPSHOT-RECOVERY] N interactor(s) with zero functions`.

## Post-processing (PostProcessor.run, utils/post_processor.py:629-748)

The post-processor runs 10-12 stages depending on skip flags. Read `_build_default_stages()` (line 329-503) to see exact construction. Each stage is a `StageDescriptor` with name, label, kind (PURE / LLM), skip_flag, requires_api_key, critical.

| # | Stage | Skip flag | Critical | What |
|---|-------|-----------|----------|------|
| 1 | `chain_group_tagging` | `skip_chain_tagging` | no | Tag claims with chain_group ID before LLM stages |
| 2 | `chain_link_completeness` | `skip_chain_link_completeness` | no | Audit `chain_link_functions` vs mediator_chain; flag `_chain_incomplete_hops` |
| 3 | `normalize_function_contexts` | `skip_normalize_function_contexts` | no | Stamp default `function_context` (direct/net/chain_derived) so DB writers can trust the field |
| 4 | `schema_pre_gate` | `skip_schema_validation` | no | Fix broken arrows/directions/chains before LLM stages |
| 5 | **`arrow_validation`** | `skip_arrow_validation` | **YES** | LLM-driven arrow validation + direct-link extraction. Tier-1 DB short-circuit at start. **Per-Tier-1 normalization runs `apply_corrections({})` for all Tier-1 hits (this session's fix).** |
| 6 | `dedup_functions` | `skip_deduplicator` | no | LLM semantic dedup |
| 7 | `evidence_validation` | `skip_validation` | no | LLM evidence validation. **Often skipped via stale localStorage flag — now logged in route header.** |
| (skip) | `update_pmids` | `skip_pmid_update` | default-skip | NCBI verification (default off; step2e covers it) |
| 8 | `interaction_metadata` | `skip_interaction_metadata` | no | Aggregate stats |
| 9 | `clean_function_names` | `skip_clean_names` | no | Standardize names |
| 10 | `quality_validation` | `skip_quality_validation` | no | PhD-depth check (writes `quality_report.json`) |
| 11 | `finalize_metadata` | `skip_finalize_metadata` | no | Last validation before write |

**Critical = abort post-processing if it fails.** Only `arrow_validation` is critical.

**Each stage:** retries up to 4 times on transient errors (TimeoutError, ConnectionError, OSError, OperationalError). Permanent errors fail immediately. Failures append to `payload["_pipeline_metadata"]["failed_stages"]` and continue (unless critical).

**Order matters:** arrow_validation runs BEFORE dedup (swapped 2026-04-29 to fix arrow-blind dedup decisions).

## DB Sync (utils/storage.py + utils/db_sync.py)

**Entry:** `storage.save_pipeline_results(protein, final_payload)` → `_sync_to_db_with_retry()` → `DatabaseSyncLayer.sync_query_results()`.

**Per-interactor write path (db_sync.py):**
1. `_get_or_create_protein()` → either creates a new Protein row or fetches existing. Calls `normalize_symbol` (UPPERCASE), classifies via `classify_symbol` → 3 states: `protein`, `pseudo`, `invalid`. Pseudo flag stored in `extra_data.is_pseudo`.
2. `_save_interaction()` → write the Interaction row. Falls back to `direction='a_to_b'` if missing. Routes through `set_primary_arrow()` to keep `arrows` JSONB and legacy `arrow` scalar consistent.
3. `_save_claims()` → write each `InteractionClaim` row. Dedup key includes `function_context` (5-col COALESCE unique constraint). Cap of 30 claims per interaction.
4. `sync_chain_relationships()` → write `IndirectChain` and `ChainParticipant` rows. Atomic — one chain = one claim's described cascade. Per memory note: **never stitch chains by protein overlap.**
5. `_tag_claims_with_chain()` → tag chain-derived claims with `chain_id` FK.

**Silent-drop log markers:**
- `[CLASSIFY]` invalid symbol → drop
- `[PSEUDO] direct interaction rejected: SYMBOL` → reject (expected behavior)
- `[DB SYNC] Preserving direct interaction: A↔B (refusing downgrade to indirect)` → keep existing direct row
- `[CHAIN] Using LLM-emitted chain as-is (X → Y → Z) even though it lacks query=Q — no query-at-head override.`
- `[LOCUS ROUTER] X->Y (chain ...): kept=N rerouted=M dropped=K`
- `[CLEANUP] Filtered N zero-function interactor(s) before save`

**This session's fix:** `save_checkpoint()` no longer calls `_sync_to_db_with_retry`. It only updates `pipeline_status` + `last_pipeline_phase`. This eliminated the doubled `[DB SYNC]` log block.

**Storage line:** `[STORAGE] protein=X db_synced=True file_cached=True created=N updated=M`

## Stage 11: Pathway pipeline (scripts/pathway_v2/run_pipeline.py)

**Default mode:** `quick_assign=True, interaction_ids=<query-discovered>`.

**Quick-assign flow:**
1. **Step 1: Init Roots** — ensure 7 canonical biological-process root pathways exist
2. **Quick Assign Pathways** — DB-first matching: Tier 1 (exact name) → Tier 2 (fuzzy/synonym, SequenceMatcher ≥ 0.80) → Tier 3 (LLM with cached hierarchy context)
3. **Ontology Enrichment** — apply KEGG/Reactome/GO mappings
4. **Step 7: Verify Pipeline (scoped)** — auto-fix consistency issues

**This session's fix:** `_check_pathway_drift_at_write` runs the keyword validator at write time. When the proposed pathway has a meaningfully lower keyword score than another DB-resident pathway (gap ≥ 2 hits AND ≥ 2× ratio), reassign. Drift entries collected per-thread → returned in `quick_assign_pathways` result → written to `pipeline_diagnostics.json` by runner.py.

**Result keys:** `passed`, `status`, `steps_completed`, `timing`, `total_seconds`, `verification`, `quick_assign` (which now includes `pathway_drifts`).

## Diagnostics file

`Logs/<protein>/pipeline_diagnostics.json` — written by runner.py:7923-7947 just before storage.save. Shape:

```json
{
  "zero_function_dropped": [...],
  "chain_pair_unrecoverable": [...],
  "pipeline_metadata": {...},
  "chain_incomplete_hops": [
    {"interactor": "VCP", "missing_hops": ["FOO->BAR", ...]}
  ],
  "pathway_drifts": [   // NEW this session
    {"interactor": "VCP", "function": "ERAD Substrate...", "from": "Protein Quality Control", "from_score": 8, "to": "ERAD", "to_score": 22, "action": "corrected", "interaction_id": 123}
  ]
}
```

`Logs/<protein>/quality_report.json` — written by `quality_validator`. Shape: `{ total_functions, flagged_functions, pass_rate, thresholds, violations[] }`.

`services/data_builder.py` reads BOTH files and merges into `result["_diagnostics"]` for the frontend.

## Request metrics line (end of run)

```
[REQUEST METRICS] core_calls_3pro=N evidence_calls_2_5pro=N arrow_llm_calls=N | arrow_tier1_hits=N | arrow_fallback_to_pro=N | quota_skipped_calls=N
```

`arrow_tier1_hits` = pairs where DB cache had pre-validated arrow → LLM call skipped.
`arrow_llm_calls` = pairs that needed actual LLM arrow validation.
`arrow_fallback_to_pro` = pairs where Pro was used as fallback after Flash quota exhaustion.

## Total step count

The progress bar uses `total_steps = pipeline_step_count + post_processor.count_steps() + N_pathway_steps`. Header logs:
```
[PROGRESS] Total steps calculated: 21 (pipeline: 10, post: 11)
```

If predicted post != actual stages run, a stage was skipped. **Always log `skip_*` flags so silent skips are visible.** This session added explicit logging of `skip_validation`, `skip_deduplicator`, `skip_arrow_determination`.

## Total wall time (typical)

- Discovery: 30-60s (1 LLM call + parsing)
- Function mapping: 2-8 min (parallel batched, depth redispatch)
- Chain resolution: 1-3 min (Track A + B in parallel, then explicit/hidden pair extraction)
- Chain claim generation: 5-15 min (heaviest phase by token volume; 30-60 pairs × ~7s each. **Currently bloated by truncation+retry+depth-expand cycle — see A1.**)
- Citation verification: 1-2 min (often skipped)
- QC + Snapshot: <1 min
- Post-processing: 3-10 min (9 stages, some LLM)
- DB sync: 1-5 min
- Pathway pipeline (quick-assign): 1-3 min

Total: 20-60 min. Target post-A1-fix: 15-40 min.
