"""Chat blueprint: /api/chat."""

import sys

from flask import Blueprint, request, jsonify

from services.chat_service import (
    build_compact_rich_context,
    build_chat_system_prompt,
    build_compact_state_from_request,
    call_chat_llm,
    get_interaction_id,
    store_interaction_id,
)
from services.error_helpers import error_response, ErrorCode
from utils.pruner import PROTEIN_RE

chat_bp = Blueprint('chat', __name__)


@chat_bp.post('/api/chat')
def chat():
    """Handle chat messages with LLM assistance."""
    try:
        data = request.get_json(silent=True)
        if not data:
            return error_response("Invalid JSON request", ErrorCode.INVALID_INPUT)

        parent = (data.get("parent") or "").strip()
        if not parent or not PROTEIN_RE.match(parent):
            return error_response("Invalid or missing parent protein", ErrorCode.INVALID_INPUT)

        messages = data.get("messages")
        if not isinstance(messages, list) or len(messages) == 0:
            return error_response("Invalid or empty messages list", ErrorCode.INVALID_INPUT)

        for msg in messages:
            if not isinstance(msg, dict):
                return error_response("Invalid message format", ErrorCode.INVALID_INPUT)
            if "role" not in msg or "content" not in msg:
                return error_response("Message missing role or content", ErrorCode.INVALID_INPUT)
            if msg["role"] not in ["user", "assistant"]:
                return error_response(f"Invalid message role: {msg['role']}", ErrorCode.INVALID_INPUT)

        last_msg = messages[-1]
        if last_msg.get("role") != "user":
            return error_response("Last message must be from user", ErrorCode.INVALID_INPUT)

        state_data = data.get("state", {})
        if not isinstance(state_data, dict):
            return error_response("Invalid state format", ErrorCode.INVALID_INPUT)

        max_history = data.get("max_history", 10)
        if not isinstance(max_history, int) or max_history < 1 or max_history > 50:
            max_history = 10

        previous_interaction_id = data.get("previous_interaction_id")
        if previous_interaction_id is not None:
            previous_interaction_id = str(previous_interaction_id).strip() or None

        # Stateful session support: auto-resolve previous_interaction_id
        session_id = data.get("session_id")
        if session_id and not previous_interaction_id:
            previous_interaction_id = get_interaction_id(parent, str(session_id))

        compact_state = build_compact_state_from_request(state_data)
        state_parent = compact_state.get("parent", "")
        visible_proteins = compact_state.get("visible_proteins", [])

        final_parent = state_parent if state_parent else parent

        if not final_parent or not PROTEIN_RE.match(final_parent):
            return error_response("Invalid parent protein in state", ErrorCode.INVALID_INPUT)

        rich_context = build_compact_rich_context(final_parent, visible_proteins)
        print(f"Chat: Built context with {len(rich_context.get('interactions', []))} interactions", file=sys.stderr)

        system_prompt = build_chat_system_prompt(final_parent, rich_context)
        print(f"Chat: System prompt length: {len(system_prompt)} chars", file=sys.stderr)

        response_text, interaction_id = call_chat_llm(
            messages,
            system_prompt,
            max_history=max_history,
            previous_interaction_id=previous_interaction_id,
        )
        print(f"Chat: Got response: {len(response_text) if response_text else 0} chars", file=sys.stderr)

        if not response_text or not response_text.strip():
            print(f"Chat ERROR: Response text is empty after LLM call", file=sys.stderr)
            return error_response("LLM returned empty response", ErrorCode.LLM_ERROR, 500)

        # Store interaction_id for stateful sessions
        if session_id and interaction_id:
            store_interaction_id(parent, str(session_id), interaction_id)

        return jsonify({"reply": response_text, "interaction_id": interaction_id}), 200

    except RuntimeError as e:
        print(f"[ERROR] Chat LLM error: {e}", file=sys.stderr)
        return error_response("LLM request failed", ErrorCode.LLM_ERROR, 500)
    except Exception as e:
        print(f"[ERROR] Chat internal error: {e}", file=sys.stderr)
        return error_response("Internal server error", ErrorCode.INTERNAL, 500)
