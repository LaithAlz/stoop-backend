"""In-process 60-second scheduler ticker (#108/#109) — FastAPI
lifespan-managed, drives BOTH the emergency escalation chain sweep
(``app/agent/emergency_chain.py::run_emergency_chain_sweep``) and the
degraded-mode retry sweep
(``app/agent/degraded_mode_sweep.py::sweep_degraded_mode_retries``).

Design choice (the campaign's "sender design menu" — (a) in-process
asyncio periodic task, RECOMMENDED for v1; matches
``docs/02-product/emergency-prefilter.md``'s own "Implementation notes":
"a ``fly machines`` cron / APScheduler loop checking unacknowledged
emergencies every 60 s is sufficient at pilot scale"). No new
infrastructure — started/stopped in ``app/main.py``'s lifespan, the SAME
lifespan that already manages the LangGraph checkpointer's connection
pool. Combining both sweeps into ONE ticker (rather than two independent
loops) stayed clean enough to keep in this single small module — see this
file for the entire scheduler surface; if a THIRD sweep ever needs
combining here and this file starts to sprawl, split it back out rather
than let it bloat.

Crash-safety
------------
The ticker itself carries NO schedule state — it only wakes every
:data:`TICK_INTERVAL_SECONDS` and asks each sweep function "what's due
right now?", which reads directly from the ``notifications`` table. A
process restart loses only the in-memory tick loop, never the schedule:
every due row is still due (or becomes due) after restart, and the very
next tick catches up — the literal meaning of the task's "retries/chain
state = data ... never in-process timers for the SCHEDULE" constraint:
the timer here only decides WHEN TO LOOK, never WHAT'S DUE.

One tick's exception never kills the loop
------------------------------------------
Each sweep call is wrapped in its OWN try/except. Both sweep functions
already have their own internal per-candidate exception handling (see
their module docstrings) — an exception escaping either one here
represents an unexpected failure in the sweep's own outer bookkeeping
(e.g. the initial due-rows SELECT itself), not a per-candidate failure.
Logged + paged to Sentry (metadata only, rule #5); the loop continues to
the next tick regardless — a single bad tick must never silently stop the
entire scheduler.
"""

from __future__ import annotations

import asyncio

import sentry_sdk
import structlog

from app.agent.degraded_mode_sweep import sweep_degraded_mode_retries
from app.agent.emergency_chain import run_emergency_chain_sweep

log = structlog.get_logger(__name__)

TICK_INTERVAL_SECONDS: float = 60.0
"""The scheduler's wake interval — emergency-prefilter.md's "60 s ...
sufficient at pilot scale". Configurable in one place; never read from
settings/env/a feature flag (this module drives the emergency path)."""

_task: asyncio.Task[None] | None = None


async def _run_one_tick() -> None:
    """Run both sweeps once, each independently guarded so one's failure
    never prevents the other from running this same tick."""
    try:
        await run_emergency_chain_sweep()
    except Exception as exc:
        log.error("scheduler_emergency_chain_sweep_failed", exc_type=type(exc).__name__)
        sentry_sdk.capture_message(
            "scheduler: emergency chain sweep tick raised",
            level="error",
            extras={"exc_type": type(exc).__name__},
        )

    try:
        await sweep_degraded_mode_retries()
    except Exception as exc:
        log.error("scheduler_degraded_mode_sweep_failed", exc_type=type(exc).__name__)
        sentry_sdk.capture_message(
            "scheduler: degraded-mode sweep tick raised",
            level="error",
            extras={"exc_type": type(exc).__name__},
        )


async def _ticker_loop(interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await _run_one_tick()


def start_scheduler() -> None:
    """Start the ticker task — called once from ``app/main.py``'s
    lifespan, AFTER checkpointer setup. Idempotent: a second call while a
    task is already running is a no-op (defensive; the lifespan only ever
    calls this once per process)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_ticker_loop(TICK_INTERVAL_SECONDS))


async def stop_scheduler() -> None:
    """Cancel and await the ticker task — called from ``app/main.py``'s
    lifespan shutdown. Safe to call even if the scheduler was never
    started (``_task is None``) or already stopped."""
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
