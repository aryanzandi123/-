# Session Changes — 2026-04-21 + Handoff to Frontend Session

This file is a **handoff brief** for a fresh Claude Code session tasked with untangling frontend/UI/visualization issues. It documents (1) everything that landed backend-side on 2026-04-20 and 2026-04-21, (2) the state of the data the frontend receives, (3) every frontend entrypoint you need to know about, and (4) known rough edges. Treat it as complete context — you should not need to read the prior session dump unless you want to.

The prior session dump (`SESSION_2026-04-20_CHANGES.md`) covers the persistence-layer cleanup. The summary below includes the key carryover points.

---

## Project map (the one-screen version)

```
/Users/aryan/Desktop/DADA/untitled folder 2 copy 54/
├── app.py                          Flask app factory; SQLAlchemy on Railway PG
├── runner.py                       Pipeline orchestrator (~5800 lines, the heart)
├── models.py                       SQLAlchemy ORM (Protein, Interaction,
│                                   InteractionClaim, IndirectChain, Pathway, PathwayParent,
│                                   PathwayInteraction, ProteinAlias)
├── visualizer.py                   Data-quality-warning heuristic (backend-only;
│                                   NOT the JS visualizer)
├── services/
│   └── data_builder.py             Builds the JSON payload served to /api/visualize/<protein>
├── routes/
│   ├── query.py                    /api/search, /api/query (POST), /api/stream (SSE),
│   │                               /api/status, /api/cancel
│   ├── results.py                  /api/results, /api/chain/<id>, /api/pathway/<id>/interactors,
│   │                               /api/claims/<protein>, /api/databased-interactors/<protein>,
│   │                               /api/protein/<symbol>/interactions
│   ├── pipeline.py                 /api/queries, /api/pipeline/*, /api/repair-pathways/<protein>
│   ├── visualization.py            / (index) and /api/visualize/<protein>
│   ├── chat.py                     /api/chat/*
│   └── health.py                   /health, /metrics
├── pipeline/prompts/               Gemini prompt factories
│   ├── iterative_research_steps.py Iterative discovery (the default PIPELINE_MODE)
│   ├── deep_research_steps.py      Chain resolution (2ab, 2ab2/3/5, 2ax, 2az)
│   └── shared_blocks.py            Common prompt fragments + schema docs
├── utils/
│   ├── arrow_effect_validator.py   Arrow validation (the quota offender — see below)
│   ├── db_sync.py                  JSON payload → DB writer (~2200 lines)
│   ├── chain_view.py               Single-write-surface for chain state (full_chain, legacy fields)
│   ├── chain_resolution.py         Chain ingest + dedup
│   ├── post_processor.py           8-stage post-processing pipeline
│   └── ...
├── scripts/pathway_v2/             Pathway assignment & verification (quick_assign + step7)
├── static/                         Flask static assets — JS lives HERE (see below)
│   ├── visualizer.js               10,668 lines. The D3 graph. Main visualization engine.
│   ├── card_view.js                5,162 lines. Pathway cards / chain shells.
│   ├── modal.js                    2,451 lines. Claim modals.
│   ├── script.js                   1,145 lines. Top-level page orchestration.
│   ├── network_topology.js         255. Force-layout helpers.
│   ├── pipeline_controls.js        178. Query submit / progress UI.
│   ├── neural_particles.js         246. Background animation.
│   ├── shared_utils.js             96.  Shared helpers.
│   └── force_config.js             145. Force sim params.
├── templates/
│   ├── index.html                  Landing page (search, query list)
│   └── visualize.html              609 lines. The main visualization template.
├── react-app/                      SEPARATE Vite+TS island — only the "pipeline events" drawer.
│                                   Main graph is plain JS + D3 in static/.
│   └── src/
│       ├── islands/pipeline-events/
│       │   ├── main.tsx
│       │   └── PipelineEventsDrawer.tsx
│       └── shared/useSSE.ts
└── migrations/                     Alembic migrations (baseline + 20260420_0001 drop_dead_tables)
```

**Key architecture call**: the main visualization is NOT React. It's ~18K lines of vanilla JS + D3 under `static/`. Only the live pipeline-progress drawer is React (`react-app/src/islands/pipeline-events/`). When you see "frontend bug," 95% probability it's in `static/visualizer.js`, `static/card_view.js`, or `static/modal.js`.

