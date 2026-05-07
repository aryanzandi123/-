"""Process-global metrics registry for pipeline observability.

Thread-safe counters for tokens, costs, jobs, and timing.
Read by /metrics endpoint; written by runner.py instrumentation hooks.
"""

import threading
import time
from typing import Any, Dict

_lock = threading.Lock()
_boot_time = time.time()

_MAX_RECENT = 10

_counters: Dict[str, Any] = {
    "jobs_started": 0,
    "jobs_completed": 0,
    "jobs_errored": 0,
    "jobs_cancelled": 0,
    "total_input_tokens": 0,
    "total_thinking_tokens": 0,
    "total_output_tokens": 0,
    "total_tokens": 0,
    "total_input_cost": 0.0,
    "total_thinking_cost": 0.0,
    "total_output_cost": 0.0,
    "total_cost": 0.0,
    "core_calls_3pro": 0,
    "evidence_calls_2_5pro": 0,
    "arrow_calls_2_5pro": 0,
    "quota_skipped_calls": 0,
    "pipeline_runs_total": 0,
    "pipeline_total_seconds": 0.0,
    "pipeline_last_seconds": 0.0,
    "recent_runs": [],
}


def increment(key: str, amount: float = 1) -> None:
    """Atomically increment a counter."""
    with _lock:
        _counters[key] = _counters.get(key, 0) + amount


def record_pipeline_complete(run_summary: dict) -> None:
    """Record a completed pipeline run with full stats."""
    with _lock:
        _counters["pipeline_runs_total"] += 1
        elapsed = run_summary.get("elapsed_seconds", 0.0)
        _counters["pipeline_total_seconds"] += elapsed
        _counters["pipeline_last_seconds"] = elapsed

        for key in ("total_input_tokens", "total_thinking_tokens",
                     "total_output_tokens", "total_tokens"):
            _counters[key] += run_summary.get(key, 0)

        for key in ("total_input_cost", "total_thinking_cost",
                     "total_output_cost", "total_cost"):
            _counters[key] += run_summary.get(key, 0.0)

        for key in ("core_calls_3pro", "evidence_calls_2_5pro",
                     "arrow_calls_2_5pro", "quota_skipped_calls"):
            _counters[key] += run_summary.get(key, 0)

        recent = _counters["recent_runs"]
        recent.append({
            "protein": run_summary.get("protein", "unknown"),
            "started_at": run_summary.get("started_at", ""),
            "elapsed_seconds": elapsed,
            "total_cost": run_summary.get("total_cost", 0.0),
            "total_tokens": run_summary.get("total_tokens", 0),
            "steps": run_summary.get("step_count", 0),
        })
        if len(recent) > _MAX_RECENT:
            _counters["recent_runs"] = recent[-_MAX_RECENT:]


def snapshot() -> dict:
    """Return a consistent copy of all metrics."""
    with _lock:
        result = dict(_counters)
        result["recent_runs"] = list(_counters["recent_runs"])
        result["uptime_seconds"] = round(time.time() - _boot_time, 1)
        return result
