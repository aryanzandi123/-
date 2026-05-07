# Diagnostic Patterns — How to Read a Pipeline Log

Pipeline runs emit ~500-2000 lines of stderr per query. This doc maps every meaningful prefix to its source file and what it means. Use this when the user pastes a log.

## Order of phases (in run order)

1. DB init banner
2. `[ROUTE /api/query]` skip flag echo
3. `[PROGRESS] Total steps calculated: ...`
4. `[StorageLayer]` known interactions load
5. `[PIPELINE BUILD]` mode + skip flags echo
6. `[PROBE]` Gemini model verification
7. `[ITER N/M]` discovery iterations
8. `[PARALLEL:function_mapping]` function mapping batched
9. `[DEPTH-CHECK]` depth re-dispatch decision
10. `[PARALLEL:function_mapping_depth_expand]` depth re-dispatch
11. `[DEDUP]` local dedup
12. `[INDIRECT]` indirect interactor count
13. `[CHAIN RESOLUTION]` Track A + B
14. `[CHAIN:promote]` newly promoted interactors
15. `[PARALLEL:function_mapping_chain_promoted]` map functions for promoted
16. `[CHAIN]` reconciliation logs
17. `[PARALLEL:ax_claim_generation_explicit]` chain claim gen (explicit)
18. `[PARALLEL:az_claim_generation_hidden]` chain claim gen (hidden)
19. `[PARALLEL:..._depth_expand]` chain claim depth re-dispatch (per phase)
20. `[ARROW DETERMINATION]` fast heuristic
21. `[DB SYNC]` / `[CHAIN]` / `[LOCUS ROUTER]` (chain resolution audit logs)
22. `POST-PROCESSING [N/M]` post-processor stages
23. `[ARROW TIER1]` arrow validation tier-1 short-circuit
24. `[DIAGNOSTICS]` diagnostics file written
25. `[DB SYNC]` / etc. (storage write — POST-FIX, only one emission)
26. `[STORAGE]` final write summary
27. `>> Step 1: Init Roots` etc. (pathway pipeline)
28. `>> Verification PASSED/FAILED`
29. `[DIAGNOSTICS] Pathway drift surface: ...` (this session)
30. `QUICK ASSIGN PASSED in Xs`
31. `[REQUEST METRICS] ...`

## Marker reference

### Database init

```
[DATABASE] Initializing PostgreSQL connection...
[DATABASE] URL: postgresql://***@66.33.22.253:51981/railway
[DATABASE] [OK] Connection verified
[DATABASE] [OK] Tables initialized
[DATABASE]   • Proteins table: N entries
```
Source: `app.py` startup. If DB count differs between pre-pipeline and post-pipeline banners, the pipeline wrote rows.

### Route header

```
[ROUTE /api/query] skip flags — normalize_function_contexts=False ... finalize_metadata=False ... direct_links=False.
[ROUTE /api/query] skip_citation_verification=True (source: env override ('true'))
[ROUTE /api/query] post-processor flags — skip_validation=False ... skip_deduplicator=False ... skip_arrow_determination=False
```
Source: `routes/query.py`. The third line was added this session — surfaces previously-silent stale localStorage flags.

### Progress

```
[PROGRESS] Total steps calculated: 21 (pipeline: 10, post: 11)
```
Source: `routes/query.py`. If predicted vs actual stage count differs, a stage was silently skipped at runtime.

### Step logging

```
📁 Step logging enabled: Logs/REST/20260503_024323
```
Source: `utils/step_logger.py`. Per-step JSON files written under that path for every LLM call.

### History load

```
[StorageLayer] No known interactions found for REST - first query
```
or
```
[DB] History loaded: N known interactions
```
Source: `utils/storage.py`. Pipeline excludes already-known interactions to avoid re-discovery.

### Pipeline build

```
[PIPELINE BUILD] mode=iterative skip_citation_verification=True skip_arrow_determination=False
   [PROBE] Known Vertex model gemini-3-flash-preview — using generate_content (no probe delay)
```
Source: `pipeline/config_dynamic.py` + `runner.py`. Confirms which mode and which model.

### Discovery iterations

```
[ITER 1/1] broad_discovery: Cast a wide net for all known protein interactors
[ITER 1] (generate_content) Total interactors: 20
```
Source: `runner.py` iterative-research dispatch. `(generate_content)` vs `(interactions)` indicates the API mode used.

