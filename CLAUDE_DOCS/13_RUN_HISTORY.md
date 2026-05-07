# Pipeline Run History

Timeline of pipeline runs the user has done in the prior session(s) and what each revealed. Use this to understand which symptoms led to which fixes — and what's still unfixed.

## Run 1: TDP43 (initial, before any fixes this session)

**Date:** 2026-05-03 ~01:12

**Configuration:** iterative mode, skip_citation_verification=True (env), all other post-processing on.

**DB state at start:** 69 proteins, 100 interactions, 126 claims (carried over from prior queries).

**Outcome:**
- 25 interactors discovered.
- Function mapping completed in 1+1 redispatch passes; 22/27 functions still flagged for shallow depth (pass_rate=18.5%).
- 4 indirect interactors → chain resolution → 7 new interactors promoted.
- Chain claim gen on 32 pairs at batch_size=2: **2 batches truncated (12.5%)** with `MAX_TOKENS`. Recovered via split-retry.
- **Chain depth-expand: 4/4 hops returned `Attached chain claims for 0/1 requested pair(s)`** — no biology attached for any retried hop.
- Post-processing: 10 stages ran (out of predicted 11; evidence_validation silently skipped).
- Arrow validation: **40/40 Tier-1 hits → LLM Stage 1 ENTIRELY SKIPPED → `apply_corrections` never ran**. New chain claims emitted in this run had no `arrow_context`/`function_effect`/`_arrow_validated`.
- DB sync: 0 new interactions, 61 updated. **`[DB SYNC] Preserving direct...` block emitted TWICE**.
- Diagnostics: 6 incomplete chain hops flagged.
- Pathway pipeline: 27 matched existing, 1 created new, 3 chain-mate repeats. Verification PASSED.
- **Pathway drift detected for VCP**: assigned 'Protein Quality Control' (score 8) but prose favored 'ERAD' (score 22). Report-only — no auto-correction.
- Request metrics: `core_calls_3pro=0 evidence_calls_2_5pro=0 arrow_llm_calls=0 arrow_tier1_hits=40 arrow_fallback_to_pro=0 quota_skipped_calls=0`.

**Symptoms identified:**
1. `_canon_pair_key` casing bug → az_depth_expand 0/1 attached
2. Tier-1 skip-all → no `apply_corrections` for new claims
3. Doubled `[DB SYNC]` logs → wasted DB writes
4. Silent evidence_validation skip
5. VCP pathway drift not corrected at write time
6. MAX_TOKENS truncation in chain claim gen

**This led to all 6 Phase 1 backend fixes (see `09_FIXES_HISTORY.md`).**

## Run 2: TDP43 (post-fix verification — never actually run)

After implementing Phase 1 + 2 + 3 fixes, the user did NOT immediately re-run TDP43. Instead they ran a different protein (REST) to test under different conditions.

## Run 3: REST (after Phase 1+2+3 fixes)

**Date:** 2026-05-03 ~02:43

**Configuration:** iterative mode, skip_citation_verification=True, all other post-processing on. CHAIN_CLAIM_BATCH_SIZE=1 (from prior fix).

**DB state at start:** 101 proteins, 156 interactions, 191 claims (after TDP43 + ATXN3 from earlier queries).

