#!/usr/bin/env python3
"""Chat migration tests for Gemini 3 Interactions behavior."""

import app as app_module
import routes.chat as chat_module
import services.chat_service as chat_service_module
import utils.gemini_runtime as gemini_runtime_module


def test_chat_endpoint_returns_interaction_id(monkeypatch):
    captured = {}

    def fake_call(messages, system_prompt, max_history=10, previous_interaction_id=None):
        captured["previous_interaction_id"] = previous_interaction_id
        captured["max_history"] = max_history
        return "stubbed reply", "ix-test-1"

    monkeypatch.setattr(chat_module, "build_compact_rich_context", lambda parent, visible: {"interactions": []})
    monkeypatch.setattr(chat_module, "build_chat_system_prompt", lambda parent, context: "system prompt")
    monkeypatch.setattr(chat_module, "call_chat_llm", fake_call)

    client = app_module.app.test_client()
    response = client.post(
        "/api/chat",
        json={
            "parent": "ATXN3",
            "messages": [{"role": "user", "content": "Summarize what we know."}],
            "state": {"parent": "ATXN3", "visible_proteins": []},
            "max_history": 7,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["reply"] == "stubbed reply"
    assert payload["interaction_id"] == "ix-test-1"
    assert captured["previous_interaction_id"] is None
    assert captured["max_history"] == 7


def test_chat_endpoint_forwards_previous_interaction_id(monkeypatch):
    captured = {}

    def fake_call(messages, system_prompt, max_history=10, previous_interaction_id=None):
        captured["previous_interaction_id"] = previous_interaction_id
        return "continuation reply", "ix-test-2"

    monkeypatch.setattr(chat_module, "build_compact_rich_context", lambda parent, visible: {"interactions": []})
    monkeypatch.setattr(chat_module, "build_chat_system_prompt", lambda parent, context: "system prompt")
    monkeypatch.setattr(chat_module, "call_chat_llm", fake_call)

    client = app_module.app.test_client()
    response = client.post(
        "/api/chat",
        json={
            "parent": "ATXN3",
            "messages": [{"role": "user", "content": "Continue our prior answer."}],
            "state": {"parent": "ATXN3", "visible_proteins": []},
            "previous_interaction_id": "ix-prev-123",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["reply"] == "continuation reply"
    assert payload["interaction_id"] == "ix-test-2"
    assert captured["previous_interaction_id"] == "ix-prev-123"


def test_call_chat_llm_builds_interactions_request(monkeypatch):
    calls = []

    class _FakeOutput:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _FakeInteraction:
        def __init__(self, text="chat ok", interaction_id="ix-live-1"):
            self.id = interaction_id
            self.outputs = [_FakeOutput(text)]

    class _FakeInteractions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _FakeInteraction()

    class _FakeClient:
        def __init__(self, api_key=None):
            self.interactions = _FakeInteractions()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(gemini_runtime_module, "get_client", lambda api_key=None: _FakeClient())

    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi, what do you need?"},
        {"role": "user", "content": "Who won the latest Super Bowl?"},
    ]
    text, interaction_id = chat_service_module.call_chat_llm(
        messages=messages,
        system_prompt="system prompt",
        max_history=10,
        previous_interaction_id=None,
    )

    assert text == "chat ok"
    assert interaction_id == "ix-live-1"
    assert len(calls) == 1
    request_payload = calls[0]
    assert request_payload["model"] == "gemini-3-flash-preview"
    assert request_payload["store"] is True
    assert request_payload["system_instruction"] == "system prompt"
    assert request_payload["generation_config"]["thinking_level"] == "medium"
    assert request_payload["generation_config"]["thinking_summaries"] == "auto"
    assert request_payload["generation_config"]["max_output_tokens"] == 8000
    assert request_payload["input"].startswith("Conversation so far:\n")
    assert {"type": "google_search"} in request_payload.get("tools", [])
    assert "previous_interaction_id" not in request_payload


def test_call_chat_llm_with_previous_interaction_uses_latest_turn_and_url_tool(monkeypatch):
    calls = []

    class _FakeOutput:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _FakeInteraction:
        def __init__(self):
            self.id = "ix-live-2"
            self.outputs = [_FakeOutput("url response")]

    class _FakeInteractions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _FakeInteraction()

    class _FakeClient:
        def __init__(self, api_key=None):
            self.interactions = _FakeInteractions()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(gemini_runtime_module, "get_client", lambda api_key=None: _FakeClient())

    messages = [
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "Summarize https://www.wikipedia.org/ please."},
    ]
    text, interaction_id = chat_service_module.call_chat_llm(
        messages=messages,
        system_prompt="system prompt",
        max_history=10,
        previous_interaction_id="ix-prev-456",
    )

    assert text == "url response"
    assert interaction_id == "ix-live-2"
    assert len(calls) == 1
    request_payload = calls[0]
    assert request_payload["input"] == "Summarize https://www.wikipedia.org/ please."
    assert request_payload["previous_interaction_id"] == "ix-prev-456"
    assert {"type": "url_context"} in request_payload.get("tools", [])