---

## Backend state (what the frontend receives)

### Primary endpoint consumed by the viz page
`GET /api/visualize/<protein>` in `routes/visualization.py:79` — delegates to `services/data_builder.build_full_json_from_db(protein)`.

The JSON shape at the top level:
```jsonc
{
  "main": "<protein>",
  "snapshot_json": {
    "main": "<protein>",
    "interactors": [ /* see below */ ]
  },
  "ctx_json": { /* same structure, for legacy consumers */ },
  "interactions": [ /* flat list the frontend iterates */ ],
  "chain_link_functions": { /* per-hop functions keyed by "A->B" pair */ },
  "_pipeline_metadata": { /* optional: unvalidated_interactors, validation_incomplete, etc. */ }
}
```

Each entry in `interactions[]` / `snapshot_json.interactors[]` has:
```jsonc
{
  "primary": "HNRNPA1",             // partner protein symbol
  "interaction_type": "direct" | "indirect",
  "confidence": 0.0–1.0,
  "direction": "main_to_primary" | "primary_to_main",
  "arrow": "binds"|"activates"|"inhibits"|"regulates",
  "arrows": { "a_to_b": [...], "b_to_a": [...] },   // JSONB, multi-arrow per direction
  "interaction_effect": "upregulated"|... ,
  "functions": [ /* scientific claims */ ],
  "evidence": [ /* papers */ ],
  "pmids": [ "12345678", ... ],

  // Chain state (indirect only, derived from chain_context.full_chain via ChainView)
  "mediator_chain": ["X", "Y"],      // between query and target, not including either endpoint
  "upstream_interactor": "Y",        // last element of mediator_chain
  "depth": N,                        // N-1 = number of hops between query and target
  "chain_context": {
    "full_chain": ["QUERY", "X", "Y", "TARGET"],
    "query_position": 0,             // query's index in full_chain
    "chain_length": 4
  },
  "chain_with_arrows": [             // per-hop arrow resolution
    {"from": "QUERY", "to": "X", "arrow": "activates"},
    ...
  ],

  // Flags (underscore-prefixed = internal metadata)
  "_synthesized_from_chain": true,   // chain hop synthesized at read time
  "_from_parent_chain_filtered": true,
  "_chain_salvaged_from_upstream": true,           // NEW this session
  "_chain_salvage_reason": "llm_emitted_upstream_without_full_chain",
  "_validation_metadata": {          // set by arrow_effect_validator
    "validator": "arrow_effect_validator",
    "validated": true,
    "skipped": false
  },
  "_validation_skipped_reason": "quota_transient"  // NEW this session
}
```

**Frontend-relevant invariants after this session:**
- Every `PathwayInteraction` is backed by a claim (no orphans). Card view can trust pathway membership.
- `function_context` on the parent interaction always agrees with its claims' rollup.
- Chain hops can now be length-2 through length-6+ (was capped at 3 before). The D3 renderer already handles arbitrary N per `visualizer.js:2544` and the shell-labeling at `card_view.js:2169`.
- Dead tables (7 of them) are gone — no frontend code ever read them.

### Secondary endpoints
| Endpoint | File:Line | Purpose |
|---|---|---|
| `GET /api/search/<protein>` | routes/query.py:20 | Search suggestions |
| `POST /api/query` | routes/query.py:59 | Start pipeline run |
| `POST /api/requery` | routes/query.py:266 | Re-run existing query |
| `GET /api/status/<protein>` | routes/query.py:340 | Poll job status |
| `GET /api/stream/<protein>` | routes/query.py:359 | **SSE** progress stream (consumed by react-app/src/islands/pipeline-events/) |
| `POST /api/cancel/<protein>` | routes/query.py:450 | Cancel running job |
| `GET /api/results/<protein>` | routes/results.py:36 | Protein-scoped result blob |
| `GET /api/chain/<int:chain_id>` | routes/results.py:52 | IndirectChain detail |
| `GET /api/pathway/<id>/interactors` | routes/results.py:144 | Pathway → interactor list |
| `GET /api/claims/<protein>` | routes/results.py:206 | All claims for a protein |
| `GET /api/databased-interactors/<protein>` | routes/results.py:276 | DB-only view |
| `GET /api/protein/<symbol>/interactions` | routes/results.py:358 | Cross-query interactions |
| `GET /api/queries` | routes/pipeline.py:52 | List of known proteins |
| `POST /api/pipeline/clear` | routes/pipeline.py:107 | Admin clear |
| `POST /api/repair-pathways/<protein>` | routes/pipeline.py:188 | Manual step7 verify |

