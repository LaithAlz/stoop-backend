"""LangGraph checkpoint persistence (#24).

Checkpoint tables (``checkpoints``, ``checkpoint_blobs``,
``checkpoint_writes``, ``checkpoint_migrations``) are owned entirely by
``langgraph-checkpoint-postgres``'s ``AsyncPostgresSaver`` — every one of
its own SQL statements (``langgraph/checkpoint/postgres/base.py``'s
``MIGRATIONS`` list, in the installed package) references those four names
UNQUALIFIED, with no schema-prefix hook of its own. The only way to steer
those unqualified names into a schema other than whatever the connecting
role's default resolves to is to pin ``search_path`` on the connection
itself, before any statement ever runs on it — that is what this module
exists to do, per schema-v1.md's v1.4 amendments and migration 0007 (which
creates the ``langgraph`` schema and revokes every grant on it).

Why a SEPARATE, dedicated psycopg3 connection pool (not any of
``app/db/session.py``'s SQLAlchemy engines or dependencies)
------------------------------------------------------------------------
- ``AsyncPostgresSaver`` is built on ``psycopg`` (3.x) + ``psycopg_pool``,
  a different driver/wire-protocol implementation entirely from this app's
  SQLAlchemy+asyncpg engines. It cannot be backed by an ``AsyncSession``.
- This pool is built directly from ``settings.database_url`` — the SAME
  admin/service-role connection string ``app/db/session.py``'s ``engine``
  uses — never ``settings.app_database_url``/``app_role``. Migration 0007
  grants ``app_role`` (and, where they exist, ``anon``/``authenticated``)
  **no privilege at all** on the ``langgraph`` schema, so there would be
  nothing for an ``app_role``-authenticated connection to do here even if
  one were used.
- Deliberately NOT routed through ``app/db/session.py``'s admin-session
  SQLAlchemy dependency (the pre-identity/service-path escape hatch that
  module's own docstring describes) — that dependency yields a SQLAlchemy
  ``AsyncSession`` (asyncpg driver), which ``AsyncPostgresSaver`` cannot
  consume. Building its own pool here means that dependency's
  machine-enforced allowlist (``tests/test_migrations_0005.py``) needs NO
  update — this module never imports or calls it, by construction, not by
  omission.

How ``search_path`` is pinned (psycopg's documented pattern)
------------------------------------------------------------------------
Two complementary mechanisms, both applied to EVERY physical connection
this pool ever opens (including ones opened later to replace a recycled or
dead one — psycopg_pool's ``kwargs``/``configure`` are pool-level settings,
not one-shot):

1. ``kwargs={"options": "-c search_path=langgraph", ...}`` — forwarded
   verbatim to ``psycopg.AsyncConnection.connect()`` as a libpq startup
   parameter (the standard psycopg recipe for a per-connection
   ``search_path``: see psycopg's own docs, "Search path" section under
   connection parameters). Applied once, in the connection's initial
   startup packet, before this process's very first statement.
2. ``configure=`` a callback that issues an explicit
   ``SET search_path TO langgraph`` the moment the pool establishes a new
   physical connection — belt-and-braces in case a pooler/proxy between
   this process and Postgres does not faithfully forward an arbitrary
   ``options`` startup parameter (see "Known caveat" below).

Both need ``autocommit=True`` (also set via ``kwargs``, mirroring the
installed library's OWN ``AsyncPostgresSaver.from_conn_string()``
classmethod, which passes ``autocommit=True`` to a raw connection for
exactly this reason) — **required**, not cosmetic: the library's migration
list includes ``CREATE INDEX CONCURRENTLY`` statements, which Postgres
categorically refuses to run inside a transaction block
(``ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction
block``). Without ``autocommit=True``, ``setup()`` would fail the first
time it reaches that migration step, since psycopg (like any DB-API-style
driver) opens an implicit transaction on the first statement of a
connection unless autocommit is enabled.

Known caveat — Supavisor / PgBouncer TRANSACTION-mode pooling
------------------------------------------------------------------------
``DATABASE_URL`` may point at Supabase's Supavisor pooler in transaction
mode (port 6543 — see ``app/db/session.py``'s module docstring for the
same pooler's effect on asyncpg prepared statements). Transaction-mode
poolers can, in general, hand different underlying Postgres backends to
different transactions issued over what looks like one persistent client
connection — a documented limitation for session-level state. Both
mechanisms above are the standard, officially-recommended approach for
this exact problem (and are unavoidable: the library gives us no
schema-qualification hook to sidestep ``search_path`` entirely) and work
unconditionally against a direct connection (local dev/CI, and this
module's own integration smoke test). Production operators pointing
``DATABASE_URL`` at a transaction-mode pooler should confirm the pooler
actually honors ``search_path`` pinning end-to-end (Supavisor, like
PgBouncer 1.21+, documents tracking/replaying a small set of "extra"
session parameters including ``search_path`` specifically for this
reason) before relying on it in production; this is a deployment-topology
question outside this issue's (#24) scope, not something either mechanism
above can unilaterally guarantee against an uncooperative pooler.

Thread convention
------------------------------------------------------------------------
ONE checkpoint thread per **case**, keyed on ``cases.langgraph_thread_id``
(``UNIQUE NOT NULL`` since migration 0002) — never per tenant channel/phone
number (a tenant's one SMS thread can span many cases over time; see
``docs/02-product/conversation-model.md``). Every graph invocation
(#25 onward) must pass
``{"configurable": {"thread_id": case.langgraph_thread_id}}`` as its
``RunnableConfig``.

**Documented exception (#34):** a message whose sender never resolves to
a known tenant (``identify_property``'s "unknown sender" branch) never
gets a ``cases`` row at all — there is no ``langgraph_thread_id`` to key
by. ``app/agent/graph.py::_resolve_thread_id`` falls back to a
per-MESSAGE thread id (``f"message:{message_id}"``) in that one case
only. This does not violate "never per tenant/phone" — a message id is
neither — and there is no ongoing case to correlate checkpoints across
multiple messages for anyway in that scenario.
"""

