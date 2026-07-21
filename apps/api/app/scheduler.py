"""In-process 60-second scheduler ticker (#108/#109/#53) — FastAPI
lifespan-managed, drives the emergency escalation chain sweep, the SMS
drain sweep, the degraded-mode retry sweep, the approve-flow draft
sender, and the deprovisioning number-release sweep:
- ``app/agent/emergency_chain.py::run_emergency_chain_sweep``
- ``app/agent/emergency_chain.py::run_sms_drain_sweep`` (safety review,
  2026-07-12, spec finding S1 / safety finding 3 — drains
  ``tenant_ack``/``emergency_sms`` rows)
- ``app/agent/degraded_mode_sweep.py::sweep_degraded_mode_retries``
- ``app/agent/draft_sender.py::sender_tick`` (#44/#45 integration commit —
  the ONLY other sanctioned outbound-send call site besides the emergency
  chain above; see ``apps/api/CLAUDE.md``'s "Send to tenant/vendor happens
  only through the draft flow or the emergency safety path"). This reuses
  the SAME 60s cadence as the other three sweeps — one scheduler owns all
  periodic work, never a second competing lifespan task. (An earlier
  revision also shipped a faster standalone loop, ``run_sender_loop``, as
  an alternative wiring; #199 deleted it as dead surface — sub-60s
  dispatch, if ever wanted, is a change to this ticker's own interval, not
  a second loop.) A landlord's undo window is unaffected in the COMMON
  case (the window is measured from approval, and a tick already runs
  every 60s regardless of when within that window approval happened); the
  worst case is a due send waiting up to just under 60s for the next
  tick, matching this ticker's existing coarse-grained cadence for every
  other sweep it drives.
- ``app/property_provisioning.py::sweep_pending_number_releases`` (#53 —
  releases a deleted property's Twilio number once its 24h grace period
  has elapsed; same "the timer only decides WHEN TO LOOK, never WHAT'S
  DUE" doctrine as every other sweep here). Runs LAST, after the draft
  sender: a release tolerates hours of delay by design (the grace period
  is 24h), so it must never sit ahead of anything time-sensitive.

Design choice (the campaign's "sender design menu" — (a) in-process
asyncio periodic task, RECOMMENDED for v1; matches
``docs/02-product/emergency-prefilter.md``'s own "Implementation notes":
"a ``fly machines`` cron / APScheduler loop checking unacknowledged
emergencies every 60 s is sufficient at pilot scale"). No new
infrastructure — started/stopped in ``app/main.py``'s lifespan, the SAME
lifespan that already manages the LangGraph checkpointer's connection
pool. Combining all four sweeps into ONE ticker (rather than independent
loops) stayed clean enough to keep in this single small module — see this
file for the entire scheduler surface; if a FIFTH sweep ever needs
combining here and this file starts to sprawl, split it back out rather
than let it bloat.

Bounding sender_tick's own worst-case duration (safety review, MEDIUM)
------------------------------------------------------------------------
``sender_tick`` shares this SAME single ticker task with the three sweeps
above — a slow tick here is a slow tick for the emergency chain sweep's
NEXT run too. Up to ``DEFAULT_BATCH_SIZE`` (25) candidate drafts, each
risking a 10s Twilio timeout (``app/integrations/twilio_send.py``'s
``AsyncTwilioHttpClient``), could otherwise stretch one tick to ~250s in
the worst case. ``app/agent/draft_sender.py::sender_tick`` bounds this
with its own wall-clock deadline (``DEFAULT_TICK_DEADLINE_SECONDS``, 25s
by default, computed from the injectable time source at tick start): once
exceeded it stops claiming NEW drafts for the rest of that tick (a draft
already claimed always finishes; leftover due candidates simply wait,
unclaimed and still ``'approved'``, for the very next tick — nothing is
lost). This bounds sender_tick's own contribution to a single tick's
duration to roughly ``DEFAULT_TICK_DEADLINE_SECONDS`` plus one in-flight
send's tail latency, so the other three sweeps' own cadence is never
starved by an unbounded draft-sending backlog. The emergency chain sweep
still runs FIRST in ``_run_one_tick_body`` (unchanged) — this deadline is
additive insurance for the LAST sweep in the tick, not a reordering.

Crash-safety
------------
The ticker itself carries NO schedule state — it only wakes every
:data:`TICK_INTERVAL_SECONDS` and asks each sweep function "what's due
right now?", which reads directly from the ``notifications`` table (the
first three sweeps) or the ``drafts`` table (``sender_tick`` — "the undo
window is data, not a sleep", ``app/agent/draft_sender.py``'s own phrase).
A process restart loses only the in-memory tick loop, never the schedule:
every due row is still due (or becomes due) after restart, and the very
next tick catches up — the literal meaning of the task's "retries/chain
state = data ... never in-process timers for the SCHEDULE" constraint:
the timer here only decides WHEN TO LOOK, never WHAT'S DUE.

One tick's exception never kills the loop — and neither does a failure
IN the failure-reporting itself (safety review, 2026-07-12, finding 4,
MEDIUM)
--------------------------------------------------------------------------
Each sweep call is wrapped in its OWN try/except, reported via
:func:`_safe_report`. Every sweep function already has its own internal
per-candidate exception handling (see their module docstrings —
``sender_tick``'s per-draft handling lives in ``_process_claimed_draft``,
which never raises) — an exception escaping any one of them here
represents an unexpected failure in that sweep's own outer bookkeeping
(e.g. the initial due-rows SELECT itself), not a per-candidate failure.

An EARLIER revision's per-sweep ``except Exception: log.error(...);
sentry_sdk.capture_message(...)`` had a real gap: if the ``log.error``/
``sentry_sdk.capture_message`` calls THEMSELVES raised (a broken logging
pipe, a Sentry transport exception — not hypothetical; Sentry's HTTP
transport can itself raise under a bad network condition), that new
exception would propagate OUT of ``_run_one_tick`` entirely uncaught,
which would kill the ``_ticker_loop``'s ``while True`` permanently and
silently (an unhandled exception in an ``asyncio.Task`` that nothing else
awaits just logs "Task exception was never retrieved" and vanishes — the
scheduler would simply stop sweeping forever, with no explicit signal).
Fixed two ways, belt-and-braces:

1. :func:`_safe_report` wraps its OWN ``log.error``/
   ``sentry_sdk.capture_message`` calls in their OWN try/except — a
   failure reporting one sweep's failure can never escape.
2. :func:`_run_one_tick` itself is wrapped in an outer try/except as a
   final backstop, and the ticker task carries a
   ``add_done_callback`` (:func:`_on_ticker_task_done`): if the task ever
   DOES exit for any reason other than an intentional
   ``stop_scheduler()`` cancellation — which should be structurally
   impossible given (1) — it is logged/paged AND THE SCHEDULER IS
   RESTARTED, so a genuinely-unanticipated bug in this file can never
   permanently stop the emergency chain from being swept.
"""