**Outcome:**
- 20 interactors discovered.
- Function mapping completed; 7/21 flagged shallow (pass_rate=66.7% — better than TDP43's 18.5%).
- 4 indirect interactors → chain resolution → 15 new interactors promoted (more than TDP43 because REST has dense epigenetic / E3 ligase networks).
- Chain claim gen on **40 pairs at batch_size=1**:
  - **First pass: 11/40 batches truncated (27.5%)** with `MAX_TOKENS`. ← STILL HAPPENING despite batch_size=1.
  - **1 hard 400 INVALID_ARGUMENT**: `"answer candidate length is too long with 8197 tokens, which exceeds the maximum token limit of 8192."` ← This is the smoking gun proving the cap is OUR setting, not the server's.
  - Retry pass: 5 still failed (`MAPK1->ERK2`, `GSK3B->AXIN1`, `GSK3B->BTRC`, `BTRC->REST`, `REST->CREBBP`).
  - Missing-recovery pass: 4/4 succeeded (with truncated-JSON repair).
  - Depth-expand pass on 13 shallow hops: **8/13 truncated (61.5%)**.
- The user pasted the log and said: "look at all these errors! all these truncations etc.! wtf?!! output tokens all fucked".

**Diagnosis:**

I (prior session) initially proposed switching chain claim to Pro 3 thinking 8192 was the Flash hard cap. **The user pushed back: "is 8192 seriously the max output? please check as far as I'm aware it is 65k".**

Verification (this session, in plan mode):
- Vertex AI official docs: "Maximum output tokens: 65,536" for `gemini-3-flash-preview`.
- The 400 error message says "exceeds the maximum token limit of **8192**" — that's the value WE set in `max_output_tokens=8192`. Server is enforcing OUR cap.
- The codebase's own `gemini_runtime.py:28` has `DEFAULT_MAX_OUTPUT_TOKENS = 65536`. Only `CHAIN_CLAIM_MAX_OUTPUT_TOKENS=8192` was wrongly set.

**Conclusion:** the .env comment author misdiagnosed; the cap is 65,536. Fix is to raise `CHAIN_CLAIM_MAX_OUTPUT_TOKENS=8192 → 65536`. **A1 in `10_OPEN_ISSUES.md`**.

**This run revealed:**
1. batch_size=1 alone wasn't enough — even single-pair chain claims overflow 8192.
2. The wrong-cap diagnosis from the prior session needed correction.
3. Chain claim phase wall-clock was bloated by retry+missing-recovery+depth-expand cascade — all caused by truncation. Fixing A1 should drop chain-claim phase ~30-40%.

**Status:** A1 fix designed and approved by user. Not yet implemented (user paused to create this handoff package).

## A1 fix landed — 2026-05-03 (between Run 3 and Run 4)

**Decision:** `CHAIN_CLAIM_MAX_OUTPUT_TOKENS = 24000` (not 12336, not 65536).

**Path:**
- User initially proposed `12336` based on "8K was solid but truncated for lots".
- I (prior session) initially recommended `65536` (the model max).
- User pushed back: "I don't want the model to use more tokens".
- Compromise: 24000 — 3× the 8K baseline, low enough to limit thinking-budget overhead even if proportional, high enough to handle every realistic case.

**Edits made:**
1. `.env CHAIN_CLAIM_MAX_OUTPUT_TOKENS=10000 → 24000` + replaced misleading "8192 server cap" comment.
2. `utils/arrow_effect_validator.py:549` `8192/16384` → `12336/24000`.
3. `pipeline/prompts/modern_steps.py:418` `8192` → `24000`.
4. `utils/gemini_runtime.py:447-470` rewrote docstring (real cap is 65,536; thinking-budget interaction unverified).
5. `.env CHAIN_CLAIM_BATCH_SIZE` left at 1 (24K isn't enough headroom for 2-pair packing).

**676 pytest tests passing.** No regression.

## Run 4: ??? (next run, post-A1)

When the user re-runs (likely REST or TDP43), expect:

- **0 `MAX_TOKENS` errors** in chain claim batches.
- **0 `INVALID_ARGUMENT 400` errors**.
- **No depth-expand recovery cascade** for truncation reasons.
- Chain claim phase wall-clock ~30-40% faster.
- `chain_pair_unrecoverable` count → 0 in diagnostics.
- `chain_incomplete_hops` count → 0 in diagnostics.

If Run 4 still shows truncation in densely-studied cases (REST cofactors):
- Bump `CHAIN_CLAIM_MAX_OUTPUT_TOKENS` to `32768` (next power-of-2).
- If still truncating: `65536` (model max).

If Run 4 looks slow per-call (suggesting thinking-budget DID scale):
- Drop to `16384`.
- Or drop `CHAIN_CLAIM_THINKING_LEVEL=low` to `off`.

**With A1 cleared, A2-A4 (chain DAG rendering) is the active worklist.** See `11_CHAIN_TOPOLOGY.md` and `10_OPEN_ISSUES.md` Priority B.

## Patterns across runs

### What's consistently working
- Discovery → 20-25 interactors (right ballpark).
- Function mapping → high pass rate after redispatch (60-70% common).
- Chain resolution → finds explicit and hidden chains; promotes 5-15 new interactors.
- DB sync → no rejected interactors that shouldn't be.
- Pathway verification → 7/7 checks passing.
- Quick-assign → matches >90% to existing pathways.

### What's consistently failing
- Chain claim gen truncation (will be fixed by A1).
- VCP pathway drift not corrected at write time (FIXED this session in P3.1).
- Some indirect interactors lack chain biology even after recovery passes (will improve dramatically post-A1).

### What's unknown until A1 lands
- Whether the chain rendering DAG issue (A2-A4) is fundamental or whether it's just a data-quality problem that A1 + the prior session's `canonical_pair_key` fix together actually solve.

## Per-protein observations

### TDP43 (Aryan's recurring example)
- Dense biology around stress granules, FUS, HNRNPA1, autophagy.
- ~25 direct interactors typical.
- ~4 indirect cascades typical (TBK1-IRF7 antiviral, VCP-LAMP2 autophagy, KEAP1-NRF2-SQSTM1, KPNA1-KPNB1 nuclear import).
- Pseudo entities: RNA, Stress Granules, Ubiquitin (UBB).

### ATXN3 (Aryan's main example for testing chain DAG)
- Heavy DUB-substrate biology (ATXN3 deubiquitinates many targets).
- VCP cascade (ATXN3 binds VCP on K48-poly-Ub substrates).
- HSP90-STUB1 chain (this is where the STUB1 inversion shows up).
- Connection to autophagy via VCP-LAMP2.
- HDAC6 has BOTH a direct claim AND chain participation under autophagy/PQC pathway — the user's HDAC6 case.

### REST (Aryan's high-density-biology test case)
- Transcriptional repression complex (SIN3A, HDAC1, HDAC2, RCOR1, KDM1A, SUV39H1, SAP30, SDS3).
- E3 ligase regulation (BTRC, FBXW7, SKP1-CUL1).
- Chaperone regulation (HSP70-HSP90AA1-CDC37-STUB1).
- Heavily studied → produces longest chain claim outputs → exposes MAX_TOKENS issues most.
- This is why Run 3 had 27.5% truncation while TDP43 had only 12.5%.

## Things to watch for in future runs

| Symptom | Likely cause | Where to look |
|---------|-------------|---------------|
| `Attached chain claims for 0/1 requested pair(s)` | Casing drift (FIXED) or attachment logic regression | `_attach_chain_claim_records` in runner.py:1779-1948 |
| `[DB SYNC] Preserving direct…` block twice | save_checkpoint regression | `utils/storage.py:152-189` |
| Stage count mismatch (predicted N, ran M) | Silent skip flag | Route's flag log |
| `[PATHWAY DRIFT CORRECTED]` not appearing | Drift detection not running | `_check_pathway_drift_at_write` in quick_assign.py |
| Chain claim phase >15min wall-clock | Truncation+retry cascade | `[PARALLEL:ax_*]` truncation count |
| `quota_skipped_calls > 0` | Vertex daily quota near exhaustion | Wait or use alternate model |
| Verification FAILED | Schema/chain consistency issue | `Logs/verification_reports/<timestamp>.txt` |

## How to interpret a new pipeline log

1. Find the `[PROGRESS] Total steps calculated:` line. Note predicted post count.
2. Find the `[REQUEST METRICS]` line. Note `arrow_tier1_hits`, `arrow_llm_calls`. If both 0, check why no LLM ran. If `arrow_tier1_hits >= total interactors`, normalization should still have run (post-fix).
3. Search for `MAX_TOKENS` and `INVALID_ARGUMENT`. Count occurrences.
4. Search for `Attached chain claims for X/Y` — find any X<Y.
5. Read `[DIAGNOSTICS]` line for incomplete chain count.
6. Check `[STORAGE]` line for `db_synced=True/False`.
7. Read `Logs/<protein>/pipeline_diagnostics.json` for the structured details.
8. Read `Logs/<protein>/quality_report.json` for depth pass rate.
