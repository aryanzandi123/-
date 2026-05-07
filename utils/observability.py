"""Structured logging + log-directory rotation utilities.

Two concerns in one place:

1. ``log_event`` — emit machine-parseable log lines for ops monitoring,
   while also keeping the developer-friendly ``[TAG]`` stderr output
   that existing code emits via ``print()``. Lets you migrate hot-path
   telemetry incrementally without rewriting every site at once.

2. ``prune_old_step_logs`` — clean up the ``Logs/<protein>/<timestamp>/``
   tree that ``utils.step_logger.StepLogger`` creates per query. Called
   once at app start, env-gated so nothing changes by default. Keeps
   the N most-recent runs per protein regardless of age, and deletes
   anything older than a max-age threshold beyond that.

Both utilities have zero hard dependency on Flask / SQLAlchemy — they
work in scripts and standalone processes too.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

# Dedicated logger so propath app loggers aren't affected by the level we set
# here. Tests can tweak this without bleeding into the Flask request log.
_logger = logging.getLogger("propath.events")
_logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# SSE event-stream plumbing (PR-4)
# ---------------------------------------------------------------------------
# log_event also pushes a compact copy of each event into the per-protein job
# dict so the SSE stream (`/api/stream/<protein>`) can surface them to the
# frontend's "Pipeline events" drawer without parsing stderr logs. Ring-buffer
# bounded to MAX_JOB_EVENTS to keep memory predictable on long runs.

MAX_JOB_EVENTS = 200

# Thread-local marker for the protein a pipeline thread is currently working
# on. Set by the job runner; read by log_event. Default is None so scripts
# that don't run inside a job don't accidentally push events anywhere.
import threading as _threading

_event_ctx = _threading.local()


def set_current_job_protein(protein: Optional[str]) -> None:
    """Tag the current thread with the protein it's running a job for."""
    _event_ctx.protein = protein


def _get_current_job_protein() -> Optional[str]:
    return getattr(_event_ctx, "protein", None)


def _push_to_job_events(event: str, level: str, tag: str, fields: dict) -> None:
    """Append to ``jobs[protein]["events"]`` when a job is active."""
    protein = _get_current_job_protein()
    if not protein:
        return
    try:
        from services.state import jobs, jobs_lock
    except Exception:
        return
    with jobs_lock:
        job = jobs.get(protein)
        if not job:
            return
        events = job.setdefault("events", [])
        events.append({
            "t": time.time(),
            "event": event,
            "level": level,
            "tag": tag,
            **{k: v for k, v in fields.items() if _json_safe(v)},
        })
        # Ring-buffer cap.
        if len(events) > MAX_JOB_EVENTS:
            del events[: len(events) - MAX_JOB_EVENTS]


def _json_safe(v: Any) -> bool:
    """Return True if ``v`` survives json.dumps safely — primitives + lists/dicts."""
    try:
        json.dumps(v, default=str)
        return True
    except Exception:
        return False


# ISO level names mapped to Python logging ints. Accepts either form.
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def log_event(
    event: str,
    *,
    level: str = "info",
    tag: Optional[str] = None,
    **fields: Any,
) -> None:
    """Emit a structured event line plus a developer-friendly stderr echo.

    Writes two lines for each call:

      1. A JSON line via the ``propath.events`` logger at the requested level —
         this is what monitoring/log aggregators should consume.
      2. A legacy ``[TAG] event key1=val1 key2=val2`` line on stderr — what
         human operators grep. Kept so we don't rip out the muscle memory
         of ``grep '\\[DB SYNC\\]' stderr.log``.

    Args:
        event: Short machine-readable identifier (``chain_pathway_drift``,
            ``discovery_zero_result``, etc.). Use snake_case.
        level: ``debug`` | ``info`` | ``warn`` | ``error``.
        tag: Human-readable prefix for the stderr echo (``DB SYNC``,
            ``CHAIN PATHWAY DRIFT``). Defaults to ``event.upper()``.
        **fields: Extra JSON-serializable key/value pairs to record.
    """
    level_int = _LEVELS.get((level or "info").lower(), logging.INFO)
    safe_fields = {k: _jsonable(v) for k, v in fields.items()}
    payload = {"event": event, **safe_fields}

    # 1. JSON to the logger so monitoring can ingest it.
    try:
        _logger.log(level_int, json.dumps(payload, default=str))
    except Exception:
        # A logging failure must never take down the caller.
        pass

    # 2. Legacy [TAG] echo to stderr for grep workflows.
    try:
        tag_str = (tag or event.replace("_", " ").upper()).strip()
        field_str = " ".join(f"{k}={v!r}" for k, v in safe_fields.items())
        prefix = f"[{tag_str}]" if tag_str else "[EVENT]"
        line = f"{prefix} {event}" + (f" {field_str}" if field_str else "")
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass

    # 3. PR-4: push to the per-protein SSE event buffer when a job is
    # active. Never raises — an observability failure must not break the
    # pipeline.
    try:
        _push_to_job_events(event, level, tag or "", safe_fields)
    except Exception:
        pass