### Parallel batched phases

```
======================================================================
[PARALLEL:function_mapping] 20 targets → 5 batch(es) (~4/batch, 5 workers)
[PARALLEL:function_mapping] Targets: ['RCOR1', 'SIN3A', ...]
======================================================================
[PARALLEL:function_mapping] Rolling dispatch enabled — 5 batch(es), concurrency=5.
[PARALLEL:function_mapping] Batch K/M done — [...] (X/M complete)
[PARALLEL:function_mapping] Completed — M/M batches succeeded
```
Source: `runner.py:_run_parallel_batched_phase` (line 3129+).

Variants:
- `[PARALLEL:function_mapping]` — initial round
- `[PARALLEL:function_mapping_depth_expand]` — depth re-dispatch (capped at 1)
- `[PARALLEL:function_mapping_chain_promoted]` — re-fire for chain-promoted interactors
- `[PARALLEL:ax_claim_generation_explicit]` — chain claim gen for explicit chains
- `[PARALLEL:az_claim_generation_hidden]` — chain claim gen for hidden chains
- `[PARALLEL:ax_claim_generation_explicit_depth_expand]` — chain claim depth re-dispatch
- `[PARALLEL:ax_claim_generation_explicit_missing_recovery]` — recovery for hops with no attached claims
- `[PARALLEL:citation_verification]` — citation verification

Failures:
```
[PARALLEL:ax_claim_generation_explicit] Batch K/M failed: No text in response (finish_reason=FinishReason.MAX_TOKENS)
```
→ `MAX_TOKENS` truncation. Currently happening 27-60% of the time due to the wrong cap. See A1.

```
Batch K/M failed: Non-retryable request/config error for gemini-3-flash-preview: 400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': 'The answer candidate length is too long with N tokens, which exceeds the maximum token limit of M.'}
```
→ Model produced N > our requested M. Same root cause as MAX_TOKENS but the server caught it before streaming.

```
[PARALLEL:ax_claim_generation_explicit] Retrying N failed interactors (attempt 1/1, wait 1s)...
```
→ Retry pass. Capped at 1 attempt by default.

```
[PARALLEL:ax_claim_generation_explicit] Batch X truncated (Y unclosed, Z items) — recovered via repair_truncated_json (no retry dispatched).
```
→ Truncated JSON recovered by `utils/json_helpers.py:repair_truncated_json`. Partial output saved.

```
[PARALLEL:ax_claim_generation_explicit] Attached chain claims for X/Y requested pair(s)
```
→ X out of Y pairs in the response had claims successfully attached to interactors. **Pre-fix, az_depth_expand was hitting 0/1 due to the canonical_pair_key casing bug. Post-fix this session, should be 1/1.**

```
[PARALLEL:ax_claim_generation_explicit] N interactors failed after all retries: ['MAPK1->ERK2', ...]
```
→ Hard failures. These pairs will have NO biology in the chain. Surfaces in `_diagnostics.chain_pair_unrecoverable`.

```
[PARALLEL:ax_claim_generation_explicit] Rolling dispatch waiting on N active batch(es) after Xs: [...]; M/N submitted
```
→ Throttle status (TPM gate or worker saturation).

### Depth-check

```
[DEPTH-CHECK] N/M function(s) flagged (K violations) — pass_rate=NN.N%
[DEPTH-CHECK] Re-dispatching N shallow interactor(s) with batch_size=1 for expansion: ['BTRC', 'USP7', ...]
```
Source: `runner.py`. Capped at one redispatch.

### Local dedup

```
[DEDUP] Removed N duplicate function(s) locally (exact + fuzzy + mechanism-overlap)
```
Source: `utils/dedup_local.py`.

### Indirect

```
[INDIRECT] N indirect interactors, M with chain data
```
Source: `runner.py`. M < N means some indirects haven't yet had chain extraction run.

### Chain resolution

