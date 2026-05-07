# Pipeline Reliability, Data Integrity & Visualization Fixes

**Date:** 2026-04-03
**Status:** Approved

## Context

The ProPaths protein interaction pipeline has no intermediate checkpoints -- a crash at any point loses all prior work (30-60 min of compute). Post-processing stages have no error handling. Parallel batch failures are swallowed silently. On the data integrity side, the claims deduplication key doesn't match the DB constraint, chain re-runs create duplicate evidence, and the file cache can go stale vs the DB. The visualization breaks on empty networks and renders shared link arrows in the wrong direction.

This design addresses all three categories: pipeline reliability (4 issues), data integrity (3 issues), and visualization (2 issues).

## Decisions Made

- **Checkpoint storage:** Incremental DB saves after each major phase
- **Failure mode:** Retry up to 3x with exponential backoff, then save partial results
- **Partial data visibility:** Hidden while pipeline is running; visible (with marker) if pipeline failed and status is "partial"
- **Cache staleness:** Invalidate file cache on every DB save

---

## 1. Pipeline Reliability

### 1A. Incremental DB Checkpoints

**Files:** `models.py`, `runner.py`, `utils/storage.py`, `services/data_builder.py`

#### Model change (`models.py`)

Add two columns to the `Protein` model:

```python
pipeline_status = db.Column(db.String(20), default="idle", index=True)
# Values: "idle" | "running" | "partial" | "complete"

last_pipeline_phase = db.Column(db.String(50))
# Values: "discovery" | "function_mapping" | "chain_resolution" | "post_processing" | "complete"
```

Generate an Alembic migration for this change.

#### New method: `StorageLayer.save_checkpoint()` (`utils/storage.py`)

```python
def save_checkpoint(
    self,
    protein_symbol: str,
    payload: Dict[str, Any],
    phase_name: str,
) -> None:
    """Save intermediate pipeline results to DB for crash recovery.

    Sets protein.pipeline_status='running' and protein.last_pipeline_phase=phase_name.
    Reuses sync_query_results() for the actual data write.
    """
    if not self._has_db():
        return

    # Sync current payload to DB (reuses existing sync logic).
    # sync_query_results uses upsert semantics -- existing interactions
    # are updated, new ones created. This is safe to call multiple times
    # with growing payloads (each checkpoint adds new interactors).
    db_stats = self._sync_to_db_with_retry(protein_symbol, payload)

    # Update pipeline tracking fields
    with self.flask_app.app_context():
        from models import Protein, db
        protein = Protein.query.filter_by(symbol=protein_symbol).first()
        if protein:
            protein.pipeline_status = "running"
            protein.last_pipeline_phase = phase_name
            db.session.commit()
```

#### Runner changes (`runner.py:run_full_job`)

Insert checkpoint saves after each major phase:

```
# After Phase 1 (discovery) completes:
storage.save_checkpoint(user_query, pipeline_payload, "discovery")

# After Phase 2a (function mapping) completes:
storage.save_checkpoint(user_query, pipeline_payload, "function_mapping")

# After Phase 2b (chain resolution) completes:
storage.save_checkpoint(user_query, pipeline_payload, "chain_resolution")

# After post-processing completes:
storage.save_checkpoint(user_query, final_payload, "post_processing")

# Final save (existing):
storage.save_pipeline_results(user_query, final_payload)
# Inside save_pipeline_results, set pipeline_status="complete"
```

At the start of `run_full_job`, set `pipeline_status="running"`:

```python
with flask_app.app_context():
    from models import Protein, db
    protein = Protein.query.filter_by(symbol=user_query).first()
    if not protein:
        protein = Protein(symbol=user_query)
        db.session.add(protein)
    protein.pipeline_status = "running"
    protein.last_pipeline_phase = None
    db.session.commit()
```

#### Read path gating (`services/data_builder.py`)

In `build_full_json_from_db()`, after loading the protein:

