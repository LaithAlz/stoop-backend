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

Pool sizing, corrected (safety review, #186 follow-up round, SECOND pass —
the FIRST pass's "shrink the floor" idea is reverted below)
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

A FIRST revision of this fix tried ``pool_size=2, max_overflow=8`` (same
10 peak, smaller steady-state floor) specifically to narrow this
process's idle connection footprint against the Supabase cap (see the
per-process accounting below). **Reverted** — safety review, this round,
measured directly against a real contended-retry workload: SQLAlchemy's
``QueuePool`` only keeps a returned connection idle for reuse if its
internal idle queue (capacity exactly ``pool_size``) isn't already full;
a returned OVERFLOW connection beyond that is physically closed rather
than kept (``QueuePool._do_return_conn``, confirmed by direct inspection
of the installed version). Combined with ``app/agent/graph.py``'s bounded
try-lock retry loop (module docstring there, "Bounded try-lock retries,
not a blocking wait" — many brief checkout/release cycles under
contention BY DESIGN, so unrelated cases are never starved), a
``pool_size`` smaller than the peak means most retries under genuine
same-case contention pay a FRESH physical-connection-establishment cost
instead of reusing an already-open one: **356 NEW physical Postgres
sessions opened in a 30s contended-retry window at ``pool_size=2,
max_overflow=8``, versus 17 at ``pool_size=10, max_overflow=0`` — for the
IDENTICAL peak capacity and the IDENTICAL completion outcome.** Fixed by
making the ENTIRE peak capacity steady-state ``pool_size`` (10) with ZERO
``max_overflow`` — every connection this pool will ever need is already
warm; there is no elastic tier left to churn. This does trade back the
smaller idle-footprint win the first revision was chasing — see the
per-process accounting below for the honest, corrected cost of that
trade, and why it is accepted anyway (retry throughput under genuine
contention is the more valuable property here).

