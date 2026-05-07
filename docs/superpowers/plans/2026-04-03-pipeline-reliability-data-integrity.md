# Pipeline Reliability, Data Integrity & Visualization Fixes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate data loss on pipeline crashes, add retry logic for transient failures, fix claims deduplication bug, add cache invalidation, and handle visualization edge cases.

**Architecture:** Add `pipeline_status` tracking to the Protein model. Insert checkpoint saves after each major pipeline phase in `runner.py`. Wrap all failure-prone calls (JSON parsing, batch execution, post-processing stages) in retry-with-backoff + save-partial-on-exhaustion. Fix the claims dedup key to match the DB constraint. Invalidate file cache on every DB write. Add empty-network graceful fallback in the visualizer.

**Tech Stack:** Python/Flask, SQLAlchemy, PostgreSQL, pytest, D3.js v7

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `models.py` | Modify (line 48) | Add `pipeline_status`, `last_pipeline_phase` columns |
| `utils/storage.py` | Modify (lines 49-109, 266) | Add `save_checkpoint()`, `_invalidate_file_cache()`, update `save_pipeline_results()` |
| `runner.py` | Modify (lines 191-211, 1196, 3497-3754) | Checkpoint calls, retry expansion, guaranteed-save wrapper |
| `utils/post_processor.py` | Modify (lines 440-470) | Per-stage try/except with retry |
| `utils/db_sync.py` | Modify (line 738) | Fix claims dedup key |
| `services/data_builder.py` | Modify (lines 342-344, 585-594) | Pipeline status gating, shared link direction fix |
| `static/visualizer.js` | Modify (line 2739) | Empty network graceful handling |
| `tests/test_pipeline_status.py` | Create | Tests for pipeline_status tracking |
| `tests/test_checkpoint_save.py` | Create | Tests for checkpoint + retry logic |
| `tests/test_claims_dedup.py` | Create | Tests for claims dedup key fix |
| `tests/test_cache_invalidation.py` | Create | Tests for cache invalidation |

---

### Task 1: Add pipeline_status columns to Protein model

**Files:**
- Modify: `models.py:48` (after `extra_data` column)
- Test: `tests/test_pipeline_status.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline_status.py
"""Tests for Protein.pipeline_status and last_pipeline_phase columns."""
import pytest


def test_protein_has_pipeline_status_column(test_app):
    """Protein model should have pipeline_status defaulting to 'idle'."""
    from models import Protein, db
    with test_app.app_context():
        p = Protein(symbol="TEST1")
        db.session.add(p)
        db.session.flush()
        assert p.pipeline_status == "idle"
        assert p.last_pipeline_phase is None


def test_pipeline_status_can_be_set(test_app):
    """pipeline_status should accept running/partial/complete values."""
    from models import Protein, db
    with test_app.app_context():
        p = Protein(symbol="TEST2")
        db.session.add(p)
        db.session.flush()

        p.pipeline_status = "running"
        p.last_pipeline_phase = "discovery"
        db.session.flush()

        reloaded = Protein.query.filter_by(symbol="TEST2").first()
        assert reloaded.pipeline_status == "running"
        assert reloaded.last_pipeline_phase == "discovery"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_pipeline_status.py -v`
Expected: FAIL — `pipeline_status` attribute does not exist on Protein.

- [ ] **Step 3: Add columns to Protein model**

In `models.py`, after line 48 (`extra_data` column), add:

```python
    # Pipeline tracking (crash recovery)
    pipeline_status = db.Column(db.String(20), default="idle", index=True)
    last_pipeline_phase = db.Column(db.String(50))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_pipeline_status.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add models.py tests/test_pipeline_status.py
git commit -m "feat(models): add pipeline_status and last_pipeline_phase to Protein"
```

---

### Task 2: Add save_checkpoint() and _invalidate_file_cache() to StorageLayer