```python
protein = Protein.query.filter_by(symbol=protein_symbol).first()
if not protein:
    return None

if protein.pipeline_status == "running":
    # Pipeline actively running -- hide partial data
    return None

# If "partial", include the status so frontend knows it's incomplete
result = _build_result(protein, ...)
if protein.pipeline_status == "partial":
    result["_pipeline_status"] = "partial"
    result["_completed_phases"] = protein.last_pipeline_phase
```

#### Update `save_pipeline_results` to set "complete"

In `utils/storage.py`, after successful DB sync in `save_pipeline_results()`:

```python
if db_stats is not None:
    stats["db_synced"] = True
    # Mark pipeline as complete
    with self.flask_app.app_context():
        protein = Protein.query.filter_by(symbol=protein_symbol).first()
        if protein:
            protein.pipeline_status = "complete"
            protein.last_pipeline_phase = "complete"
            db.session.commit()
```

### 1B. Retry-then-save for Step Failures

#### `_parse_with_retry` (`runner.py:191-211`)

Expand from 1 retry to 3 retries with exponential backoff. On final failure, return last good payload instead of raising:

```python
def _parse_with_retry(
    step, prompt, raw_output, current_payload, call_kwargs,
    max_retries=3,
):
    """Parse model output; on failure, retry with backoff. Returns last good payload on exhaustion."""
    try:
        return parse_json_output(
            raw_output, list(step.expected_columns), previous_payload=current_payload,
        )
    except PipelineError as first_exc:
        last_exc = first_exc
        for attempt in range(max_retries):
            wait = 2 ** attempt  # 1s, 2s, 4s
            print(
                f"[WARN] Parse failed for {step.name} (attempt {attempt + 1}/{max_retries}), "
                f"retrying in {wait}s: {last_exc}",
                file=sys.stderr, flush=True,
            )
            time.sleep(wait)
            try:
                retry_output, _ = call_gemini_model(step, prompt, **call_kwargs)
                return parse_json_output(
                    retry_output, list(step.expected_columns),
                    previous_payload=current_payload,
                )
            except (PipelineError, Exception) as exc:
                last_exc = exc

        # All retries exhausted -- return current payload and log failure
        print(
            f"[ERROR] All {max_retries} retries failed for {step.name}: {last_exc}",
            file=sys.stderr, flush=True,
        )
        if current_payload is None:
            current_payload = {}
        current_payload.setdefault("_pipeline_metadata", {}).setdefault("failed_steps", []).append({
            "step": step.name,
            "error": str(last_exc),
        })
        return current_payload
```

#### `_run_parallel_batched_phase` (`runner.py:1062`)

The existing code already handles truncated batches with sub-batch retry. The fix targets complete API failures (exception swallowed at line ~1196):

After the futures loop, add retry logic for failed batches:

```python
# After futures processing, identify failed batches
failed_batch_names = []
for idx in range(num_batches):
    if idx not in results:
        failed_batch_names.extend(batches[idx])

if failed_batch_names:
    # Retry failed batches individually with backoff
    for attempt in range(3):
        if not failed_batch_names:
            break
        time.sleep(2 ** attempt)
        retry_calls = [_build_single_call(name) for name in failed_batch_names]
        # ... submit retry calls, collect results
        # Remove successfully retried names from failed_batch_names

    if failed_batch_names:
        current_payload.setdefault("_pipeline_metadata", {}).setdefault("failed_batches", []).append({
            "phase": phase_name,
            "failed_interactors": failed_batch_names,
        })
```

#### `post_processor.run()` (`utils/post_processor.py:420`)

Wrap each stage in try/except with retry:

```python
for stage in self.active_stages():
    if stage.requires_api_key and not api_key:
        skipped_stages.append(stage.name)
        continue

    current_step += 1
    if update_status:
        update_status(text=stage.label, current_step=current_step, total_steps=total_steps)

    last_exc = None
    for attempt in range(4):  # 1 initial + 3 retries
        try:
            payload = stage.fn(payload, api_key=api_key, verbose=verbose,
                               step_logger=step_logger, user_query=user_query,
                               flask_app=flask_app)
            break
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                wait = 2 ** attempt
                print(f"[WARN] Stage '{stage.name}' failed (attempt {attempt + 1}/4), "
                      f"retrying in {wait}s: {exc}", file=sys.stderr, flush=True)
                time.sleep(wait)
    else:
        # All attempts failed -- log and continue
        print(f"[ERROR] Stage '{stage.name}' failed after 4 attempts: {last_exc}",
              file=sys.stderr, flush=True)
        payload.setdefault("_pipeline_metadata", {}).setdefault("failed_stages", []).append({
            "stage": stage.name,
            "error": str(last_exc),
        })
```

