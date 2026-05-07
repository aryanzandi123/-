# Open Issues — THE Worklist

**Read this first if you only have time for one technical doc.** This is what's pending, in priority order. Each item: what, why, where, how, impact, status.

The user explicitly asked for THIS LIST. Treat it as the worklist. Cross items off as you complete them. Add new items as you discover them.

## Priority A — Active worklist

### A1 ✅ DONE 2026-05-03 — Chain-claim MAX_TOKENS truncation (24K cap)

**Resolved.** `.env CHAIN_CLAIM_MAX_OUTPUT_TOKENS` set to `24000`. Companion bumps:
- `utils/arrow_effect_validator.py:549` simple/complex → `12336/24000`
- `pipeline/prompts/modern_steps.py:418` (citation verification) → `24000`
- `utils/gemini_runtime.py:447-470` docstring rot fix
- `.env CHAIN_CLAIM_BATCH_SIZE` left at 1

See `09_FIXES_HISTORY.md` § 1.7 for the full rationale (why 24K, not 12336, not 65536).

**Verification status:** All 676 pytest tests pass. Awaiting next pipeline run for empirical confirmation.

**Expected on next run:**
- 0 `MAX_TOKENS` finish_reasons in chain claim phase
- 0 hard 400 INVALID_ARGUMENT
- `_diagnostics.chain_pair_unrecoverable` empty
- `_diagnostics.chain_incomplete_hops` near-empty
- ~30-40% wall-clock drop on chain-claim phase

If the next run still shows truncation: bump `CHAIN_CLAIM_MAX_OUTPUT_TOKENS` to 32768 or 65536 (model cap).

**This unblocks A2-A4** — frontend chain rendering can now assume clean chain data.

---

## Priority B — NOW THE TOP — Frontend chain rendering DAG fix

The user has been very explicit about wanting "REMAKE AND PERFECT" treatment for the card view. Read `11_CHAIN_TOPOLOGY.md` for the full design space. The three layers below are the structured plan.

### A2 ✅ DONE 2026-05-03 (going forward only) — Layer 1: Canonical biological direction at write-time

**Status:** LANDED. See `09_FIXES_HISTORY.md` § Phase B.1.

**What:** When persisting `IndirectChain.chain_proteins`, normalize the order so `chain[i] → chain[i+1]` is always the BIOLOGICAL CAUSE → EFFECT direction. Use `chain_with_arrows[i].arrow` semantics:
- `activates`/`inhibits`/`phosphorylates`/`ubiquitinates`/etc. → keep order
- `is_substrate_of`/`is_activated_by`/`is_phosphorylated_by`/etc. → REVERSE the chain
- Mixed → split or stamp `_canonical_direction='LLM-asis'` warning so frontend renders both arrows

**Why:** the LLM emits chains in query-centric order (ATXN3 at head). Biologically, STUB1 ubiquitinates HSP90AA1 (STUB1 upstream), but LLM emits `[ATXN3, HSP90AA1, STUB1]` because that's how it's framing the cascade. Card view renders `chain[k+1]` as child of `chain[k]` so STUB1 ends up VISUALLY downstream of HSP90AA1 — exactly the inversion the user observed.

**Where:** `utils/chain_resolution.py` (canonical-direction helper) + `utils/db_sync.py:sync_chain_relationships` (call helper before write).

**Effort:** ~80-120 lines of code + 4-5 unit tests.

**Verification:** unit tests for each verb-family direction inference; query a protein with known reverse-direction biology (e.g. STUB1 chain), confirm chain renders STUB1 above HSP90AA1.

---

### A3 ✅ DONE 2026-05-03 — Layer 2: Chain-pathway gate fix at write-time

**Status:** LANDED. See `09_FIXES_HISTORY.md` § Phase A.1 + A.2. The `recompute_pathway_name()` half of the original Layer 2 design was REJECTED — it would invert `_unify_all_chain_claims`'s deliberate priority order. The implemented half emits `chain_pathways[]` in `_chain_fields_for` and widens the frontend gate.