from __future__ import annotations

import re

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from app.config import settings

log = structlog.get_logger(__name__)

LANGGRAPH_SCHEMA = "langgraph"
"""The dedicated, RLS-free, admin-engine-only schema checkpoint tables live
in — see schema-v1.md's v1.4 amendments and migration 0007."""

_SET_SEARCH_PATH_SQL = "SET search_path TO " + LANGGRAPH_SCHEMA


def _psycopg_dsn(url: str) -> str:
    """Force a plain ``postgresql://`` URI psycopg understands, stripping
    any SQLAlchemy driver suffix (``+asyncpg``, ``+psycopg``, ...).

    Mirrors ``app/db/session.py``'s ``_asyncpg_url`` in the opposite
    direction: psycopg accepts standard libpq/URI connection strings and
    has no notion of SQLAlchemy's ``+driver`` suffix.
    """
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql://", url)


async def _configure_search_path(conn: AsyncConnection[DictRow]) -> None:
    """Pool ``configure`` callback — pins ``search_path`` the moment a new
    physical connection is established (belt-and-braces alongside the
    ``options`` startup parameter; see module docstring)."""
    await conn.execute(_SET_SEARCH_PATH_SQL)


_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None


def _get_pool() -> AsyncConnectionPool[AsyncConnection[DictRow]]:
    """Build (once, lazily) the dedicated psycopg connection pool used ONLY
    by the LangGraph checkpointer. See the module docstring for why this is
    a separate pool/driver from ``app/db/session.py``'s SQLAlchemy engines,
    and for the ``autocommit``/``search_path`` rationale.
    """
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=_psycopg_dsn(settings.database_url),
            kwargs={
                # Required — see module docstring "How search_path is
                # pinned": AsyncPostgresSaver.setup() runs CREATE INDEX
                # CONCURRENTLY, which Postgres refuses inside a transaction
                # block.
                "autocommit": True,
                # Supavisor/PgBouncer transaction-mode pooler compatibility
                # (mirrors app/db/session.py's asyncpg equivalent): disable
                # psycopg's server-side prepared-statement reuse so a
                # statement is never replayed against a different physical
                # backend than the one that prepared it.
                "prepare_threshold": None,
                # Startup-packet search_path pin — see module docstring.
                "options": f"-c search_path={LANGGRAPH_SCHEMA}",
            },
            configure=_configure_search_path,
            min_size=1,
            max_size=5,
            open=False,
        )
    return _pool


def get_checkpointer() -> AsyncPostgresSaver:
    """Return an ``AsyncPostgresSaver`` bound to the dedicated
    ``langgraph``-schema connection pool.

    Cheap and synchronous — ``AsyncPostgresSaver.__init__`` only stores the
    pool reference and creates a lock; it opens no connection itself. Safe
    to call once per graph invocation (#25 onward) rather than caching a
    single saver instance.

    ORDERING CONTRACT: ``setup_checkpointer()`` must have run first (it
    opens the pool); in the API process the app lifespan guarantees this.
    Any OTHER entrypoint (worker process, script, future graph runner)
    that uses the returned saver before setup will get
    ``psycopg_pool.PoolClosed: ... is not open yet`` — that error means
    "call ``setup_checkpointer()`` first", not a database outage.
    """
    return AsyncPostgresSaver(conn=_get_pool())


async def setup_checkpointer() -> None:
    """Idempotently create/migrate the checkpoint tables inside the
    ``langgraph`` schema (``AsyncPostgresSaver.setup()``, #24).

    MUST be called once at FastAPI startup (``app/main.py``'s lifespan),
    AFTER ``verify_request_engine_role_separation`` — see that function's
    docstring for why role separation is checked first. Failure here
    RAISES (fail closed): the agent graph cannot run without checkpoint
    tables, so a broken checkpoint store must abort startup exactly like a
    broken role-separation check does, rather than silently serve traffic
    that will fail unpredictably the first time a case is created.

    ``.setup()`` itself is idempotent — ``CREATE TABLE IF NOT EXISTS`` plus
    a ``checkpoint_migrations`` version table it consults before applying
    any migration past what has already been applied — so it is safe (and
    cheap) to run on every process start, including when the Anthropic
    key/graph is otherwise unused this deploy.
    """
    pool = _get_pool()
    await pool.open()
    saver = get_checkpointer()
    try:
        await saver.setup()
    except Exception:
        log.error("checkpointer_setup_failed")
        raise
    log.info("checkpointer_setup_complete", schema=LANGGRAPH_SCHEMA)


async def close_checkpointer() -> None:
    """Close the checkpointer pool and forget it (shutdown symmetry).

    Called after ``yield`` in the app lifespan. Also the seam
    ``tests/conftest.py``'s autouse reset uses between tests: the pool (and
    psycopg_pool's internal ``asyncio.Lock`` + background worker tasks) are
    bound to the event loop that opened them, and this repo runs one event
    loop PER TEST — a module-global pool surviving across tests is exactly
    the cross-loop singleton shape that caused the #141 flaky-401 incident.
    Idempotent; safe to call when the pool was never opened.
    """
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:  # pragma: no cover - close is best-effort
            log.warning("checkpointer_pool_close_failed")
        _pool = None


__all__: list[str] = [
    "LANGGRAPH_SCHEMA",
    "close_checkpointer",
    "get_checkpointer",
    "setup_checkpointer",
]
