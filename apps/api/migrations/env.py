"""Alembic environment — async SQLAlchemy setup.

Key design decisions:
- URL comes from ``app.config.settings.database_url`` (never hardcoded).
- We force the ``postgresql+asyncpg://`` scheme so asyncpg is always used,
  regardless of whether the env var was set with or without the driver suffix.
- ``target_metadata = None`` until ORM models exist (Phase 1 has none).
  Update this once models land (#9+).
- We run in async mode using ``AsyncEngine`` + ``run_sync`` per Alembic docs.
"""

from __future__ import annotations

import asyncio
import re
import sys
from logging.config import fileConfig
from pathlib import Path

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
    connectable = create_async_engine(get_url(), echo=False)

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
