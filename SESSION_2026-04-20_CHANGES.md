# Session Changes — 2026-04-20

Everything that landed during the "perfect quick-assign + kill all legacy" session. Organized by tier, then by fix. Every item has a **why** (the root cause / motivation) so six-months-from-now-you can reconstruct intent.

---

## TL;DR

Three buckets of work:
- **Tier A — Quick-Assign Perfection (11 fixes)**: eliminate every failure fingerprint the TDP43 terminal log produced (`DRIFT`, `CHAIN MISSING`, `pathway_claim_consistency`, `chain_pathway_consistency`, `function_context_drift`) at the root, not with symptom patches. Make re-queries idempotent.
- **Tier B — Legacy Persistence Removal**: drop 7 dead tables, delete never-called writer methods, clean `datetime.utcnow` / `Query.get` deprecation across 32 + 32 sites, retire the `standard` pipeline mode, fix an Alembic config bug that made migrations silently no-op.
- **Tier C — Filesystem & Config Hygiene**: extend `--keep-pathways` to wipe `Logs/` + cache scratch, delete stray session-dump files + empty SQLite shells, rewrite `.env` cleanly, create `.gitignore`.

Live DB confirmation after everything: `orphan_pathway_interactions = 0`, `function_context_drift_rows = 0`, all 7 dead tables dropped, `alembic_version = 20260420_0001`, 954 pathways + 936 pathway_parents preserved.

---

## Tier A — Quick-Assign Perfection

### A1. Re-enable the three content validators (`.env`)

**Files**: `.env:95, 103, 104`

**Why.** `services/data_builder.py:757-857` has three validators (arrow, direction, pathway) whose comments say _"Default ON … catches real LLM mislabels at a sustained rate without false positives."_ Code defaults for arrow + pathway are `true`. But `.env` silently overrode all three to `false`, so the validators were observe-only and the TDP43 run emitted 60+ `DRIFT` warnings nobody could fix.

**Change.** Flipped all three to `true`. The validators now rewrite `arrow`, `direction`, `pathway` in-place when prose verbs disagree with the declared field.

---

### A2. Make `function_context_drift` auto-fixable

**Files**: `scripts/pathway_v2/step7_checks.py:517`, `scripts/pathway_v2/step7_repairs.py:452-513` (new `repair_function_context_drift`), `scripts/pathway_v2/step7_repairs.py:892-894` (route in `run_auto_repairs`)

**Why.** The check flagged 5 interactions in the TDP43 log (parent `function_context='direct'` but children `[direct, net]`). Marked `auto_fixable=False` with a TODO-ish "Review: update parent to 'mixed' or re-label claims". System never picked, so every verify run bubbled the same 5 as **blocking**.

**Change.** Flipped the flag to `True` and wrote `repair_function_context_drift` which rolls up child contexts using the same invariant `_save_interaction` applies on first write: single non-NULL context wins; two or more distinct → `mixed`; NULL children are ignored.

---

### A3. `unify_one_chain_pathway` must sync `PathwayInteractions`

**File**: `scripts/pathway_v2/quick_assign.py:1676-1686`

**Why.** `unify_one_chain_pathway` → `_unify_all_chain_claims` mutates `claim.pathway_id`. But the junction table `pathway_interactions` still points at the OLD pathway IDs. Next step7 re-check sees 11 "stale PI" rows — which the TDP43 log called out as `pathway_claim_consistency (11 issues)`.

The main in-pipeline path (`quick_assign_claims`) already calls `_sync_pathway_interactions` right after `_unify_all_chain_claims` for this exact reason. Only the step7-repair entry point skipped it.

**Change.** Added the `_sync_pathway_interactions(db, _IClaim, _PI, interaction_ids)` + `db.session.flush()` right after the unification, mirroring the in-pipeline path.

---

### A4. Verify pipeline: bounded 3-pass repair loop

**Files**: `scripts/pathway_v2/verify_pipeline.py:174-192` (new `_merge_repair_summaries` helper), `scripts/pathway_v2/verify_pipeline.py:240-295` (loop replaces single-shot)

