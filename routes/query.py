"""Query blueprint: /api/search, /api/query, /api/requery, /api/status, /api/cancel, /api/stream (SSE)."""

import json
import os
import re
import sys
import threading
import time

from flask import Blueprint, request, jsonify, Response

from services.state import jobs, jobs_lock, evict_stale_jobs, CACHE_DIR, to_bool, get_job_condition, cleanup_job_condition
from services.error_helpers import error_response, ErrorCode
from utils.gemini_runtime import parse_request_mode, get_request_mode
from runner import run_full_job, run_requery_job

query_bp = Blueprint('query', __name__)


@query_bp.route('/api/search/<protein>')
def search_protein(protein):
    """Search for a protein in the database (no querying/research).

    If the symbol is a generic biomolecule class (RNA, Ubiquitin, Proteasome,
    ...), return a structured ``status='pseudo_class'`` response so the UI
    can prompt the user to specify a real gene symbol instead of querying
    a class name as if it were a single protein.
    """
    if not protein or len(protein) > 100:
        return error_response(
            "Protein name length must be 1-100 characters.",
            ErrorCode.INVALID_INPUT,
        )

    try:
        from models import db, Protein, Interaction
        from utils.db_sync import classify_symbol, _PSEUDO_WHITELIST
        from utils.protein_aliases import canonicalize_protein_name

        # Canonicalize at the API edge so ``atxn3``, ``ATXN3``, ``MJD``,
        # ``α-synuclein`` all resolve to the same canonical row before
        # any DB lookup. Canonicalize BEFORE the format regex (P1-B2)
        # so Greek-letter aliases reach the resolver instead of being
        # rejected as "invalid format" up-front.
        canonical = canonicalize_protein_name(protein) or protein
        protein = canonical

        # Validate the canonical form is well-formed. Real gene symbols
        # are ASCII alphanumerics; if canonicalization couldn't produce
        # one, the user typed something unrecognized.
        if not re.match(r'^[a-zA-Z0-9_-]+$', protein):
            return error_response(
                "Unrecognized protein symbol. Please specify a gene symbol "
                "(e.g. 'SNCA' instead of 'α-synuclein').",
                ErrorCode.INVALID_INPUT,
            )

        # Refuse pseudo-class queries up-front, even if a Protein row exists
        # (e.g. backfill flagged it as is_pseudo). The user typed "RNA" but
        # really wants a specific RNA-binding protein. Return suggested
        # specifics from the DB to guide them.
        if classify_symbol(protein) == "pseudo":
            suggestions = []
            try:
                # Find the top 5 real-protein interactors that have a chain
                # involving this pseudo entity. Cheap heuristic: pull names
                # of proteins linked through any indirect interaction whose
                # chain_proteins JSONB contains the pseudo symbol.
                from models import IndirectChain
                rows = (
                    db.session.query(IndirectChain)
                    .filter(IndirectChain.chain_proteins.contains([protein]))
                    .limit(20)
                    .all()
                )
                seen: set = set()
                for r in rows:
                    for sym in (r.chain_proteins or []):
                        if sym == protein or sym in seen:
                            continue
                        if classify_symbol(sym) == "protein":
                            suggestions.append(sym)
                            seen.add(sym)
                        if len(suggestions) >= 5:
                            break
                    if len(suggestions) >= 5:
                        break
            except Exception:
                suggestions = []
            return jsonify({
                "status": "pseudo_class",
                "protein": protein,
                "message": (
                    f"{protein!r} is a generic biomolecule class, not a queryable "
                    "protein. It can appear as a chain mediator in interaction "
                    "cascades but is not a stand-alone interactor. Please specify "
                    "a real gene symbol."
                ),
                "suggestions": suggestions,
            })

        protein_obj = Protein.query.filter_by(symbol=protein).first()

        if not protein_obj:
            return jsonify({
                "status": "not_found",
                "protein": protein
            })

        # If the row exists but is flagged is_pseudo, behave like the
        # whitelist branch above (covers symbols that became pseudo after
        # the row was created).
        if getattr(protein_obj, "is_pseudo", False):
            return jsonify({
                "status": "pseudo_class",
                "protein": protein,
                "message": (
                    f"{protein!r} is stored as a generic biomolecule class. "
                    "Specify a real gene symbol to query."
                ),
                "suggestions": [],
            })

        interaction_count = db.session.query(Interaction).filter(
            (Interaction.protein_a_id == protein_obj.id) |
            (Interaction.protein_b_id == protein_obj.id)
        ).count()

        return jsonify({
            "status": "found",
            "protein": protein,
            "has_interactions": interaction_count > 0,
            "interaction_count": interaction_count,
            "last_queried": protein_obj.last_queried.isoformat() if protein_obj.last_queried else None,
            "query_count": protein_obj.query_count
        })

    except Exception as e:
        print(f"Search failed: {e}", file=sys.stderr)
        return error_response("Database search failed", ErrorCode.INTERNAL, 500)