from __future__ import annotations

import asyncio

import sentry_sdk
import structlog

from app.agent.degraded_mode_sweep import sweep_degraded_mode_retries
from app.agent.draft_sender import sender_tick
from app.agent.emergency_chain import run_emergency_chain_sweep, run_sms_drain_sweep
from app.integrations.sms_sender import get_default_sms_sender
from app.property_provisioning import sweep_pending_number_releases

log = structlog.get_logger(__name__)

TICK_INTERVAL_SECONDS: float = 60.0
"""The scheduler's wake interval — emergency-prefilter.md's "60 s ...
sufficient at pilot scale". Configurable in one place; never read from
settings/env/a feature flag (this module drives the emergency path)."""

_task: asyncio.Task[None] | None = None


def _safe_report(log_event: str, sentry_message: str, exc: Exception) -> None:
    """Best-effort log + Sentry page for one sweep's failure — wrapped so
    a failure IN the reporting itself can never propagate and kill the
    ticker loop (safety review, 2026-07-12, finding 4, MEDIUM). Metadata
    only (event name/exception type), never a message body or phone
    number, rule #5."""
    try:
        log.error(log_event, exc_type=type(exc).__name__)
    except Exception:  # noqa: S110 -- pragma: no cover -- logging itself must never raise here
        pass
    try:
        sentry_sdk.capture_message(
            sentry_message, level="error", extras={"exc_type": type(exc).__name__}
        )
    except Exception:  # noqa: S110 -- pragma: no cover -- a broken Sentry transport must never raise
        pass