def _jsonable(value: Any) -> Any:
    """Best-effort coerce a value into something ``json.dumps`` can serialize."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


# ---------------------------------------------------------------------------
# Log-directory rotation
# ---------------------------------------------------------------------------

def prune_old_step_logs(
    base_dir: str | os.PathLike = "Logs",
    *,
    max_age_days: Optional[int] = None,
    keep_latest_per_protein: int = 3,
    max_dirs_deleted: int = 500,
) -> dict[str, int]:
    """Remove old per-query log directories to keep ``Logs/`` bounded.

    Directory layout is ``<base_dir>/<protein>/<timestamp>/...``. For each
    protein we keep the ``keep_latest_per_protein`` most-recently-modified
    directories regardless of age, then prune anything older than
    ``max_age_days`` among the rest.

    Safe no-op when ``base_dir`` doesn't exist. Bounded by
    ``max_dirs_deleted`` per call so a misconfigured rotation pass can't
    wipe a huge corpus in one shot.

    Args:
        base_dir: Root of the step-logging tree (default ``Logs``).
        max_age_days: Delete eligible directories older than this many days.
            ``None`` (default) reads ``LOG_ROTATION_MAX_AGE_DAYS`` env var
            (default 14).
        keep_latest_per_protein: Always retain this many newest runs per
            protein, even if they're older than ``max_age_days``.
        max_dirs_deleted: Upper bound on deletions per call (safety net).

    Returns:
        Stats dict — ``{"scanned": N, "deleted": N, "kept": N,
        "errors": N}``.
    """
    if max_age_days is None:
        try:
            max_age_days = int(os.getenv("LOG_ROTATION_MAX_AGE_DAYS", "14"))
        except ValueError:
            max_age_days = 14

    base = Path(base_dir)
    stats = {"scanned": 0, "deleted": 0, "kept": 0, "errors": 0}
    if not base.exists() or not base.is_dir():
        return stats

    cutoff = time.time() - (max_age_days * 86400)

    for protein_dir in base.iterdir():
        if not protein_dir.is_dir():
            continue
        try:
            runs = [d for d in protein_dir.iterdir() if d.is_dir()]
        except OSError:
            stats["errors"] += 1
            continue

        # Sort newest-first by mtime; first N are always retained.
        runs.sort(key=lambda d: _safe_mtime(d), reverse=True)
        stats["scanned"] += len(runs)

        keepers = runs[:keep_latest_per_protein]
        candidates = runs[keep_latest_per_protein:]
        stats["kept"] += len(keepers)

        for run_dir in candidates:
            if stats["deleted"] >= max_dirs_deleted:
                break
            mtime = _safe_mtime(run_dir)
            if mtime > cutoff:
                stats["kept"] += 1
                continue
            try:
                shutil.rmtree(run_dir)
                stats["deleted"] += 1
            except OSError as exc:
                stats["errors"] += 1
                log_event(
                    "log_rotation_delete_failed",
                    level="warn",
                    tag="LOG ROTATION",
                    path=str(run_dir),
                    error=str(exc),
                )

        if stats["deleted"] >= max_dirs_deleted:
            log_event(
                "log_rotation_capped",
                level="warn",
                tag="LOG ROTATION",
                deleted=stats["deleted"],
                cap=max_dirs_deleted,
                note="Stopping this pass to avoid runaway deletion; re-run later.",
            )
            break

    return stats


def _safe_mtime(path: Path) -> float:
    """Return path's mtime, or 0 if unreadable (sorts oldest → pruned first)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def maybe_prune_at_startup() -> None:
    """Run log rotation if ``ENABLE_LOG_ROTATION=true`` in env.

    Designed to be called once from ``app.py`` after other startup work.
    Silent no-op when disabled. Exceptions are swallowed — log rotation
    must never block app boot.
    """
    if os.getenv("ENABLE_LOG_ROTATION", "false").lower() != "true":
        return
    try:
        keep = int(os.getenv("LOG_ROTATION_KEEP_LATEST", "3"))
    except ValueError:
        keep = 3
    try:
        stats = prune_old_step_logs(keep_latest_per_protein=keep)
        if stats["deleted"] or stats["errors"]:
            log_event(
                "log_rotation_complete",
                level="info",
                tag="LOG ROTATION",
                **stats,
            )
    except Exception as exc:  # defense in depth
        log_event(
            "log_rotation_startup_failed",
            level="warn",
            tag="LOG ROTATION",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Chain context backfill (B9 from the whole-codebase audit)
# ---------------------------------------------------------------------------

def maybe_backfill_chain_context_at_startup() -> None:
    """Run the chain_context backfill if ``ENABLE_BACKFILL_AT_STARTUP=true``.

    Legacy indirect Interaction rows don't have ``chain_context.full_chain``
    populated; the frontend's ``buildFullChainPath`` falls back to
    reconstruction paths that force the query to chain-head — losing real
    biology for any query-as-middle chain. The
    ``scripts/migrate_backfill_chain_context.py`` script already does the
    repair correctly and is idempotent (skips rows that already have
    full_chain). This hook wires it into app boot so operators don't have
    to remember to run it manually after a deploy.

    Default OFF — opt in via ``.env`` so the first boot after a deploy
    doesn't silently do a schema-scan. Once enabled, subsequent boots
    are cheap (O(number_of_indirect_rows) dict-key-check).
    """
    if os.getenv("ENABLE_BACKFILL_AT_STARTUP", "false").lower() != "true":
        return
    try:
        # Import lazily — backfill imports ChainView + app ctx, don't pull
        # those onto the module-load path just to check an env flag.
        from models import db, Interaction
        from utils.chain_view import ChainView
    except Exception as exc:
        log_event(
            "backfill_import_failed",
            level="warn",
            tag="CHAIN BACKFILL",
            error=str(exc),
        )
        return

    try:
        stats = {"scanned": 0, "backfilled": 0, "already_set": 0, "skipped": 0}
        for row in Interaction.query.filter(
            Interaction.interaction_type == "indirect"
        ).yield_per(200):
            stats["scanned"] += 1
            data = row.data if isinstance(row.data, dict) else {}
            ctx = data.get("chain_context") if isinstance(data, dict) else None
            if isinstance(ctx, dict) and isinstance(ctx.get("full_chain"), list):
                stats["already_set"] += 1
                continue
            query_protein = (row.discovered_in_query or "").strip() or None
            mediator = data.get("mediator_chain") or row.mediator_chain
            if not query_protein or not isinstance(mediator, list) or not mediator:
                stats["skipped"] += 1
                continue
            try:
                view = ChainView.from_interaction_data(
                    data, query_protein=query_protein
                )
                if view.is_empty:
                    stats["skipped"] += 1
                    continue
                view.apply_to_interaction(row)
                stats["backfilled"] += 1
            except Exception:
                stats["skipped"] += 1
        if stats["backfilled"]:
            db.session.commit()
            log_event(
                "chain_context_backfilled_at_startup",
                level="info",
                tag="CHAIN BACKFILL",
                **stats,
            )
        else:
            log_event(
                "chain_context_backfill_noop",
                level="debug",
                tag="CHAIN BACKFILL",
                **stats,
            )
    except Exception as exc:
        log_event(
            "chain_context_backfill_failed",
            level="warn",
            tag="CHAIN BACKFILL",
            error=str(exc),
        )