@query_bp.route('/api/query', methods=['POST'])
def start_query():
    """Start a new pipeline job in the background."""
    from flask import current_app

    data = request.get_json(silent=True) or {}
    protein_name = data.get('protein')
    if not protein_name:
        return error_response("Protein name is required", ErrorCode.INVALID_INPUT)

    if len(protein_name) > 50:
        return error_response("Protein name too long (max 50 characters)", ErrorCode.INVALID_INPUT)

    # Canonicalize at the API edge BEFORE format validation (P1-B2).
    # Without this, Greek-letter aliases like 'α-synuclein' or 'NF-κB'
    # are rejected by the ASCII regex before they ever reach the
    # alias resolver. The job_id keys on the canonical name so
    # duplicate-spelling clicks merge onto one run.
    try:
        from utils.protein_aliases import canonicalize_protein_name
        canonical = canonicalize_protein_name(protein_name)
        if canonical:
            protein_name = canonical
    except Exception:
        # Alias-resolution glitch (e.g., DB hiccup) shouldn't block the
        # job. Fall through with the user's input — the pipeline's
        # internal db_sync will normalize at write time.
        pass

    # Validate the (now canonical) form. Canonical gene symbols are
    # ASCII alphanumerics + hyphens/underscores.
    if not re.match(r'^[a-zA-Z0-9_-]+$', protein_name):
        return error_response(
            "Unrecognized protein symbol. Please specify a gene symbol "
            "(e.g. 'SNCA' instead of 'α-synuclein').",
            ErrorCode.INVALID_INPUT,
        )

    # Pseudo-class gate (P1-B1): refuse generic biomolecule classes
    # (RNA, Ubiquitin, Proteasome, ...) as primary queries. They can
    # appear as chain mediators but are not stand-alone interactors.
    # /api/search has the same gate; without it here, /api/query lets
    # 'RNA' kick off a full pipeline run.
    try:
        from utils.db_sync import classify_symbol
        if classify_symbol(protein_name) == "pseudo":
            return jsonify({
                "status": "pseudo_class",
                "protein": protein_name,
                "error_code": ErrorCode.INVALID_INPUT,
                "message": (
                    f"{protein_name!r} is a generic biomolecule class, not a "
                    "queryable protein. Specify a real gene symbol instead."
                ),
            }), 400
    except Exception as _classify_exc:
        # Classifier unavailable (e.g. DB hiccup) — fall through so
        # the request isn't blocked by an infrastructure glitch.
        print(f"[/api/query] classify_symbol unavailable ({_classify_exc}); proceeding without pseudo gate", file=sys.stderr, flush=True)

    # Extract configuration (with defaults and validation)
    try:
        interactor_rounds = int(data.get('interactor_rounds', 3))
        function_rounds = int(data.get('function_rounds', 3))
        discovery_iterations = int(data.get('discovery_iterations', 5))

        interactor_rounds = max(3, min(8, interactor_rounds))
        function_rounds = max(3, min(8, function_rounds))
        discovery_iterations = max(1, min(10, discovery_iterations))
    except (TypeError, ValueError):
        return error_response("Invalid parameter values for rounds/depth/iterations", ErrorCode.INVALID_INPUT)

    # 2026-05-03: Surface skip_validation explicitly so the route's flag log
    # never silently hides the fact that evidence_validation is off. The
    # frontend persists skip_validation in localStorage (static/script.js:1021),
    # so a stale toggle from a previous session can disable evidence
    # validation across all subsequent runs without any visible signal —
    # the run still finishes, the post-processor just runs 10 stages
    # instead of 11. Operators saw "[POST-PROCESSING [N/11]]" predicted
    # but only 10 stages ran with no explanation of which one was skipped.
    skip_validation = bool(data.get('skip_validation', False))
    skip_deduplicator = bool(data.get('skip_deduplicator', False))
    skip_arrow_determination = bool(data.get('skip_arrow_determination', False))
    if skip_arrow_determination:
        print(
            "[WARN] Arrow determination SKIPPED — directions will use heuristic only, "
            "which may produce incorrect bidirectional assignments",
            file=sys.stderr, flush=True,
        )
    print(
        f"[ROUTE /api/query] post-processor flags — "
        f"skip_validation={skip_validation} (UI/POST {data.get('skip_validation')!r}) "
        f"[evidence_validation stage], "
        f"skip_deduplicator={skip_deduplicator} (UI/POST {data.get('skip_deduplicator')!r}) "
        f"[dedup_functions stage], "
        f"skip_arrow_determination={skip_arrow_determination} "
        f"(UI/POST {data.get('skip_arrow_determination')!r}) "
        f"[in-pipeline arrow heuristic, NOT post-processor arrow_validation].",
        file=sys.stderr, flush=True,
    )

    default_skip_fact_checking = to_bool(os.getenv("DEFAULT_SKIP_FACT_CHECKING"), default=True)
    skip_fact_checking = to_bool(data.get('skip_fact_checking'), default=default_skip_fact_checking)

    # Quick pathway assignment is now the DEFAULT mode (DB-first matching
    # against the existing hierarchy + LLM only on misses). The full LLM
    # pathway pipeline produced biologically-wrong parents on the
    # 2026-04-29 ULK1 run ("Synaptic Plasticity → Metabolism &
    # Bioenergetics") and is much slower (~15-30 min vs ~2 min). Operators
    # can opt out per-request via ``quick_pathway_assignment: false`` in
    # the POST body, or globally via ``DEFAULT_QUICK_PATHWAY_ASSIGNMENT=false``.
    _default_qpa = os.environ.get(
        "DEFAULT_QUICK_PATHWAY_ASSIGNMENT", "true"
    ).strip().lower() in ("1", "true", "yes")
    if 'quick_pathway_assignment' in data:
        quick_pathway_assignment = bool(data.get('quick_pathway_assignment'))
    else:
        quick_pathway_assignment = _default_qpa

    # --- Post-processing skip flags ---
    # Hard env override: when an env var is set (any value), it WINS over
    # the UI toggle. Matches the skip_citation_verification pattern so
    # users can force post-processing shape across all runs without
    # toggling the UI each time. Pass env var explicitly ""/"false" to
    # force-enable; set to "true" to force-skip.
    def _env_override(env_name: str, ui_value, default: bool = False) -> tuple:
        raw = os.getenv(env_name)
        if raw is not None and raw != "":
            return to_bool(raw, default=default), f"env {env_name}={raw!r}"
        return to_bool(ui_value, default=default), f"UI ({ui_value!r})"

    skip_schema_validation, _src_schema = _env_override('DEFAULT_SKIP_SCHEMA_VALIDATION', data.get('skip_schema_validation'))
    skip_interaction_metadata, _src_imeta = _env_override('DEFAULT_SKIP_INTERACTION_METADATA', data.get('skip_interaction_metadata'))
    skip_pmid_update, _src_pmid = _env_override('DEFAULT_SKIP_PMID_UPDATE', data.get('skip_pmid_update'))
    skip_arrow_validation, _src_arrow = _env_override('DEFAULT_SKIP_ARROW_VALIDATION', data.get('skip_arrow_validation'))
    skip_clean_names, _src_clean = _env_override('DEFAULT_SKIP_CLEAN_NAMES', data.get('skip_clean_names'))
    skip_finalize_metadata, _src_fmeta = _env_override('DEFAULT_SKIP_FINALIZE_METADATA', data.get('skip_finalize_metadata'))
    skip_direct_links, _src_dlink = _env_override('SKIP_DIRECT_LINK_EXTRACTION', data.get('skip_direct_links'))
    # New in 2026-04-15 R5: let users skip the normalize_function_contexts
    # stage that silently rewrites _context / arrow_context / chain_context
    # fields on every function. Skipping it preserves whatever shape the
    # upstream steps produced.
    skip_normalize_function_contexts, _src_norm = _env_override('SKIP_NORMALIZE_FUNCTION_CONTEXTS', data.get('skip_normalize_function_contexts'))
    print(
        f"[ROUTE /api/query] skip flags — "
        f"normalize_function_contexts={skip_normalize_function_contexts} ({_src_norm}), "
        f"arrow_validation={skip_arrow_validation} ({_src_arrow}), "
        f"finalize_metadata={skip_finalize_metadata} ({_src_fmeta}), "
        f"direct_links={skip_direct_links} ({_src_dlink}).",
        file=sys.stderr, flush=True,
    )
    # skip_citation_verification: env override (hard) > UI flag > default False.
    # The env var can be named SKIP_CITATION_VERIFICATION or skip_citation_verification
    # (case-insensitive match, checked in both forms for .env compatibility).
    _env_citation_override = (
        os.getenv('SKIP_CITATION_VERIFICATION')
        or os.getenv('skip_citation_verification')
        or os.getenv('DEFAULT_SKIP_CITATION_VERIFICATION')
    )
    if _env_citation_override is not None:
        skip_citation_verification = to_bool(_env_citation_override, default=False)
        _source = f"env override ({_env_citation_override!r})"
    else:
        skip_citation_verification = to_bool(data.get('skip_citation_verification'), default=False)
        _source = f"UI/POST body ({data.get('skip_citation_verification')!r})"
    print(
        f"[ROUTE /api/query] skip_citation_verification={skip_citation_verification} "
        f"(source: {_source})",
        file=sys.stderr, flush=True,
    )

    # --- Advanced settings ---
    ALLOWED_MODELS = {'gemini-3.1-pro-preview', 'gemini-2.5-pro', 'gemini-3-flash-preview'}
    model_overrides = {}
    for key in ('gemini_model_core', 'gemini_model_evidence', 'gemini_model_arrow', 'gemini_model_flash'):
        val = (data.get(key) or '').strip()
        if val and val in ALLOWED_MODELS:
            model_overrides[key] = val

    def _extract_int(key, lo, hi):
        raw = data.get(key)
        if raw is None or raw == '':
            return None
        try:
            return max(lo, min(hi, int(raw)))
        except (TypeError, ValueError):
            return None

    def _extract_float(key, lo, hi):
        raw = data.get(key)
        if raw is None or raw == '':
            return None
        try:
            return max(lo, min(hi, float(raw)))
        except (TypeError, ValueError):
            return None

    validation_max_workers = _extract_int('validation_max_workers', 1, 30)
    validation_batch_size = _extract_int('validation_batch_size', 1, 20)
    validation_batch_delay = _extract_float('validation_batch_delay', 0.0, 10.0)
    allow_output_clamp = bool(data.get('allow_output_clamp', False))
    iterative_delay = _extract_float('iterative_delay_seconds', 0.0, 30.0)

    verbose_pipeline = bool(data.get('verbose_pipeline', False))
    enable_step_logging = bool(data.get('enable_step_logging', False))
    max_chain_claims = _extract_int('max_chain_claims', 1, 10)
    chain_claim_style = (data.get('chain_claim_style') or '').strip().lower()
    if chain_claim_style not in ('identical', 'tailored'):
        chain_claim_style = None

    try:
        request_mode = parse_request_mode(data.get('request_mode'), default=get_request_mode())
    except ValueError as exc:
        return error_response(str(exc), ErrorCode.INVALID_INPUT)

    # Pipeline mode from UI (iterative, modern, standard) — sets env for this job
    pipeline_mode = data.get('pipeline_mode', '').strip().lower()
    if pipeline_mode in ('iterative', 'modern', 'standard'):
        os.environ['PIPELINE_MODE'] = pipeline_mode

    with jobs_lock:
        current_job = jobs.get(protein_name)
        if current_job:
            current_status = current_job.get("status")
            if current_status == "processing":
                cancel_event_check = current_job.get("cancel_event")
                if cancel_event_check and cancel_event_check.is_set():
                    pass
                else:
                    return jsonify({"status": "processing", "message": "Job already in progress."})

        cancel_event = threading.Event()
        jobs[protein_name] = {
            "status": "processing",
            "progress": "Initializing pipeline...",
            "cancel_event": cancel_event
        }
        thread = threading.Thread(
            target=run_full_job,
            kwargs=dict(
                user_query=protein_name,
                jobs=jobs,
                lock=jobs_lock,
                num_interactor_rounds=interactor_rounds,
                num_function_rounds=function_rounds,
                skip_validation=skip_validation,
                skip_deduplicator=skip_deduplicator,
                skip_arrow_determination=skip_arrow_determination,
                skip_fact_checking=skip_fact_checking,
                quick_pathway_assignment=quick_pathway_assignment,
                flask_app=current_app._get_current_object(),
                request_mode=request_mode,
                discovery_iterations=discovery_iterations,
                model_overrides=model_overrides or None,
                validation_max_workers=validation_max_workers,
                validation_batch_size=validation_batch_size,
                validation_batch_delay=validation_batch_delay,
                allow_output_clamp=allow_output_clamp,
                iterative_delay_seconds=iterative_delay,
                verbose_pipeline=verbose_pipeline,
                enable_step_logging=enable_step_logging,
                max_chain_claims=max_chain_claims,
                chain_claim_style=chain_claim_style,
                skip_schema_validation=skip_schema_validation,
                skip_interaction_metadata=skip_interaction_metadata,
                skip_pmid_update=skip_pmid_update,
                skip_arrow_validation=skip_arrow_validation,
                skip_clean_names=skip_clean_names,
                skip_finalize_metadata=skip_finalize_metadata,
                skip_direct_links=skip_direct_links,
                skip_normalize_function_contexts=skip_normalize_function_contexts,
                skip_citation_verification=skip_citation_verification,
            ),
        )
        thread.daemon = True
        thread.start()

    return jsonify({"status": "processing", "protein": protein_name})