**Files:**
- Modify: `utils/storage.py:49-109` (save_pipeline_results) and add new methods
- Test: `tests/test_checkpoint_save.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkpoint_save.py
"""Tests for StorageLayer checkpoint saves and cache invalidation."""
import json
import os
import pytest
from unittest.mock import patch, MagicMock


def test_save_checkpoint_sets_running_status(test_app, sample_payload):
    """save_checkpoint should sync to DB and set pipeline_status='running'."""
    from utils.storage import StorageLayer
    from models import Protein, db

    with test_app.app_context():
        storage = StorageLayer(flask_app=test_app)
        # Create protein first
        p = Protein(symbol="ATXN3")
        db.session.add(p)
        db.session.commit()

        storage.save_checkpoint("ATXN3", sample_payload, "discovery")

        reloaded = Protein.query.filter_by(symbol="ATXN3").first()
        assert reloaded.pipeline_status == "running"
        assert reloaded.last_pipeline_phase == "discovery"


def test_save_pipeline_results_sets_complete(test_app, sample_payload):
    """save_pipeline_results should set pipeline_status='complete'."""
    from utils.storage import StorageLayer
    from models import Protein, db

    with test_app.app_context():
        storage = StorageLayer(flask_app=test_app)
        p = Protein(symbol="ATXN3", pipeline_status="running")
        db.session.add(p)
        db.session.commit()

        storage.save_pipeline_results("ATXN3", sample_payload)

        reloaded = Protein.query.filter_by(symbol="ATXN3").first()
        assert reloaded.pipeline_status == "complete"
        assert reloaded.last_pipeline_phase == "complete"


def test_invalidate_file_cache_deletes_files(tmp_cache):
    """_invalidate_file_cache should remove both snapshot and metadata files."""
    from utils.storage import StorageLayer

    # Create cache files
    for suffix in ("", "_metadata"):
        path = os.path.join(tmp_cache, f"ATXN3{suffix}.json")
        with open(path, "w") as f:
            json.dump({"test": True}, f)

    StorageLayer._invalidate_file_cache("ATXN3")

    assert not os.path.exists(os.path.join(tmp_cache, "ATXN3.json"))
    assert not os.path.exists(os.path.join(tmp_cache, "ATXN3_metadata.json"))


def test_save_pipeline_results_invalidates_cache_before_rewrite(test_app, sample_payload, tmp_cache):
    """After DB sync, stale cache files should be deleted before fresh ones are written."""
    from utils.storage import StorageLayer
    from models import Protein, db

    with test_app.app_context():
        storage = StorageLayer(flask_app=test_app)
        p = Protein(symbol="ATXN3")
        db.session.add(p)
        db.session.commit()

        # Write stale cache
        stale_path = os.path.join(tmp_cache, "ATXN3.json")
        with open(stale_path, "w") as f:
            json.dump({"stale": True}, f)

        storage.save_pipeline_results("ATXN3", sample_payload)

        # Cache should exist but NOT contain stale data
        if os.path.exists(stale_path):
            with open(stale_path) as f:
                data = json.load(f)
            assert "stale" not in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_checkpoint_save.py -v`
Expected: FAIL — `save_checkpoint` does not exist; `_invalidate_file_cache` does not exist; `pipeline_status` not set to "complete".

- [ ] **Step 3: Implement _invalidate_file_cache**

In `utils/storage.py`, add after the `_write_file_cache` method (after line ~283):

```python
    @staticmethod
    def _invalidate_file_cache(protein_symbol: str) -> None:
        """Delete stale file cache — DB is source of truth after sync."""
        for suffix in ("", "_metadata"):
            path = os.path.join(CACHE_DIR, f"{protein_symbol}{suffix}.json")
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
```

- [ ] **Step 4: Implement save_checkpoint**

In `utils/storage.py`, add after `save_pipeline_results` (after line 109):

```python
    def save_checkpoint(
        self,
        protein_symbol: str,
        payload: Dict[str, Any],
        phase_name: str,
    ) -> None:
        """Save intermediate pipeline results to DB for crash recovery.

        Calls the same sync logic as save_pipeline_results but sets
        pipeline_status='running' instead of 'complete'.
        """
        if not self._has_db():
            return

        self._sync_to_db_with_retry(protein_symbol, payload)

        with self._db_context():
            from models import Protein, db
            protein = Protein.query.filter_by(symbol=protein_symbol).first()
            if protein:
                protein.pipeline_status = "running"
                protein.last_pipeline_phase = phase_name
                db.session.commit()
```