async def _run_one_tick_body() -> None:
    """Run every sweep once, each independently guarded so one's failure
    never prevents the others from running this same tick."""
    try:
        await run_emergency_chain_sweep()
    except Exception as exc:
        _safe_report(
            "scheduler_emergency_chain_sweep_failed",
            "scheduler: emergency chain sweep tick raised",
            exc,
        )

    try:
        await run_sms_drain_sweep()
    except Exception as exc:
        _safe_report(
            "scheduler_sms_drain_sweep_failed", "scheduler: sms drain sweep tick raised", exc
        )

    try:
        await sweep_degraded_mode_retries()
    except Exception as exc:
        _safe_report(
            "scheduler_degraded_mode_sweep_failed",
            "scheduler: degraded-mode sweep tick raised",
            exc,
        )

    try:
        await sender_tick(sender=get_default_sms_sender())
    except Exception as exc:
        _safe_report(
            "scheduler_draft_sender_tick_failed",
            "scheduler: draft sender tick raised",
            exc,
        )

    try:
        await sweep_pending_number_releases()
    except Exception as exc:
        _safe_report(
            "scheduler_number_release_sweep_failed",
            "scheduler: number-release sweep tick raised",
            exc,
        )


async def _run_one_tick() -> None:
    """Outer backstop around :func:`_run_one_tick_body` — see module
    docstring "One tick's exception never kills the loop". Should never
    actually catch anything in practice (every sweep call inside is
    already individually guarded via :func:`_safe_report`), but this is
    the LAST line of defense before an exception would reach the ticker
    task itself."""
    try:
        await _run_one_tick_body()
    except Exception:  # pragma: no cover — defensive final backstop
        try:
            log.error("scheduler_tick_failed_unexpectedly")
        except Exception:  # noqa: S110 -- last-resort backstop, must never itself raise
            pass


async def _ticker_loop(interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await _run_one_tick()


def _on_ticker_task_done(task: asyncio.Task[None]) -> None:
    """Runs when the ticker task finishes, for ANY reason. A clean
    cancellation (``stop_scheduler()``'s own doing) is expected and does
    nothing further. Anything else — the loop somehow exiting
    non-cancelled, which ``_run_one_tick``'s own guarantees should make
    structurally impossible — is logged/paged and the scheduler is
    RESTARTED (safety review, 2026-07-12, finding 4, MEDIUM): a genuinely
    unanticipated bug here must never permanently stop the emergency chain
    from being swept."""
    if task.cancelled():
        return

    exc = task.exception()
    try:
        if exc is not None:
            log.error("scheduler_ticker_task_died_unexpectedly", exc_type=type(exc).__name__)
            sentry_sdk.capture_message(
                "scheduler: ticker task died unexpectedly -- restarting",
                level="error",
                extras={"exc_type": type(exc).__name__},
            )
        else:  # pragma: no cover — _ticker_loop never returns normally (while True)
            log.error("scheduler_ticker_task_exited_without_cancellation")
    except Exception:  # noqa: S110 -- pragma: no cover -- reporting must never block the restart
        pass

    start_scheduler()


def start_scheduler() -> None:
    """Start the ticker task — called once from ``app/main.py``'s
    lifespan, AFTER checkpointer setup (and, per finding 4, from
    :func:`_on_ticker_task_done` if the task ever dies unexpectedly).
    Idempotent: a second call while a task is already running is a no-op."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_ticker_loop(TICK_INTERVAL_SECONDS))
    _task.add_done_callback(_on_ticker_task_done)


async def stop_scheduler() -> None:
    """Cancel and await the ticker task — called from ``app/main.py``'s
    lifespan shutdown. Safe to call even if the scheduler was never
    started (``_task is None``) or already stopped. The done-callback sees
    a genuine cancellation here and does NOT restart."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None


def reset_scheduler_for_tests() -> None:
    """Test-only seam: forget the module-level task reference WITHOUT
    cancelling it — mirrors ``app/agent/checkpointer.py``'s ``_pool = None``
    reset convention (``tests/conftest.py``): each test runs its own event
    loop, and a task bound to an earlier loop must never be awaited from a
    later one."""
    global _task
    _task = None


__all__: list[str] = [
    "TICK_INTERVAL_SECONDS",
    "reset_scheduler_for_tests",
    "start_scheduler",
    "stop_scheduler",
]
