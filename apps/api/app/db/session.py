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

- Supavisor transaction-mode pooler (Supabase's port 6543) compatibility:
  see ``_ASYNCPG_POOLER_CONNECT_ARGS`` below — required for both the app
  engine (here) and Alembic's engine (``migrations/env.py``) whenever
  ``DATABASE_URL`` points at the pooler rather than a direct connection.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

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
# Supavisor / PgBouncer transaction-mode pooler compatibility.
#
# Why: Supabase's connection pooler (Supavisor, port 6543, "transaction"
# mode) multiplexes many client sessions across a small set of physical
# server connections and can hand a *different* physical backend to the
# same logical connection between transactions. asyncpg's SQLAlchemy dialect
# by default (a) caches prepared statements per DBAPI connection and (b)
# names them sequentially ("__asyncpg_stmt_1__", ...). Combined with pooling,
# two different client sessions can independently reach "stmt_1" on the same
# shared backend, producing
# ``asyncpg.exceptions.DuplicatePreparedStatementError: prepared statement
# "__asyncpg_stmt_1__" already exists``. Disabling the dialect's statement
# cache (``prepared_statement_cache_size=0``) makes every ``PREPARE`` a
# single, throwaway operation, and generating a globally-unique name per
# prepare (via ``prepared_statement_name_func``) means two sessions can never
# collide even when Supavisor happens to route them to the same backend.
# This is the recipe documented in the installed SQLAlchemy version's own
# asyncpg dialect docstring (``sqlalchemy/dialects/postgresql/asyncpg.py``,
# sections "Prepared Statement Cache" and "Prepared Statement Name with
# PGBouncer" — note the dialect's actual kwarg is
# ``prepared_statement_cache_size``, not ``statement_cache_size``):
#
#   engine = create_async_engine(
#       "postgresql+asyncpg://user:pass@somepgbouncer/dbname",
#       connect_args={
#           "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
#       },
#   )
#
# (cache section) ``prepared_statement_cache_size=0`` disables the cache
# outright — the pgbouncer-recipe name_func alone is not enough to protect a
# pooled (non-``NullPool``) engine like ours, whose asyncpg connections are
# checked out and reused across many separate transactions/backends.
#
# A THIRD, separate knob is also required: plain ``statement_cache_size=0``
# (no ``prepared_``-prefix — this one is asyncpg's OWN driver-level cache,
# not the SQLAlchemy dialect's; it is forwarded verbatim to
# ``asyncpg.connect()`` since the dialect only special-cases the two
# ``prepared_statement_*`` names above). This was confirmed necessary by live
# testing against the real pooler: with only the two SQLAlchemy-level knobs
# set, ``pool_pre_ping``'s ping (``AsyncAdapt_asyncpg_connection.ping()`` ->
# ``self._connection.fetchrow(";")``) still collided, because it calls
# asyncpg's raw ``fetchrow()`` convenience method directly — bypassing the
# dialect's ``_prepare()`` entirely — which uses asyncpg's own internal
# statement cache with asyncpg's own sequential auto-naming
# (``__asyncpg_stmt_<n>__``, distinct from our uuid4-based names). Disabling
# it too closes that gap. This is exactly the knob asyncpg's own error hint
# recommends: "you can set statement_cache_size to 0 when creating the
# asyncpg connection object".
#
# Harmless against a direct/local Postgres connection (e.g. docker-compose
# on port 5432): it only disables opportunistic performance caches and
# switches statement names from sequential to random; correctness is
# unaffected either way.
# ---------------------------------------------------------------------------


def _asyncpg_prepared_statement_name() -> str:
    """Generate a globally-unique prepared-statement name for every PREPARE.

    Called once per statement (cache disabled, see above) so two pooled
    sessions can never collide on the same generated name.
    """
    return f"__asyncpg_{uuid4()}__"


_ASYNCPG_POOLER_CONNECT_ARGS: dict[str, Any] = {
    # SQLAlchemy asyncpg-dialect knobs (see ``_prepare()`` in
    # sqlalchemy/dialects/postgresql/asyncpg.py).
    "prepared_statement_cache_size": 0,
    "prepared_statement_name_func": _asyncpg_prepared_statement_name,
    # Plain asyncpg driver knob (forwarded to ``asyncpg.connect()``) —
    # disables asyncpg's OWN cache used by its raw convenience methods
    # (``fetchrow``/``fetch``/``fetchval``/``execute``), e.g. as used by
    # ``pool_pre_ping``'s connection ping. See comment block above.
    "statement_cache_size": 0,
}

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
    # Supavisor/PgBouncer transaction-mode pooler compatibility — see the
    # module docstring and the comment above ``_ASYNCPG_POOLER_CONNECT_ARGS``.
    connect_args=_ASYNCPG_POOLER_CONNECT_ARGS,
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