- [ ] **Step 5: Update save_pipeline_results to set 'complete' and invalidate cache**

In `utils/storage.py`, modify `save_pipeline_results`. After the DB sync success check (after line 81), add cache invalidation and status update:

```python
        # --- PRIMARY: PostgreSQL via DatabaseSyncLayer ---
        if self._has_db():
            db_stats = self._sync_to_db_with_retry(protein_symbol, final_payload)
            if db_stats is not None:
                stats["db_synced"] = True
                stats["interactions_created"] = db_stats.get("interactions_created", 0)
                stats["interactions_updated"] = db_stats.get("interactions_updated", 0)
                stats["proteins_created"] = db_stats.get("proteins_created", 0)
                # Invalidate stale cache before rewrite
                self._invalidate_file_cache(protein_symbol)
                # Mark pipeline as complete
                with self._db_context():
                    from models import Protein, db
                    protein = Protein.query.filter_by(symbol=protein_symbol).first()
                    if protein:
                        protein.pipeline_status = "complete"
                        protein.last_pipeline_phase = "complete"
                        db.session.commit()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_checkpoint_save.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add utils/storage.py tests/test_checkpoint_save.py
git commit -m "feat(storage): add save_checkpoint, cache invalidation, pipeline_status tracking"
```

---

### Task 3: Gate build_full_json_from_db on pipeline_status

**Files:**
- Modify: `services/data_builder.py:342-344`
- Test: `tests/test_pipeline_status.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pipeline_status.py`:

```python
def test_build_full_json_returns_none_while_running(test_app):
    """build_full_json_from_db should return None when pipeline_status='running'."""
    from models import Protein, db
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        p = Protein(symbol="RUNTEST", pipeline_status="running")
        db.session.add(p)
        db.session.commit()

        result = build_full_json_from_db("RUNTEST")
        assert result is None


def test_build_full_json_returns_data_when_partial(test_app):
    """build_full_json_from_db should return data with _pipeline_status when 'partial'."""
    from models import Protein, db
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        p = Protein(symbol="PARTTEST", pipeline_status="partial", last_pipeline_phase="discovery")
        db.session.add(p)
        db.session.commit()

        result = build_full_json_from_db("PARTTEST")
        # Should return data (even if empty interactions), with status marker
        assert result is not None
        assert result.get("_pipeline_status") == "partial"


def test_build_full_json_returns_data_when_complete(test_app):
    """build_full_json_from_db should return data normally when 'complete'."""
    from models import Protein, db
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        p = Protein(symbol="DONETEST", pipeline_status="complete")
        db.session.add(p)
        db.session.commit()

        result = build_full_json_from_db("DONETEST")
        assert result is not None
        assert "_pipeline_status" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_pipeline_status.py::test_build_full_json_returns_none_while_running -v`
Expected: FAIL — function doesn't check pipeline_status.

- [ ] **Step 3: Add pipeline_status gating**

In `services/data_builder.py`, replace lines 342-344:

```python
    main_protein = Protein.query.filter_by(symbol=protein_symbol).first()
    if not main_protein:
        return None
```

With:

```python
    main_protein = Protein.query.filter_by(symbol=protein_symbol).first()
    if not main_protein:
        return None

    # Hide partial data while pipeline is actively running
    if main_protein.pipeline_status == "running":
        return None
```

Then at the END of `build_full_json_from_db`, just before the final return, add the partial marker:

```python
    # Mark partial results so frontend knows data is incomplete
    if main_protein.pipeline_status == "partial":
        result["_pipeline_status"] = "partial"
        result["_completed_phases"] = main_protein.last_pipeline_phase
```

