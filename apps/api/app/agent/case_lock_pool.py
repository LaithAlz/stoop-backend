"""Dedicated connection pool for the per-case advisory lock (#186 item 1).

The problem this module fixes — pool starvation, not just latency
------------------------------------------------------------------------
``app/agent/graph.py``'s :func:`app.agent.graph._case_lock` holds a Postgres
``pg_advisory_xact_lock`` for the FULL DURATION of an LLM-bound case-graph
span (the whole ``case_graph.ainvoke(...)`` call, easily several seconds —
classification alone has a 20s budget, see
``docs/02-product/emergency-prefilter.md``). That lock is
TRANSACTION-scoped by design (``pg_advisory_xact_lock`` releases with the
holding transaction — the deliberately-correct choice under Supavisor
transaction pooling, see ``app/agent/graph.py``'s "Per-case serialization"
docstring), which means the connection/session holding it is checked out of
whatever pool it came from for that entire span too.

Before this fix, that connection came from ``app/db/session.py``'s ADMIN
engine (``pool_size=5, max_overflow=5`` — 10 total) — the SAME pool every
node INSIDE that span (``identify_property``, ``load_context``,
``classify_severity``, ``draft_response``, ...) also checks out its own,
separate, briefly-held connection from to do its own work. At ten
concurrent, DISTINCT-case ``run_graph`` calls, all ten admin-pool
connections are held by locks with none left for the nodes running inside
those very locks to check out — a genuine DEADLOCK, not merely degraded
throughput (the #186 issue's own PR #187 review sharpened this from
"latency" to "hard deadlock at a concrete threshold").

The fix: a SEPARATE, dedicated pool for lock-holding connections only —
mirrors ``app/agent/checkpointer.py``'s own "why a separate pool" rationale
(that module's dedicated psycopg pool exists so checkpoint I/O never
competes with the admin engine either), adapted to this pool's actual
driver: the lock is taken via plain SQL (``pg_advisory_xact_lock``) over
this app's ordinary SQLAlchemy/asyncpg stack, not psycopg, so a small
dedicated ``AsyncEngine`` (not a psycopg ``AsyncConnectionPool``) is the
natural shape here. With lock-holding and node-checkout connections drawn
from two independent pools, ten concurrent case runs can now genuinely
complete: the admin pool stays free for the (brief) node checkouts inside
each span regardless of how many case locks are held concurrently.

Pool sizing — deliberately kept in the SAME connection-budget class as the
admin engine, not invented larger
------------------------------------------------------------------------
``app/db/session.py``'s module docstring sizes the admin pool for a Fly
1-CPU/1-GB machine against Supabase's free-tier connection cap (~60 total):
"5 per process is safe across multiple machines/processes". Doubling that
budget with an unboundedly large second pool here would quietly undermine
that reasoning. Instead, :data:`_LOCK_POOL_SIZE` / :data:`_LOCK_MAX_OVERFLOW`
mirror the admin engine's OWN shape (5 base + 5 overflow = 10 total) — the
same concurrency ceiling this codebase already reasoned about and pinned
in the admin engine's own docstring, just now serving ONLY lock-holding
connections instead of being shared with node checkouts. This is not a
capacity increase; it is the SAME 10-concurrent-case budget, decoupled from
the pool the in-span nodes also need.

:data:`_LOCK_POOL_TIMEOUT` is deliberately SHORTER than SQLAlchemy's default
(30s, still used by the admin engine) — the #186 issue's own prescribed
safe failure direction: once genuine demand exceeds this pool's capacity, a
checkout should fail FAST (``sqlalchemy.exc.TimeoutError``, propagating up
through :func:`app.agent.graph.run_graph` /
:func:`app.agent.graph.resume_case_thread` to their existing callers —
``app/agent/graph_entry.py::enqueue_classification`` already wraps
``run_graph`` in a try/except that pages Sentry and attempts a last-resort
``needs_eyes`` notification) rather than blocking a tenant-facing request
for up to 30s before that same safe fallback fires. The Tier-0 prefilter
still runs pre-graph regardless (``app/routers/webhooks/twilio.py``), so a
saturated lock pool never gates the emergency path.

Why NOT reuse ``app/db/session.py``'s ``_build_engine``/``engine`` directly
------------------------------------------------------------------------
``_build_engine`` hardcodes the admin engine's OWN pool_size/max_overflow
and has no ``pool_timeout`` parameter — reusing it here would mean either
(a) sharing the very pool this fix needs to separate from, or (b) adding a
timeout parameter to a shared helper whose only other caller (the request
engine) has no reason to want a shorter timeout. A small, dedicated
``create_async_engine(...)`` call here, reusing only the pooler-COMPATIBILITY
knobs that must never drift between engines (see below), keeps the two
engines' pool-sizing concerns independent while their Supavisor-compatibility
behavior stays identical by construction (the SAME imported constant, not a
duplicated copy that could silently diverge).

Reuses ``app.db.session._ASYNCPG_POOLER_CONNECT_ARGS`` directly (not a
re-implemented copy) — unlike ``app/agent/checkpointer.py``'s
``_psycopg_dsn`` (which mirrors ``app/db/session.py``'s URL normalizer in
the OPPOSITE direction, for a different driver, and so is written locally
on purpose), this pool needs the IDENTICAL three asyncpg/Supavisor
compatibility knobs the admin/request engines already use — importing the
one shared constant means the three keys can never accidentally drift
between this engine and the other two. The URL-scheme normalization
(``postgresql://`` -> ``postgresql+asyncpg://``) IS reimplemented locally
below, matching this repo's established convention for that specific
one-line regex (``app/db/session.py``, ``migrations/env.py``, and this
module now independently normalize the same way, by design, per
``app/db/session.py``'s own docstring: "matching the same approach used in
migrations/env.py").

Test-suite reset (cross-event-loop pool reuse hazard)
------------------------------------------------------------------------
This module's engine is a process-lifetime module-global, exactly like
``app/db/session.py``'s ``engine``. ``tests/conftest.py``'s
``_reset_case_lock_pool`` autouse fixture disposes (``close=False``) this
engine's connection pool before every test, for the identical reason
``_reset_admin_engine_pool`` does for the admin/request engines: a pooled
asyncpg connection binds to the event loop that created it, and this repo
runs one event loop per test (``asyncio_default_fixture_loop_scope=
function``) — a connection surviving across tests would raise ``RuntimeError:
... attached to a different loop`` the instant a later test's loop tried to
use it.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.db.session import _ASYNCPG_POOLER_CONNECT_ARGS

# ---------------------------------------------------------------------------
# Pool sizing — see module docstring "Pool sizing" for the full rationale.
# ---------------------------------------------------------------------------

_LOCK_POOL_SIZE = 5
"""Mirrors ``app/db/session.py``'s admin-engine ``pool_size`` — same
connection-budget class, a SEPARATE pool, not a larger one."""

_LOCK_MAX_OVERFLOW = 5
"""Mirrors the admin engine's ``max_overflow`` — burst to 10 total, same as
today's admin-pool ceiling, now serving ONLY lock-holding connections."""