```
======================================================================
[CHAIN RESOLUTION] Starting (Track A + Track B in parallel)
======================================================================
[TRACK-A:2ab] Batch 1: N indirects (...)
[CHAIN:step2ab_chain_determination] Running...
[CHAIN:step2ab_chain_determination] Complete.
[TRACK-A:2ab5-code] Extracted pairs from N chains, LLM comparison needed: True
[CHAIN:step2ab5_extract_pairs_explicit] Running...
[TRACK-B:2ab2-code] Found N candidate claims with non-pair proteins
[CHAIN:step2ab2_hidden_indirect_detection] Running...
[CHAIN:step2ab2_hidden_indirect_detection] Complete.
[TRACK-B:2ab2-llm] Confirmed N hidden indirect candidates
[CHAIN:step2ab3_hidden_chain_determination] Running...
[CHAIN:step2ab3_hidden_chain_determination] Complete.
[TRACK-B:2ab4-code] Extracted pairs from N hidden chains
[CHAIN:promote] Promoted N new interactors: [...]
[CHAIN RESOLUTION] Complete. Explicit chains: N, Hidden chains: M. New interactors promoted: K.
```
Source: `runner.py:_run_chain_resolution_phase` and friends.

### Reconciliation

```
[CHAIN] Reconciled N indirect interactor(s) from canonical full_chain.
[CHAIN] Zero-skip: returning all N hop pair(s) for fresh claim generation in step2ax_claim_generation_explicit
```
Source: `runner.py`. "Zero-skip" means no pairs were skipped due to existing chain_link_functions; all are sent to LLM.

### Chain depth expand

```
[PARALLEL:step2ax_claim_generation_explicit] Chain depth-expand for N shallow hop(s): [...]
```
Source: `runner.py`. Re-runs the LLM for shallow hops (capped at one redispatch per pair).

### Arrow determination heuristic

```
[ARROW DETERMINATION] Applying fast heuristic...
[ARROW DETERMINATION] Heuristic applied
```
Source: `runner.py:7445-7480`. Code-only majority-vote of function arrows. NOT the LLM arrow validation stage.

### Pre-storage chain audit (only once now, post-fix)

```
[DB SYNC] Preserving direct interaction: SQSTM1↔TDP43 (refusing downgrade to indirect)
[CHAIN] Using LLM-emitted chain as-is (CFTR → U2AF65 → HNRNPA1) even though it lacks query=TDP43 — no query-at-head override.
[LOCUS ROUTER] KPNA1->KPNB1 (chain KPNA1 → KPNB1 → TDP43): kept=1 rerouted=1 dropped=0
  [REROUTED → parent] Importin-Alpha/Beta Heterodimerization (mentions-query-and-hop, mentioned=['KPNA1', 'KPNB1', 'TDP43'])
```
Source: `utils/db_sync.py:sync_chain_relationships`. **Pre-fix this block emitted twice; post-fix only once.**

`Preserving direct interaction` = chain-detected interaction tried to overwrite an existing direct row → refused.
`Using LLM-emitted chain as-is` = chain doesn't have query at head → kept as-is per LLM.
`[LOCUS ROUTER] X->Y (chain ...): kept=K rerouted=R dropped=D` = locus router results for that hop.

### Post-processing

```
────────────────────────────────────────────────────────────
  POST-PROCESSING [N/M]: <Stage label>...
  (stage: <stage_name>, kind: PURE | LLM)
────────────────────────────────────────────────────────────
  [OK] <stage_name> complete
```
Source: `utils/post_processor.py:run`.

Failures:
```
[WARN] Stage 'X' transient error (attempt N/4), retrying in Ys: ...
[ERROR] Stage 'X' permanent error (no retry): ...
[ERROR] Stage 'X' failed: ...
[ABORT] Critical stage 'X' failed — aborting post-processing to prevent corrupted data
```
**`[ABORT]` only fires for `arrow_validation`** (the only critical stage). Aborts all subsequent post-processing.

### Tier-1 arrow short-circuit (this session updated)