@query_bp.route('/api/requery', methods=['POST'])
def requery_protein():
    """Re-query an existing protein to find new interactors."""
    from flask import current_app

    data = request.get_json(silent=True) or {}
    protein_name = data.get('protein', '').strip()
    if not protein_name:
        return error_response("Protein name is required", ErrorCode.INVALID_INPUT)

    if len(protein_name) > 50:
        return error_response("Protein name too long (max 50 characters)", ErrorCode.INVALID_INPUT)

    if not re.match(r'^[a-zA-Z0-9_-]+$', protein_name):
        return error_response(
            "Invalid protein name format. Please use only letters, numbers, hyphens, and underscores.",
            ErrorCode.INVALID_INPUT,
        )

    try:
        interactor_rounds = int(data.get('interactor_rounds', 3))
        function_rounds = int(data.get('function_rounds', 3))

        interactor_rounds = max(1, min(8, interactor_rounds))
        function_rounds = max(1, min(8, function_rounds))
    except (TypeError, ValueError):
        return error_response("Invalid parameter values", ErrorCode.INVALID_INPUT)

    default_skip_fact_checking = to_bool(os.getenv("DEFAULT_SKIP_FACT_CHECKING"), default=True)
    skip_fact_checking = to_bool(data.get('skip_fact_checking'), default=default_skip_fact_checking)
    skip_deduplicator = bool(data.get('skip_deduplicator', False))

    try:
        request_mode = parse_request_mode(data.get('request_mode'), default=get_request_mode())
    except ValueError as exc:
        return error_response(str(exc), ErrorCode.INVALID_INPUT)

    with jobs_lock:
        current_job = jobs.get(protein_name)
        if current_job:
            current_status = current_job.get("status")
            if current_status == "processing":
                cancel_event_check = current_job.get("cancel_event")
                if cancel_event_check and cancel_event_check.is_set():
                    pass
                else:
                    return jsonify({"status": "processing", "message": "Job already in progress."})

        cancel_event = threading.Event()
        jobs[protein_name] = {
            "status": "processing",
            "progress": "Initializing re-query...",
            "cancel_event": cancel_event,
        }
        thread = threading.Thread(
            target=run_requery_job,
            kwargs=dict(
                user_query=protein_name,
                jobs=jobs,
                lock=jobs_lock,
                num_interactor_rounds=interactor_rounds,
                num_function_rounds=function_rounds,
                skip_deduplicator=skip_deduplicator,
                skip_fact_checking=skip_fact_checking,
                flask_app=current_app._get_current_object(),
                request_mode=request_mode,
            ),
        )
        thread.daemon = True
        thread.start()

    return jsonify({"status": "processing", "protein": protein_name})


