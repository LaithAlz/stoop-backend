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
that reasoning. :data:`_LOCK_POOL_SIZE` / :data:`_LOCK_MAX_OVERFLOW` keep
this pool's PEAK ceiling in that same 10-concurrent-case class this
codebase already reasoned about — decoupled from the pool the in-span
nodes also need, never a capacity increase over what the admin pool alone
used to bound.

Full per-process connection-budget accounting (safety review, #186
follow-up round, ADVISORY — do the math explicitly rather than reason
about each pool in isolation)
------------------------------------------------------------------------
Four pools now exist per process: the admin engine (``app/db/session.py``,
``pool_size=5, max_overflow=5`` — 10 peak), the request engine (SAME
object as the admin engine, and so SAME connections, until the #22
operator step sets ``APP_DATABASE_URL`` — at which point it becomes a
genuinely SEPARATE 5+5=10 peak), the checkpointer's dedicated psycopg pool
(``app/agent/checkpointer.py``, ``min_size=1, max_size=5`` — 5 peak), and
THIS pool. Worst case (role separation flipped in production): 10 + 10 +
5 + this pool's peak. At this pool's ORIGINAL 5+5=10, that is 35 per
process — two Fly machines under simultaneous burst would want up to 70
connections, against Supabase's free-tier ~60 cap the admin engine's own
docstring already budgets against. This pool's connections are also the
LONGEST-lived of the four in practice: a lock HOLDER sits genuinely
idle-in-transaction at the Postgres level for the ENTIRE LLM-bound span
(the advisory-lock transaction does no further DB work of its own while
Python awaits the Anthropic call) — the most expensive kind of connection
to leave provisioned. Fixed: :data:`_LOCK_POOL_SIZE` drops to 2 (from 5)
while :data:`_LOCK_MAX_OVERFLOW` rises to 8 (from 5) — the PEAK ceiling
stays the identical 10 (no reduction in how many concurrent case-locks
this process can hold at once, so the "Bounded try-lock retries" fix above
is unaffected), but the STEADY-STATE idle footprint drops from 5
always-open connections to 2: SQLAlchemy's ``QueuePool`` keeps ``pool_size``
connections open indefinitely as a floor, while ``max_overflow`` connections
above that are opened only under genuine demand and closed promptly once
the burst subsides (``app/db/session.py``'s own module docstring, same
wording, for the admin engine's identical overflow semantics). Bringing
the worst-case per-process total down to 32 (10 + 10 + 5 + 2-to-10
depending on load) narrows, though does not eliminate, the two-machine
headroom question above — a genuine capacity-planning item for whoever
provisions the first Fly scale-out, not something a pool-sizing constant
alone can fully resolve.

**A discovered trade-off, honestly flagged, not silently absorbed**: a
small ``pool_size`` interacts with ``app/agent/graph.py``'s bounded
try-lock retry loop (module docstring there, "Bounded try-lock retries,
not a blocking wait") in a way worth naming explicitly.
``QueuePool._do_return_conn`` only keeps a returned connection idle for
reuse if the pool's internal idle queue (capacity exactly
``pool_size`` — here, 2) isn't already full; a returned OVERFLOW
connection beyond that is physically closed rather than kept
(``sqlalchemy/pool/impl.py``, confirmed by direct inspection of the
installed version). Combined with the retry loop's own pattern (release
back to the pool on every LOSING non-blocking attempt, re-checkout on the
next retry), more than 2 concurrent attempts against ONE hot, contended
case means most retries pay a FRESH physical-connection-establishment
cost (empirically several hundred ms against a local Docker Postgres in
this repo's own test environment — see ``tests/test_agent_case_lock_
retry.py``) rather than reusing an already-open one. This does NOT
reopen the head-of-line-blocking bug (BLOCKING-2) — waiting still never
PINS a pool slot, so unrelated cases are never starved — but it DOES mean
a genuinely busy/chatty case's own backlog can drain more slowly than a
larger steady-state pool would allow, purely from repeated
connection-establishment overhead rather than actual lock contention.
Accepted here per this round's explicit sizing directive (the
Supabase-connection-budget concern above); a future revision could widen
:data:`_LOCK_POOL_SIZE` again (trading idle footprint back for retry
throughput) if chatty-case latency under real production load turns out
to matter more than the two-machine connection-budget headroom does.

