# Fixes Already Landed (Prior Session, 2026-05-03)

All of the below are committed-to-disk, tested (676 pytest tests passing), and live. Don't re-do them. Don't undo them. Reference here so a fresh session knows the state.

## Phase 1 — Backend root causes

### 1.1 `canonical_pair_key` case-insensitivity ✅ DONE

**Problem:** `_canon_pair_key("VCP","TFEB")` and `_canon_pair_key("vcp","tfeb")` produced two DIFFERENT keys (`"TFEB|VCP"` vs `"tfeb|vcp"`) because the function sorted by case-insensitive comparison but emitted whichever casing arrived first. `_attach_chain_claim_records` lookups missed these casing-drifted pairs. Concrete symptom in TDP43 log: `[PARALLEL:az_claim_generation_hidden_depth_expand] Attached chain claims for 0/1 requested pair(s)` for ALL 4 retried hops.

**Fix:** `utils/chain_resolution.py:46-69` — uppercase BOTH endpoints before sorting:

```python
def canonical_pair_key(a: str, b: str) -> str:
    if not a or not b:
        return f"{a or ''}{_PAIR_SEPARATOR}{b or ''}"
    lo, hi = sorted([str(a).strip().upper(), str(b).strip().upper()])
    return f"{lo}{_PAIR_SEPARATOR}{hi}"
```

**Why this fix in particular:** `canonical_pair_key` is the single source of truth for pair keys (used by `runner.py`, `db_sync.py`, `storage.py`, `data_builder.py`, `post_processor.py`). One fix at the source eliminates the casing drift everywhere.

**Expected effect on next run:** `Attached chain claims for X/Y` should be 1/1 for all retried hops; `chain_pair_unrecoverable` count should drop.

### 1.2 Doubled `[DB SYNC]` log block ✅ DONE

**Problem:** `runner.py:7826` called `storage.save_checkpoint(user_query, pipeline_payload, "pipeline_complete")` AFTER the main pipeline but BEFORE post-processing. Then `storage.save_pipeline_results(...)` at line 7939 called it AGAIN after post-processing. Both invoked `_sync_to_db_with_retry → DatabaseSyncLayer.sync_query_results → sync_chain_relationships`. Result: every `[DB SYNC] Preserving direct interaction` / `[CHAIN] Using LLM-emitted chain as-is` / `[LOCUS ROUTER]` log emitted twice. Also doubled the actual DB write cost.

**Fix:** `utils/storage.py:152-189` — `save_checkpoint` is now metadata-only:

```python
def save_checkpoint(self, protein_symbol, payload, phase_name):
    """Mark pipeline phase progress on the Protein row.

    2026-05-03: stripped of its eager full-DB-sync side effect.
    [...long comment explaining why...]
    """
    if not self._has_db():
        return
    with self._db_context():
        from models import Protein, db
        protein = Protein.query.filter_by(symbol=protein_symbol).first()
        if protein:
            protein.pipeline_status = "running"
            protein.last_pipeline_phase = phase_name
            db.session.commit()
```

**Why metadata-only is safe:** the eager sync pretended to be "crash recovery" but couldn't actually recover anything — the payload only stabilizes after post-processing, so a crash before `save_pipeline_results` would leave the DB with arrows derived from un-validated arrows anyway. Re-running the query is the actual recovery path.

**Expected effect on next run:** `[DB SYNC]`/`[CHAIN]`/`[LOCUS ROUTER]` block emits ONCE only (after post-processing).

### 1.3 Tier-1 arrow validation skip-all ✅ DONE

**Problem:** When all interactors for a query were already in DB (re-run scenario), `_preflight_tier1_arrows` correctly identified them all as Tier-1 hits and `validate_arrows_for_payload` skipped LLM Stage 1 entirely. **But it also skipped `apply_corrections`**, which is the function that populates `arrow_context` (dual `net_arrow`/`direct_arrow`), `function_effect`, `_arrow_validated=True`, etc. for new chain-derived claims emitted in the run. Result: new chain claims went straight to DB / frontend without these fields.