---

## Everything this session changed (2026-04-20 and 2026-04-21)

Work happened in four rounds. Listed in chronological order; each tier is independent but all landed.

### Round 1 — Persistence-layer cleanup (details in SESSION_2026-04-20_CHANGES.md)
- `.env`: flipped `ARROW_AUTO_CORRECT`, `DIRECTION_AUTO_CORRECT`, `PATHWAY_AUTO_CORRECT` to `true`
- `step7_checks` + `step7_repairs`: made `function_context_drift` auto-fixable with a new `repair_function_context_drift`
- `quick_assign.unify_one_chain_pathway`: now calls `_sync_pathway_interactions` after unify
- `verify_pipeline`: bounded 3-pass repair loop + `_merge_repair_summaries`
- `runner._reconstruct_chains`: salvage length-3 chains from `upstream_interactor` + second pass after chain resolution
- `quick_assign._sync_pathway_interactions`: **removed JSONB augmentation** — PIs now sourced ONLY from claims (eliminates orphans by construction)
- `check_chain_pathway_consistency` + repair: env-gated on `CHAIN_PATHWAY_UNIFY` so diverse chains aren't silently flattened
- `_unify_all_chain_claims`: new `chain_ids` scope param (precise scoping)
- `quick_assign_claims`: idempotency short-circuit (re-queries finish sub-second with `skipped_noop=True`)
- Deleted never-called `_save_chain_context_claims` + 5 dead JSONB writes (`_chain_context_functions`, `_chain_context_overlay`)
- Retired `PIPELINE_MODE=standard` branch in runner.py
- `datetime.utcnow` → `_utcnow()` helper / `datetime.now(timezone.utc).replace(tzinfo=None)` (32 call sites)
- `Model.query.get(id)` → `db.session.get(Model, id)` (32 call sites)
- New Alembic migration drops 7 dead tables (applied live — `alembic_version = 20260420_0001`)
- `--keep-pathways` now wipes `Logs/<protein>/timestamp/`, `Logs/verification_reports/`, `Logs/cleanup_reports/`, scratch cache dirs
- New `.gitignore`; rewrote `.env` cleanly (144 → 98 lines); deleted session dumps + `dah.md/` + empty `instance/*.db`

### Round 2 — Chain-length un-capping (uses up the length-3 ceiling)
Fixed the LLM prompt bias that caused every chain to cap at 3 proteins:

| File | Change |
|---|---|
| `pipeline/prompts/iterative_research_steps.py:85-90` | Flipped framing: "length-3 is MINIMUM" → "length-3 is RARE, almost always incomplete" |
| `pipeline/prompts/iterative_research_steps.py:109-113` | Fixed the "Length-4 cascade" example that showed only 3 proteins — now `CHIP → STUB1 → HSPA8 → query` with `depth: 4` |
| `pipeline/prompts/deep_research_steps.py:391-395` | Step2ab resolver: "Chain length NOT capped. Length-3 rare; most cascades 4+. Do NOT collapse." |
| `pipeline/prompts/deep_research_steps.py:519-522` | Step2ab3 hidden-chain resolver: symmetric |
| `runner.py` (salvage block ~1750-1802) | Louder `[CHAIN SALVAGE]` WARN log + `_chain_salvage_reason` tag so salvaged length-3 chains are distinguishable from real ones |
| `visualizer.py:14` | Deleted stale `depth limit = 3` docstring |

**Confirmed working**: the 2026-04-21 ATXN3 run produced chains up to `length=5` (`ATXN3 → BECN1 → PIK3C3 → ATG14 → ATG7`), `length=4` (`ATXN3 → FOXO4 → POLR2A → SOD2`), plus some length-3s. The LLM is no longer stuck at 3.