**What:** Two changes:
1. `IndirectChain.recompute_pathway_name()` already exists (in models.py) — call it from `quick_assign` after chain claims are unified. So `IndirectChain.pathway_name` always equals the majority-vote of its chain-derived claims, eliminating drift.
2. `services/data_builder.py:_chain_fields_for` should emit `chain_pathways: [<every pathway any chain claim landed in>]` array. The frontend's `groupChainsByChainId` gate becomes:
   ```
   include chain in pathway P if:
     EITHER any chain claim is assigned to P
     OR     both chain endpoints are direct interactors under P
   ```
   This eliminates the silent-drop-by-pathway-gate root cause that hides HDAC6's chain participation.

**Where:** `scripts/pathway_v2/quick_assign.py` (call `recompute_pathway_name`) + `services/data_builder.py:_chain_fields_for` (emit `chain_pathways`) + `static/card_view.js:groupChainsByChainId` (use `chain_pathways`).

**Effort:** ~30-50 lines.

**Verification:** query ATXN3 (or whatever has HDAC6 as a chain mediator AND direct partner). Confirm HDAC6 chain renders under the same pathway as its direct claim.

---

### A4 ✅ DONE 2026-05-03 — Layer 3: Card view always-render-full-chains + cross-links + edge labels

**Status:** LANDED. See `09_FIXES_HISTORY.md` § Phase B.3. Edge labels with verb + arrow-type-coded color, dashed cross-link Bezier paths between same-protein duplicates with hover highlight, all wired into `_renderCardViewImpl` post-pass.

**What:** Three coordinated changes:

1. **Always render full chains.** Currently chain pre-pass starts from `chainProteins[0]` and creates duplicates only for already-assigned nodes mid-chain. Replace with: every chain renders as a complete sequence using `_uid = ${proteinId}::chain::${chainId}::${position}` for every node, regardless of direct-pass assignment. Direct nodes coexist with chain nodes; both are real.

2. **Visual cross-links between duplicate-of-same-protein nodes.** After D3 tree layout, a post-pass walks `nodesById`, finds proteins with multiple representations (`primary_node` + N chain duplicates), and adds faint dashed SVG paths between them, colored by chain lane palette. Hover highlights all instances of the same symbol.

3. **Direction-aware chain edge labels.** Use `chain_with_arrows[i].arrow` to label each tree edge with verb + direction badge. Even when spatial direction is constrained by tree layout (parent→child), the BIOLOGICAL direction is unambiguous from the label.

**Where:** `static/card_view.js:buildCardHierarchy` (chain pre-pass rewrite) + new post-layout pass for cross-links + edge label rendering.

**Effort:** ~150-250 lines + 50 lines CSS.

**Verification:** screenshot test — query a protein with the HDAC6 / STUB1 case (the user can provide). Confirm HDAC6 appears in BOTH chain context AND direct context with cross-link line. Confirm STUB1 chain shows direction labels even when spatially constrained.

---

### A5 — Layer 4 (optional): Sub-DAG layout for dense cascades

**Status:** NOT STARTED. Only pursue if Layer 3's cross-links don't visually solve the user's "non-linearity" perception.

**What:** For pathways where a single chain has ≥2 cycles or ≥3 distinct branches, drop strict tree and use a constrained force-directed sub-layout JUST for that chain's nodes. Pathway boundary stays a tree; chain becomes a small DAG-shaped insert.

**Where:** new layout helper in `static/card_view.js` + optionally pull in `dagre.js` (or hand-roll).

**Effort:** ~300 lines.

**Verification:** screenshot of a known cycle/branching case (regulatory feedback loops e.g. PI3K/AKT/mTOR/TSC1/TSC2 negative feedback).

---

## Priority C — Backend optimizations (low-priority, fold in opportunistically)