### 1C. Guaranteed Save on Any Exit Path (`runner.py:run_full_job`)

Wrap the entire pipeline execution in try/except/finally:

```python
def run_full_job(user_query, ...):
    current_payload = None
    try:
        # ... existing pipeline code ...
        # Set pipeline_status = "running" at start
        # Checkpoint after each phase
        # Final save sets "complete"
    except Exception as exc:
        # Unhandled crash -- save whatever we have
        if current_payload:
            try:
                storage.save_checkpoint(user_query, current_payload, "crashed")
                _set_pipeline_status(user_query, "partial", flask_app)
            except Exception:
                pass  # Best effort
        # Update job state
        with jobs_lock:
            if user_query in jobs:
                jobs[user_query]["status"] = "error"
                jobs[user_query]["progress"] = f"Pipeline error: {exc}"
                jobs[user_query]["_finished_at"] = time.time()
        raise
```

---

## 2. Data Integrity

### 2A. Claims Dedup Key Fix

**File:** `utils/db_sync.py:738`

Change the deduplication dictionary key from `function_name` to `(function_name, pathway_name)`:

```python
# Line 738 -- change:
_existing_by_name = {c.function_name: c for c in existing_claims}

# To:
_existing_by_key = {(c.function_name, c.pathway_name): c for c in existing_claims}
```

Update ALL lookups in `_save_claims` that reference this dict:

- Line ~763: `existing = _existing_by_name.get(func_name)` becomes `_existing_by_key.get((func_name, pathway_name))`
- Line ~792: Same pattern
- Line ~827: Same pattern
- Any other references to `_existing_by_name`

### 2B. Chain Duplicate Evidence Prevention

**File:** `utils/db_sync.py`, in `sync_chain_relationships` around line 1039

When an existing interaction is found for a chain link, check for duplicate claims before saving:

```python
if existing:
    if chain_link_funcs:
        # Check which claims are actually new
        existing_chain_claims = InteractionClaim.query.filter_by(
            interaction_id=existing.id,
            function_context="chain_derived",
        ).all()
        existing_claim_keys = {
            (c.function_name, c.pathway_name) for c in existing_chain_claims
        }
        new_funcs = [
            f for f in chain_link_funcs
            if (f.get("function", ""), f.get("pathway")) not in existing_claim_keys
        ]
        if new_funcs:
            self._save_chain_context_claims(
                interaction=existing, functions=new_funcs,
                chain_record=chain_record, discovered_in=query_protein,
                chain_pathway=chain_pathway,
            )
    if parent_interaction and chain_record:
        self._tag_claims_with_chain(parent_interaction, chain_record)
    continue
```

### 2C. Cache Invalidation on DB Save

**File:** `utils/storage.py`

Add cache invalidation inside `save_pipeline_results()`, right after successful DB sync:

```python
# After db_stats success check (around line 86):
if stats["db_synced"]:
    self._invalidate_file_cache(protein_symbol)
```

New static method:

```python
@staticmethod
def _invalidate_file_cache(protein_symbol: str) -> None:
    """Delete stale file cache after DB write -- DB is source of truth."""
    for suffix in ("", "_metadata"):
        path = os.path.join(CACHE_DIR, f"{protein_symbol}{suffix}.json")
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
```

Then the existing `_write_file_cache()` call (which runs after invalidation) re-creates the files from the freshly-saved payload.

---

## 3. Visualization

### 3A. Empty Network Handling

**File:** `static/visualizer.js`, around line 2739

Add early return with clean empty state before the D3 force simulation setup:

