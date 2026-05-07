"""Shared pytest fixtures for ProPaths test suite."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Exclude manual test directory from automatic collection
collect_ignore_glob = ["manual/*"]

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _clear_gemini_client_cache():
    """Clear the singleton client cache between tests to prevent cross-contamination."""
    import utils.gemini_runtime as rt
    rt._client_cache.clear()
    yield
    rt._client_cache.clear()


@pytest.fixture
def sample_interactor():
    """A single interactor dict with full field set."""
    return {
        "primary": "VCP",
        "confidence": 0.9,
        "arrow": "binds",
        "direction": "main_to_primary",
        "support_summary": "Co-IP validated in human cells",
        "pmids": ["12345678", "23456789"],
        "functions": [
            {
                "function": "Protein quality control",
                "arrow": "activates",
                "interaction_effect": "activates",
                "interaction_direction": "main_to_primary",
                "cellular_process": "ERAD",
                "biological_consequence": ["substrate degradation"],
                "specific_effects": ["ubiquitin chain editing"],
                "pmids": ["12345678"],
                "confidence": 0.85,
            }
        ],
    }


@pytest.fixture
def sample_payload(sample_interactor):
    """A minimal valid pipeline payload with snapshot_json and ctx_json."""
    return {
        "snapshot_json": {
            "main": "ATXN3",
            "interactors": [sample_interactor],
        },
        "ctx_json": {
            "main": "ATXN3",
            "interactors": [sample_interactor],
            "interactor_history": ["VCP"],
            "query": "ATXN3",
            "timestamp": "2026-01-01T00:00:00",
        },
    }


@pytest.fixture
def sample_ctx_json(sample_interactor):
    """Standalone ctx_json for testing pipeline functions."""
    return {
        "main": "ATXN3",
        "interactors": [sample_interactor],
        "interactor_history": ["VCP"],
    }


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect CACHE_DIR to a temp directory for safe file I/O tests."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr("utils.storage.CACHE_DIR", str(cache_dir))
    return str(cache_dir)


@pytest.fixture
def test_app():
    """Flask app with in-memory SQLite for database integration tests."""
    from flask import Flask
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = True
    from models import db
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app


@pytest.fixture
def fake_gemini_client():
    """A reusable mock Gemini client for testing API-calling code."""

    class _Usage:
        prompt_token_count = 100
        thoughts_token_count = 50
        candidates_token_count = 200
        total_token_count = 350

    class _Resp:
        def __init__(self, text="{}"):
            self.text = text
            self.usage_metadata = _Usage()
            self.candidates = []

    class _FakeModels:
        def __init__(self):
            self.calls = []

        def generate_content(self, model, contents, config):
            self.calls.append({"model": model, "contents": contents})
            return _Resp('{"ctx_json":{"main":"TEST","interactors":[]},"step_json":{}}')

    class _FakeClient:
        def __init__(self):
            self.models = _FakeModels()

    return _FakeClient()


@pytest.fixture
def mock_api_key(monkeypatch):
    """Set fake Vertex AI config for tests that need it."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    return "test-project"
