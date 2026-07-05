"""Async SQLAlchemy engines, session factories, and the FastAPI dependency.

Two engines (#22 — app_role / RLS role separation)
----------------------------------------------------
- ``engine`` / ``AsyncSessionFactory`` — the ADMIN/service engine, always
  built from ``settings.database_url``. This backs ``routers/health.py``'s
  ``/readyz`` (imports ``engine`` directly) and any future service job that
  must run outside RLS entirely (migrations build their own copy of this
  same engine in ``migrations/env.py``, rather than importing this module,
  to keep Alembic usable in contexts where the rest of ``app.config``
  can't be constructed).
- ``get_session`` (the FastAPI request-path dependency used by every
  router) uses a SEPARATE ``request_engine`` / ``RequestSessionFactory``,
  built from ``settings.app_database_url`` **when set**. That second
  connection string is meant to authenticate as ``app_role`` — the
  ``NOLOGIN`` Postgres role migration 0005 creates and RLS policies key
  off (schema-v1.md v1.2 amendments). When ``app_database_url`` is unset
  (local dev, CI, and production until the one-time operator step below is
  done), request sessions silently fall back to the admin engine, and a
  one-time WARNING is logged at import time noting that RLS is not yet
  enforced by role separation — no secrets in that log line, just a flag
  that a human hasn't flipped the switch yet. "One-time" here means
  "once per process": this module is a singleton imported exactly once,
  so the module-level ``log.warning`` call below only ever fires once per
  running process, not once per request.

  Production flip (operator step — do this BEFORE any tenant data exists
  in the target database; flipping role separation on after real landlord
  data is already flowing through the admin-engine-as-request-engine
  fallback is a migration project of its own, not a config change):
    1. Migration 0005 already created ``app_role`` with ``NOLOGIN`` — no
       password, ever, in a migration.
    2. An operator runs, once, directly against the live database:
       ``ALTER ROLE app_role LOGIN PASSWORD '<a freshly generated secret>';``
    3. ``fly secrets set APP_DATABASE_URL=postgresql+asyncpg://``
       ``app_role.<project-ref>:<password>@<same pooler host>:6543/postgres``
       (same Supavisor pooler host as ``DATABASE_URL``, different
       user/password).
    4. Redeploy. Request sessions now connect as ``app_role`` (RLS-scoped);
       the admin engine keeps backing migrations/health checks only.

  In ``production``, ``app.config.Settings`` refuses to boot at all if
  ``APP_DATABASE_URL`` is unset (see ``config.py``'s
  ``_require_app_database_url_in_production``) — the fallback above is a
  safe default only up until real tenant data exists.

``get_admin_session`` — the pre-identity / service-path escape hatch
---------------------------------------------------------------------
Some writes structurally CANNOT go through an RLS-scoped (``app_role``)
session, because there is no ``landlord_id``/GUC to scope them by yet, or
because losing the write outright would be catastrophic rather than
merely wrong:

- ``GET /v1/me``'s provisioning upsert (``routers/me.py``) — the first
  call for a brand-new auth user creates the very ``landlords`` row that
  ``require_landlord`` depends on to set the GUC in the first place. Empi
  rically reproduced (#22 safety review): the INSERT and the
  ``ON CONFLICT DO UPDATE`` upsert are BOTH rejected by ``landlords``'
  ``WITH CHECK`` under ``app_role`` — a freshly-``gen_random_uuid()``'d
  ``id`` can never equal a GUC value set before that id exists. There is
  no way to "set the GUC first" for a row that doesn't exist yet, so this
  path MUST run on the admin engine, unscoped, by design — not a bug, not
  a workaround.
- The auth.users lifecycle trigger (migration 0004, #15) — a
  ``SECURITY DEFINER`` function that runs as whichever role owns it
  (the migrating/admin role), entirely outside any request's GUC context.
  Already unaffected by this dependency (triggers don't call application
  code), noted here only for completeness.
- Twilio webhook ingestion (#40, forward note) — persisting an inbound
  message MUST use ``get_admin_session``, never an RLS-scoped session: if
  landlord/property resolution fails, races, or the GUC is set to the
  wrong value, an RLS-scoped session would silently reject or misfile the
  INSERT instead of storing it — exactly the catastrophic direction
  never-break rule #1 (the emergency line is never gated) forbids. #40's
  implementation must wire this up; this note exists so nobody
  accidentally reaches for ``get_session``/``require_landlord`` there
  instead.

Landlord-SCOPED endpoints (#53 onward) MUST use ``require_landlord``
(``app/deps.py``) + the ordinary request-path ``session`` it yields —
``get_admin_session`` is the deliberately-unscoped exception, not the
default. Reach for it only for the specific pre-identity/service cases
above; scoping it out to "anywhere it's convenient" would silently
re-open the exact RLS gap #22 closes. A machine-enforced allowlist test
(``tests/test_migrations_0005.py::test_get_admin_session_referenced_only_
by_allowlisted_files``, #22 safety review item 12) greps ``app/`` for
every file that references ``get_admin_session`` and red-fails if that set
ever grows without a deliberate, reviewed update — extend it explicitly
when #40's webhook ingestion lands (a legitimate admin-engine caller).

``verify_request_engine_role_separation`` — startup self-check (#22
safety review item 13b)
------------------------------------------------------------------------
Setting ``APP_DATABASE_URL`` is necessary but not SUFFICIENT proof that
RLS role separation is actually in effect — a typo'd URL that happens to
resolve to the SAME role as ``DATABASE_URL``, or to a role that (like
``postgres``/``service_role`` on live Supabase) holds ``BYPASSRLS``, would
satisfy every truthiness/presence check in this module while silently
providing ZERO isolation: every policy would be in place and every query
would still see everything, and nobody would notice until a real
cross-landlord data leak. This function is called once at FastAPI startup
(``app/main.py``'s lifespan) to catch exactly that: when
``app_database_url`` is set, it queries the REQUEST engine's OWN
``current_user`` and ``rolbypassrls`` and refuses to let the app start
serving traffic if either check fails. When ``app_database_url`` is
unset, this is a no-op — the existing fallback WARNING above already
covers that (documented, safe-for-now) state.

Design decisions
----------------
- Module-level singleton engines: one (or two) per process, created once at
  import. Never create engines per-request — that defeats connection
  pooling entirely.

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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
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
# Engine factory — shared pool/pooler-compat config for both engines below.
# ---------------------------------------------------------------------------


def _build_engine(url: str) -> AsyncEngine:
    """Create a pooled async engine with the standard Fly/Supavisor settings.

    Both the admin engine and the (optional) request engine share this
    configuration — see the module docstring for the pool-sizing and
    pooler-compat rationale. Factored out rather than duplicated so the two
    engines can never silently drift apart on these settings.
    """
    return create_async_engine(
        _asyncpg_url(url),
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
# Admin/service engine — one per process, not per-request. Always built from
# DATABASE_URL. Backs migrations (env.py builds its own copy), /readyz, and
# any future service job that must run outside RLS entirely.
# ---------------------------------------------------------------------------

engine = _build_engine(settings.database_url)

AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ---------------------------------------------------------------------------
# Request-path engine — app_role once APP_DATABASE_URL is set (#22 operator
# step, see module docstring); falls back to the admin engine otherwise
# (local dev, CI, and production before that one-time step runs). The
# fallback WARNING below fires at most once per process (module-level code,
# executed exactly once at import) and never logs the URL, username, or
# password — see never-break rule #5.
# ---------------------------------------------------------------------------

request_engine: AsyncEngine
RequestSessionFactory: async_sessionmaker[AsyncSession]

if settings.app_database_url:
    request_engine = _build_engine(settings.app_database_url)
    RequestSessionFactory = async_sessionmaker(
        request_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
else:
    request_engine = engine
    RequestSessionFactory = AsyncSessionFactory
    log.warning(
        "rls_not_enforced_by_role_separation",
        detail=(
            "APP_DATABASE_URL is unset — request-path sessions are using "
            "the admin engine, not a dedicated app_role connection"
        ),
    )

# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a per-request ``AsyncSession`` from the request-path engine.

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
    async with RequestSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


class RoleSeparationVerificationError(RuntimeError):
    """Raised by ``verify_request_engine_role_separation`` when
    ``APP_DATABASE_URL`` is set but does NOT actually provide RLS role
    separation (#22 safety review item 13b) — refuses to let the app start
    serving traffic. Never constructed with the connection string,
    username@host, or password; only role names (see that function's own
    docstring for why those are safe to include).
    """


async def get_admin_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a per-request ``AsyncSession`` from the ADMIN engine — always,
    regardless of ``APP_DATABASE_URL``. Bypasses RLS/``app_role`` entirely.

    See the module docstring ("``get_admin_session`` — the pre-identity /
    service-path escape hatch") for WHEN this is the correct dependency to
    use instead of ``get_session``/``require_landlord``. Today's only
    caller is ``GET /v1/me`` (``routers/me.py``) — its provisioning upsert
    cannot be scoped by a GUC for a ``landlords`` row that doesn't exist
    yet.

    Deliberately a near-duplicate of ``get_session``'s body rather than a
    shared helper parameterized by session factory: keeping the two
    functions textually distinct (rather than one call site choosing a
    factory) makes ``grep get_admin_session`` in ``app/`` an honest,
    complete list of every place that bypasses RLS on purpose.

    Lifecycle: identical to ``get_session`` — commits on clean exit, rolls
    back on any exception, always closes the session.
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


# ---------------------------------------------------------------------------
# Startup self-check — presence of APP_DATABASE_URL is not proof it works.
# ---------------------------------------------------------------------------


async def verify_request_engine_role_separation() -> None:
    """Startup self-check (#22 safety review item 13b): when
    ``APP_DATABASE_URL`` is set, actually PROVE the request engine is
    role-separated from the admin engine — don't just trust that setting
    it was enough.

    Runs one query on the REQUEST engine:
    ``SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname =
    current_user``. Refuses to let the app start serving traffic
    (raises ``RoleSeparationVerificationError``) if EITHER:

    - ``rolbypassrls`` is true for that role — RLS would be silently
      skipped for every request, exactly as if role separation had never
      been configured (this is precisely the ``postgres``/``service_role``
      situation on live Supabase — see migration 0005's "LIVE ROLE FACTS"
      — so pointing ``APP_DATABASE_URL`` at either of those by mistake
      must be caught here, not discovered later via a data leak); or
    - ``current_user`` is the SAME role the admin engine connects as — a
      copy-paste error (``APP_DATABASE_URL`` accidentally set to the same
      value as ``DATABASE_URL``) that would make ``request_engine`` and
      ``engine`` behaviorally identical.

    A clear structured log line precedes the raise. No secrets in it: role
    names (``current_user`` values) are Postgres identifiers, not
    credentials — never the connection string, host, or password, which
    never reach this function's logging or exception message.

    When ``app_database_url`` is unset, this is a deliberate no-op — the
    module-level fallback WARNING (logged once at import time, above)
    already covers that documented, safe-for-now state, and there is no
    dedicated request-path connection to even check yet.

    Called once, at FastAPI startup (``app/main.py``'s lifespan) — an
    exception raised here aborts startup entirely, before any traffic is
    served, which is the correct failure mode for "RLS role separation
    that was configured wrong is worse than not configuring it at all
    (silent, undetected)".
    """
    if not settings.app_database_url:
        return

    async with request_engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    request_user, request_bypassrls = row[0], bool(row[1])

    admin_user = engine.url.username

    if request_bypassrls or request_user == admin_user:
        log.error(
            "rls_role_separation_verification_failed",
            request_user=request_user,
            admin_user=admin_user,
            request_bypassrls=request_bypassrls,
        )
        raise RoleSeparationVerificationError(
            "The request-path database role is not actually separated from "
            "the admin role -- refusing to start. Either APP_DATABASE_URL "
            "points at a role with BYPASSRLS set, or it points at the same "
            "role as DATABASE_URL. See app/db/session.py's module "
            "docstring for the operator step."
        )
