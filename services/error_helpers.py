"""Standardized error response helpers."""

from flask import jsonify


class ErrorCode:
    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTERNAL = "INTERNAL_ERROR"
    LLM_ERROR = "LLM_ERROR"
    PIPELINE_BUSY = "PIPELINE_BUSY"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"


def error_response(message: str, code: str, status: int = 400):
    """Return standardized JSON error response."""
    return jsonify({"error": message, "code": code}), status