Full per-process connection-budget accounting (safety review, #186
follow-up round — do the math explicitly rather than reason about each
pool in isolation; corrected a plain addition error THIS round caught:
an earlier revision of this docstring claimed the first-revision sizing's
worst-case total came "down to 32" — the correct sum, shown below, is 27)
------------------------------------------------------------------------
Four pools exist per process: the admin engine (``app/db/session.py``,
``pool_size=5, max_overflow=5`` — 10 peak), the request engine (SAME
object as the admin engine, and so SAME connections, until the #22
operator step sets ``APP_DATABASE_URL`` — at which point it becomes a
genuinely SEPARATE 5+5=10 peak), the checkpointer's dedicated psycopg pool
(``app/agent/checkpointer.py``, ``min_size=1, max_size=5`` — 5 peak), and
THIS pool. Worst case (role separation flipped in production, all four
pools simultaneously near their own peak): 10 (admin) + 10 (request) + 5
(checkpointer) + this pool's own peak. At THIS constant's current value
(``pool_size=10, max_overflow=0`` — fixed, no elastic range of its own
any more), that is a flat **35 connections per process** — not a range,
because this pool no longer has a floor-to-peak spread to add one. Two
Fly machines under simultaneous burst would then want up to **70
connections against Supabase's free-tier ~60 cap** the admin engine's own
docstring already budgets against — a genuine breach, and this pool's
own sizing constant CANNOT close it (this pool is only one of four
contributors, and is now deliberately non-elastic). The now-REVERTED
first-revision sizing (``pool_size=2, max_overflow=8``) would have
ranged from 27 (10 + 10 + 5 + 2, this pool at its own idle floor) up to
the SAME 35 peak — a narrower idle-time number, at the connection-churn
cost measured above. **This two-machine capacity breach is a
capacity-planning flag, not something any pool-sizing constant in this
module can fix on its own** — it belongs in the ops runbook / production
-hardening pass (roadmap #72's lane: "Full hardening pass: runbook,
alerts, unit-economics queries") as a concrete number to size real
infrastructure against (a Supabase paid tier's higher connection cap, or
fewer/larger Fly machines, or ``APP_DATABASE_URL`` staying unset a while
longer so the request engine keeps folding into the admin engine) rather
than something to silently absorb here.

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
   ``run_graph``) — pages Sentry (``sentry_sdk.capture_message``, metadata
   only) on EVERY failure, then branches on exception type (safety review,
   #186 follow-up round, NEW-2): for
   :class:`app.agent.graph.CaseLockAcquisitionTimeoutError` SPECIFICALLY —
   a TRANSIENT resource condition, not a logic failure — it queues a
   durable ``degraded_retry`` marker
   (:func:`app.agent.nodes.degraded_mode.queue_degraded_retry`), so
   ``app/agent/degraded_mode_sweep.py``'s existing 1/5/15-minute
   retry-then-escalate ladder becomes the terminal path once the
   contention/burst has drained. For any OTHER exception (including a bare
   pool-checkout ``sqlalchemy.exc.TimeoutError``, which never carries a
   resolved ``case_id`` to queue a retry against), it falls back to the
   ORIGINAL one-shot last-resort ``needs_eyes`` notification insert
   (:func:`app.agent.graph_entry._attempt_last_resort_needs_eyes``). A
   ``BackgroundTasks`` callback has no HTTP caller left to surface
   anything to otherwise, so SOME durable artifact always results either
   way.
2. **The dashboard approve/reject/edit-and-send endpoints**
   (``app/routers/drafts.py``, calling ``resolve_draft_decision``) — catch
   only :class:`app.agent.graph.DraftStaleError` /
   :class:`app.agent.graph.CaseNotAwaitingApprovalError`; a bare
   ``TimeoutError`` is NOT one of those, so it propagates past this
   router's own handling entirely. **Updated (safety review, #186
   follow-up round, NEW-1):** ``app/main.py`` now ALSO registers a
   catch-all ``Exception`` handler (``_unhandled_exception_handler``,
   api-contracts.md v1.21) alongside ``AuthError``/``AppError``/
   ``RequestValidationError`` — a ``TimeoutError`` matches no MORE
   specific handler, so it now resolves to this one, returning the SAME
   house JSON error envelope (``{"error": {"code": "internal_error",
   "message", "request_id"}}``, WITH a real ``request_id``) every other
   endpoint's error responses already use, instead of Starlette's
   previous plain-text default. Paged to Sentry explicitly by that
   handler (metadata only, same shape as everywhere else in this
   codebase) — no longer dependent on Sentry's own
   ``FastApiIntegration``/``StarletteIntegration`` auto-capture (which a
   registered handler intercepts ahead of, see that handler's own
   docstring). Still no ``needs_eyes`` fallback on this path — the draft
   simply stays ``pending`` for the landlord to retry from a reloaded
   dashboard, which now also shows a properly-shaped error instead of a
   bare, unstyled server error page.
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

_LOCK_POOL_SIZE = 10
"""The FULL peak capacity, all as steady-state ``pool_size`` — see module
docstring "Pool sizing, corrected (safety review, #186 follow-up round,
second pass)" for why a small floor + large overflow (this constant's
PREVIOUS value, 2) was reverted: overflow connections are not kept warm
(SQLAlchemy's ``QueuePool`` closes a returned overflow connection
immediately whenever its idle queue — capacity exactly ``pool_size`` — is
already full), so a small ``pool_size`` under this fix's own bounded
try-lock retry loop (many brief checkout/release cycles under contention)
means most retries pay a fresh physical-connection-establishment cost
instead of reusing an already-open one — measured directly: 356 NEW
physical Postgres sessions in a 30s contended-retry window at
``pool_size=2, max_overflow=8``, versus 17 at this constant's CURRENT
value, for the IDENTICAL peak capacity and the IDENTICAL completion
outcome. All 10 connections are now genuinely kept warm."""

_LOCK_MAX_OVERFLOW = 0
"""No overflow AT ALL — see :data:`_LOCK_POOL_SIZE`'s own docstring. Every
connection this pool will ever use is already counted in ``pool_size``;
elastic overflow bought nothing but connection-churn overhead once the
retry loop's own access pattern (BLOCKING-2) is accounted for."""

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
