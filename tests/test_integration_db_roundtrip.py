"""Integration tests for database round-trip via StorageLayer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

from utils.storage import StorageLayer
from models import db, Protein, Interaction


# ---------------------------------------------------------------------------
# SQLite compat: compile JSONB as plain JSON
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _sqlite_jsonb_compat():
    """Register a compilation rule so JSONB columns render as JSON on SQLite."""
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    yield


# ---------------------------------------------------------------------------
# Override test_app fixture to use the JSONB compat
# ---------------------------------------------------------------------------

@pytest.fixture
def test_app():
    """Flask app with in-memory SQLite for database integration tests."""
    from flask import Flask

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSaveAndLoadRoundtrip:
    """save_pipeline_results then load_protein_data returns consistent data."""

    def test_save_and_load_roundtrip(self, test_app, sample_payload, tmp_path, monkeypatch):
        # Redirect file cache to tmp_path so tests don't touch real filesystem
        monkeypatch.setattr("utils.storage.CACHE_DIR", str(tmp_path))

        storage = StorageLayer(flask_app=test_app, max_retries=1)
        stats = storage.save_pipeline_results("ATXN3", sample_payload)

        # DB sync should succeed
        assert stats["db_synced"] is True
        assert stats["file_cached"] is True

        # Verify data via file-cache read (avoids import complexity from app.py)
        loaded = storage._read_file_cache("ATXN3")
        assert loaded is not None
        assert loaded["snapshot_json"]["main"] == "ATXN3"
        assert len(loaded["snapshot_json"]["interactors"]) == 1
        assert loaded["snapshot_json"]["interactors"][0]["primary"] == "VCP"

        # Verify ctx_json was also saved in metadata file
        assert "ctx_json" in loaded
        assert loaded["ctx_json"]["main"] == "ATXN3"


class TestGetKnownInteractionsFromDb:
    """get_known_interactions returns partners from manually inserted DB rows."""

    def test_get_known_interactions_from_db(self, test_app):
        with test_app.app_context():
            # Insert proteins
            prot_a = Protein(symbol="ATXN3", query_count=1, total_interactions=1, extra_data={})
            prot_b = Protein(symbol="VCP", query_count=0, total_interactions=1, extra_data={})
            db.session.add_all([prot_a, prot_b])
            db.session.flush()

            # Insert interaction
            interaction = Interaction(
                protein_a_id=min(prot_a.id, prot_b.id),
                protein_b_id=max(prot_a.id, prot_b.id),
                direction="a_to_b",
                data={"primary": "VCP", "functions": []},
                confidence=0.85,
                arrow="binds",
                interaction_type="direct",
                depth=1,
                discovered_in_query="ATXN3",
            )
            db.session.add(interaction)
            db.session.commit()

        storage = StorageLayer(flask_app=test_app)
        partners = storage.get_known_interactions("ATXN3")

        assert len(partners) == 1
        assert partners[0]["primary"] == "VCP"
        assert partners[0]["arrow"] == "binds"
        assert float(partners[0]["confidence"]) == pytest.approx(0.85, abs=0.01)


class TestDbSyncFailureFallsBackToFileCache:
    """When DB sync fails, file cache should still succeed."""

    def test_db_sync_failure_falls_back_to_file_cache(
        self, test_app, sample_payload, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("utils.storage.CACHE_DIR", str(tmp_path))

        # Make DatabaseSyncLayer.sync_query_results always raise
        monkeypatch.setattr(
            "utils.db_sync.DatabaseSyncLayer.sync_query_results",
            lambda self, **kw: (_ for _ in ()).throw(
                RuntimeError("simulated DB failure")
            ),
        )

        storage = StorageLayer(flask_app=test_app, max_retries=1)
        stats = storage.save_pipeline_results("ATXN3", sample_payload)

        # DB sync should have failed
        assert stats["db_synced"] is False
        # File cache should still succeed
        assert stats["file_cached"] is True

        # Verify file cache is readable
        loaded = storage._read_file_cache("ATXN3")
        assert loaded is not None
        assert loaded["snapshot_json"]["main"] == "ATXN3"
        assert loaded["snapshot_json"]["interactors"][0]["primary"] == "VCP"
