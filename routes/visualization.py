"""Visualization blueprint: /, /api/visualize."""

import json
import logging
import sys
import traceback
from html import escape as escape_html

from flask import Blueprint, render_template, make_response, jsonify, request, current_app

from services.state import CACHE_DIR
from services.data_builder import build_full_json_from_db
from services.error_helpers import error_response, ErrorCode
from utils.pruner import PROTEIN_RE

logger = logging.getLogger(__name__)

viz_bp = Blueprint('visualization', __name__)


@viz_bp.route('/')
def index():
    """Serve the main HTML page."""
    return render_template('index.html')


# Server-side HTML cache. Keyed by (protein_upper, content_fingerprint) so
# invalidation is automatic: when the underlying DB payload changes the
# fingerprint changes and the entry misses naturally. Capped to avoid
# unbounded memory growth on a long-running process. Hash is over the
# serialized payload — not cheap, but much cheaper than re-rendering
# the full visualization HTML for a 60+ interactor protein.
import hashlib as _hashlib
from collections import OrderedDict as _OrderedDict

_VIZ_HTML_CACHE: "_OrderedDict[tuple, str]" = _OrderedDict()
_VIZ_HTML_CACHE_MAX_ENTRIES = 32


def _viz_html_cache_enabled() -> bool:
    """Disable generated-HTML cache in dev so app.py reflects edits instantly."""
    if current_app.config.get("DEBUG") or current_app.config.get("TESTING"):
        return False
    return current_app.config.get("SEND_FILE_MAX_AGE_DEFAULT") != 0


def _set_viz_cache_headers(response, cache_hit: str | None = None):
    if _viz_html_cache_enabled():
        response.headers['Cache-Control'] = 'private, max-age=60'
    else:
        response.headers['Cache-Control'] = 'no-store, max-age=0'
    if cache_hit:
        response.headers['X-Viz-Cache'] = cache_hit
    return response


def _viz_cache_get(protein_key: str, fingerprint: str) -> str | None:
    if not _viz_html_cache_enabled():
        return None
    key = (protein_key, fingerprint)
    html = _VIZ_HTML_CACHE.get(key)
    if html is not None:
        # LRU touch — move to most-recent end.
        _VIZ_HTML_CACHE.move_to_end(key)
    return html


def _viz_cache_put(protein_key: str, fingerprint: str, html: str) -> None:
    if not _viz_html_cache_enabled():
        return
    key = (protein_key, fingerprint)
    _VIZ_HTML_CACHE[key] = html
    _VIZ_HTML_CACHE.move_to_end(key)
    while len(_VIZ_HTML_CACHE) > _VIZ_HTML_CACHE_MAX_ENTRIES:
        _VIZ_HTML_CACHE.popitem(last=False)