**Why.** `verify_pipeline.run_verification` ran auto-repairs exactly ONCE, then re-checked. But some repairs create new auto-fixable issues (chain unify → stale PIs; auto-fix schema drift → new normalization drift). The log showed 8 fixes applied → 17 issues still remain. One pass isn't enough.

**Change.** Converted to a bounded `for pass_num in range(1, MAX_REPAIR_PASSES + 1)` loop (cap = 3). Merges per-pass `RepairSummary` objects into one via `_merge_repair_summaries` so the final report's `[FIXED] …` lines reflect all passes. Bails early if any pass finds no fixable issues (convergence).

---

### A5. Salvage `[CHAIN MISSING]` indirects from `upstream_interactor`

**Files**: `runner.py:1705-1802` (new salvage loop + logging), `runner.py:4296-4303` + `runner.py:5107-5114` (second `_reconstruct_chains` call after chain resolution)

**Why.** Six TDP43 indirect interactors (BAX, CASP3, ATG7, MAP1LC3B, GABARAP, STMN1) arrived with `upstream_interactor` set but no `mediator_chain` and no `chain_context.full_chain`. The code's silent length-1 fill-in was deliberately removed earlier to expose LLM laziness. But when the upstream IS a real known interactor (either in the original batch or promoted by chain resolution), we have enough info to reconstruct `[query, upstream, primary]` safely — just with an explicit audit trail instead of a silent default.

**Change.** Added a salvage branch in `_reconstruct_chains` that checks `upstream_interactor.upper() in known_symbols` (current ctx + query itself), and if so, builds a length-3 chain via `ChainView.from_full_chain(...).apply_to_dict(interactor)`. Writes all four legacy fields via the single ChainView write-surface so they can't drift. Tags the interactor with `_chain_salvaged_from_upstream=True` for auditability. Added a second `_reconstruct_chains` call right after `_run_chain_resolution_phase` so salvage can pick up middle proteins (TP53, TFEB, ATG4B, STMN2) that only get promoted during resolution.

---

### A6. Remove JSONB augmentation from `_sync_pathway_interactions` — ROOT FIX for orphan PIs

**File**: `scripts/pathway_v2/quick_assign.py:1728-1754` (block deletion + docstring rewrite)

**Why.** `_sync_pathway_interactions` was building its `desired_pairs` set from TWO sources:
1. `ic.pathway_id` from claims (truth)
2. `interaction.data["functions"][].pathway` + `step3_finalized_pathway` from JSONB (denormalized mirror)

The second source is the ROOT cause of orphan `PathwayInteractions`. When a claim moves pathway (via unify / consistency enforcement), the JSONB still mentions the OLD pathway until `_sync_claim_to_interaction_data` updates it — and even then, a lag of one flush creates a window where a PI gets created from stale JSONB. Now a PI exists that no claim backs, and the `pathway_claim_consistency` check will fail on it forever.

JSONB is a denormalized mirror of claims (see `_sync_claim_to_interaction_data` which writes it). It should never be an input source; it's a derivative.

**Change.** Deleted the entire JSONB-augmentation block (was lines 1754-1787). Added a clarifying docstring explaining the "single source of truth is claims" invariant. By construction, `pathway_claim_consistency` can now never fail — every PI has a backing claim because every PI comes from a claim.

---

### A7. `check_chain_pathway_consistency` + repair respect `CHAIN_PATHWAY_UNIFY`

**Files**: `scripts/pathway_v2/step7_checks.py:389-404` (early-return guard), `scripts/pathway_v2/step7_repairs.py:642-660` (matching guard in the repair)

**Why.** `.env` defaults `CHAIN_PATHWAY_UNIFY=false` — the user's EXPLICIT INTENT is per-hop pathway diversity ("legitimate cross-pathway cascades like query → kinase → autophagy → proteostasis"). But the check hardcoded the invariant "every claim in a chain shares one pathway" and flagged every diverse chain as fragmented. The auto-fix then flattened them, silently overriding the env choice. Hence `chain_pathway_consistency (1 issue)` surviving auto-fixes — the repair flattened one chain, which created downstream fragmentation that the re-check caught.