@query_bp.route('/api/status/<protein>')
def get_status(protein):
    """Check the status of a running job."""
    evict_stale_jobs()

    with jobs_lock:
        job_status = jobs.get(protein)

    if job_status:
        serializable_status = {k: v for k, v in job_status.items() if k != "cancel_event"}
        return jsonify(serializable_status)

    cache_path = os.path.join(CACHE_DIR, f"{protein}.json")
    if os.path.exists(cache_path):
        return jsonify({"status": "complete"})

    return jsonify({"status": "not_found"})


@query_bp.route('/api/stream/<protein>')
def stream_status(protein):
    """SSE stream for real-time job status updates.

    Replaces client-side polling with a single persistent connection.
    Falls back to 2-second server-side check when no condition signal arrives.
    """
    if not re.match(r'^[a-zA-Z0-9_-]+$', protein):
        return jsonify({"error": "Invalid protein name"}), 400

    def generate():
        cond = get_job_condition(protein)
        last_payload = None
        try:
            while True:
                try:
                    with jobs_lock:
                        job_status = jobs.get(protein)

                    if job_status:
                        payload = {k: v for k, v in job_status.items() if k != "cancel_event"}
                        # PR-4: surface pipeline events to the frontend drawer.
                        # The events list is an in-place ring buffer populated
                        # by utils.observability.log_event for the current job.
                        # We include the tail only so repeat frames stay small.
                        events_tail = (payload.get("events") or [])[-50:]
                        payload["events"] = events_tail
                    else:
                        cache_path = os.path.join(str(CACHE_DIR), f"{protein}.json")
                        if os.path.exists(cache_path):
                            payload = {"status": "complete"}
                        else:
                            payload = {"status": "not_found"}

                    # Only send when payload actually changed
                    payload_json = json.dumps(payload, default=str)
                    if payload_json != last_payload:
                        last_payload = payload_json
                        yield f"data: {payload_json}\n\n"

                    # Terminal states: send final event and close
                    status = payload.get("status", "")
                    if status in ("complete", "error", "cancelled", "not_found"):
                        yield f"event: done\ndata: {payload_json}\n\n"
                        return

                    # Wait for condition signal or timeout after 2s
                    with cond:
                        cond.wait(timeout=2)
                except GeneratorExit:
                    # Client disconnected mid-stream. Previously this was
                    # silently swallowed — log at info level so operational
                    # tracing can see disconnect patterns, then re-raise so
                    # Flask/Werkzeug finalizes the generator cleanly.
                    print(
                        f"[SSE] Client disconnected from /api/stream/{protein}",
                        file=sys.stderr,
                    )
                    raise
                except Exception as exc:
                    print(
                        f"[SSE] Stream error for {protein}: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    error_payload = json.dumps({"status": "error", "error": "Stream error"})
                    yield f"event: error\ndata: {error_payload}\n\n"
                    return
        finally:
            # Always release the per-protein condition variable so a
            # disconnected/errored stream doesn't leak shared state.
            try:
                cleanup_job_condition(protein, cond)
            except Exception as cleanup_exc:
                print(
                    f"[SSE] cleanup_job_condition failed for {protein}: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                    file=sys.stderr,
                )

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


@query_bp.route('/api/cancel/<protein>', methods=['POST'])
def cancel_job(protein):
    """Cancel a running job by setting its cancellation event."""
    if not protein:
        return error_response("Protein name is required", ErrorCode.INVALID_INPUT)

    with jobs_lock:
        job = jobs.get(protein)
        if not job:
            return error_response("Job not found", ErrorCode.JOB_NOT_FOUND, 404)

        if job.get("status") != "processing":
            return error_response("Job is not currently processing", ErrorCode.CONFLICT)

        cancel_event = job.get("cancel_event")
        if cancel_event:
            cancel_event.set()
            job["status"] = "cancelling"
            job["progress"] = {"text": "Cancelling..."}
            return jsonify({"status": "cancelling", "message": "Cancellation requested"}), 200
        else:
            return error_response("Job does not support cancellation", ErrorCode.CONFLICT)