**Fix:** `utils/arrow_effect_validator.py:1711-1845` — added `_apply_tier1_normalization_to_payload` that runs `apply_corrections({})` (empty corrections — so only the deterministic auto-generation steps execute) on every Tier-1-hit interactor, even when the LLM stage is fully skipped.

```python
def _apply_tier1_normalization_to_payload(payload, tier1_indices, verbose=False):
    """Run deterministic apply_corrections({}) on Tier-1-hit interactors.
    [...]
    """
    if not isinstance(payload, dict) or not tier1_indices:
        return
    snapshot = payload.get("snapshot_json", payload) or {}
    interactors = snapshot.get("interactors") or []
    if not interactors:
        return
    main_protein = (
        snapshot.get("main")
        or (payload.get("ctx_json", {}) or {}).get("main")
        or ""
    )
    for idx in tier1_indices:
        if 0 <= idx < len(interactors) and isinstance(interactors[idx], dict):
            try:
                apply_corrections(interactors[idx], {}, main_protein, verbose=False)
            except Exception as exc:
                print(f"[ARROW TIER1] normalization failed for ...", flush=True)
```

Wired in `validate_arrows_for_payload` so it runs whether some, none, or all interactors were Tier-1.

**Expected effect:** new chain claims get `arrow_context`, `function_effect`, `_arrow_validated=True` populated even on re-runs.

### 1.4 P3.1 write-time pathway drift detection ✅ DONE

**Problem:** Pathway content validator was already running at READ time (`services/data_builder.py:945-973`) but with `PATHWAY_AUTO_CORRECT=false` — drifts logged but never persisted. The DB read kept returning the drifted assignment until a future quick_assign run happened to choose differently. Concrete symptom: `[PATHWAY DRIFT] VCP: 1/2 function(s) have pathway assignment disagreeing with prose keywords — assigned='Protein Quality Control' (score=8) top='ERAD' (score=22).` Drift never corrected.

**Fix:** `scripts/pathway_v2/quick_assign.py` — added `_check_pathway_drift_at_write(claim, proposed_pw)` invoked from `_apply_llm_pathway_to_claim` BEFORE `_assign_claim_pathway_safe`:

- Skips chain-derived claims (chain dominates).
- Builds claim-dict from the SQLAlchemy claim's prose fields.
- Calls `utils.pathway_content_validator.classify_pathway`.
- If verdict is "drift" AND implied pathway exists in DB → return implied pathway (gets assigned instead of proposed).
- Records every drift to a thread-local collector.

Also added drift collector machinery (`_begin_drift_collection`, `_end_drift_collection`, `_record_drift`) and updated `quick_assign_pathways` to flush collected drifts into its return value.

`runner.py:8001-8056` then writes the drift entries into `Logs/<protein>/pipeline_diagnostics.json`. `services/data_builder.py` already reads that file into `_diagnostics` for the frontend.

**ENV opt-out:** `PATHWAY_DRIFT_WRITE_TIME=false`.

**Expected effect on next run:** `[PATHWAY DRIFT CORRECTED]` log lines for any rehomed claim, `pathway_drifts: [...]` in diagnostics file, frontend renders "rehomed" / "drift" badges per `cv_diagnostics.applyPathwayDriftBadges`.

### 1.5 Echo skip flags in route log ✅ DONE

**Problem:** Static `routes/query.py:1-300` only echoed 4 skip flags. `skip_validation` (which gates `evidence_validation` post-processing) was read but not logged. Result: a stale localStorage `skip_validation=true` from a previous browser session would silently disable evidence_validation forever.

**Fix:** `routes/query.py:215-238` — added explicit echo of `skip_validation`, `skip_deduplicator`, `skip_arrow_determination` with their UI values:

```python
print(
    f"[ROUTE /api/query] post-processor flags — "
    f"skip_validation={skip_validation} (UI/POST {data.get('skip_validation')!r}) "
    f"[evidence_validation stage], "
    f"skip_deduplicator={skip_deduplicator} (UI/POST {data.get('skip_deduplicator')!r}) "
    f"[dedup_functions stage], "
    f"skip_arrow_determination={skip_arrow_determination} "
    f"(UI/POST {data.get('skip_arrow_determination')!r}) "
    f"[in-pipeline arrow heuristic, NOT post-processor arrow_validation].",
    file=sys.stderr, flush=True,
)
```

