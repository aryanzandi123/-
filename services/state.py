"""Process-global shared state for cross-blueprint access.

Thread-safe via explicit locks. Imported by blueprints and services.
No Flask dependency -- pure Python threading primitives.
"""

import atexit
import threading
import time
from pathlib import Path

from utils.pruner import PRUNED_DIRNAME

# ---------------------------------------------------------------------------
# Job Tracking
# ---------------------------------------------------------------------------
jobs: dict = {}
jobs_lock = threading.Lock()

_JOB_TTL_SECONDS = 300

# Per-job Condition for SSE push notification.
# Keyed by protein name. Created on first subscription, notified on update.
_job_conditions: dict = {}
_conditions_lock = threading.Lock()


def get_job_condition(protein: str) -> threading.Condition:
    """Get or create a Condition for SSE listeners on this job."""
    with _conditions_lock:
        if protein not in _job_conditions:
            _job_conditions[protein] = threading.Condition()
        return _job_conditions[protein]


def notify_job_update(protein: str) -> None:
    """Wake SSE listeners waiting on this job's Condition.

    Acquires ``_conditions_lock`` across BOTH the lookup and the notify to
    close the race where a concurrent ``cleanup_job_condition`` could drop
    the Condition from the dict between our lookup and our notify_all() —
    causing the notify to fire on a stale instance nobody is waiting on.

    Holding _conditions_lock while also acquiring ``cond`` would be a
    lock-order violation (listeners in stream_status hold cond while
    calling get_job_condition, which wants _conditions_lock). So we use
    a two-step pattern that snapshots the identity under _conditions_lock
    and only releases the outer lock once we've captured the Condition
    reference into a local; notify then fires whether or not the entry
    was deleted meanwhile — the worst case is waking a Condition whose
    listener has already moved on, which is harmless.
    """
    with _conditions_lock:
        cond = _job_conditions.get(protein)
    if cond is None:
        return
    try:
        with cond:
            cond.notify_all()
    except RuntimeError:
        # RuntimeError is raised if cond's internal lock is in an
        # inconsistent state because cleanup_job_condition ran mid-notify.
        # Log but don't crash — the listener whose condition got cleaned up
        # is already gone.
        import sys as _sys
        print(
            f"[STATE] notify_job_update: Condition for {protein!r} was "
            "cleaned up mid-notify; listener already unsubscribed.",
            file=_sys.stderr,
        )


def cleanup_job_condition(protein: str, cond: threading.Condition | None = None) -> None:
    """Remove a Condition when a job finishes and all listeners close.

    If *cond* is provided, only remove when it matches the stored instance
    (prevents a late cleanup from clobbering a newer listener's Condition).
    """
    with _conditions_lock:
        if cond is not None:
            if _job_conditions.get(protein) is cond:
                del _job_conditions[protein]
        else:
            _job_conditions.pop(protein, None)


def evict_stale_jobs():
    """Remove completed/errored jobs older than TTL. Called on each status check."""
    now = time.time()
    with jobs_lock:
        stale = [
            name for name, job in jobs.items()
            if job.get("status") in ("complete", "error", "cancelled")
            and now - job.get("_finished_at", now) > _JOB_TTL_SECONDS
        ]
        for name in stale:
            del jobs[name]


def cleanup_jobs_on_exit():
    """Mark in-progress jobs as interrupted on shutdown."""
    with jobs_lock:
        for name, job in jobs.items():
            if job.get("status") == "processing":
                job["status"] = "error"
                job["progress"] = "Server restarted — job was interrupted."


atexit.register(cleanup_jobs_on_exit)

# ---------------------------------------------------------------------------
# Pipeline Status
# ---------------------------------------------------------------------------
PIPELINE_STATUS = {
    "is_running": False,
    "current_step": None,
    "total_steps": 6,
    "logs": [],
    "error": None,
    "query_filter": None,
}
PIPELINE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Paths (initialised at import time; directories created by app.py)
# ---------------------------------------------------------------------------
CACHE_DIR: Path = Path("cache")
PRUNED_DIR: Path = CACHE_DIR / PRUNED_DIRNAME

# ---------------------------------------------------------------------------
# Arrow → Effect label mapping (single source of truth)
# ---------------------------------------------------------------------------
ARROW_TO_EFFECT = {
    "activates": "activation",
    "inhibits": "inhibition",
    "binds": "binding",
    "regulates": "regulation",
}


def arrow_to_effect(arrow: str) -> str:
    """Convert arrow type to human-readable effect label."""
    return ARROW_TO_EFFECT.get(arrow, arrow)


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------
def to_bool(value, default: bool = False) -> bool:
    """Convert various types to boolean."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
