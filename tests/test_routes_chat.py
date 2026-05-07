#!/usr/bin/env python3
"""Integration tests for the chat blueprint."""

import app as app_module
import routes.chat as chat_module
import services.chat_service as chat_service_module


def test_chat_missing_parent():
    client = app_module.app.test_client()
    response = client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "hello"}],
        "state": {},
    })
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == "INVALID_INPUT"


def test_chat_empty_messages():
    client = app_module.app.test_client()
    response = client.post("/api/chat", json={
        "parent": "ATXN3",
        "messages": [],
        "state": {"parent": "ATXN3", "visible_proteins": []},
    })
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == "INVALID_INPUT"


def test_chat_invalid_json():
    client = app_module.app.test_client()
    response = client.post("/api/chat", data="not json", content_type="text/plain")
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == "INVALID_INPUT"


def test_chat_session_id_stores_and_retrieves(monkeypatch):
    """Verify stateful session mapping works."""
    call_count = [0]

    def fake_call(messages, system_prompt, max_history=10, previous_interaction_id=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: no previous_interaction_id expected
            assert previous_interaction_id is None
            return "first reply", "ix-session-1"
        else:
            # Second call: should auto-resolve from session mapping
            assert previous_interaction_id == "ix-session-1"
            return "second reply", "ix-session-2"

    monkeypatch.setattr(chat_module, "build_compact_rich_context", lambda parent, visible: {"interactions": []})
    monkeypatch.setattr(chat_module, "build_chat_system_prompt", lambda parent, context: "system prompt")
    monkeypatch.setattr(chat_module, "call_chat_llm", fake_call)

    client = app_module.app.test_client()

    # First turn with session_id
    response1 = client.post("/api/chat", json={
        "parent": "ATXN3",
        "messages": [{"role": "user", "content": "Hello"}],
        "state": {"parent": "ATXN3", "visible_proteins": []},
        "session_id": "test-session-abc",
    })
    assert response1.status_code == 200
    payload1 = response1.get_json()
    assert payload1["interaction_id"] == "ix-session-1"

    # Second turn with same session_id — should auto-resolve previous_interaction_id
    response2 = client.post("/api/chat", json={
        "parent": "ATXN3",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "Follow up"},
        ],
        "state": {"parent": "ATXN3", "visible_proteins": []},
        "session_id": "test-session-abc",
    })
    assert response2.status_code == 200
    payload2 = response2.get_json()
    assert payload2["interaction_id"] == "ix-session-2"
