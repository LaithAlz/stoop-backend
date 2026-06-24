"""Async SQLAlchemy engine, session factory, and FastAPI dependency.

Design decisions
----------------
- Module-level singleton engine: one per process, created once at import.
  Never create engines per-request — that defeats connection pooling entirely.

- asyncpg driver: the URL is normalised defensively from ``postgresql://``
  (or ``postgresql+anything://``) to ``postgresql+asyncpg://``, matching the
  same approach used in migrations/env.py.

- Pool sizing for a Fly 1-CPU/1-GB machine:
    pool_size=5   — modest; leaves room for the connection to Supabase
                    (free tier cap ~60, 5 per process is safe across multiple
                    machines / processes).
    max_overflow=5 — allows burst up to 10 total; overflows are closed
                     promptly when the burst subsides.
    pool_pre_ping=True   — adds ~1 ms per checkout to detect stale sockets;
                           essential with Supabase's idle-timeout behaviour.
    pool_recycle=300     — recycle connections every 5 minutes; shorter than
                           Supabase's idle-timeout window.

- ``expire_on_commit=False``: accessing ORM attributes after commit must not
  trigger a lazy re-fetch on a closed connection.  Safe because every handler
  returns/discards objects before the session closes.

- ``get_session`` commits on clean exit, rolls back on any exception, and
  always closes the session in the ``finally`` block — prevents session leaks.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# URL normalisation (same regex as migrations/env.py for consistency)
# ---------------------------------------------------------------------------


def _asyncpg_url(url: str) -> str:
    """Force ``postgresql+asyncpg://`` scheme regardless of what was set."""
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


# ---------------------------------------------------------------------------
# Module-level engine — one per process, not per-request.
# ---------------------------------------------------------------------------

engine = create_async_engine(
    _asyncpg_url(settings.database_url),
    # Pool sizing — see module docstring for rationale.
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    pool_recycle=300,
    # Keep echo off in all environments; queries are visible in structlog
    # DEBUG logs when needed, but echo=True is too noisy for production.
    echo=False,
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a per-request ``AsyncSession``.

    Usage::

        @router.get("/example")
        async def example(session: Annotated[AsyncSession, Depends(get_session)]):
            result = await session.execute(text("SELECT 1"))
            ...

    Lifecycle:
    - Opens a new session from the pool.
    - On clean exit: commits the transaction.
    - On any exception: rolls back the transaction (re-raises).
    - Always: closes the session (returns the connection to the pool).
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