:data:`_LOCK_POOL_TIMEOUT` is deliberately SHORTER than SQLAlchemy's default
(30s, still used by the admin engine) — the #186 issue's own prescribed
safe failure direction: once genuine demand exceeds this pool's capacity, a
checkout should fail FAST (``sqlalchemy.exc.TimeoutError``) rather than
block a tenant- or landlord-facing request for up to 30s. That
``TimeoutError`` propagates up through :func:`app.agent.graph.run_graph` /
:func:`app.agent.graph.resume_case_thread` /
:func:`app.agent.graph.resolve_draft_decision` to whichever of THREE
different callers happened to be waiting on this pool — named here
honestly (safety review, #186 follow-up round) because they land
DIFFERENTLY, not identically:

1. **The Twilio SMS webhook's background classification task**
   (``app/agent/graph_entry.py::enqueue_classification``, calling
   ``run_graph``) — already wraps that call in a try/except that pages
   Sentry (``sentry_sdk.capture_message``, metadata only) AND attempts a
   last-resort ``needs_eyes`` notification insert
   (:func:`app.agent.graph_entry._attempt_last_resort_needs_eyes`). This is
   the ONLY one of the three with a purpose-built fallback notification —
   a ``BackgroundTasks`` callback has no HTTP caller left to surface
   anything to otherwise.
2. **The dashboard approve/reject/edit-and-send endpoints**
   (``app/routers/drafts.py``, calling ``resolve_draft_decision``) — catch
   only :class:`app.agent.graph.DraftStaleError` /
   :class:`app.agent.graph.CaseNotAwaitingApprovalError`; a bare
   ``TimeoutError`` is NOT one of those, so it propagates past this
   router's own handling entirely. ``app/main.py`` registers exception
   handlers for ``AuthError``/``AppError``/``RequestValidationError``
   only — a ``TimeoutError`` matches none of them, so it surfaces as
   Starlette's own default response: a PLAIN ``500`` (``"Internal Server
   Error"``, ``text/plain`` — verified against this installed Starlette
   version — never the house JSON error envelope
   ``{"error": {"code", ...}}`` every OTHER error path in this codebase
   returns). Sentry's ``FastApiIntegration``/``StarletteIntegration``
   (``app/observability.py::init_sentry``) auto-captures any exception
   that reaches that default handler, so this path DOES still page ops —
   just without a matching envelope shape or a ``needs_eyes`` fallback;
   the draft simply stays ``pending`` for the landlord to retry from a
   reloaded dashboard.
3. **Approve-by-SMS** (``app/agent/approve_by_sms.py``, calling
   ``resolve_draft_decision`` from inside the SAME Twilio webhook request
   as (1) above, for a landlord's "1"/"2"/"UNDO" reply) — safety review,
   #186 follow-up round, BLOCKING finding: this path used to swallow the
   exception via ``app/routers/webhooks/twilio.py``'s ``_safe_step`` with
   its default ``alert_on_failure=False`` (log-only — ``log.error`` alone
   never reaches Sentry, ``LoggingIntegration(event_level=None)``) and
   then unconditionally return, leaving NEITHER a Sentry page NOR a
   ``needs_eyes`` fallback — the landlord's reply would silently vanish.
   Fixed: that call site now passes ``alert_on_failure=True`` and, on
   failure, falls through to the SAME ``_ensure_needs_eyes_notification``
   fallback an unrecognized/uncorrelated approve-by-SMS token already gets
   — see ``_run_post_persist_side_effects``'s own docstring in that module.

The Tier-0 prefilter still runs pre-graph regardless of all three
(``app/routers/webhooks/twilio.py``), so a saturated lock pool never gates
the emergency path.

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

_LOCK_POOL_SIZE = 2
"""Steady-state floor — deliberately SMALLER than the admin engine's own
``pool_size=5`` (see module docstring "Full per-process connection-budget
accounting"): this pool's connections are the longest-lived/most
idle-in-transaction-prone of the four per-process pools, so keeping fewer
of them open by default (with ``max_overflow`` covering genuine bursts)
narrows this process's total connection footprint against Supabase's
free-tier cap."""

_LOCK_MAX_OVERFLOW = 8
"""Burst capacity above :data:`_LOCK_POOL_SIZE` — 2 + 8 = 10 peak,
UNCHANGED from before (still the same concurrency ceiling the "Bounded
try-lock retries" fix in ``app/agent/graph.py`` assumes), opened only
under genuine demand and closed promptly once the burst subsides
(``app/db/session.py``'s own documented overflow semantics)."""

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
