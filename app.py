"""ProPaths — thin Flask shell.

Creates the Flask app, configures the database, registers blueprints,
and re-exports names needed by external scripts.
"""

from dotenv import load_dotenv
import os

# Snapshot SKIP_APP_BOOTSTRAP from the parent shell BEFORE load_dotenv
# mutates os.environ. ``load_dotenv(override=True)`` would otherwise
# clobber a shell-set value with whatever is (or isn't) in .env, which
# defeats the purpose of the skip flag for tools like Alembic and ad-hoc
# scripts that need a non-bootstrapping import.
_SHELL_SKIP_APP_BOOTSTRAP = os.environ.get("SKIP_APP_BOOTSTRAP", "")

# Load .env file with override to ensure fresh values
load_dotenv(override=True)

# Restore the shell-set skip flag if .env didn't provide one. This
# preserves the explicit shell intent without disabling load_dotenv
# overrides for everything else.
if _SHELL_SKIP_APP_BOOTSTRAP and not os.environ.get("SKIP_APP_BOOTSTRAP"):
    os.environ["SKIP_APP_BOOTSTRAP"] = _SHELL_SKIP_APP_BOOTSTRAP

import sys
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from flask import Flask
from flask_compress import Compress

# --- Structured logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    stream=sys.stderr,
)
logger = logging.getLogger('propath')

# --- App Setup ---
app = Flask(__name__)
Compress(app)

# Static-file cache policy.
# In dev (FLASK_DEBUG=true) serve with max-age=0 so edits to
# static/modal.js / card_view.js / visualizer.js / *.css propagate on a
# normal browser refresh. In prod keep the 24h cache so repeat visitors
# aren't redownloading ~18K lines of JS on every navigation.
_flask_debug = os.getenv('FLASK_DEBUG', '').strip().lower() in ('1', 'true', 'yes', 'on')
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0 if _flask_debug else 86400

# --- Database Configuration (PostgreSQL via Railway) ---
database_url = os.getenv('DATABASE_PUBLIC_URL') or os.getenv('DATABASE_URL')
# Defensive strip: Railway's "copy as secret" export format can paste back
# as `DATABASE_URL=postgresql://...`, which when stored in .env becomes
# literally `DATABASE_URL=DATABASE_URL=postgresql://...`. os.getenv then
# returns the value with a leading `DATABASE_URL=` that SQLAlchemy can't
# parse. Strip it here instead of relying on the operator to notice.
if database_url and database_url.startswith('DATABASE_URL='):
    database_url = database_url[len('DATABASE_URL='):]
if not database_url:
    print("[WARN]WARNING: DATABASE_URL not set. Using SQLite fallback (local dev only).", file=sys.stderr)
    database_url = 'sqlite:///fallback.db'
elif database_url.startswith('postgres://'):
    # Railway provides postgres:// but SQLAlchemy 1.4+ requires postgresql://
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
# Hard-fail on malformed URLs — don't silently fall through to SQLite
# when the operator expected Postgres. A malformed URL is almost always
# a config bug that should surface loudly at boot.
if not database_url.startswith(('postgresql://', 'sqlite://')):
    raise RuntimeError(
        f"DATABASE_URL is malformed: got {database_url!r}. "
        "Expected a postgresql:// or sqlite:// URI."
    )

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
_engine_options = {
    'pool_pre_ping': True,  # Verify connections before using
}
if database_url.startswith('postgresql'):
    _engine_options.update({
        'pool_size': 5,
        'max_overflow': 5,       # Cap at 10 total connections
        'pool_recycle': 900,     # 15 min — matches Railway idle timeout
        'connect_args': {'connect_timeout': 10},
    })
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = _engine_options


def _mask_database_url(url: str) -> str:
    """Return a log-safe database URL with credentials removed."""
    try:
        parsed = urlsplit(url)
        if not parsed.netloc:
            return parsed.scheme + "://***"
        host = parsed.hostname or "***"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        safe_netloc = f"***@{host}" if parsed.username else host
        return urlunsplit((parsed.scheme, safe_netloc, parsed.path, "", ""))
    except Exception:
        return "***"

# Initialize SQLAlchemy with app
from models import db
db.init_app(app)


@app.teardown_appcontext
def shutdown_session(exception=None):
    """Ensure scoped session is removed after each request / app context."""
    db.session.remove()


# Read-only DB connection probe at import time. Useful as boot
# diagnostics; never writes. Tools that import the app (Alembic, db
# health checks) get a clean signal whether the connection works
# without triggering schema creation.
with app.app_context():
    print("\n" + "="*60, file=sys.stderr)
    print("[DATABASE] Initializing PostgreSQL connection...", file=sys.stderr)
    print(f"[DATABASE] URL: {_mask_database_url(database_url)}", file=sys.stderr)
    try:
        db.session.execute(db.text('SELECT 1'))
        print("[DATABASE] [OK]Connection verified", file=sys.stderr)
    except Exception as e:
        print(f"\n[ERROR][DATABASE] Connection probe failed: {e}", file=sys.stderr)
        print("   The app may still start, but DB-backed routes will 500.", file=sys.stderr)
        print("="*60 + "\n", file=sys.stderr)
        import traceback as _tb
        _tb.print_exc(file=sys.stderr)