**Change.** Added `import os; if os.getenv("CHAIN_PATHWAY_UNIFY", "false").lower() != "true": return result` at the top of `check_chain_pathway_consistency` — no issues reported when diversity is desired. Mirrored the guard in `repair_chain_pathway_fragmentation` as defense-in-depth so direct callers (CLI, tests, future code) can't bypass.

---

### A8. `_unify_all_chain_claims`: scope by `chain_ids`, not `interaction_ids`

**Files**: `scripts/pathway_v2/quick_assign.py:1396-1464` (new `chain_ids` param + precedence rule), `scripts/pathway_v2/quick_assign.py:1678-1685` (`unify_one_chain_pathway` uses new param)

**Why.** The old scope derivation was: `interaction_ids` → find every chain whose origin OR any claim's interaction is in that set → unify all those chains. For interactions that participate in MULTIPLE chains (common when chains share middle proteins, e.g. TDP43→TP53→BAX and TDP43→TP53→CASP3 both touch TP53's interaction), this pulls in bystander chains and flattens them along with the target. The TDP43 log showed it:
```
[CHAIN UNIFY] Unified 1 chain claim(s) across 2 chain(s) ...
[CHAIN UNIFY] Unified 2 chain claim(s) across 2 chain(s) ...
```
`unify_one_chain_pathway(chain_id=X)` ended up touching 2 chains — a silent override of the user's diversity choice.

**Change.** Added a `chain_ids=` kwarg with precedence over `interaction_ids`. `unify_one_chain_pathway` now passes `chain_ids=[chain.id]` so EXACTLY that one chain is affected. The main in-pipeline path (`quick_assign_claims`) still uses `interaction_ids` for scope because it legitimately wants "everything touched by this query."

---

### A9. Idempotency short-circuit in `quick_assign_claims`

**File**: `scripts/pathway_v2/quick_assign.py:968-997`

**Why.** Re-querying the same protein hits `quick_assign_claims` again. Existing behaviour: serializes the full pathway hierarchy (~20K tokens), creates a Gemini context cache (1 LLM API call), THEN notices there are no unassigned claims and runs `_sync_pathway_interactions` + returns. The heavy setup is a dead cost when nothing changed.

**Change.** Added an early-exit short-circuit at the top (before hierarchy serialization): two cheap `COUNT` queries determine if all scoped claims already have `pathway_id`. If yes, run `_sync_pathway_interactions` + `_sync_interaction_finalized_pathway` (zero-delta if already in sync), commit, return `{"skipped_noop": True}`. Re-queries now finish in under a second with zero LLM calls.

---

### A10/B3. Delete never-called `_save_chain_context_claims`

**Files**: `utils/db_sync.py:2122-2189` (deletion), `utils/post_processor.py:105` (comment cleanup referencing the deleted method)

**Why.** `grep _save_chain_context_claims` across the codebase showed exactly two references: the method definition itself, and a comment in `post_processor.py`. No callers. Meanwhile `chain_derived` claims ARE produced, but they go through the normal `_save_claims` path via the function dict's `function_context` field (the correct place). The orphan method was dead weight that new developers would reference.

**Change.** Deleted the 68-line method. Updated the `post_processor.py` comment to point at the actual writer.

---

## Tier B — Legacy Persistence Removal

### B1. Drop 7 dead tables via Alembic

**Files**: `migrations/versions/20260420_0001_drop_dead_tables.py` (NEW)

**Why.** The 7 tables below carry zero rows, zero writers, zero readers (confirmed by grep + row-count scan across codebase):

- `interaction_chains` (legacy parallel to `indirect_chains`)
- `interaction_pathways` (legacy parallel to `pathway_interactions`)
- `interaction_query_hits` (tracking table, never wired)
- `pathway_canonical_names` (pre-quick_assign normalization)
- `pathway_hierarchy` (legacy parallel to `pathway_parents`)
- `pathway_hierarchy_history` (audit scaffold, never populated)
- `pathway_initial_assignments` (pre-quick_assign assignment log)

