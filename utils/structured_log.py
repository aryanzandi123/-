"""Structured JSON line logging for pipeline execution.

Emits machine-parsable events to stderr via Python logging.
Runs alongside the existing file-based StepLogger without interference.
"""

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger("propath.pipeline")


class StructuredPipelineLog:
    """Emit structured JSON log events for a pipeline run."""

    def __init__(self, protein: str, run_id: Optional[str] = None):
        self.protein = protein
        self.run_id = run_id or str(uuid.uuid4())[:8]
        self.run_start = time.time()
        self._step_starts: Dict[str, float] = {}

    def _emit(self, event_type: str, data: dict) -> None:
        """Emit a single structured log line."""
        record = {
            "ts": time.time(),
            "event": event_type,
            "run_id": self.run_id,
            "protein": self.protein,
            **data,
        }
        try:
            logger.info(json.dumps(record, default=str))
        except Exception:
            logger.info(json.dumps({"event": event_type, "error": "serialization_failed"}))

    def pipeline_start(self, total_steps: int, request_mode: str) -> None:
        """Log pipeline start."""
        self._emit("pipeline_start", {
            "total_steps": total_steps,
            "request_mode": request_mode,
        })

    def step_start(self, step_name: str, step_index: int) -> None:
        """Log step start."""
        self._step_starts[step_name] = time.time()
        self._emit("step_start", {
            "step": step_name,
            "step_index": step_index,
        })

    def step_complete(self, step_name: str, step_index: int,
                      token_stats: dict, cost_stats: dict,
                      interactor_count: int = 0, function_count: int = 0) -> None:
        """Log step completion with metrics."""
        elapsed = time.time() - self._step_starts.get(step_name, self.run_start)
        self._emit("step_complete", {
            "step": step_name,
            "step_index": step_index,
            "elapsed_s": round(elapsed, 2),
            "tokens": token_stats,
            "cost": cost_stats,
            "interactors": interactor_count,
            "functions": function_count,
        })

    def pipeline_complete(self, pipeline_token_stats: dict,
                          request_metrics: dict) -> None:
        """Log pipeline completion with full summary."""
        elapsed = time.time() - self.run_start
        self._emit("pipeline_complete", {
            "elapsed_s": round(elapsed, 2),
            "tokens": {
                "input": pipeline_token_stats.get("total_input_tokens", 0),
                "thinking": pipeline_token_stats.get("total_thinking_tokens", 0),
                "output": pipeline_token_stats.get("total_output_tokens", 0),
                "total": pipeline_token_stats.get("total_tokens", 0),
            },
            "cost": {
                "input": round(pipeline_token_stats.get("total_input_cost", 0.0), 6),
                "thinking": round(pipeline_token_stats.get("total_thinking_cost", 0.0), 6),
                "output": round(pipeline_token_stats.get("total_output_cost", 0.0), 6),
                "total": round(pipeline_token_stats.get("total_cost", 0.0), 6),
            },
            "api_calls": request_metrics,
        })

    def pipeline_error(self, error: str) -> None:
        """Log pipeline error."""
        elapsed = time.time() - self.run_start
        self._emit("pipeline_error", {
            "elapsed_s": round(elapsed, 2),
            "error": str(error),
        })