### Round 3 — Arrow validation quota & log hygiene

| Fix | File | Change |
|---|---|---|
| 3.1 | `utils/arrow_effect_validator.py:53` | `DEFAULT_MAX_WORKERS: 20 → 5` (stops tripping Vertex per-minute RPM) |
| 3.1b | `utils/arrow_effect_validator.py` ~430-465 | New retry loop around the `generate_content` call: 3 attempts, 2s/4s/8s exponential backoff on transient 429; daily quota bypasses retry |
| 3.2 | `utils/arrow_effect_validator.py` transient-quota branch | Now increments `quota_skipped_calls` AND calls `_mark_validation_skipped(..., "quota_transient")` so metric is honest + downstream can badge "unvalidated" |
| 3.3 | `utils/db_sync.py:1112-1130` | `[DB SYNC] Preserving direct interaction: X↔Y` deduped per `(protein_a_id, protein_b_id)` — prints once per pair, not once per chain hop |
| 3.4 | `scripts/pathway_v2/quick_assign.py:873, 883` | Two silent `counters["failed"] += 1` sites now emit `[CLAIM FAILED] interaction_id=… function=… reason=…` via logger.warning |
| 3.5 | `visualizer.py:136-157` | Heuristic "arrow may not match function name" now skips when `_validation_metadata.validated == True` (no more false positives for validated arrows) |

### Round 4 — Critical bugfix: the `sys` NameError
My Round 3.1b retry loop used `sys.stderr` and a local `import time` alias, but `arrow_effect_validator.py` had never imported `sys`. On the ATXN3 run, this made every arrow validation fail with `name 'sys' is not defined` (10 errors). Fixed:

```python
# Top of utils/arrow_effect_validator.py
import os
import sys       # ← ADDED
import json
import re
import threading
import time      # ← ADDED (promoted from local-only)
```

Runtime-import check confirms module loads cleanly. **The next query will actually execute arrow validation** — which in turn means `_validation_metadata` will be set, which in turn means Round 3.5's heuristic gating will kick in and the false-positive DQW warnings will disappear.

---

## What the next Claude session needs to know (frontend focus)

### Expected behavior on the next TDP43/ATXN3 query
- Arrow validation ACTUALLY runs (no `name 'sys' is not defined`)
- `_validation_metadata.validated=true` set on every successfully-validated function
- Chain lengths up to 5-6+ appear in `chain_context.full_chain`
- `[DB SYNC] Preserving direct interaction:` fires once per pair
- `[CLAIM FAILED] …` replaces silent `1 failed` when it happens
- Data Quality Warnings section drops the false positives

### Frontend observations I can offer

**Main graph rendering** — `static/visualizer.js` is a 10.7K-line D3 v7 setup. Central functions to look at first:
- The initial snapshot consumer (search for `snapshot_json` early in the file)
- The chain render path (search for `chain_with_arrows`, `full_chain`)
- The interactor-node creation (search for `enter().append("g")`)
- The modal-opening handler (search for `onClick`, `modal.js` calls)

**Card view** — `static/card_view.js:2169` has the `shellLabels` dynamic depth labeling. It already handles length-4+ chains. If chain shells still look wrong despite backend producing length-5 data, suspect:
- A slicing like `.slice(0, 3)` hidden somewhere in the node-positioning
- Hard-coded shell positions expecting exactly 2 shells

**Modal rendering** — `static/modal.js` renders scientific claims. Each claim has `function_name`, `mechanism` (cellular_process), `effect_description`, `biological_consequences[]`, `specific_effects[]`, `evidence[]`, `pmids[]`, `arrow`, `pathway_name`, `function_context`.

**Per-hop claims** — `chain_link_functions` in the top-level payload is a dict keyed by `"A->B"` pair string. For a hop like `VCP->UFD1`, the frontend should render THIS dict entry (not the parent-fallback). **Known backend data gap**: many hops show `[DB SYNC] Chain hop X->Y: no LLM functions + no DB rehydration` — meaning that hop's specific functions weren't generated. The read-time fallback uses parent-interaction functions. If the frontend displays parent functions for a hop without any visual indicator, users can't tell "this biology is specifically the VCP→UFD1 hop" vs "this is ATXN3's general VCP biology." Consider adding a "from parent" badge.