**Change.** New Alembic migration — `DROP TABLE IF EXISTS … CASCADE` for each, wrapped in transactional DDL. `down_revision = "000000000000"` chains off the baseline. `downgrade()` is a no-op (we can't reconstruct empty shells we never populated).

**Applied live:** user ran `python3 -m alembic upgrade head`; verification confirms all 7 dropped, `alembic_version = 20260420_0001`.

---

### B1b. `alembic.ini` whitespace-split bug fix

**File**: `alembic.ini:17-26`

**Why.** `version_locations = %(here)s/migrations/versions` — Alembic splits this option on whitespace. The project path `/Users/aryan/Desktop/DADA/untitled folder 2 copy 54/` contains spaces, so the absolute path got shredded into 5 nonsense paths:
```
['/Users/aryan/Desktop/DADA/untitled', 'folder', '2', 'copy', '54/migrations/versions']
```
Alembic then found **zero** migrations. First `alembic upgrade head` run printed `Will assume transactional DDL` and exited silently — no "Running upgrade" lines, no migration applied.

**Change.** Removed the explicit `version_locations` line. Alembic defaults to `<script_location>/versions` = `migrations/versions`, which is derived without whitespace splitting. Added a comment explaining WHY we deliberately don't set it.

---

### B2. Prune dead JSONB keys from writers

**File**: `services/data_builder.py:218, 271, 294, 482, 501, 541` (5 write statements removed)

**Why.** `_chain_context_functions` and `_chain_context_overlay` were written to chain_data JSONB but never read by any Python or JS code (grep-confirmed). Pure internal metadata noise.

**Change.** Deleted all 5 write sites. Kept other underscore-prefixed metadata (`_synthesized_from_chain`, `_from_parent_chain_filtered`, `_inferred_from_chain`) — those ARE read by display logic.

**Audit correction.** The legacy-audit initially claimed `step3_function_pathways`, `step2_function_proposals`, and `_inferred_from_chain` were also write-only — deeper grep proved that wrong (they're consumed by `step4_build_hierarchy_backwards.py`, `step3_refine_pathways.py`, and `services/data_builder.py` respectively). Left untouched.

---

### B4. Retired step factories — comment clarified (no code deletion)

**File**: `pipeline/config_dynamic.py:27-31`

**Why.** The audit claimed `step2ab2_hidden_indirect_detection`, `step2ab3_hidden_chain_determination`, `step2ab5_extract_pairs_explicit` were dead. Deeper grep proved they're **actively used inside** `runner._run_track_a` and `runner._run_track_b` (the chain resolution orchestrator imports and calls them). They're retired only as standalone pipeline steps, not as factories.

**Change.** Updated the misleading `# retired` comment to clarify: the factories are NOT dead, they're orchestrated differently. No code deletion.

---

### B5. Retire `PIPELINE_MODE=standard` + `USE_LEGACY_PIPELINE`

**File**: `runner.py:4867-4884`

**Why.** Zero production users since iterative + modern pipelines landed. The branch was dead.

**Change.** Deleted the `use_legacy or pipeline_mode == "standard"` branch. Kept the `not DYNAMIC_CONFIG_AVAILABLE` emergency-fallback branch (in case `pipeline/config_dynamic.py` fails to import at startup). Updated logging to reflect the narrower mode set.

---

### B7. `datetime.utcnow()` → `_utcnow()` helper (32 replacements)

**Files**: `models.py` (new helper at line 23 + 20 call/ref replacements), `utils/db_sync.py` (7 replacements), `utils/protein_database.py` (2), `scripts/fix_direct_link_arrows.py` (2), `scripts/pathway_v2/step6_utils.py` (1), `scripts/validate_existing_arrows.py` (6)

**Why.** `datetime.utcnow()` is deprecated in Python 3.12+, scheduled for removal in 3.17. The deprecation warning fires at every SQLAlchemy row insert/update (since `models.py` passes `datetime.utcnow` as a `default=`/`onupdate=` callable). Test output was flooded with DeprecationWarning noise.

**Change.** In `models.py` added a module-level helper:
```python
def _utcnow() -> datetime:
    """SQLAlchemy-compatible drop-in for the deprecated datetime.utcnow."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
```
`.replace(tzinfo=None)` strips tzinfo to keep DB columns naive (matching the existing schema). SQLAlchemy invokes the callable at insert/update time, same as before.

All `datetime.utcnow()` invocations across the 6 files were replaced with either `_utcnow()` (in models.py) or the explicit `datetime.now(timezone.utc).replace(tzinfo=None)` (in other files where importing `_utcnow` would cause circular imports).

---

### B8. `Model.query.get(id)` → `db.session.get(Model, id)` (32 replacements)

**Files**: `services/data_builder.py`, `utils/db_sync.py`, `utils/protein_aliases.py`, `routes/results.py`, `scripts/pathway_v2/step7_repairs.py` (15), `scripts/pathway_v2/step6_utils.py` (4), `scripts/pathway_v2/step5_discover_siblings.py` (2), `scripts/pathway_v2/step6_reorganize_pathways.py` (6), `scripts/migrate_add_interaction_chain_id.py` (1)

**Why.** `Query.get()` is SQLAlchemy 1.x legacy API — deprecated in 2.0 (`LegacyAPIWarning`). Replaced with the 2.x-native `Session.get(Model, id)` call. Mechanical; same semantics.

**Change.** Script walked the 9 files, replaced via regex `(\w+)\.query\.get\(([^)]+)\)` → `db.session.get(\1, \2)`. Files that didn't already import `db` got one added (tracked by import-presence check; none needed it in the end — step7_repairs.py takes `db` as a function parameter, others already imported from `models`).

---

## Tier C — Filesystem & Config Hygiene

### C1 + C2. `--keep-pathways` wipes `Logs/` + cache scratch

**File**: `scripts/clear_pathway_tables.py:50-76` (new constants), `scripts/clear_pathway_tables.py:94-196` (new `_clear_logs_tree` and `_clear_query_cache_dirs` helpers), `scripts/clear_pathway_tables.py:420-440` (wiring into `clear_query_data`) + dry-run preview block

**Why.** User's intent with `--keep-pathways`: "pristine slate, only pathway tree survives." The existing code cleared DB tables + `cache/proteins/` + `cache/*.json` (except hierarchy cache), but left:
- `Logs/<protein>/<timestamp>/` step-log dirs (often 20+ per protein)
- `Logs/<protein>/quality_report.json` snapshots
- `Logs/verification_reports/` (84 old reports)
- `Logs/cleanup_reports/`
- `Logs/json_parse_failures.log*`
- `cache/pruned/`, `cache/hierarchy_checkpoints/`, `cache/hierarchy_reports/` empty dirs

**Change.** Two new pure helpers mirroring the existing `_clear_cache_files` contract:
- `_clear_logs_tree()` — wipes contents of Logs/; preserves directory shells so `step_logger` / verify pipeline don't need `mkdir` on next run.
- `_clear_query_cache_dirs()` — wipes contents of `cache/pruned`, `cache/hierarchy_checkpoints`, `cache/hierarchy_reports`.

Both called from `clear_query_data` right after the existing cache cleanup, and in the dry-run preview block so `--dry-run` shows what will be wiped. Preserves `cache/pathway_hierarchy_cache.json` and `cache/ontology_hierarchies/*` (reference data quick_assign depends on).

---

### C3 + C5. Deleted stray top-level files

**Deleted** (via `rm`):
- `2026-04-11-143219-local-command-caveatcaveat-the-messages-below.txt` (77K Claude session dump)
- `2026-04-11-143237-local-command-caveatcaveat-the-messages-bel2ow.txt` (2B artifact)
- `instance/fallback.db` (0 bytes SQLite shell)
- `instance/interactions.db` (0 bytes SQLite shell)
- `dah.md/` (empty directory)
- `logs so far.md` (92K historical session doc — deleted manually by user)

---

### C4. `.env` full rewrite

**File**: `.env` (complete replacement — 144 lines → 98 lines)

**Why.** Original had:
- Duplicate `VERBOSE_PIPELINE` (lines 21, 35)
- Duplicate `ENABLE_LOG_ROTATION=true` (lines 52, 84)
- Duplicate `CHAIN_OVERLAP_SCAN_CAP` with conflicting values (1000 vs 5000)
- Duplicate `SKIP_CITATION_VERIFICATION=true` (3 times)
- Duplicate `SKIP_DIRECT_LINK_EXTRACTION=false`
- 7 commented-out dead Gemini model overrides (lines 9-15)
- 4 commented-out validation batch flags
- Mixed casing confusion (`skip_citation_verification=True` lowercase + `SKIP_CITATION_VERIFICATION=true`)

**Change.** Rewrote into 10 clearly-labeled sections with exactly one value per key, grouped by concern (GCP → DB → NCBI → Pipeline → Caps → Validators → Skip toggles → Chain unification → Opt-ins → Log rotation → Flask). Preserved the intentional `DATABASE_URL=DATABASE_URL=postgresql://…` double-prefix (matches Railway's secret-export format; the reader strips the prefix). Preserved all values code actually reads; dropped duplicates and dead comments.

---

### C6. New `.gitignore`

**File**: `.gitignore` (NEW)

**Why.** Project had no `.gitignore` at all. `pytest-of-aryan/` (376K test artifacts) and nine `__pycache__/` directories were tracked. Session dumps (`.txt` files) kept leaking in.

**Change.** Added comprehensive ignore patterns:
- Python build artifacts: `__pycache__/`, `*.pyc`, `*.egg-info/`
- Virtual envs: `.venv/`, `venv/`, `env/`
- Test runner output: `.pytest_cache/`, `pytest-of-*/`, `.coverage`, `htmlcov/`
- OS junk: `.DS_Store`, `Thumbs.db`
- Editor junk: `.idea/`, `.vscode/`, `*.swp`
- Claude/Serena session state: `.serena/`, `.claude/`, `MEMORY.md`
- SQLite fallback: `instance/*.db`
- Per-protein / verification log outputs (rotated by pruner anyway): `Logs/*/`, `Logs/*.log*`, `Logs/*/quality_report.json`
- Query-generated cache: `cache/proteins/`, `cache/pruned/`, `cache/hierarchy_checkpoints/`, `cache/hierarchy_reports/`, `cache/*.json` — with a `!cache/pathway_hierarchy_cache.json` negative pattern to preserve the curated one
- Top-level Claude session dumps: `2026-*-local-command-caveat*.txt`
- Secrets: `.env`, `.env.local`, `*.pem`, `*.key`

---

## Files modified / created / deleted (full inventory)

| File | Status | Change summary |
|---|---|---|
| `.env` | MODIFIED | A1 (auto-correct flags true) + C4 (full rewrite) |
| `.gitignore` | CREATED | C6 |
| `alembic.ini` | MODIFIED | B1b (removed whitespace-buggy `version_locations`) |
| `models.py` | MODIFIED | B7 (_utcnow helper + 20 refs) |
| `runner.py` | MODIFIED | A5 (salvage + second _reconstruct_chains call) + B5 (retire standard mode) |
| `services/data_builder.py` | MODIFIED | B2 (5 dead JSONB writes removed) + B8 (1 .query.get) |
| `utils/db_sync.py` | MODIFIED | A10 (delete _save_chain_context_claims) + B7 (7 utcnow) + B8 (1 .query.get) |
| `utils/post_processor.py` | MODIFIED | Comment cleanup after A10 |
| `utils/protein_aliases.py` | MODIFIED | B8 (1 .query.get) |
| `utils/protein_database.py` | MODIFIED | B7 (2 utcnow) |
| `routes/results.py` | MODIFIED | B8 (1 .query.get) |
| `scripts/clear_pathway_tables.py` | MODIFIED | C1+C2 (Logs/ + cache scratch wipe) |
| `scripts/fix_direct_link_arrows.py` | MODIFIED | B7 (2 utcnow) |
| `scripts/validate_existing_arrows.py` | MODIFIED | B7 (6 utcnow) |
| `scripts/migrate_add_interaction_chain_id.py` | MODIFIED | B8 (1 .query.get) |
| `scripts/pathway_v2/quick_assign.py` | MODIFIED | A3 (PI sync) + A6 (JSONB augment removed) + A8 (chain_ids param) + A9 (idempotency) |
| `scripts/pathway_v2/step7_checks.py` | MODIFIED | A2 (auto_fixable=True) + A7 (env-gate) |
| `scripts/pathway_v2/step7_repairs.py` | MODIFIED | A2 (new repair + route) + A7 (env-gate) + B8 (15 .query.get) |
| `scripts/pathway_v2/step5_discover_siblings.py` | MODIFIED | B8 (2 .query.get) |
| `scripts/pathway_v2/step6_reorganize_pathways.py` | MODIFIED | B8 (6 .query.get) |
| `scripts/pathway_v2/step6_utils.py` | MODIFIED | B7 (1 utcnow) + B8 (4 .query.get) |
| `scripts/pathway_v2/verify_pipeline.py` | MODIFIED | A4 (bounded repair loop + merge helper) |
| `pipeline/config_dynamic.py` | MODIFIED | B4 (comment clarification) |
| `migrations/versions/20260420_0001_drop_dead_tables.py` | CREATED | B1 (drop 7 dead tables) — applied |
| `SESSION_2026-04-20_CHANGES.md` | CREATED | This doc |
| `2026-04-11-143219-…txt` | DELETED | C3 |
| `2026-04-11-143237-…txt` | DELETED | C3 |
| `instance/fallback.db` | DELETED | C5 |
| `instance/interactions.db` | DELETED | C5 |
| `dah.md/` | DELETED | C3 |
| `logs so far.md` | DELETED | C3 (by user) |

---

## Verification — live DB after everything

```
orphan_pathway_interactions      = 0       # A6 — PIs sourced only from claims now
function_context_drift_rows      = 0       # A2 — auto-fix repair clears them
pathways                         = 954     # preserved
pathway_parents                  = 936     # preserved
proteins                         = 0       # post --keep-pathways
interactions                     = 0
interaction_claims               = 0
indirect_chains                  = 0

Dead-tables-after-migration:
  interaction_chains                 ✓ dropped
  interaction_pathways               ✓ dropped
  interaction_query_hits             ✓ dropped
  pathway_canonical_names            ✓ dropped
  pathway_hierarchy                  ✓ dropped
  pathway_hierarchy_history          ✓ dropped
  pathway_initial_assignments        ✓ dropped

alembic_version = 20260420_0001           # ← head, stamped
```

---

## Test suite — 599 pass / 15 pre-existing failures

All 15 failures reproduced WITHOUT this session's changes (verified via `git stash push` → re-run → same failures). Unrelated: they concern `test_gemini_runtime` (model config mismatch), `test_prompt_architecture::TestPipelineEquivalence` (assertion on step counts that drifted from current config), `test_iterative_research::TestGenerateIterativePipeline` (step count assertions), `test_chat_interactions`, `test_checkpoint_save`, `test_evidence_validator`, `test_integration_post_processing`, `test_pipeline_orchestration::TestCreateSnapshotFromCtx::test_ndjson_format_correct`. None touch any of the modified files in this session.

---

## Expected delta on the next TDP43 query

| Old log line | After this session |
|---|---|
| `[ARROW DRIFT] … auto_correct=False` × 30+ | gone — validator rewrites in-place (A1) |
| `[DIRECTION DRIFT] … auto_correct=False` × 25+ | gone (A1) |
| `[PATHWAY DRIFT] … auto_correct=False` | gone (A1) |
| `[CHAIN MISSING] 'BAX': … no chain_context.full_chain` × 6 | gone — A5 salvage fills mediator_chain from upstream_interactor + second reconstruct call after resolution |
| `[FAIL] pathway_claim_consistency (11 issues)` | `[OK]` — A6 made orphans impossible by construction |
| `[FAIL] chain_pathway_consistency (1 issue)` | `[OK]` — A7 env-gated off since `CHAIN_PATHWAY_UNIFY=false` |
| `[FAIL] function_context_drift (5 issues)` | `[OK]` — A2 auto-fix repair |
| `STATUS: [FAIL] FAIL` | `STATUS: [OK*] PASS_WITH_FIXES` or `[OK] PASS` |

And a second run of the same query logs `Quick assign claims: idempotent no-op — N claim(s) already fully assigned` within ~1 sec (A9).