```
[ARROW TIER1] DB pre-flight: 40/40 interactor(s) already validated in DB — skipping LLM for those.
[ARROW VALIDATION] All interactors hit Tier-1 DB — skipping LLM, running deterministic normalization on cached arrows.
  [STAGE 2] Extracting direct mediator links...
  [DIRECT LINKS] Processing N indirect pairs (Tier2 budget=K/L, Tier3-only=M)
  [DIRECT LINKS] Budget exhausted — N pairs will skip Tier 2 nested-pipeline and use Tier 3 evidence-only.
    [TIER1-DB] X -> Y: found in DB (N claims)
  [DIRECT LINKS] Tier 1 (DB): N/M found, 0 need Tier 2/3
  [DIRECT LINKS] Extracted N / M links (Tier1=N)
  [OK] arrow_validation complete
```
Source: `utils/arrow_effect_validator.py:validate_arrows_for_payload`. **Post-fix, when all interactors are Tier-1 hits, the deterministic normalization still runs (`_apply_tier1_normalization_to_payload`) so new chain claims get `arrow_context`.**

### Diagnostics write

```
[DIAGNOSTICS] Wrote Logs/TDP43/pipeline_diagnostics.json (dropped=0, unrecoverable=0, incomplete_chains=6)
```
Source: `runner.py:7942`. Numbers in parens come from the diagnostics dict.

### Storage

```
============================================================
[STORAGE] protein=TDP43 db_synced=True file_cached=True created=0 updated=61
============================================================
```
Source: `utils/storage.py`. `created=N`/`updated=M` count Interaction rows.

`db_synced=False` means all 3 retries failed. Check the preceding `[ERROR]` lines.

### Pathway pipeline

```
================================================================================
RUNNING QUICK PATHWAY ASSIGNMENT
================================================================================
>> Step 1: Init Roots...
   done (8.5s)
>> Quick Assign Pathways...
[INFO] scripts.pathway_v2.quick_assign: Created/retrieved hierarchy context cache: ...
[INFO] scripts.pathway_v2.quick_assign: Quick assign claims: N claims need pathway assignment
[INFO] scripts.pathway_v2.llm_utils: LLM JSON accepted (model=..., len=N, root=<none>, items=0)
[INFO] scripts.pathway_v2.quick_assign:   [PATHWAY CONSISTENCY] Unified N claim(s) where same protein+function had different pathways
[INFO] scripts.pathway_v2.quick_assign:   [CHAIN CONSISTENCY] Unified N claim(s) so every link in each chain shares the chain's dominant pathway
[INFO] scripts.pathway_v2.quick_assign:   [CHAIN UNIFY] Unified N chain claim(s) across M chain(s) to one pathway each
[INFO] scripts.pathway_v2.quick_assign:   Synced PathwayInteraction: +N created, -M stale removed (K total pairs)
[INFO] scripts.pathway_v2.quick_assign:   Synced step3_finalized_pathway on N interaction(s)
[INFO] scripts.pathway_v2.quick_assign: Quick assign claims complete: A matched existing, B created new, C repeat-new (chain mates), D failed (out of N total; sum=N)
   done (Xs)
>> Ontology Enrichment...
   done (Xs)
>> Step 7: Verify Pipeline (scoped)...
[INFO] scripts.pathway_v2.verify_pipeline: STEP 7: PATHWAY VERIFICATION
[INFO] scripts.pathway_v2.verify_pipeline: Mode: Auto-Fix (scoped to N interactions)
[INFO] scripts.pathway_v2.verify_pipeline: Running verification checks...
[INFO] scripts.pathway_v2.step7_checks: Running interaction checks...
...
[INFO] scripts.pathway_v2.verify_pipeline: Report saved to: ...
[INFO] scripts.pathway_v2.verify_pipeline: VERIFICATION PASSED - Data is ready for production
   done (Xs)
================================================================================
QUICK ASSIGN PASSED in 67.3s (4 steps)
================================================================================

>> Verification PASSED (0 auto-fixes applied)
```
Source: `scripts/pathway_v2/run_pipeline.py` + `verify_pipeline.py`.

### Pathway drift surface (this session)

```
[PATHWAY DRIFT CORRECTED] interaction_id=X function='Y': reassigning 'A' (score N) → 'B' (score M) via write-time prose keyword analysis.
[PATHWAY DRIFT WRITE-TIME] interaction_id=X function='Y': drift detected (assigned='A' score=N, implied='B' score=M) but implied pathway not in DB; keeping proposed.
[DIAGNOSTICS] Pathway drift surface: K corrected, L report-only → written to Logs/TDP43/pipeline_diagnostics.json
```
Source: `scripts/pathway_v2/quick_assign.py:_check_pathway_drift_at_write` + `runner.py`.

