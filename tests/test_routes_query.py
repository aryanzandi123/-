#!/usr/bin/env python3
"""Integration tests for the query blueprint."""

import app as app_module
import services.state as state_module
import routes.query as query_module


class _FakeThread:
    captured_target = None
    captured_args = None

    def __init__(self, target=None, args=None, **kwargs):
        _FakeThread.captured_target = target
        _FakeThread.captured_args = args
        self.daemon = kwargs.get("daemon", False)

    def start(self):
        return None


def _reset_job_state():
    with state_module.jobs_lock:
        state_module.jobs.clear()


def test_search_protein_invalid_name():
    client = app_module.app.test_client()
    response = client.get("/api/search/!!!bad")
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == "INVALID_INPUT"


def test_search_protein_not_found():
    client = app_module.app.test_client()
    response = client.get("/api/search/ZZZZNOTREAL999")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "not_found"


def test_query_missing_protein():
    client = app_module.app.test_client()
    response = client.post("/api/query", json={})
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == "INVALID_INPUT"


def test_query_invalid_protein_name():
    client = app_module.app.test_client()
    response = client.post("/api/query", json={"protein": "!!!bad"})
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == "INVALID_INPUT"


def test_query_starts_job(monkeypatch):
    _reset_job_state()
    monkeypatch.setattr(query_module.threading, "Thread", _FakeThread)

    client = app_module.app.test_client()
    response = client.post("/api/query", json={"protein": "ATXN3"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "processing"
    assert payload["protein"] == "ATXN3"


def test_status_not_found():
    _reset_job_state()
    client = app_module.app.test_client()
    response = client.get("/api/status/ZZZZNOTREAL999")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "not_found"


def test_cancel_nonexistent_job():
    _reset_job_state()
    client = app_module.app.test_client()
    response = client.post("/api/cancel/ZZZZNOTREAL999")
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["code"] == "JOB_NOT_FOUND"


def test_requery_missing_protein():
    """Verify /api/requery validates protein name."""
    client = app_module.app.test_client()
    response = client.post("/api/requery", json={})
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == "INVALID_INPUT"


def test_requery_starts_job(monkeypatch):
    """Verify /api/requery launches a background job."""
    _reset_job_state()
    monkeypatch.setattr(query_module.threading, "Thread", _FakeThread)

    client = app_module.app.test_client()
    response = client.post("/api/requery", json={"protein": "ATXN3"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "processing"
    assert payload["protein"] == "ATXN3"
