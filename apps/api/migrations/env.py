"""Alembic environment — async SQLAlchemy setup.

Key design decisions:
- URL comes from ``app.config.settings.database_url`` (never hardcoded).
- We force the ``postgresql+asyncpg://`` scheme so asyncpg is always used,
  regardless of whether the env var was set with or without the driver suffix.
- ``target_metadata = None`` until ORM models exist (Phase 1 has none).
  Update this once models land (#9+).
- We run in async mode using ``AsyncEngine`` + ``run_sync`` per Alembic docs.
- Supavisor transaction-mode pooler (Supabase port 6543) compatibility: the
  online-migration engine passes the same ``connect_args`` as the app engine
  (``app/db/session.py``) — see that module's docstring for the full
  rationale. Short version: Supavisor's transaction mode shares physical
  server connections across client sessions, and asyncpg's default
  sequential prepared-statement names collide across sessions sharing a
  backend (``DuplicatePreparedStatementError``). Disabling the dialect's
  statement cache and generating a UUID-based name per prepared statement
  makes every statement single-use and collision-free. This is harmless
  against a direct/local Postgres connection (e.g. docker-compose, port
  5432) — it only forgoes a cache and randomises statement names.
"""

from __future__ import annotations

import asyncio
import re
import sys
from logging.config import fileConfig
from pathlib import Path
from uuid import uuid4

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Ensure the ``app`` package (apps/api/) is importable when Alembic is
# invoked from any working directory (e.g. repo root or CI).
_here = Path(__file__).resolve().parent.parent  # apps/api/
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

# ---------------------------------------------------------------------------
# Alembic Config object — gives access to values in alembic.ini.
# ---------------------------------------------------------------------------
config = context.config

# Interpret the config file's logging section if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Target metadata — None until ORM models exist.
# ---------------------------------------------------------------------------
target_metadata = None


# ---------------------------------------------------------------------------
# Resolve the database URL from app settings.
# ---------------------------------------------------------------------------


def get_url() -> str:
    """Return the async DB URL.

    Strategy (in order):
    1. Try to import ``app.config.settings.database_url`` — the canonical
       source when the full app is available.
    2. Fall back to the ``DATABASE_URL`` environment variable directly —
       works in migration-only contexts where Supabase credentials are absent
       (e.g. CI Alembic step before app startup, local ``alembic upgrade head``
       against docker-compose Postgres).

    In both cases ``postgresql://`` is normalised to ``postgresql+asyncpg://``.
    """
    import os  # noqa: PLC0415

    url: str | None = None

    try:
        from app.config import settings  # noqa: PLC0415

        url = settings.database_url
    except Exception:  # noqa: BLE001
        # App settings unavailable (missing Supabase creds, etc.) — fall
        # back to the raw env var which is sufficient for migrations.
        url = os.environ.get("DATABASE_URL")

    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Export it before running alembic: "
            "export DATABASE_URL=postgresql+asyncpg://user:pass@host/db"
        )

    # Normalise: strip any existing driver suffix, then re-add asyncpg.
    url = re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)
    return url


# ---------------------------------------------------------------------------
# Supavisor / PgBouncer transaction-mode pooler compatibility.
#
# Why: Supabase's connection pooler (Supavisor, port 6543, "transaction"
# mode) shares a small set of physical server connections across many client
# sessions, handing the same logical connection a *different* physical
# backend between transactions. asyncpg's SQLAlchemy dialect by default
# caches prepared statements per DBAPI connection and names them
# sequentially, so two sessions sharing a backend can independently reach
# the same generated name, raising
# ``asyncpg.exceptions.DuplicatePreparedStatementError``. Setting
# ``prepared_statement_cache_size=0`` makes every ``PREPARE`` a single,
# throwaway statement, and ``prepared_statement_name_func`` gives each one a
# globally-unique (uuid4-based) name — collision-free even when Supavisor
# routes two sessions to the same backend. This is the recipe documented in
# the installed SQLAlchemy version's asyncpg dialect docstring
# (``sqlalchemy/dialects/postgresql/asyncpg.py``, "Prepared Statement Cache"
# and "Prepared Statement Name with PGBouncer" sections — the SQLAlchemy
# dialect's own kwarg is ``prepared_statement_cache_size``, not
# ``statement_cache_size``).
#
# A THIRD, separate knob is also required: plain ``statement_cache_size=0``
# (no ``prepared_`` prefix) is asyncpg's OWN driver-level cache (forwarded
# verbatim to ``asyncpg.connect()``), distinct from the SQLAlchemy dialect's
# cache above. Confirmed necessary by live testing against the real pooler:
# with only the two SQLAlchemy-level knobs, ``pool_pre_ping``-style pings
# (``connection.fetchrow(";")``, asyncpg's raw convenience method) still
# collided, since they bypass the dialect's ``_prepare()`` and use asyncpg's
# own cache/auto-naming. This module doesn't set ``pool_pre_ping`` itself,
# but disabling this here too keeps the two engines' pooler-compat config
# identical and covers any asyncpg-internal calls Alembic itself may trigger.
#
# Duplicated here (rather than imported from ``app.db.session``) so Alembic
# keeps working in migration-only contexts where ``app.config.settings``
# cannot be constructed (see ``get_url()`` above) — this module must not
# require the full app settings to import.
#
# Harmless against a direct/local Postgres connection (e.g. docker-compose,
# port 5432): it only disables opportunistic performance caches and
# randomises statement names; correctness is unaffected either way.
# ---------------------------------------------------------------------------


def _asyncpg_prepared_statement_name() -> str:
    """Generate a globally-unique prepared-statement name for every PREPARE."""
    return f"__asyncpg_{uuid4()}__"


_ASYNCPG_POOLER_CONNECT_ARGS: dict[str, object] = {
    "prepared_statement_cache_size": 0,
    "prepared_statement_name_func": _asyncpg_prepared_statement_name,
    "statement_cache_size": 0,
}


# ---------------------------------------------------------------------------
# Offline migration (generate SQL without a live DB connection).
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection needed)."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migration (async engine, real DB connection).
# ---------------------------------------------------------------------------


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside a connection."""
    connectable = create_async_engine(
        get_url(),
        echo=False,
        # Supavisor/PgBouncer transaction-mode pooler compatibility — see
        # the module docstring and the comment above
        # ``_ASYNCPG_POOLER_CONNECT_ARGS``.
        connect_args=_ASYNCPG_POOLER_CONNECT_ARGS,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def do_run_migrations(connection: object) -> None:
    """Synchronous callback run inside the async connection's run_sync."""
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Entry point for online (connected) migrations."""
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
