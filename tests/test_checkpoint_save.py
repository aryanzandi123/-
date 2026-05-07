"""Tests for StorageLayer checkpoint saves and cache invalidation."""
import json
import os
import pytest
from unittest.mock import patch
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


# ---------------------------------------------------------------------------
# SQLite compat: compile JSONB as plain JSON
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _sqlite_jsonb_compat():
    """Register a compilation rule so JSONB columns render as JSON on SQLite."""

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    yield


def test_save_checkpoint_sets_running_status(test_app, sample_payload):
    """save_checkpoint should sync to DB and set pipeline_status='running'."""
    from utils.storage import StorageLayer
    from models import Protein, db

    with test_app.app_context():
        storage = StorageLayer(flask_app=test_app)
        p = Protein(symbol="ATXN3")
        db.session.add(p)
        db.session.commit()

        with patch.object(storage, "_sync_to_db_with_retry", return_value={"interactions_created": 0, "interactions_updated": 0, "proteins_created": 0}):
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

        with patch.object(storage, "_sync_to_db_with_retry", return_value={"interactions_created": 1, "interactions_updated": 0, "proteins_created": 0}):
            storage.save_pipeline_results("ATXN3", sample_payload)

        reloaded = Protein.query.filter_by(symbol="ATXN3").first()
        assert reloaded.pipeline_status == "complete"
        assert reloaded.last_pipeline_phase == "complete"


def test_invalidate_file_cache_deletes_files(tmp_cache):
    """_invalidate_file_cache should remove both snapshot and metadata files."""
    from utils.storage import StorageLayer

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

        stale_path = os.path.join(tmp_cache, "ATXN3.json")
        with open(stale_path, "w") as f:
            json.dump({"stale": True}, f)

        with patch.object(storage, "_sync_to_db_with_retry", return_value={"interactions_created": 1, "interactions_updated": 0, "proteins_created": 0}):
            storage.save_pipeline_results("ATXN3", sample_payload)

        # Stale data must be gone — either file deleted or rewritten with fresh data
        assert not os.path.exists(stale_path) or "stale" not in json.load(open(stale_path))


def test_set_pipeline_status_helper(test_app):
    """_set_pipeline_status should update protein status in DB."""
    from models import Protein, db

    with test_app.app_context():
        p = Protein(symbol="CRASHTEST")
        db.session.add(p)
        db.session.commit()

    import runner
    runner._set_pipeline_status("CRASHTEST", "partial", test_app, phase="crashed")

    with test_app.app_context():
        reloaded = Protein.query.filter_by(symbol="CRASHTEST").first()
        assert reloaded.pipeline_status == "partial"
        assert reloaded.last_pipeline_phase == "crashed"


def test_post_processor_continues_on_stage_failure():
    """PostProcessor should retry failing stages and continue to next stage on exhaustion."""
    from utils.post_processor import PostProcessor, StageDescriptor, StageKind

    call_count = {"value": 0}

    def failing_stage(payload, **kw):
        call_count["value"] += 1
        if call_count["value"] <= 4:  # Fail all 4 attempts
            raise RuntimeError("Simulated stage failure")
        return payload

    def passing_stage(payload, **kw):
        payload["_passed"] = True
        return payload

    pp = PostProcessor(stages=[
        StageDescriptor(name="bad_stage", label="Bad", fn=failing_stage, kind=StageKind.PURE),
        StageDescriptor(name="good_stage", label="Good", fn=passing_stage, kind=StageKind.PURE),
    ])

    result, step = pp.run({"test": True})
    assert result.get("_passed") is True
    assert "bad_stage" in str(result.get("_pipeline_metadata", {}).get("failed_stages", []))