(Where `result` is whatever dict the function currently returns.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_pipeline_status.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add services/data_builder.py tests/test_pipeline_status.py
git commit -m "feat(data_builder): gate results on pipeline_status, mark partial results"
```

---

### Task 4: Add checkpoint saves and guaranteed-save wrapper in runner.py

**Files:**
- Modify: `runner.py:3497-3754` (run_full_job)
- Test: `tests/test_checkpoint_save.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_checkpoint_save.py`:

```python
def test_run_full_job_saves_on_crash(test_app, monkeypatch):
    """If the pipeline crashes, partial results should still be saved to DB."""
    from models import Protein, db

    with test_app.app_context():
        p = Protein(symbol="CRASHTEST")
        db.session.add(p)
        db.session.commit()

    # We can't easily run the full pipeline in tests, but we can verify
    # the helper function _set_pipeline_status works correctly
    import runner
    runner._set_pipeline_status("CRASHTEST", "partial", test_app)

    with test_app.app_context():
        reloaded = Protein.query.filter_by(symbol="CRASHTEST").first()
        assert reloaded.pipeline_status == "partial"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_checkpoint_save.py::test_run_full_job_saves_on_crash -v`
Expected: FAIL — `_set_pipeline_status` does not exist.

- [ ] **Step 3: Add _set_pipeline_status helper and checkpoint calls**

In `runner.py`, near the top (after imports), add:

```python
def _set_pipeline_status(protein_symbol: str, status: str, flask_app, phase: str = None) -> None:
    """Update protein pipeline_status in DB. Best-effort (swallows errors)."""
    try:
        with flask_app.app_context():
            from models import Protein, db
            protein = Protein.query.filter_by(symbol=protein_symbol).first()
            if protein:
                protein.pipeline_status = status
                if phase:
                    protein.last_pipeline_phase = phase
                db.session.commit()
    except Exception as exc:
        print(f"[WARN] Failed to set pipeline_status={status}: {exc}", file=sys.stderr)
```

Then in `run_full_job` (line ~3497), wrap the main body:

**At the top of run_full_job, after job dict setup:**
```python
    _set_pipeline_status(user_query, "running", flask_app)
    current_payload = None  # Track for crash-save
```

**After `_run_main_pipeline_for_web` returns (line ~3700):**
```python
    current_payload = pipeline_payload
    storage.save_checkpoint(user_query, pipeline_payload, "pipeline_complete")
```

**After `post_processor.run` returns (line ~3715):**
```python
    current_payload = final_payload
```

**Wrap the entire pipeline body in try/except (around the existing code from line ~3684 to ~3814):**
```python
    try:
        # ... existing pipeline code (lines 3684-3814) ...
        pass
    except Exception as exc:
        # Guaranteed save: persist whatever we have
        if current_payload is not None:
            try:
                storage.save_checkpoint(user_query, current_payload, "crashed")
            except Exception:
                pass
        _set_pipeline_status(user_query, "partial", flask_app, phase="crashed")
        with lock:
            if user_query in jobs and jobs[user_query].get('cancel_event') is cancel_event:
                jobs[user_query]['status'] = 'error'
                jobs[user_query]['progress'] = f'Pipeline error: {exc}'
                jobs[user_query]['_finished_at'] = time.time()
        notify_job_update(user_query)
        raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_checkpoint_save.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run existing tests to check for regressions**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/ -v --timeout=30 -x`
Expected: All existing tests pass.

- [ ] **Step 6: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add runner.py tests/test_checkpoint_save.py
git commit -m "feat(runner): add checkpoint saves and guaranteed-save-on-crash wrapper"
```

---

### Task 5: Expand _parse_with_retry to 3 retries with backoff

**Files:**
- Modify: `runner.py:191-211`

- [ ] **Step 1: Replace _parse_with_retry**

Replace lines 191-211 in `runner.py`:

```python
def _parse_with_retry(
    step: StepConfig,
    prompt: str,
    raw_output: str,
    current_payload: Optional[Dict[str, Any]],
    call_kwargs: Dict[str, Any],
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Parse model output; on failure, retry with exponential backoff.

    After max_retries exhausted, returns current_payload with failure metadata
    instead of raising — so downstream checkpoint saves can persist partial work.
    """
    try:
        return parse_json_output(
            raw_output, list(step.expected_columns), previous_payload=current_payload,
        )
    except PipelineError as first_exc:
        last_exc = first_exc
        for attempt in range(max_retries):
            wait = 2 ** attempt
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

        # All retries exhausted — return current payload with failure metadata
        print(
            f"[ERROR] All {max_retries} retries failed for {step.name}: {last_exc}",
            file=sys.stderr, flush=True,
        )
        if current_payload is None:
            current_payload = {}
        current_payload.setdefault("_pipeline_metadata", {}).setdefault(
            "failed_steps", []
        ).append({"step": step.name, "error": str(last_exc)})
        return current_payload
```

- [ ] **Step 2: Run existing tests to check for regressions**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/ -k "parse" -v`
Expected: All parse-related tests pass.

- [ ] **Step 3: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add runner.py
git commit -m "fix(runner): expand _parse_with_retry to 3 retries with exponential backoff"
```

---

### Task 6: Add retry for failed batches in parallel phase

**Files:**
- Modify: `runner.py` (in `_run_parallel_batched_phase`, after the futures loop around line 1196)

- [ ] **Step 1: Add failed-batch tracking and retry**

After the existing futures loop (around line 1200, after the `if not results:` check), add tracking for completely failed batches:

```python
    # Track which batches failed completely (not in results dict)
    failed_batch_indices = [i for i in range(num_batches) if i not in results]
    failed_batch_names = []
    for idx in failed_batch_indices:
        failed_batch_names.extend(batches[idx])

    # Retry failed batches with exponential backoff (up to 3 attempts)
    if failed_batch_names:
        for retry_attempt in range(3):
            if not failed_batch_names:
                break
            wait = 2 ** retry_attempt
            print(
                f"[PARALLEL:{phase_name}] Retrying {len(failed_batch_names)} failed "
                f"interactors (attempt {retry_attempt + 1}/3, wait {wait}s)...",
                file=sys.stderr, flush=True,
            )
            time.sleep(wait)

            retry_calls = []
            for name in failed_batch_names:
                step = step_factory(round_num=200 + retry_attempt) if _accepts_round else step_factory()
                base_prompt = build_prompt(step, current_payload, user_query, False,
                                           known_interactions=known_interactions)
                if batch_directive_fn is not None:
                    directive_text = batch_directive_fn([name])
                else:
                    directive_text = batch_directive_template.format(count=1, batch_names=name)
                directive = f"\n\n{'='*60}\n{directive_text}\n{'='*60}\n"
                retry_calls.append(dict(step=step, prompt=base_prompt + directive,
                                        batch_idx=3000 + retry_attempt * 100, batch_names=[name]))

            still_failed = []
            with ThreadPoolExecutor(max_workers=min(max_workers, len(retry_calls))) as executor:
                futures_retry = {executor.submit(_worker, a): a["batch_names"][0] for a in retry_calls}
                for future in as_completed(futures_retry):
                    interactor_name = futures_retry[future]
                    try:
                        r = future.result()
                        raw = r["raw_output"]
                        if not _is_truncated(raw):
                            current_payload = parse_json_output(
                                raw, list(r["step"].expected_columns),
                                previous_payload=current_payload)
                        else:
                            still_failed.append(interactor_name)
                    except Exception:
                        still_failed.append(interactor_name)
            failed_batch_names = still_failed

        # Record any permanently failed interactors in metadata
        if failed_batch_names:
            print(
                f"[PARALLEL:{phase_name}] {len(failed_batch_names)} interactors "
                f"failed after all retries: {failed_batch_names}",
                file=sys.stderr, flush=True,
            )
            current_payload.setdefault("_pipeline_metadata", {}).setdefault(
                "failed_batches", []
            ).append({"phase": phase_name, "failed_interactors": failed_batch_names})
```

- [ ] **Step 2: Run existing tests**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/ -k "parallel or batch" -v`
Expected: All existing tests pass.

- [ ] **Step 3: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add runner.py
git commit -m "fix(runner): retry failed batches in parallel phase with exponential backoff"
```

---

### Task 7: Add per-stage error handling to PostProcessor.run()

**Files:**
- Modify: `utils/post_processor.py:440-470`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_checkpoint_save.py`:

```python
def test_post_processor_continues_on_stage_failure():
    """PostProcessor should retry failing stages and continue to next stage on exhaustion."""
    from utils.post_processor import PostProcessor, Stage, StageKind

    call_count = {"value": 0}

    def failing_stage(payload, **kw):
        call_count["value"] += 1
        if call_count["value"] <= 4:  # Fail all 4 attempts (1 initial + 3 retries)
            raise RuntimeError("Simulated stage failure")
        return payload

    def passing_stage(payload, **kw):
        payload["_passed"] = True
        return payload

    pp = PostProcessor(skip_flags={})
    pp._stages = [
        Stage(name="bad_stage", label="Bad", fn=failing_stage, kind=StageKind.PURE),
        Stage(name="good_stage", label="Good", fn=passing_stage, kind=StageKind.PURE),
    ]

    result, step = pp.run({"test": True})
    assert result.get("_passed") is True  # Second stage still ran
    assert "bad_stage" in str(result.get("_pipeline_metadata", {}).get("failed_stages", []))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_checkpoint_save.py::test_post_processor_continues_on_stage_failure -v`
Expected: FAIL — stage exception propagates and kills the pipeline.

- [ ] **Step 3: Implement per-stage retry with error handling**

Replace the stage execution loop in `utils/post_processor.py` (lines 440-465):

```python
        skipped_stages: list[str] = []
        for stage in self.active_stages():
            if stage.requires_api_key and not api_key:
                print(f"[WARN] Skipping stage '{stage.name}': no API key", file=sys.stderr)
                skipped_stages.append(stage.name)
                continue

            current_step += 1
            if update_status:
                update_status(
                    text=stage.label,
                    current_step=current_step,
                    total_steps=total_steps,
                )

            # Retry each stage up to 4 times (1 initial + 3 retries) with backoff
            last_exc = None
            for attempt in range(4):
                try:
                    payload = stage.fn(
                        payload,
                        api_key=api_key,
                        verbose=verbose,
                        step_logger=step_logger,
                        user_query=user_query,
                        flask_app=flask_app,
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < 3:
                        wait = 2 ** attempt
                        print(
                            f"[WARN] Stage '{stage.name}' failed (attempt {attempt + 1}/4), "
                            f"retrying in {wait}s: {exc}",
                            file=sys.stderr, flush=True,
                        )
                        import time
                        time.sleep(wait)
            else:
                # All attempts failed — log and continue to next stage
                print(
                    f"[ERROR] Stage '{stage.name}' failed after 4 attempts: {last_exc}",
                    file=sys.stderr, flush=True,
                )
                payload.setdefault("_pipeline_metadata", {}).setdefault(
                    "failed_stages", []
                ).append({"stage": stage.name, "error": str(last_exc)})

            if consume_metrics and stage.kind in (StageKind.LLM, StageKind.EXTERNAL_API):
                consume_metrics(payload)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_checkpoint_save.py::test_post_processor_continues_on_stage_failure -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/ -v --timeout=30 -x`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add utils/post_processor.py tests/test_checkpoint_save.py
git commit -m "fix(post_processor): add per-stage retry with backoff, continue on failure"
```

---

### Task 8: Fix claims dedup key to match DB constraint

**Files:**
- Modify: `utils/db_sync.py:738` and downstream lookups
- Test: `tests/test_claims_dedup.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claims_dedup.py
"""Tests for InteractionClaim deduplication key fix."""
import pytest


def test_same_function_different_pathways_both_saved(test_app):
    """Same function_name in different pathways should create separate claims."""
    from models import Protein, Interaction, InteractionClaim, db
    from utils.db_sync import DatabaseSyncLayer

    with test_app.app_context():
        # Setup: two proteins with an interaction
        p1 = Protein(symbol="PROT_A")
        p2 = Protein(symbol="PROT_B")
        db.session.add_all([p1, p2])
        db.session.flush()

        interaction = Interaction(
            protein_a_id=p1.id,
            protein_b_id=p2.id,
            data={
                "functions": [
                    {"function": "DNA Repair", "pathway": "Base Excision Repair",
                     "arrow": "activates", "evidence": [], "pmids": []},
                    {"function": "DNA Repair", "pathway": "Nucleotide Excision Repair",
                     "arrow": "activates", "evidence": [], "pmids": []},
                ],
                "arrow": "activates",
                "direction": "a_to_b",
            },
            direction="a_to_b",
            arrow="activates",
            discovered_in_query="PROT_A",
        )
        db.session.add(interaction)
        db.session.flush()

        sync = DatabaseSyncLayer()
        count = sync._save_claims(interaction, interaction.data, "PROT_A")

        # BOTH claims should be created (different pathways)
        claims = InteractionClaim.query.filter_by(interaction_id=interaction.id).all()
        assert len(claims) == 2
        pathways = {c.pathway_name for c in claims}
        assert "Base Excision Repair" in pathways
        assert "Nucleotide Excision Repair" in pathways
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_claims_dedup.py -v`
Expected: FAIL — only 1 claim created (second pathway collapsed by name-only key).

- [ ] **Step 3: Fix the dedup key**

In `utils/db_sync.py`, line 738, change:

```python
        _existing_by_name = {c.function_name: c for c in existing_claims}
```

To:

```python
        _existing_by_key = {(c.function_name, c.pathway_name): c for c in existing_claims}
```

Then update ALL references to `_existing_by_name` in `_save_claims`:

**Line 764** — change:
```python
                existing = _existing_by_name.get(func_name)
```
To:
```python
                existing = _existing_by_key.get((func_name, pathway_name))
```

**Line 794** — change:
```python
                existing = _existing_by_name.get(func_name)
```
To:
```python
                existing = _existing_by_key.get((func_name, pathway_name))
```

**Line 827** — change:
```python
            existing = _existing_by_name.get(func_name)
```
To:
```python
            existing = _existing_by_key.get((func_name, pathway_name))
```

Note: `pathway_name` is already extracted above each of these lookups (from `data.get("step3_finalized_pathway")` or `func.get("pathway")`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/test_claims_dedup.py -v`
Expected: PASS

- [ ] **Step 5: Run existing DB tests for regressions**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/ -k "claim or db_sync or database" -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add utils/db_sync.py tests/test_claims_dedup.py
git commit -m "fix(db_sync): use (function_name, pathway_name) as claims dedup key"
```

---

### Task 9: Prevent duplicate chain-derived claims on re-run

**Files:**
- Modify: `utils/db_sync.py:1039-1052`

- [ ] **Step 1: Add duplicate check before saving chain claims**

In `utils/db_sync.py`, replace lines 1039-1052:

```python
                if existing:
                    # ADD chain-context claims to existing direct interaction
                    if chain_link_funcs:
                        self._save_chain_context_claims(
                            interaction=existing,
                            functions=chain_link_funcs,
                            chain_record=chain_record,
                            discovered_in=query_protein,
                            chain_pathway=chain_pathway,
                        )
                    # Tag the indirect chain claims on the parent interaction
                    if parent_interaction and chain_record:
                        self._tag_claims_with_chain(parent_interaction, chain_record)
                    continue  # Don't create new interaction for this link
```

With:

```python
                if existing:
                    # ADD chain-context claims ONLY if they don't already exist
                    if chain_link_funcs:
                        existing_chain_claims = InteractionClaim.query.filter_by(
                            interaction_id=existing.id,
                            function_context="chain_derived",
                        ).all()
                        existing_claim_keys = {
                            (c.function_name, c.pathway_name) for c in existing_chain_claims
                        }
                        new_funcs = [
                            f for f in chain_link_funcs
                            if (f.get("function", ""), f.get("pathway"))
                            not in existing_claim_keys
                        ]
                        if new_funcs:
                            self._save_chain_context_claims(
                                interaction=existing,
                                functions=new_funcs,
                                chain_record=chain_record,
                                discovered_in=query_protein,
                                chain_pathway=chain_pathway,
                            )
                    if parent_interaction and chain_record:
                        self._tag_claims_with_chain(parent_interaction, chain_record)
                    continue
```

Note: `InteractionClaim` is already imported at the top of `_save_claims` — verify it's also accessible in `sync_chain_relationships` (add `from models import InteractionClaim` if needed).

- [ ] **Step 2: Run existing chain/DB tests**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/ -k "chain or db_sync" -v`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add utils/db_sync.py
git commit -m "fix(db_sync): prevent duplicate chain-derived claims on re-run"
```

---

### Task 10: Fix shared link direction in data_builder.py

**Files:**
- Modify: `services/data_builder.py:585-594`

- [ ] **Step 1: Fix source/target assignment for shared links**

In `services/data_builder.py`, replace lines 585-588:

```python
            shared_data = shared_ix.data.copy()
            shared_data["_db_id"] = shared_ix.id
            shared_data["source"] = protein_a.symbol
            shared_data["target"] = protein_b.symbol
```

With:

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
                # Bidirectional or unknown — alphabetical for consistency
                if protein_a.symbol < protein_b.symbol:
                    shared_data["source"] = protein_a.symbol
                    shared_data["target"] = protein_b.symbol
                else:
                    shared_data["source"] = protein_b.symbol
                    shared_data["target"] = protein_a.symbol
```

- [ ] **Step 2: Run existing data_builder tests**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/ -k "data_builder or roundtrip" -v`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add services/data_builder.py
git commit -m "fix(data_builder): use stored direction for shared link source/target"
```

---

### Task 11: Handle empty networks in visualizer.js

**Files:**
- Modify: `static/visualizer.js` (around line 2739)

- [ ] **Step 1: Add empty-state early return**

In `static/visualizer.js`, find the existing fallback check at line 2739:

```javascript
  if ((proteins.length === 0 || interactions.length === 0) && SNAP.interactors && SNAP.interactors.length > 0) {
```

Add a NEW check BEFORE this block (immediately before line 2739):

```javascript
  // Handle truly empty networks (no interactions at all)
  if (interactions.length === 0 && (!SNAP.interactors || SNAP.interactors.length === 0)) {
    const svg = d3.select('#graph-container svg');
    if (svg.node()) {
      svg.selectAll('*').remove();
      const rect = svg.node().getBoundingClientRect();
      const w = rect.width || 800;
      const h = rect.height || 600;

      svg.append('text')
        .attr('x', w / 2)
        .attr('y', h / 2 - 10)
        .attr('text-anchor', 'middle')
        .attr('fill', 'var(--text-secondary, #888)')
        .attr('font-size', '16px')
        .text('No interactions found for ' + (SNAP.main || 'this protein'));

      svg.append('text')
        .attr('x', w / 2)
        .attr('y', h / 2 + 15)
        .attr('text-anchor', 'middle')
        .attr('fill', 'var(--text-tertiary, #666)')
        .attr('font-size', '13px')
        .text('Run the pipeline to discover interactions');
    }

    // Dim interactive controls
    document.querySelectorAll('.filter-chip, .legend-item, .stats-bar .stat').forEach(function(el) {
      el.style.opacity = '0.3';
      el.style.pointerEvents = 'none';
    });
    return;
  }
```

- [ ] **Step 2: Verify manually**

Open the app, query a protein that has no interactions in the database. The graph container should show the empty state message instead of a broken blank SVG.

- [ ] **Step 3: Commit**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add static/visualizer.js
git commit -m "fix(visualizer): graceful empty state when no interactions found"
```

---

### Task 12: Final integration verification

- [ ] **Step 1: Run full test suite**

Run: `cd "/Users/aryan/Desktop/untitled folder 2" && python -m pytest tests/ -v --timeout=60`
Expected: All tests pass, including new tests.

- [ ] **Step 2: Verify checkpoint save flow end-to-end (manual)**

1. Start the Flask app
2. Submit a query for a new protein
3. While pipeline is running, check DB: `Protein.pipeline_status` should be `"running"`
4. Check `/api/results/<protein>` returns `None` (hidden while running)
5. Let pipeline complete
6. Check DB: `pipeline_status` should be `"complete"`
7. Check `/api/results/<protein>` returns full data
8. Check cache files were re-created fresh

- [ ] **Step 3: Verify retry behavior (if possible)**

If you can trigger a transient Gemini failure (rate limit, malformed response), verify:
- The retry log messages appear in stderr
- The pipeline continues past the failure
- `_pipeline_metadata.failed_steps` or `failed_stages` is populated in the final payload

- [ ] **Step 4: Final commit with all test changes**

```bash
cd "/Users/aryan/Desktop/untitled folder 2"
git add -A
git commit -m "test: add integration verification for pipeline reliability fixes"
```