_LOCK_POOL_TIMEOUT = 10
"""Seconds. Deliberately shorter than SQLAlchemy's 30s default (still used
by the admin engine) — see module docstring "Pool sizing" for the
fail-fast-into-the-existing-Sentry/needs_eyes-safety-net rationale."""


def _asyncpg_url(url: str) -> str:
    """Force ``postgresql+asyncpg://`` regardless of what was configured.

    Deliberately re-implemented here (not imported) — see module docstring
    "Why NOT reuse app/db/session.py's _build_engine/engine directly": this
    repo's established convention (``app/db/session.py``, ``migrations/
    env.py``) is to keep this specific one-line regex local to each module
    that needs it, while the pooler-compatibility ``connect_args`` constant
    below IS imported directly rather than duplicated.
    """
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


# ---------------------------------------------------------------------------
# The dedicated engine — module-global, process-lifetime, built once at
# import time (SQLAlchemy ``AsyncEngine`` objects do not themselves bind to
# an event loop at construction; only the physical connections their pool
# opens do — the same reasoning ``app/db/session.py``'s module-level
# ``engine`` relies on).
# ---------------------------------------------------------------------------

case_lock_engine: AsyncEngine = create_async_engine(
    _asyncpg_url(settings.database_url),
    pool_size=_LOCK_POOL_SIZE,
    max_overflow=_LOCK_MAX_OVERFLOW,
    pool_timeout=_LOCK_POOL_TIMEOUT,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
    # Same Supavisor/PgBouncer transaction-mode pooler compatibility knobs
    # as app/db/session.py's admin/request engines — imported directly, see
    # module docstring, so the three keys can never silently drift.
    connect_args=_ASYNCPG_POOLER_CONNECT_ARGS,
)

CaseLockSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    case_lock_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_case_lock_session() -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` from the dedicated case-lock pool.

    Lifecycle mirrors ``app/db/session.py``'s admin-session dependency
    exactly (commit on clean exit, rollback on any exception, always close)
    — the ``pg_advisory_xact_lock`` this session's caller (``app.agent.graph.
    _case_lock``) takes releases automatically with whichever of those two
    outcomes ends the transaction. This module never imports or calls that
    dependency itself — see ``tests/test_migrations_0005.py``'s
    machine-enforced admin-session-caller allowlist, which this module is
    deliberately absent from, and its own docstring, "Why NOT reuse
    app/db/session.py's _build_engine/engine directly": this pool's
    connections bypass RLS the SAME way the admin engine's do (both are
    always built from ``settings.database_url``), but through its OWN
    dedicated engine/session factory, never through the admin engine's
    request-scoped dependency function.
    """
    async with CaseLockSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


__all__: list[str] = [
    "CaseLockSessionFactory",
    "case_lock_engine",
    "get_case_lock_session",
]