### A6 — Cached content threading for chain claim gen

**Status:** NOT STARTED. Infrastructure exists (`utils/gemini_runtime.py:create_or_get_system_cache`) and is used by `quick_assign.py` (hierarchy cache). Not threaded to chain claim gen.

**What:** Chain claim gen has 30-60 calls per run with the SAME ~5K token system prompt. Cache the system prompt once, reuse for all calls.

**Where:** `runner.py:_run_parallel_batched_phase` — call `create_or_get_system_cache` once per phase, pass cache name to all child calls. Modify step factories' `cache_system_prompt=True` flag.

**Impact:** 90% cost reduction on system prompt tokens for chain claim phase (~$0.50-2/run).

**Effort:** ~50 lines + plumbing.

**Verification:** confirm `cached_content_token_count` shows on subsequent calls.

---

### A7 — Tune `thinking_level` per step

**Status:** NOT STARTED. Per-step configs already explicit; opportunistic improvement.

**What:** Audit each step config in `pipeline/config_dynamic.py` and `pipeline/prompts/*.py`. Confirm thinking_level matches task complexity:
- Pure JSON formatting (citation, dedup): `low`
- Validation with reasoning (evidence, arrow): `medium`
- Disagreement resolution (arrow on dual-track indirect): `high`
- Discovery: `medium`
- Function mapping: `medium`
- Chain claim gen: `low` (currently — keep)

**Where:** all step factories.

**Impact:** 10-20% reduction in thinking-tax tokens.

**Effort:** ~1-2 hours auditing.

---

### A8 — Streaming output for long-running phases

**Status:** NOT STARTED. UX improvement.

**What:** Use Gemini streaming output (`stream_generate_content`) for discovery / function mapping so the SSE stream can show partial results during long calls.

**Where:** `utils/gemini_runtime.py` (add streaming wrapper) + `runner.py:call_gemini_model` (route to stream vs sync).

**Impact:** Better UX during long phases.

**Effort:** ~100 lines.

---

## Priority D — Quality / hygiene

### A9 — Test coverage for the prior session's fixes

**Status:** smoke-checked but no dedicated unit tests yet.

**What:** Add unit tests for:
- `canonical_pair_key("vcp","TFEB") == "TFEB|VCP"` (case-insensitive)
- `save_checkpoint` body no longer triggers `_sync_to_db_with_retry`
- `_apply_tier1_normalization_to_payload` populates `arrow_context` for indirect interactors
- `_check_pathway_drift_at_write` reassigns when implied has higher score AND is in DB
- `_check_pathway_drift_at_write` does NOT reassign for chain-derived claims

**Where:** `tests/test_chain_resolution.py`, `tests/test_storage.py`, `tests/test_arrow_effect_validator.py`, `tests/test_pathway_drift.py` (new).

**Effort:** ~60-100 lines per test file.

---

### A10 — Audit cv_diagnostics.js pseudo-name list

**Status:** mirrored manually with `utils/db_sync._PSEUDO_WHITELIST`.

**What:** Currently `cv_diagnostics.js:PSEUDO_NAMES` is hand-synced to `utils/db_sync._PSEUDO_WHITELIST`. Drift-prone. Replace with a `/api/pseudo_whitelist` endpoint that emits the canonical list, fetched once on page load.

**Where:** new `routes/results.py` endpoint + `cv_diagnostics.js` fetch on init.

**Effort:** ~30 lines.

---

### A11 — Surface `_diagnostics.pathway_drifts` from data_builder

**Status:** runner.py writes the drifts to `pipeline_diagnostics.json` (this session). data_builder reads that file at line ~1845. Should already work end-to-end. Verify on next run.

**What:** Confirm `result["_diagnostics"]["pathway_drifts"]` appears in `/api/results/<p>` after a run with corrections.

**Where:** `services/data_builder.py:1839-1860` (read path is automatic — JSON merge).

**Effort:** verify only.

---

