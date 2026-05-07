"""Alembic runtime entry point.

Reads ``DATABASE_URL`` directly from the environment and pulls
SQLAlchemy metadata from ``models.db`` — no Flask app import. This
eliminates the import-time side effects (``db.create_all``, log
rotation, chain backfill) that used to run when Alembic loaded
``app.py``, which made ``alembic upgrade --autogenerate`` compare
against a DB the import had just created and produce empty diffs even
when the schema and models drifted.

If you need an app context for some reason (e.g., a custom migration
hook), wrap that hook only — don't import ``app`` at module level here.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the repo root importable regardless of where Alembic was invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Belt-and-suspenders: even if a hook in this env.py later imports
# something that pulls in app.py, the bootstrap won't run.
os.environ.setdefault("SKIP_APP_BOOTSTRAP", "1")

# Load .env so DATABASE_URL is populated when Alembic is invoked from
# the shell without an explicit env. Mirrors what app.py does.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(override=False)
except ImportError:
    pass

# Pull metadata from the live SQLAlchemy instance. ``models.py`` defines
# every ORM class against ``db = SQLAlchemy()``; importing the module
# registers all tables on ``db.metadata``. No app context needed for
# autogenerate — Alembic just inspects the metadata graph.
from models import db  # noqa: E402

config = context.config

# Honor alembic.ini logging config.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_database_url() -> str:
    """Pick DATABASE_URL with the same fallback ordering as app.py."""
    url = (os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL") or "").strip()
    # Defensive: handle accidental ``DATABASE_URL=postgresql://...`` paste.
    if url.startswith("DATABASE_URL="):
        url = url[len("DATABASE_URL="):]
    if url.startswith("postgres://"):
        # SQLAlchemy 1.4+ requires postgresql://
        url = url.replace("postgres://", "postgresql://", 1)
    if not url:
        # Fall back to whatever alembic.ini might have configured.
        url = config.get_main_option("sqlalchemy.url") or ""
    return url


config.set_main_option("sqlalchemy.url", _resolve_database_url())

# Metadata used for autogenerate.
target_metadata = db.metadata


def run_migrations_offline() -> None:
    """Generate SQL without opening a DB connection. Used for --sql mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Standard path: open a connection and apply migrations."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