### Known data quirks likely to be surfacing in the UI

1. **`arrow` column vs `arrows` JSONB**: `Interaction.arrow` is the scalar legacy mirror; `arrows` is the real JSONB. Some JS readers may still prefer `arrow`; for proper directional rendering consume `arrows.a_to_b[0]` / `arrows.b_to_a[0]`. This dual-storage will be collapsed in a future refactor (noted as B6 in the prior session doc).
2. **`_chain_salvaged_from_upstream=true`**: these length-3 chains are synthetic. Consider adding a visual indicator ("reconstructed — LLM emission incomplete") so users know they're not ground-truth biology. Field: `interactor._chain_salvaged_from_upstream`.
3. **`_validation_skipped_reason="quota_transient"`**: arrows on these functions weren't validated because of a transient 429 after retries. Consider an un-validated badge. Field: `function._validation_skipped_reason` or `interactor._validation_metadata.skipped`.
4. **`_pipeline_metadata.unvalidated_interactors`** (top-level): list of interactor names whose validation was skipped. A page-level banner ("validation incomplete for N interactors") is a low-effort add.
5. **CHAIN_ARROW_DRIFT** messages from backend: when the canonical `arrows` JSONB for a direct interaction disagrees with the dominant hop-function arrow, db_sync prints and trusts the hop function. The written `chain_with_arrows[i].arrow` reflects the hop's truth. If the frontend reads `Interaction.arrow` instead of `chain_with_arrows`, it'll render the old (incorrect) value. Worth auditing.
6. **Pathway-card zero-claim protection** — Round 1 Fix 6 removed JSONB-based PI augmentation. Any pathway card that previously showed an interactor via JSONB but not via claims will no longer appear. This is correct, but users who memorized the old display may notice missing pills.

### Common frontend-vs-backend drift patterns you'll likely see

- **Frontend uses hardcoded field name that backend renamed** — grep `static/` for likely stale field names (e.g. `upstream_interactor`, `mediator_chain`, `function_effect`, `chain_link_functions`). The backend might emit newer names; double-check what the payload actually has vs what the JS reads.
- **Frontend hardcodes depth=3 assumptions** — now proven false. Any node-placement code that does `if (depth === 3)` or `case 3:` is suspect. Audit `visualizer.js` for numeric-depth branching.
- **Frontend uses `function.arrow` but backend auto-corrects via `_arrow_corrected_from`** — after Round 1, the `arrow` field IS the corrected value. `_arrow_corrected_from` is only present when correction happened. If the frontend shows "binds" visually but backend says "activates," the frontend might be reading stale cache; force-reload.
- **Frontend expects `chain_link_functions[pair_key]` to always be populated** — many hops are empty (read-time fallback to parent functions). Frontend needs to handle the empty-hop case gracefully.
- **Pagination / slicing** — the prior audit found `chain_link_functions` entries of length 10 in `.env` (`MAX_CHAIN_CLAIMS_PER_LINK=10`). Frontend may or may not respect this cap.

---

## How to run & verify

### Start the app
```bash
cd "/Users/aryan/Desktop/DADA/untitled folder 2 copy 54"
python3 app.py
# → http://127.0.0.1:5004
```

### Run a fresh query
1. Hit `http://127.0.0.1:5004/`
2. Search for a protein (e.g. `TDP43`, `ATXN3`, `HDAC6`, `PERK`)
3. Click through to visualization
4. Check terminal for `[CHAIN SALVAGE]`, `[CLAIM FAILED]`, `[ARROW METRICS]` lines

### Clear DB to a pristine state (keeps pathway hierarchy)
```bash
python3 scripts/clear_pathway_tables.py --keep-pathways --dry-run   # preview
python3 scripts/clear_pathway_tables.py --keep-pathways             # actually run
```