Read-time drift surface (legacy, still emitted by `services/data_builder.py:945-973`):
```
[PATHWAY DRIFT] VCP: 1/2 function(s) have pathway assignment disagreeing with prose keywords — report-only (PATHWAY_AUTO_CORRECT=false).
  - assigned='Protein Quality Control' (score=8) top='ERAD' (score=22)
```

### Cleanup

```
[CLEANUP] Filtered N zero-function interactor(s) before save
[CLEANUP] Removed N duplicate function(s) locally
```
Source: `runner.py` + `utils/dedup_local.py`.

### Snapshot recovery

```
[SNAPSHOT-RECOVERY] N interactor(s) with zero functions
```
Source: `runner.py`. Pre-snapshot cleanup. If chain-only interactors had no functions but had `chain_link_functions`, they're kept; orphans dropped.

### Request metrics

```
[REQUEST METRICS] core_calls_3pro=N | evidence_calls_2_5pro=N | arrow_llm_calls=N | arrow_tier1_hits=N | arrow_fallback_to_pro=N | quota_skipped_calls=N
```
Source: `runner.py` end-of-run. Useful for diagnosing whether LLM was called or DB cache served.

### Doubled `[DB SYNC]` (PRE-FIX symptom — should NOT appear post-fix)

If you see the entire `[DB SYNC] Preserving direct interaction` + `[CHAIN] Using LLM-emitted chain as-is` + `[LOCUS ROUTER]` block emitted **twice** in one run (once before post-processing, once after), the `save_checkpoint` regression is back. The fix in `utils/storage.py:152-189` should keep this from recurring.

### Werkzeug request log (Flask debug server)

```
2026-05-03 02:43:23,668 [INFO] werkzeug: 127.0.0.1 - - [03/May/2026 02:43:23] "POST /api/query HTTP/1.1" 200 -
2026-05-03 02:43:23,685 [INFO] werkzeug: 127.0.0.1 - - [03/May/2026 02:43:23] "GET /api/stream/REST HTTP/1.1" 200 -
```
Source: Flask debug server. Just request log; ignore unless investigating routing.

## Reading order when a log arrives

1. **Search for `[ABORT]`, `[ERROR]`, `MAX_TOKENS`, `INVALID_ARGUMENT`, `db_synced=False`, `Verification FAILED`.** Critical signals.
2. **Check the `[REQUEST METRICS]` line** at the end. Tells you whether LLM was called heavily or DB cache served.
3. **Check `[STORAGE]`** to confirm db_synced=True.
4. **Check `[DIAGNOSTICS] Wrote ...`** for the diagnostics file path. Open `Logs/<protein>/pipeline_diagnostics.json` to see structured details.
5. **Look for `Attached chain claims for X/Y requested pair(s)`** with X<Y — these are the pairs that lost biology.
6. **Look for `[CHAIN AUDIT]`** lines — flag chain incomplete hops.
7. **Confirm post-processing stage count** matches `[PROGRESS] Total steps calculated: ...post: N`. Mismatch = silent skip.

## Common pathological log patterns

| Pattern | Cause | Fix |
|---------|-------|-----|
| Many `Batch X failed: No text in response (finish_reason=FinishReason.MAX_TOKENS)` | `max_output_tokens` cap too low for current output volume | A1 fix landed (24000); if still happening on next run, bump CHAIN_CLAIM_MAX_OUTPUT_TOKENS to 32768 or 65536 |
| `[PARALLEL:az_claim_generation_hidden_depth_expand] Attached chain claims for 0/1 requested pair(s)` repeatedly | `canonical_pair_key` casing mismatch | FIXED this session |
| `[PATHWAY DRIFT] VCP: ... assigned='Protein Quality Control' (score=8) top='ERAD' (score=22)` | Read-time drift detection but no write-time correction | FIXED this session — drift correction at write |
| `[DB SYNC] Preserving direct interaction:` block emitted twice | `save_checkpoint` doing eager DB sync | FIXED this session |
| `[POST-PROCESSING [10/11]]` (predicted 11, only 10 ran) | `skip_validation=True` from stale localStorage | FIXED this session — visible in route log now |
| `[PARALLEL:ax_claim_generation_explicit] N interactors failed after all retries` | Hard MAX_TOKENS failures even after retry | A1 fix raising max_output |
