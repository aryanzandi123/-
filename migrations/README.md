# Migrations — Alembic

ProPaths uses [Alembic](https://alembic.sqlalchemy.org/) for versioned
schema migrations. The legacy ad-hoc ``scripts/migrate_*.py`` one-shots
still live in the repo for historical reference, but new migrations go
here.

## Quick start

```bash
# Generate a new migration by diffing models vs the live DB.
alembic revision --autogenerate -m "add is_synthetic to interaction_claims"

# Apply all pending upgrades.
alembic upgrade head

# Roll back one revision.
alembic downgrade -1

# Show current schema version in the DB.
alembic current

# Preview the SQL that ``upgrade`` would emit without running it.
alembic upgrade head --sql
```

## Writing migrations

- Every revision file goes under ``migrations/versions/``.
- Prefer ``op.create_index``, ``op.add_column``, etc. over raw SQL where
  possible — makes downgrades reliable.
- When a migration is data-only (no schema change), you can still hook
  it into Alembic: write the data change in ``upgrade()`` and either
  a no-op or an inverse in ``downgrade()``.
- For expensive backfills, batch the writes and commit per batch —
  Alembic runs inside a single transaction by default, which is fine
  for small schema changes but can bloat the WAL on big data migrations.

## Relationship to the legacy ad-hoc scripts

The ad-hoc migration scripts in ``scripts/`` (``migrate_add_chain_table.py``,
``migrate_schema_tightening.py``, etc.) predate Alembic and are kept for
one reason: they've already been applied in production and we don't want
to retroactively version them. For any NEW schema work, write a proper
Alembic revision here.

To register the current live DB as the "baseline" (so Alembic knows
everything existing is already applied), run once per environment:

```bash
alembic stamp head
```

This writes the current revision ID into ``alembic_version`` without
actually running any migration.