def bootstrap_app(application=app, *, skip_create_all: bool | None = None) -> None:
    """Run startup side effects.

    Imports of ``app`` (by Alembic, db_health_check.py, scripts that just
    want the configured Flask object) used to trigger ``db.create_all``,
    log pruning, and chain-context backfill at import time — making
    Alembic autogenerate compare against a DB the import side-effect just
    created, masking real diff. This factory moves the WRITE side
    effects out of import; production callers (the ``__main__`` block and
    any WSGI entry point) invoke it explicitly before serving traffic.

    Idempotent — safe to call multiple times. Read-only operations
    (connection probe, table count log) stay at module import time
    because they're cheap and surface boot issues early.
    """
    if skip_create_all is None:
        skip_create_all = os.environ.get("SKIP_DB_CREATE_ALL", "").lower() in ("1", "true", "yes")

    with application.app_context():
        try:
            if not skip_create_all:
                db.create_all()
            else:
                print(
                    "[DATABASE] Skipping db.create_all() because "
                    "SKIP_DB_CREATE_ALL is set",
                    file=sys.stderr,
                )
            from models import Protein, Interaction, InteractionClaim
            protein_count = Protein.query.count()
            interaction_count = Interaction.query.count()
            claim_count = InteractionClaim.query.count()
            print("[DATABASE] [OK]Tables initialized", file=sys.stderr)
            print(f"[DATABASE]   • Proteins table: {protein_count} entries", file=sys.stderr)
            print(f"[DATABASE]   • Interactions table: {interaction_count} entries", file=sys.stderr)
            print(f"[DATABASE]   • Claims table: {claim_count} entries", file=sys.stderr)
            print(f"[DATABASE] [OK]Database ready for sync", file=sys.stderr)
            print("="*60 + "\n", file=sys.stderr)
        except Exception as e:
            print(f"\n[ERROR][DATABASE] bootstrap_app failed: {e}", file=sys.stderr)
            print("   Falling back to file-based cache only.", file=sys.stderr)
            print("="*60 + "\n", file=sys.stderr)
            import traceback as _tb
            _tb.print_exc(file=sys.stderr)

    # Local fs side effects — cheap, idempotent.
    from services.state import CACHE_DIR, PRUNED_DIR
    CACHE_DIR.mkdir(exist_ok=True)
    PRUNED_DIR.mkdir(exist_ok=True)

    # Opt-in log rotation for the Logs/<protein>/<timestamp>/ tree.
    # Gated by ENABLE_LOG_ROTATION=true.
    from utils.observability import (
        maybe_prune_at_startup,
        maybe_backfill_chain_context_at_startup,
    )
    maybe_prune_at_startup()
    # Idempotent chain_context backfill. Opt-in via
    # ENABLE_BACKFILL_AT_STARTUP=true in .env; default OFF so first boot
    # after a fresh pull is never surprised by an implicit scan.
    with application.app_context():
        maybe_backfill_chain_context_at_startup()


# --- Register Blueprints ---
from routes import register_blueprints
register_blueprints(app)

# Auto-bootstrap when the app is imported by gunicorn / `python app.py`.
# Tools that need a non-bootstrapping import (Alembic, ad-hoc scripts,
# unit tests) set ``SKIP_APP_BOOTSTRAP=1`` before importing ``app`` —
# this lets them get the configured Flask + SQLAlchemy objects without
# triggering DB writes or log rotation.
if os.environ.get("SKIP_APP_BOOTSTRAP", "").lower() not in ("1", "true", "yes"):
    bootstrap_app(app)

# ---------------------------------------------------------------------------
# Backward-compatibility re-exports
# External scripts use:  from app import app, db
#                         from app import build_full_json_from_db
# These re-exports keep them working without changes.
# ---------------------------------------------------------------------------
from services.data_builder import build_full_json_from_db  # noqa: F401
from services.state import jobs, jobs_lock  # noqa: F401

# Backward compat aliases for existing test monkeypatches
import threading  # noqa: F401  — test_server_query_defaults patches app_module.threading
from services.chat_service import (  # noqa: F401
    build_compact_rich_context as _build_compact_rich_context,
    build_chat_system_prompt as _build_chat_system_prompt,
    call_chat_llm as _call_chat_llm,
)
from utils.gemini_runtime import get_client  # noqa: F401
from utils.pruner import PROTEIN_RE  # noqa: F401


if __name__ == '__main__':
    # Direct ``python app.py`` invocation — bootstrap is already run at
    # import time (above) unless SKIP_APP_BOOTSTRAP was set.
    app.run(host='127.0.0.1', port=5003, debug=True, threaded=True, use_reloader=True)
