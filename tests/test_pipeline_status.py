"""Tests for Protein.pipeline_status and last_pipeline_phase columns."""
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


def test_build_full_json_returns_none_while_actually_running(test_app):
    """build_full_json_from_db should return None when pipeline_status='running' AND job is active."""
    from models import Protein, db
    from services.data_builder import build_full_json_from_db
    from services.state import jobs, jobs_lock

    with test_app.app_context():
        p = Protein(symbol="RUNTEST", pipeline_status="running")
        db.session.add(p)
        db.session.commit()

        # Simulate an active job in the jobs dict
        with jobs_lock:
            jobs["RUNTEST"] = {"status": "processing"}

        try:
            result = build_full_json_from_db("RUNTEST")
            assert result is None
        finally:
            with jobs_lock:
                jobs.pop("RUNTEST", None)


def test_build_full_json_returns_data_when_stale_running(test_app):
    """build_full_json_from_db should return data when status='running' but no active job (stale)."""
    from models import Protein, db
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        p = Protein(symbol="STALETEST", pipeline_status="running")
        db.session.add(p)
        db.session.commit()

        # No active job — stale "running" status should NOT block data
        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("STALETEST")
        # Should return data (even if empty interactions), not None
        assert result is not None


@patch("services.data_builder._inject_cross_protein_chain_claims")
def test_build_full_json_returns_data_when_partial(_mock_inject, test_app):
    """build_full_json_from_db should return data with _pipeline_status when 'partial'."""
    from models import Protein, db
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        p = Protein(symbol="PARTTEST", pipeline_status="partial", last_pipeline_phase="discovery")
        db.session.add(p)
        db.session.commit()

        result = build_full_json_from_db("PARTTEST")
        assert result is not None
        assert result.get("_pipeline_status") == "partial"


@patch("services.data_builder._inject_cross_protein_chain_claims")
def test_build_full_json_returns_data_when_complete(_mock_inject, test_app):
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