### One-shot DB health check
```bash
python3 -c "
from app import app, db
from sqlalchemy import text
with app.app_context():
    for sql,label in [
      ('SELECT COUNT(*) FROM pathway_interactions pi LEFT JOIN interaction_claims ic ON ic.interaction_id=pi.interaction_id AND ic.pathway_id=pi.pathway_id WHERE ic.id IS NULL', 'orphan_pis'),
      ('SELECT COUNT(*) FROM interactions i JOIN interaction_claims ic ON ic.interaction_id=i.id WHERE i.function_context IS NOT NULL AND i.function_context!=\"mixed\" GROUP BY i.id,i.function_context HAVING COUNT(DISTINCT COALESCE(ic.function_context,\"<null>\"))>1 OR MIN(COALESCE(ic.function_context,\"<null>\"))!=i.function_context', 'drift_rows'),
      ('SELECT depth, COUNT(*) FROM interactions WHERE interaction_type=\"indirect\" GROUP BY depth ORDER BY depth', 'depth_histogram'),
    ]:
        print(label, '=', db.session.execute(text(sql)).scalar() if 'histogram' not in label else list(db.session.execute(text(sql))))
"
```
Expected: `orphan_pis=0`, `drift_rows=0`, histogram with values spanning `depth=2..5+`.

### Tests
```bash
python3 -m pytest tests/ -q
```
Expected: ~599 pass, ~15 failures — all pre-existing (model-name assertions, pipeline-step-count assertions). Verified by stashing changes + re-running in earlier sessions.

### Alembic stamp check
```bash
python3 -c "
from app import app, db
from sqlalchemy import text
with app.app_context():
    print(db.session.execute(text('SELECT version_num FROM alembic_version')).scalar())
"
# → 20260420_0001
```

---

## Files modified across all four rounds

| File | Rounds | Why |
|---|---|---|
| `.env` | 1 | auto-correct flags + full rewrite |
| `.gitignore` | 1 | NEW |
| `alembic.ini` | 1 | whitespace-split bug fix (version_locations deletion) |
| `models.py` | 1 | _utcnow helper + 20 refs |
| `runner.py` | 1, 2 | salvage + second reconstruct call + retire standard-mode + salvage log |
| `services/data_builder.py` | 1 | 5 dead JSONB writes removed + .query.get→session.get |
| `utils/db_sync.py` | 1, 3 | delete _save_chain_context_claims + utcnow + .query.get + preserving-direct dedup |
| `utils/post_processor.py` | 1 | comment cleanup |
| `utils/protein_aliases.py` | 1 | .query.get |
| `utils/protein_database.py` | 1 | utcnow |
| `utils/arrow_effect_validator.py` | 3, 4 | workers 20→5 + retry loop + metric + imports fix |
| `routes/results.py` | 1 | .query.get |
| `pipeline/prompts/iterative_research_steps.py` | 2 | chain-length anti-bias rewording |
| `pipeline/prompts/deep_research_steps.py` | 2 | step2ab + step2ab3 stronger language |
| `pipeline/config_dynamic.py` | 1 | clarifying comments |
| `scripts/clear_pathway_tables.py` | 1 | Logs/ + cache scratch wipe helpers |
| `scripts/fix_direct_link_arrows.py` | 1 | utcnow |
| `scripts/validate_existing_arrows.py` | 1 | utcnow |
| `scripts/migrate_add_interaction_chain_id.py` | 1 | .query.get |
| `scripts/pathway_v2/quick_assign.py` | 1, 3 | unify_one_chain_pathway + JSONB augment removal + chain_ids param + idempotency + failed-claim log |
| `scripts/pathway_v2/step7_checks.py` | 1 | auto_fixable=True + env-gate |
| `scripts/pathway_v2/step7_repairs.py` | 1 | repair_function_context_drift + env-gate + .query.get |
| `scripts/pathway_v2/step5_discover_siblings.py` | 1 | .query.get |
| `scripts/pathway_v2/step6_reorganize_pathways.py` | 1 | .query.get |
| `scripts/pathway_v2/step6_utils.py` | 1 | utcnow + .query.get |
| `scripts/pathway_v2/verify_pipeline.py` | 1 | bounded repair loop + merge helper |
| `visualizer.py` | 2, 3 | stale docstring + heuristic gate on validated metadata |
| `migrations/versions/20260420_0001_drop_dead_tables.py` | 1 | NEW — dropped 7 dead tables (applied) |
| `SESSION_2026-04-20_CHANGES.md` | 1 | NEW |
| `SESSION_2026-04-21_CHANGES.md` | (this doc) | NEW |