**Expected effect:** silent skips become visible in stderr; user can immediately tell when localStorage is stale.

### 1.6 MAX_TOKENS prevention via batch_size=1 ⚠️ SUPERSEDED — see 1.7

**What was attempted:** `.env CHAIN_CLAIM_BATCH_SIZE=2 → 1`. Reasoning: smaller batches → less per-call output → fewer truncations.

**Why it didn't work:** the actual cap is 65,536 not 8192. Truncation is caused by `CHAIN_CLAIM_MAX_OUTPUT_TOKENS=8192` being below what PhD-depth chain claims produce. **batch_size=1 didn't help — see REST run logs (8/13 truncated in depth-expand at batch_size=1).** The real fix is the cap bump in 1.7.

**Status:** the batch_size=1 stays at 1 for now (24K isn't enough to safely pack 2 pairs).

### 1.7 ✅ DONE — Bump cap suite to 24000 (the actual fix for chain-claim truncation)

**Problem:** REST run at `CHAIN_CLAIM_MAX_OUTPUT_TOKENS=8192` (later 10000) showed 27.5% first-pass truncation, 61.5% in depth-expand, 1 hard 400 INVALID_ARGUMENT (`"answer candidate length is too long with 8197 tokens, which exceeds the maximum token limit of 8192"`). The 5 unrecoverable hops (`MAPK1->ERK2`, `GSK3B->AXIN1`, `GSK3B->BTRC`, `BTRC->REST`, `REST->CREBBP`) were all REST cofactors with PhD-depth output 10K-15K tokens.

**Diagnosis:** the `8192` was a self-imposed cap in `.env`. The "Vertex Flash 3 hard cap is 8192" claim in the .env comment was wrong — verified via Vertex AI docs (real cap = 65,536).

**Fix (5 edits):**

1. `.env CHAIN_CLAIM_MAX_OUTPUT_TOKENS=10000 → 24000` (line 160) + replaced the misleading "8192 is the server cap" comment with the correct framing.
2. `utils/arrow_effect_validator.py:549`: `8192/16384` → `12336/24000` for simple/complex branches.
3. `pipeline/prompts/modern_steps.py:418` (`step2e_citation_verification`): `8192` → `24000`.
4. `utils/gemini_runtime.py:447-470` (`enforce_thinking_mode` docstring): rewrote stale "DEFAULT=8192" + "25% of output budget" claims to reflect reality (real Flash 3 cap = 65,536; thinking-budget-vs-cap interaction is not authoritatively documented; sized using `output + 3-6K thinking headroom` rule).
5. `.env CHAIN_CLAIM_BATCH_SIZE=1` left at 1 (24K isn't enough to safely pack 2 pairs at the new ceiling).

**Why 24000 specifically (not 65536, not 12336):**

- The user's intuition was that 8K was solid; they proposed 12336.
- 12336 fixes ~80% of REST cases but leaves the worst (10K-15K) still truncating.
- 65536 is the model max but bets uncertain on whether thinking-budget scales proportionally with cap. If it does, 65K means ~16K wasted thinking per call.
- 24000 is the safe middle: 3× the 8K baseline → bombproof for any realistic output (REST cofactors fit), but small enough that even under the worst proportional-thinking assumption (~25%) the thinking budget stays at ~6K.
- `max_output_tokens` is a CEILING, not a target. Setting 24K does NOT make the model emit 24K of output. It emits what the prompt + schema demand. So no risk of "encouraging more verbose output" — that was a common but wrong intuition.

**What's still NOT done in this fix (intentional):**

- `_PATHWAY_MAX_OUTPUT_TOKENS = 8192` (`scripts/pathway_v2/llm_utils.py:334`) stays — pathway responses are genuinely small (~2K tokens of JSON) and don't truncate.
- `LLM_MAX_OUTPUT_TOKENS = 1000` (`scripts/pathway_v2/quick_assign.py:67`) and the inline 2000/4000 values stay — pathway micro-classifications are deliberately tiny.
- `services/chat_service.py:564 = 8000` stays — chat replies are intentionally short.
- All sites at `65536` stay — they were already correct.

**Verification:** all 676 pytest tests pass. `[ARROW VALIDATION]` integration smoke-checked via test suite.

**Expected effect on next run:**
- 0% MAX_TOKENS finish_reason in chain claim batches.
- 0 hard 400 INVALID_ARGUMENT errors.
- `chain_pair_unrecoverable` count → 0.
- `chain_incomplete_hops` count → 0 (or near-0).
- Chain claim phase wall-clock should drop ~30-40% (no retry+missing-recovery+depth-expand cascade caused by truncation).

## Phase 2 — Vertex AI tuning

### 2.1 Batch API for Flash ✅ DONE

**Problem:** `runner.py:4096` had `use_batch_transport = (effective_request_mode == "batch" and model_name == "gemini-3.1-pro-preview")`. So Batch API was Pro-only. Function mapping and chain claim phases on Flash never reached the Batch transport, leaving 50% cost saving on the table.

**Fix:** `runner.py:4091-4117` — `_BATCH_ELIGIBLE_MODELS` set:

```python
_BATCH_ELIGIBLE_MODELS = {
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3-pro",
    "gemini-3-flash",
}
use_batch_transport = (
    effective_request_mode == "batch" and model_name in _BATCH_ELIGIBLE_MODELS
)
```

**Note:** Batch mode is not the default. Activated only via env or per-step request_mode override. So this fix is enabling-the-capability, not changing-the-default.

### 2.2 Pathway v2 max_output_tokens correction ✅ DONE

**Problem:** `scripts/pathway_v2/llm_utils.py:32` had `_PATHWAY_MAX_OUTPUT_TOKENS = 20000`. Comment claimed "60K is wasteful". But pathway v2 calls Flash, which (we now know) has a real cap of 65,536 — but pathway responses are typically only ~2K tokens. The 20000 was above what was actually needed; not a bug per se.

**Fix:** `scripts/pathway_v2/llm_utils.py:325-334` — set to 8192 with a comment clarifying it's the practical Flash response size, not the model's hard cap.

**Note:** This was based on the WRONG assumption that 8192 was the hard cap. **Re-evaluate when A1 lands.** If raising chain-claim to 65536 works, also evaluate raising this.

### 2.3-2.5 Skipped (not relevant or risky)

- 2.3 (arrow validator output budget): current 8K/16K is correct for the schema; agent's "underutilized" claim was wrong.
- 2.4 (thinking_level default flip): risky to flip globally; per-step configs already explicit.

## Phase 3 — Frontend perfection

### 3.1 Per-hop "no biology" badges ✅ DONE

**Where:** `static/cv_diagnostics.js:265-385` — `applyPartialChainBadges`.

**What:** previously applied "(partial)" only to the parent indirect interactor. Extended to ALSO walk chain hop nodes (via D3 datum `_chainId`/`_chainPosition`/`_chainProteins` triplet) and apply a per-hop "no biology" badge on the SPECIFIC missing hop.

**CSS:** `static/styles.css:1209-1273` — `.cv-hop-missing-badge` (dashed amber) + dark-mode variant.

### 3.2 Pathway-drift badges ✅ DONE

**Where:** `static/cv_diagnostics.js` — new `applyPathwayDriftBadges`.

**What:** reads `_diagnostics.pathway_drifts` (array of `{interactor, function, from, from_score, to, to_score, action}`). Applies green "rehomed" badge for `action=corrected` and amber "drift" badge for `action=report-only`. Title shows full breakdown.

**CSS:** `static/styles.css:1234-1273` — `.cv-pathway-drift-badge.corrected` (green) + `.report-only` (amber) + dark-mode variants.

**Wiring:** `static/card_view.js:_renderCardViewImpl` — calls `applyPathwayDriftBadges` after every render (deferred via setTimeout).

### 3.3 Diagnostics banner counts ✅ DONE

**Where:** `static/cv_diagnostics.js:75-235` — `renderDiagnosticsBanner`.

**What:** banner now shows "pathway rehomed: N" (green) and "pathway drift: N" (amber) when `_diagnostics.pathway_drifts` has entries. Details section enumerates each drift.

### 4.1 Multi-chain modal banners ✅ DONE

**Where:** `static/modal.js:796-906`.

**What:** modal renders ONE banner per chain entity in `L.all_chains[]`. Each banner has its own `data-chain-id`, prev/next nav, pathway name, and "chain N of M" tag. Falls back to single banner via `L._chain_entity` for older payloads.

**Test case:** an interaction in 2 chains (e.g. ATXN3↔MTOR via VCP→RHEB AND via TSC2→TSC1) now shows 2 stacked banners.

### 4.2 Multi-chain navigation scoping ✅ DONE

**Where:** `static/modal.js:177-235`.

**What:** the click handler for `[data-chain-nav]` now matches `i.chain_id == chainId` OR `i.chain_ids[]` contains chainId. Prev/next stays within the SAME chain even when the hop participates in multiple.

## Verification done after all fixes

- Python AST parse: all 8 modified files clean
- JS syntax: all 3 modified files pass `node --check`
- pytest: 676 tests passing (no regressions)
- Smoke checks:
  - `canonical_pair_key("vcp","tfeb") == "TFEB|VCP"`
  - `_apply_tier1_normalization_to_payload` callable
  - `_check_pathway_drift_at_write` + collector wired
  - `save_checkpoint` body no longer calls `_sync_to_db_with_retry`

## Phase A — Quick-assign hardening (2026-05-03 later)

### A.1 ✅ DONE — `chain_pathways[]` emission in `_chain_fields_for`

**Where:** `services/data_builder.py:85-148` (the canonical chain payload emitter).

**What:** Each chain summary in `all_chains[]` now carries its own `chain_pathways: [<distinct pathway names>]` array — every distinct pathway any of that chain's claims landed in. Plus a top-level `chain_pathways[]` union across all this row's chain memberships, used by the parent gate in `groupChainsByChainId`.

**Why this fix:** Layer 2 of `11_CHAIN_TOPOLOGY.md`. The chain-pathway gate in `static/card_view.js` was silently dropping HDAC6's chain when the chain's own `pathway_name` pointed elsewhere — even though some of the chain's claims had landed in the pathway being viewed. The widening admits the chain when ANY claim landed in the expanded pathway.

### A.2 ✅ DONE — Frontend chain pathway gate widened

**Where:** `static/card_view.js:171-262` (`groupChainsByChainId`).

**What:** Both the parent gate (line 200-204) and the per-instance gate (line 240-244) now OR-in a `claimAssignedHere` / `instClaimHere` check that uses `inter.chain_pathways[]` / `inst.chainPathways[]` from Phase A.1.

### A.3 ✅ DONE — `max_output_tokens` constants in quick_assign

**Where:** `scripts/pathway_v2/quick_assign.py:67-82`.

**What:** Renamed `LLM_MAX_OUTPUT_TOKENS` → `LLM_QUICK_CLASSIFY_MAX_OUTPUT_TOKENS=1000` (used at line 794). Added `LLM_CHAIN_BATCH_MAX_OUTPUT_TOKENS=2048` (used at the chain-group LLM call ~line 1972) and `LLM_BATCH_ASSIGN_MAX_OUTPUT_TOKENS=4096` (used at the batched standalone call ~line 2089). Replaced the inline `2000` / `4000` magic numbers.

**Why these values (not 8192 as the prior plan suggested):** Pathway-assign LLM responses are tiny — chain decision is one ~150-token JSON object; batch returns ≤ `LLM_BATCH_SIZE=8` such objects (~1.0-1.2K total). 2048/4096 leaves 10×/3× headroom; 8192 was unjustified.

### A.4 ✅ VERIFIED — drift collector flush wiring

End-to-end audit confirmed: `_check_pathway_drift_at_write` → `_record_drift` → `_end_drift_collection` → `quick_assign_pathways` `result["pathway_drifts"]` → `run_pipeline.py` wraps as `result["quick_assign"]["pathway_drifts"]` → `runner.py:8019` reads → `Logs/<protein>/pipeline_diagnostics.json` → `services/data_builder.py:1885-1889` merges → frontend `cv_diagnostics.applyPathwayDriftBadges`. All links intact.

## Phase B — Chain DAG (Layer 1 + 3 of `11_CHAIN_TOPOLOGY.md`)

### B.1 ✅ DONE — Canonical chain direction at write time (going forward only)

**Where:** new helper in `utils/chain_resolution.py` after `canonical_pair_key` (line 73+), wired into `utils/db_sync.py:sync_chain_relationships` end (~line 2531).

**What:** `canonicalize_chain_direction(chain_proteins, chain_with_arrows)` returns `(proteins, arrows, was_reversed)` ordered cause→effect. Reverses both arrays in lockstep when reverse-direction verbs (`is_substrate_of`, `is_phosphorylated_by`, …) strictly dominate. Mixed-direction chains keep LLM order; per-edge arrow labels (Phase B.3 #3) carry the biology in those cases.

**Going forward only:** the helper fires only when `chain_just_created=True` (newly-discovered chains in this sync call). Existing chains in the DB (e.g. inverted STUB1/HSP90AA1 case) stay in their stored order until that protein is re-queried — relying on Layer 3 edge labels to communicate biological direction visually.

**Tests:** new `tests/test_chain_canonicalization.py` — 20 tests covering forward-only, reverse-only, mixed, empty, single-arrow, idempotency, malformed entries, unknown verbs.

### B.3 ✅ DONE — Layer 3 card-view chain rendering

**Where:** `static/card_view.js` chain pre-pass (~line 1320-1450) + new post-layout pass (~line 3656+) + `static/styles.css` (appended ~100 lines of CSS).

**What:** Three coordinated changes:

1. **Inbound chain arrow stamping.** Every non-root chain node gets `_inboundChainArrow` from `chainGroup.arrows[k-1].arrow`. Renders as a verb label on the edge into that node, color-coded per arrow type via `.cv-chain-edge-label.arrow-{type}` CSS classes. Reverse-verbs (`is_*_by`) render in italic so the user reads direction even when spatial layout is constrained.

2. **Visual cross-links between same-protein duplicates.** New `_renderDuplicateCrossLinks(nodes)` post-pass groups all rendered nodes by base protein (via `_duplicateOf` or `data.id`); for any symbol with N>1 instances, draws faint dashed cubic-Bezier paths between them (capped at 5 per protein for readability).

3. **Hover highlight.** `mouseenter.cv-duplicate` handler highlights all instances of the same protein simultaneously and brightens the cross-links between them.

**CSS:** `.cv-duplicate-crosslink` (faint dashed by default, accent on `.highlighted`), `.cv-protein-active` (accent stroke + drop-shadow), `.cv-chain-edge-label.arrow-*` (color-coded by verb type, with dark-mode variants).

## Phase C — Frontend P0/P1 fixes

### C.1 ✅ DONE — SNAP freeze + visualizer mutation collision

**Where:** `templates/visualize.html:583-635` (legacy backfill before freeze), `static/visualizer.js:2830-2837` (write-back removed).

**What:** Moved the legacy-shape backfill (transforming `SNAP.interactors[]` → modern `SNAP.proteins[]` + `SNAP.interactions[]`) UPSTREAM of `Object.freeze(SNAP)`. The previous write-back at `static/visualizer.js:2832` was silently failing in strict mode (SNAP is frozen) and leaving the legacy fallback path effectively dead. The freeze invariant is preserved — kept visualizer.js's local `proteins`/`interactions` working-vars but dropped the no-op SNAP write.

**Why keep the freeze:** templates/visualize.html freezes SNAP intentionally as the load-bearing invariant — every JS file (cv_diagnostics, card_view, modal, visualizer) reads from it as immutable source-of-truth. Removing the freeze trades strict-mode silent-fail for unbounded silent state corruption across 9 JS files.

### C.3 ✅ DONE — `_lastModalArgs` lifecycle clear-on-close

**Where:** `static/modal.js:128-145` (`closeModal`).

**What:** Clear `_lastModalArgs = null` when the modal closes so the next open doesn't re-render a stale "pathway-only / show-all" toggle state against a different node's links.

## Phase D — Database hygiene

### D.1 ✅ DONE — `scripts/repair_denormalized_counters.py`

Periodic / on-demand repair script. Recomputes:
- `Protein.total_interactions` by aggregating from the `interactions` table (counts each protein's appearances as `protein_a` or `protein_b`).
- `IndirectChain.pathway_name` via `chain.recompute_pathway_name()` (majority-vote of chain claims).
- Pathway hierarchy + usage_count via the existing `scripts/pathway_v2/step7_repairs.py:recalculate_all_*` helpers.

Idempotent. Run: `python3 -m scripts.repair_denormalized_counters`.

`Pathway.protein_count` is intentionally NOT touched — see Phase D.3 / `10_OPEN_ISSUES.md`.

### D.2 ✅ DONE — `function_context` NOT NULL

**Where:** new migration `migrations/versions/20260503_0007_function_context_not_null.py`, model edits in `models.py` for both `Interaction` and `InteractionClaim`.

**What:** `function_context` was previously nullable on both tables; the 5-col COALESCE unique index already collapsed NULL to `''` for dedup, but readers special-cased NULL and writers occasionally landed it. The migration backfills NULL → `'direct'` then sets `NOT NULL DEFAULT 'direct'`. Models declare `nullable=False, default='direct', server_default='direct'` so the Python default fires before INSERT (covering tests + new code) and the DB-level default catches anything the ORM misses.

**Bug surfaced + fixed:** `_save_claims` stale-check at `utils/db_sync.py:1862` was using `_clamp(fn.get("function_context"), 20)` — i.e., raw `func.function_context` without the parent fallback the creation loop applies. That meant new claims with `fn_ctx='direct'` (from interaction.function_context) ended up keyed under `'direct'` in `_existing_by_key` but were keyed under `''` in `current_fn_keys`, marking them as stale and DELETING them right after creation. Fixed to use the same parent-fallback as the creation loop. Pre-existing latent bug; only became visible after function_context started being populated reliably.

### D.4 ✅ DONE — Direction implicit-fallback logging

**Where:** `utils/db_sync.py:_save_interaction` (~line 1086-1130).

**What:** When the payload's `direction` is missing and we silent-default `stored_direction = "a_to_b"`, emit `utils.observability.log_event("direction_fallback", level="warn", protein_a, protein_b, arrow, fallback_to, reason="payload missing 'direction' field")`. The silent default was how query-relative ↔ canonical conversions drifted unnoticed.

## What HASN'T been done from the plan

These were proposed in the prior session's plan but NOT implemented (deferred or determined unnecessary):

- **2.2: cached_content threading for chain claim gen.** The infrastructure exists (`gemini_runtime.py:create_or_get_system_cache`, used by `quick_assign.py`). Wiring it through chain claim gen would save ~90% of system prompt tokens × 30-60 calls per run. Bigger refactor (~15-20 min); deferred.
- **2.4: thinking_level default flip.** Default is "high" (Vertex's own default). Flipping globally to "low" risks under-thinking. Per-step configs are explicit; let them stay.
- **5.1: Visualizer chain rendering perfection.** User emphasized card view + modal as primary. Visualizer's existing rendering is adequate; deferred.
- **A2 Layer 1 (going-forward only)** — landed (Phase B.1 above). Backfill of existing inverted chains was explicitly skipped per user decision; existing data relies on Layer 3 edge labels (Phase B.3) to communicate biological direction.
- **C.2 cvState ↔ PathwayState desync** — pending audit. Direct mutations of `cvState.selectedRoots.add/delete` exist at `static/card_view.js:4060-4067, 4076, 5362`; whether these should route through `PathwayState.toggleSelection` requires deeper investigation of the two state managers' intended responsibilities (card-view-local vs cross-component).
- **D.3 Pathway.protein_count decision** — column is read at `services/data_builder.py:1503` and emitted to the frontend (no JS consumer reads it), but never written by any production path. Pending user decision: wire it up or remove.