## Priority E — User-pending design questions (asked but not yet answered)

### A12 — Chain visualization choice (HDAC6 case)

The user's open question: should HDAC6 (in chain `…→HSP90AA1→HDAC6`) appearing as both a chain participant AND a direct ATXN3 partner be rendered as:
- (a) Two separate cards with a cross-link line (Layer 3 approach)
- (b) One card with a chain-participation pill
- (c) Sub-DAG showing HDAC6 with both an upstream-chain edge AND a direct-to-ATXN3 edge

Default in `11_CHAIN_TOPOLOGY.md`: (a). Alternatives kept for discussion.

### A13 — Direction-inversion handling (STUB1 case)

The user's open question: when chain biology runs OPPOSITE to query-centric ordering (STUB1 ubiquitinates HSP90AA1), should we:
- (a) Reverse the chain at write time so layout reflects biology (Layer 1)
- (b) Keep query-centric order in storage but render visual arrow labels (Layer 3 edge labels)
- (c) Both

Default in `11_CHAIN_TOPOLOGY.md`: (a) + (c). Alternatives kept for discussion.

---

## Already-fixed (don't redo) — pointer to `09_FIXES_HISTORY.md`

- 1.1 `canonical_pair_key` case-insensitivity ✅
- 1.2 doubled `[DB SYNC]` log block ✅
- 1.3 Tier-1 arrow validation skip-all (added `_apply_tier1_normalization_to_payload`) ✅
- 1.4 P3.1 write-time pathway drift detection ✅
- 1.5 Echo skip flags in route log ✅
- 1.6 MAX_TOKENS prevention via batch_size=1 (superseded by 1.7) ⚠️
- 1.7 Chain-claim cap suite to 24000 (the actual fix) ✅
- 2.1 Batch API for Flash ✅
- 2.2 Pathway v2 max_output_tokens (set to 8192 — left untouched; pathway responses are tiny by design) ✅
- 3.1 Per-hop "no biology" badges ✅
- 3.2 Pathway-drift badges ✅
- 3.3 Diagnostics banner counts ✅
- 4.1 Multi-chain modal banners ✅
- 4.2 Multi-chain navigation scoping ✅
- A.1 `chain_pathways[]` emission in `_chain_fields_for` ✅ ← 2026-05-03 (Phase A)
- A.2 Frontend chain pathway gate widened ✅ ← 2026-05-03
- A.3 quick_assign max_output_tokens constants normalized ✅ ← 2026-05-03
- B.1 Layer 1 canonical chain direction (going forward only) ✅ ← 2026-05-03
- B.3 Layer 3 card-view edge labels + cross-links ✅ ← 2026-05-03
- C.1 SNAP freeze + visualizer mutation collision ✅ ← 2026-05-03
- C.3 `_lastModalArgs` lifecycle clear-on-close ✅ ← 2026-05-03
- D.1 `scripts/repair_denormalized_counters.py` ✅ ← 2026-05-03
- D.2 `function_context` NOT NULL (migration `20260503_0007`) ✅ ← 2026-05-03 latest
- D.4 Direction implicit-fallback logging ✅ ← 2026-05-03

## Things the user has explicitly said are NOT issues

- **Flash-only is intentional.** Don't propose Pro 3 unless Flash is genuinely incapable.
- **PhD-depth (6-10 sentences / 3-5 cascades) is non-negotiable.** Don't downgrade.
- **No git operations.** Don't propose them.
- **CLAUDE.md / ARCHITECTURE.md exist.** Don't duplicate them — but DO supplement them with this CLAUDE_DOCS folder.

## Things the user is highly likely to ask about next

After A1 lands, expect:
1. Re-run a query and assess: did MAX_TOKENS actually go to 0? (Verification step)
2. Then push on the chain DAG layer (A2-A4). The user has said this is the #1 frontend gripe.
3. Possibly revisit `cached_content` (A6) for cost optimization once correctness is solid.