Deleted:
- `2026-04-11-143219-local-command-caveatcaveat-the-messages-below.txt`
- `2026-04-11-143237-local-command-caveatcaveat-the-messages-bel2ow.txt`
- `instance/fallback.db`
- `instance/interactions.db`
- `dah.md/`
- `logs so far.md`

---

## Suggested first moves for the frontend session

1. **Run a fresh query** (TDP43 or ATXN3) to generate current-format data in the DB. Inspect the network tab in the browser to get a real `/api/visualize/<protein>` response. Save that response — it's your ground truth for what the frontend receives.

2. **Diff the response against what `visualizer.js` reads**. Grep `static/*.js` for every field name that appears in the response. Any field in the response that's NOT grep-matched in the JS is unused. Any field the JS reads that's NOT in the response is a bug.

3. **Look for hardcoded length-3 assumptions** in `visualizer.js` and `card_view.js`:
   ```bash
   grep -nE "(\.slice\(0, *3\)|depth *=== *3|depth *== *3|shellLabels\[3\]|length *>= *3|length *=== *3)" static/*.js
   ```

4. **Scan for stale field reads** — if the JS reads `_chain_context_functions`, it's reading a field that no longer exists (Round 1 deleted those writes). Same for `step2_proposal`, `step2_function_proposals`, `step3_function_pathways` (kept by backend but rarely needed in frontend).

5. **Check SSE event consumption** — `react-app/src/islands/pipeline-events/` consumes `/api/stream/<protein>`. The event shape is emitted in `utils/observability.py` (or wherever the SSE emits). If the progress drawer is stuck, that's where the mismatch lives.

6. **Check for stale `cache/*.json` file-caches** — some endpoints may still short-circuit to file-cache JSON instead of live DB. Round 1's `--keep-pathways` wipes these, but during dev they come back. If the UI shows wrong data, inspect `cache/<PROTEIN>.json` and `cache/proteins/<protein>/` for stale payloads.

7. **Pipeline-metadata surfacing** — a `_pipeline_metadata.unvalidated_interactors` field is set when arrow validation skips. No frontend code uses it yet. Adding a page-level "⚠ N arrows unvalidated" banner is a quick win for UX honesty.

---

## Contact-like context for the next session

**User's style**: direct, frustrated when things don't work, explicit about what they want, doesn't want planning-theater ("just fix it"), trusts you to be independent. Prefers one-shot big-bang fixes over incremental. Uses auto mode a lot.

**Repo mechanical quirks**:
- Project path contains spaces (`untitled folder 2 copy 54/`). Whitespace-sensitive tooling bites (we caught alembic's `version_locations` splitting). Always quote paths.
- Railway PostgreSQL backing DB (URL in `.env`). The sandbox rate-limits destructive DB operations — user may need to run `alembic upgrade head`, `rm`, etc. manually.
- Python 3.14 environment. Deprecation warnings are common; they are not errors (yet).
- Flask's auto-reloader is enabled in dev — changes to .py files restart the app silently. The DB pool reconnects.
- Pre-existing pytest failures (~15) — treat as noise, don't chase unless confirmed new.

**Things NOT to touch without asking**:
- `.env` contains real Railway credentials
- Don't run destructive `alembic` / `rm` actions without user confirmation
- Don't modify the pathway hierarchy (954 pathways + 936 pathway_parents — curated)
- Don't regress the JSONB-augmentation removal in `_sync_pathway_interactions` (would re-introduce orphan PIs)

**Useful `.env` toggles for diagnosing**:
- `VERBOSE_PIPELINE=true` — full prompts/responses to console
- `CHAIN_PATHWAY_UNIFY=true` — if you want chains flattened to one pathway
- `ENABLE_TIER2_NESTED_PIPELINE=true` — expensive; only for filling every missing chain link
- `VALIDATION_MAX_WORKERS=N` — override arrow-validator parallelism (default now 5)

Good luck. The backend is in much better shape than it was 24h ago; the frontend just needs to catch up.