def _fingerprint_payload(result: dict) -> str:
    """Stable short hash of the payload for cache-key purposes.

    ``default=str`` handles datetime, Decimal, UUID, and similar non-JSON
    types. The narrow ``(TypeError, ValueError)`` catch below only fires
    for truly non-serializable shapes (circular refs, custom objects
    without __str__), which we log at debug so operators can investigate
    why a payload never caches. Previously a blanket ``except: return ""``
    silently disabled the cache for any payload that raised, including
    transient encoding issues that logging would have surfaced.
    """
    try:
        blob = json.dumps(result, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        logger.debug(
            "Visualization payload not serializable; cache skipped. "
            "err=%s: %s", type(exc).__name__, exc,
        )
        return ""
    return _hashlib.blake2s(blob.encode("utf-8"), digest_size=12).hexdigest()


def _render_spa_shell(protein: str, raw_json):
    """Render the React SPA shell (templates/visualize.html).

    The React rewrite is still useful as an opt-in experiment via ``?spa=1``.
    The daily-driver visualization route defaults to the legacy shell because
    that card/modal stack is the currently stable UI.

    Server-side hydration via window.__PROPATHS_BOOTSTRAP__ when raw_json
    is provided. Workspace routes pass raw_json=None and let the SPA fetch
    each protein via /api/results/<p>.
    """
    main = (raw_json or {}).get('snapshot_json', {}).get('main') if raw_json else protein
    main = main or protein
    cache_bust = str(int(__import__('time').time()))
    html = render_template(
        'visualize.html',
        main_protein=str(main),
        raw_json=raw_json,
        cache_bust=cache_bust,
    )
    response = make_response(html)
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['X-Viz-Shell'] = 'spa'
    return response


@viz_bp.route('/workspace/<protein_list>')
def get_workspace(protein_list):
    """Multi-protein workspace shell. Frontend parses :proteinList and
    fetches each via /api/results/<p>. No server-side hydration."""
    if not protein_list or len(protein_list) > 256:
        return error_response("Invalid workspace list", ErrorCode.INVALID_INPUT)
    return _render_spa_shell(protein_list, raw_json=None)


@viz_bp.route('/api/visualize/<protein>')
def get_visualization(protein):
    """Generate and serve HTML visualization.

    Default = stable legacy vanilla-JS shell at
    ``templates/visualize_legacy.html`` (loads JS from ``/static/_legacy/``).
    ``?spa=1`` opts into the React SPA while that rewrite is being repaired.
    """
    if not PROTEIN_RE.match(protein):
        return error_response("Invalid protein name", ErrorCode.INVALID_INPUT)
    try:
        from utils.protein_aliases import canonicalize_protein_name
        protein = canonicalize_protein_name(protein) or protein
        result = build_full_json_from_db(protein)
        if result:
            logger.debug("Visualization for %s: keys=%s", protein, list(result.keys()))
            if 'snapshot_json' in result:
                snap = result['snapshot_json']
                logger.debug("  snapshot: main=%s, proteins=%d, interactions=%d",
                             snap.get('main'), len(snap.get('proteins', [])),
                             len(snap.get('interactions', [])))

            # Keep the stable card/modal UI on the default route. The React
            # SPA remains available behind an explicit opt-in while its layout
            # and modal behavior are repaired.
            if request.args.get('spa') == '1':
                return _render_spa_shell(protein, raw_json=result)

            fingerprint = _fingerprint_payload(result)
            cached_html = _viz_cache_get(protein.upper(), fingerprint) if fingerprint else None
            if cached_html is not None:
                response = make_response(cached_html)
                return _set_viz_cache_headers(response, 'hit')

            from visualizer import create_visualization_from_dict
            html = create_visualization_from_dict(result)
            if fingerprint:
                _viz_cache_put(protein.upper(), fingerprint, html)

            response = make_response(html)
            return _set_viz_cache_headers(response, 'miss')
        else:
            # Postgres is the single source of truth. The previous file-
            # cache fallback let /api/visualize render data that
            # /api/results 404'd on, so the same protein could appear
            # alive in one URL and dead in another. Drop it; if Postgres
            # doesn't have the protein, neither does the app.
            logger.debug("Protein %s not found in database — returning 404.", protein)
            return error_response("Protein not found", ErrorCode.NOT_FOUND, 404)
    except Exception as e:
        print(f"Database visualization failed for {protein}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        # Always return a useful response — never a bare 500 that leaves the
        # browser on a blank page. Clients hitting /api/visualize/<protein>
        # directly (rare) get the structured error_response; clients doing
        # a user-facing reload get a small HTML error card they can act on
        # ("Retry" / "Report"). Accept header tells us which to send.
        wants_html = "text/html" in (request.headers.get("Accept") or "")
        if wants_html:
            safe_name = escape_html(protein)
            safe_err = escape_html(str(e))
            html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Visualization error — {safe_name}</title>
<style>
  body {{ font: 14px/1.5 -apple-system, sans-serif; max-width: 640px;
         margin: 64px auto; padding: 0 24px; color: #111; }}
  h1 {{ font-size: 20px; margin: 0 0 12px; color: #991b1b; }}
  code {{ background: #f4f4f5; padding: 2px 6px; border-radius: 4px;
          font-size: 12px; }}
  .detail {{ background: #fef2f2; border-left: 3px solid #dc2626;
             padding: 12px 16px; margin-top: 16px; white-space: pre-wrap;
             font-family: ui-monospace, Menlo, monospace; font-size: 12px; }}
  .retry {{ display: inline-block; margin-top: 16px; padding: 8px 16px;
            background: #111; color: #fff; text-decoration: none;
            border-radius: 6px; font-weight: 500; }}
</style></head><body>
<h1>Visualization failed for <code>{safe_name}</code></h1>
<p>The server couldn't build the visualization. This is usually a
transient database or pipeline issue — retry in a few seconds.</p>
<div class="detail">{safe_err}</div>
<a class="retry" href="javascript:location.reload()">Retry</a>
</body></html>"""
            resp = make_response(html, 500)
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            return resp
        return error_response("Database query failed", ErrorCode.INTERNAL, 500)