```javascript
// After proteins/interactions extraction, before force simulation:
if (interactions.length === 0 && (!SNAP.interactors || SNAP.interactors.length === 0)) {
    const svg = d3.select('#graph-container svg');
    svg.selectAll('*').remove();

    const width = svg.node().getBoundingClientRect().width || 800;
    const height = svg.node().getBoundingClientRect().height || 600;

    svg.append('text')
        .attr('x', width / 2)
        .attr('y', height / 2)
        .attr('text-anchor', 'middle')
        .attr('fill', 'var(--text-secondary, #888)')
        .attr('font-size', '16px')
        .text('No interactions found for ' + (SNAP.main || 'this protein'));

    // Disable interactive controls that reference nonexistent data
    document.querySelectorAll('.filter-chip, .legend-item, .stats-bar .stat')
        .forEach(el => el.style.opacity = '0.3');

    return;
}
```

Also add similar empty checks in:
- Card view init (`card_view.js`) -- show "No interactions to display"
- Table view init -- show empty table message

### 3B. Shared Link Direction Fix

**File:** `services/data_builder.py`, in the shared interactions section (~line 585)

Replace the current canonical-order assignment with direction-aware source/target:

```python
shared_data = shared_ix.data.copy()
shared_data["_db_id"] = shared_ix.id

# Use stored absolute direction for correct arrow rendering
if shared_ix.direction == "a_to_b":
    shared_data["source"] = protein_a.symbol
    shared_data["target"] = protein_b.symbol
elif shared_ix.direction == "b_to_a":
    shared_data["source"] = protein_b.symbol
    shared_data["target"] = protein_a.symbol
else:
    # Bidirectional or unknown -- alphabetical order for consistency
    if protein_a.symbol < protein_b.symbol:
        shared_data["source"] = protein_a.symbol
        shared_data["target"] = protein_b.symbol
    else:
        shared_data["source"] = protein_b.symbol
        shared_data["target"] = protein_a.symbol

shared_data["direction"] = shared_ix.direction or "bidirectional"
shared_data["_is_shared_link"] = True
```

---

## Files Modified (Summary)

| File | Changes |
|------|---------|
| `models.py` | Add `pipeline_status`, `last_pipeline_phase` columns to Protein |
| `runner.py` | Checkpoint saves after each phase, try/except wrapper, retry logic in `_parse_with_retry` |
| `utils/storage.py` | New `save_checkpoint()`, `_invalidate_file_cache()`, set status="complete" in `save_pipeline_results()` |
| `utils/post_processor.py` | Wrap each stage in try/except with 3 retries and exponential backoff |
| `utils/db_sync.py` | Fix claims dedup key (line 738), add chain duplicate prevention (line ~1039) |
| `services/data_builder.py` | Pipeline status gating in `build_full_json_from_db()`, shared link direction fix |
| `static/visualizer.js` | Empty network graceful handling |
| `static/card_view.js` | Empty state for card view |
| Migration script | Alembic migration for new Protein columns |

## Verification

1. **Checkpoint recovery:** Start pipeline, kill process mid-function-mapping. Verify DB has discovery data, `pipeline_status="partial"`. Re-run query, verify it resumes.
2. **Retry-then-save:** Mock Gemini to return malformed JSON 4 times. Verify 3 retries happen, then partial save occurs with `failed_steps` metadata.
3. **Post-processor resilience:** Mock NCBI API to fail. Verify citation stage retries 3x, logs failure, and remaining stages still execute.
4. **Claims dedup:** Create interaction with same function in two pathways. Re-run sync. Verify both claims exist (not collapsed).
5. **Chain dedup:** Run pipeline twice for same protein. Verify no duplicate PMID arrays in chain-derived interactions.
6. **Cache invalidation:** Save to DB, verify cache files deleted, verify re-created from fresh payload.
7. **Empty network:** Query protein with zero interactions. Verify clean empty state, no console errors.
8. **Shared direction:** Create shared interaction A-B with direction "a_to_b". Verify visualization shows arrow from A to B (not reversed).
