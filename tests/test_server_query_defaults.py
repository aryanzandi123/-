#!/usr/bin/env python3
"""Tests for /api/query default skip_fact_checking behavior."""

import app as app_module


class _FakeThread:
    captured_target = None
    captured_kwargs = None

    def __init__(self, target=None, args=None, kwargs=None, **extra):
        _FakeThread.captured_target = target
        _FakeThread.captured_kwargs = kwargs
        self.daemon = extra.get("daemon", False)

    def start(self):
        return None


def _reset_job_state():
    with app_module.jobs_lock:
        app_module.jobs.clear()


def test_query_defaults_skip_fact_checking_true(monkeypatch):
    _reset_job_state()
    monkeypatch.setenv("DEFAULT_SKIP_FACT_CHECKING", "true")
    monkeypatch.setenv("GEMINI_REQUEST_MODE", "batch")
    monkeypatch.setattr(app_module.threading, "Thread", _FakeThread)

    client = app_module.app.test_client()
    response = client.post(
        "/api/query",
        json={
            "protein": "ATXN3",
            "interactor_rounds": 3,
            "function_rounds": 3,
        },
    )

    assert response.status_code == 200
    assert _FakeThread.captured_kwargs is not None
    assert _FakeThread.captured_kwargs["skip_fact_checking"] is True
    assert _FakeThread.captured_kwargs["request_mode"] == "batch"


def test_query_explicit_skip_fact_checking_false_overrides_default(monkeypatch):
    _reset_job_state()
    monkeypatch.setenv("DEFAULT_SKIP_FACT_CHECKING", "true")
    monkeypatch.setenv("GEMINI_REQUEST_MODE", "batch")
    monkeypatch.setattr(app_module.threading, "Thread", _FakeThread)

    client = app_module.app.test_client()
    response = client.post(
        "/api/query",
        json={
            "protein": "ATXN3",
            "interactor_rounds": 3,
            "function_rounds": 3,
            "skip_fact_checking": False,
            "request_mode": "standard",
        },
    )

    assert response.status_code == 200
    assert _FakeThread.captured_kwargs is not None
    assert _FakeThread.captured_kwargs["skip_fact_checking"] is False
    assert _FakeThread.captured_kwargs["request_mode"] == "standard"
