"""Health and metrics endpoints for ProPaths observability.

GET /health  — Liveness probe (DB, cache, active jobs).
GET /metrics — Operational metrics (JSON or Prometheus text).
"""

import os
import time

from flask import Blueprint, jsonify, request

health_bp = Blueprint("health", __name__)

_boot_time = time.time()


@health_bp.route("/health", methods=["GET"])
def health_check():
    """Liveness probe: DB connectivity, cache dir, active jobs."""
    checks = {}
    healthy = True

    # --- Database connectivity ---
    try:
        from models import db
        db.session.execute(db.text("SELECT 1"))
        checks["database"] = {"status": "ok"}
    except Exception as exc:
        checks["database"] = {"status": "error", "detail": str(exc)}
        healthy = False

    # --- Cache directory writable ---
    try:
        from services.state import CACHE_DIR
        cache_path = str(CACHE_DIR)
        if os.path.isdir(cache_path) and os.access(cache_path, os.W_OK):
            checks["cache_dir"] = {"status": "ok", "path": cache_path}
        else:
            checks["cache_dir"] = {"status": "error", "detail": "not writable or missing"}
            healthy = False
    except Exception as exc:
        checks["cache_dir"] = {"status": "error", "detail": str(exc)}
        healthy = False

    # --- Active jobs ---
    try:
        from services.state import jobs, jobs_lock
        with jobs_lock:
            active = sum(
                1 for j in jobs.values()
                if j.get("status") == "processing"
            )
        checks["active_jobs"] = {"status": "ok", "count": active}
    except Exception as exc:
        checks["active_jobs"] = {"status": "error", "detail": str(exc)}

    status_code = 200 if healthy else 503
    return jsonify({
        "status": "healthy" if healthy else "unhealthy",
        "checks": checks,
        "uptime_seconds": round(time.time() - _boot_time, 1),
    }), status_code


@health_bp.route("/metrics", methods=["GET"])
def metrics_endpoint():
    """Operational metrics in JSON or Prometheus text format."""
    from services import metrics as metrics_registry
    from services.state import jobs, jobs_lock

    snap = metrics_registry.snapshot()

    # Inject live active-job count
    with jobs_lock:
        active_jobs = sum(
            1 for j in jobs.values()
            if j.get("status") == "processing"
        )
    snap["active_jobs"] = active_jobs

    fmt = request.args.get("format", "json")
    if fmt == "prometheus":
        lines = _build_prometheus_lines(snap)
        return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

    # Default JSON response — structured grouping
    avg_seconds = (
        round(snap["pipeline_total_seconds"] / snap["pipeline_runs_total"], 2)
        if snap["pipeline_runs_total"] > 0
        else 0.0
    )
    return jsonify({
        "uptime_seconds": snap["uptime_seconds"],
        "jobs": {
            "started": snap["jobs_started"],
            "completed": snap["jobs_completed"],
            "errored": snap["jobs_errored"],
            "cancelled": snap["jobs_cancelled"],
            "active": active_jobs,
        },
        "tokens": {
            "input": snap["total_input_tokens"],
            "thinking": snap["total_thinking_tokens"],
            "output": snap["total_output_tokens"],
            "total": snap["total_tokens"],
        },
        "cost": {
            "input": snap["total_input_cost"],
            "thinking": snap["total_thinking_cost"],
            "output": snap["total_output_cost"],
            "total": snap["total_cost"],
        },
        "api_calls": {
            "core_3pro": snap["core_calls_3pro"],
            "evidence_2_5pro": snap["evidence_calls_2_5pro"],
            "arrow_2_5pro": snap["arrow_calls_2_5pro"],
            "quota_skipped": snap["quota_skipped_calls"],
        },
        "pipeline": {
            "runs_total": snap["pipeline_runs_total"],
            "total_seconds": round(snap["pipeline_total_seconds"], 2),
            "avg_seconds": avg_seconds,
            "last_seconds": round(snap["pipeline_last_seconds"], 2),
        },
        "recent_runs": snap["recent_runs"],
    })


def _build_prometheus_lines(snap: dict) -> list:
    """Convert metrics snapshot to Prometheus text exposition format."""
    lines = []

    def _gauge(name, value, help_text=""):
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    def _counter(name, value, help_text=""):
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")

    _gauge("propath_uptime_seconds", snap["uptime_seconds"], "Process uptime in seconds")
    _gauge("propath_active_jobs", snap.get("active_jobs", 0), "Currently running jobs")

    _counter("propath_jobs_started_total", snap["jobs_started"], "Total jobs started")
    _counter("propath_jobs_completed_total", snap["jobs_completed"], "Total jobs completed")
    _counter("propath_jobs_errored_total", snap["jobs_errored"], "Total jobs errored")
    _counter("propath_jobs_cancelled_total", snap["jobs_cancelled"], "Total jobs cancelled")

    _counter("propath_tokens_input_total", snap["total_input_tokens"], "Total input tokens")
    _counter("propath_tokens_thinking_total", snap["total_thinking_tokens"], "Total thinking tokens")
    _counter("propath_tokens_output_total", snap["total_output_tokens"], "Total output tokens")
    _counter("propath_tokens_total", snap["total_tokens"], "Total tokens all types")

    _counter("propath_cost_input_dollars", snap["total_input_cost"], "Total input cost USD")
    _counter("propath_cost_thinking_dollars", snap["total_thinking_cost"], "Total thinking cost USD")
    _counter("propath_cost_output_dollars", snap["total_output_cost"], "Total output cost USD")
    _counter("propath_cost_total_dollars", snap["total_cost"], "Total cost USD")

    _counter("propath_api_calls_core_3pro_total", snap["core_calls_3pro"], "Core 3 Pro API calls")
    _counter("propath_api_calls_evidence_2_5pro_total", snap["evidence_calls_2_5pro"], "Evidence 2.5 Pro API calls")
    _counter("propath_api_calls_arrow_2_5pro_total", snap["arrow_calls_2_5pro"], "Arrow 2.5 Pro API calls")
    _counter("propath_api_calls_quota_skipped_total", snap["quota_skipped_calls"], "Quota-skipped API calls")

    _counter("propath_pipeline_runs_total", snap["pipeline_runs_total"], "Total pipeline runs")
    _counter("propath_pipeline_seconds_total", round(snap["pipeline_total_seconds"], 2), "Total pipeline seconds")
    _gauge("propath_pipeline_last_seconds", round(snap["pipeline_last_seconds"], 2), "Last pipeline run duration")

    return lines
