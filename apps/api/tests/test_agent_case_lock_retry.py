"""Tests for the bounded try-lock retry loop in ``app/agent/graph.py``'s
``_case_lock`` (#186 follow-up round, BLOCKING-2 — "head-of-line
blocking").

Safety review reproduced live: the ORIGINAL (item 1 only) fix used a
single BLOCKING ``pg_advisory_xact_lock`` call issued after a connection
was already checked out of the dedicated lock pool
(``app/agent/case_lock_pool.py``). A connection checked out to run a
blocking statement stays checked out for however long that statement
blocks — for a caller waiting behind a HOT, contended case, that is
bounded only by the current holder's entire run duration, not by
anything the pool itself controls. Twelve messages racing for the SAME
case (genuinely, correctly serialized) could alone pin every connection
the dedicated pool has, starving EIGHT unrelated, uncontended cases that
have nothing to do with the hot case's contention at all.

Fixed with a bounded, non-blocking ``pg_try_advisory_xact_lock`` retry
loop (module docstring in ``app/agent/graph.py``, "Bounded try-lock
retries, not a blocking wait"): an attempt that does not acquire the lock
releases its connection immediately and sleeps OUTSIDE the pool before
retrying, so waiting itself never pins a pool slot.

Marker: ``integration`` — requires a running Postgres instance (the
dedicated pool connects for real; no schema/migration is needed at all,
since ``pg_try_advisory_xact_lock`` is a builtin requiring no tables —
this module deliberately does NOT run alembic, unlike most other
integration test modules in this repo).

These tests call ``app.agent.graph._case_lock`` directly against
freshly-generated, never-persisted UUIDs — the lock is a pure
Postgres-side primitive keyed on int4 pairs derived from a UUID's own
bits (``_case_lock_keys``); no ``cases`` row needs to exist for it to
work, which keeps this module fast and self-contained (no seeding
helpers, no fake Anthropic client).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SQLATimeoutError

import app.agent.case_lock_pool as case_lock_pool_mod
import app.agent.graph as graph_mod
import app.db.session as db_session_mod

# Fast, deterministic retry cadence for these tests -- the real module
# defaults (0.1s base + up to 0.1s jitter, 30s bound) are sized for
# production LLM-span durations, not test wall-clock budgets. Every test
# below monkeypatches these down before exercising the retry loop.
_FAST_RETRY_BASE_SECONDS = 0.01
_FAST_RETRY_JITTER_SECONDS = 0.01
_FAST_MAX_WAIT_SECONDS = 5.0


@pytest.fixture(autouse=True)
def _fast_retry_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Applies to every test in this module — see module-level constants
    above. ``_case_lock`` looks these up as plain module globals at call
    time (not captured at function-definition time), so patching the
    module attributes here is visible to every subsequent ``_case_lock``
    call in the test, exactly like ``monkeypatch.setattr`` everywhere else
    in this codebase's test suite."""
    monkeypatch.setattr(graph_mod, "_CASE_LOCK_RETRY_BASE_SECONDS", _FAST_RETRY_BASE_SECONDS)
    monkeypatch.setattr(graph_mod, "_CASE_LOCK_RETRY_JITTER_SECONDS", _FAST_RETRY_JITTER_SECONDS)
    monkeypatch.setattr(graph_mod, "_CASE_LOCK_MAX_WAIT_SECONDS", _FAST_MAX_WAIT_SECONDS)


async def _hold_case_lock_briefly(case_id: uuid.UUID, *, hold_seconds: float) -> None:
    """Acquire ``_case_lock`` for *case_id*, simulate brief in-span work
    (an ``asyncio.sleep``), and release. Raises whatever ``_case_lock``
    itself raises (``CaseLockAcquisitionTimeoutError`` on exhausting the
    bounded retry window) -- callers decide whether that is expected."""
    async with graph_mod._case_lock(case_id):  # noqa: SLF001
        await asyncio.sleep(hold_seconds)


async def _hold_case_lock_and_touch_admin_pool(case_id: uuid.UUID, *, hold_seconds: float) -> None:
    """Same as :func:`_hold_case_lock_briefly`, but ALSO checks out and
    releases a connection from the ADMIN pool while holding the case
    lock -- simulating a real in-span node's own DB access
    (``identify_property``/``classify_severity``/... each check out their
    own admin-pool connection independently, per ``app/agent/graph.py``'s
    own module docstring). This is the structural shape that would have
    deadlocked under the PRE-#186-item-1 design (the lock and the node
    checkout drawing from the SAME shared pool) -- proving it still
    succeeds even at N > this pool's own peak capacity is the assertion
    that actually distinguishes this branch from that earlier design, not
    merely that uncontended locks resolve quickly in isolation."""
    async with graph_mod._case_lock(case_id):  # noqa: SLF001
        async with db_session_mod.engine.connect() as admin_conn:
            await admin_conn.execute(text("SELECT 1"))
        await asyncio.sleep(hold_seconds)


# ---------------------------------------------------------------------------
# BLOCKING-2 / ADVISORY-4(a) — N concurrent DISTINCT cases, N > the
# dedicated pool's own peak capacity, all complete.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_n_greater_than_pool_capacity_distinct_cases_all_complete() -> None:
    """``N`` (15) exceeds the dedicated pool's own peak
    (``_LOCK_POOL_SIZE + _LOCK_MAX_OVERFLOW`` == 10) — every one of the 15
    concurrent, DISTINCT-case lock holds ALSO touches the admin pool
    mid-span (simulating real in-span node work), and every single one
    must still complete without raising. This is the assertion that would
    have deadlocked under the shared-pool design item 1 replaced (all
    admin-pool connections held by locks, none left for the node
    checkouts running inside them) -- on this branch, the two pools are
    genuinely independent, so admin-pool checkouts never even notice how
    many case locks are concurrently held.
    """
    peak = case_lock_pool_mod._LOCK_POOL_SIZE + case_lock_pool_mod._LOCK_MAX_OVERFLOW  # noqa: SLF001
    n = peak + 5
    assert n > peak, "test setup: N must exceed the dedicated pool's own peak capacity"

    case_ids = [uuid.uuid4() for _ in range(n)]
    results = await asyncio.gather(
        *(_hold_case_lock_and_touch_admin_pool(cid, hold_seconds=0.05) for cid in case_ids),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"{len(failures)}/{n} distinct-case lock holds failed: {failures!r}"


# ---------------------------------------------------------------------------
# BLOCKING-2 / ADVISORY-4(a) — chatty-case scenario: a deep backlog on ONE
# hot case must never starve UNRELATED cases' own, otherwise-instant
# acquisitions.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_chatty_case_backlog_does_not_starve_unrelated_cases() -> None:
    """Reproduces the reviewer's own repro shape: 12 messages racing for
    the SAME (hot) case_id, genuinely serialized (correct), running
    CONCURRENTLY with 8 UNRELATED, distinct-case messages that have
    nothing to do with the hot case's contention. Before this fix, the 12
    hot-case waiters alone could pin every connection the dedicated pool
    has (each blocked, holding a connection, waiting its turn) -- leaving
    the 8 unrelated cases unable to even ATTEMPT their own, otherwise
    -instant, uncontended acquisition.

    Asserts the unrelated 8 finish in a MEANINGFULLY SMALLER fraction of
    the hot case's own total drain time (a RELATIVE comparison against
    ``last_hot_elapsed``, not a fixed absolute bound): this environment's
    own connection-establishment latency (observed directly against this
    dedicated pool: ~0.4s just to open a fresh physical connection,
    dominating over this test's millisecond-scale ``hold_seconds``) makes
    an absolute wall-clock prediction for the hot chain's own duration
    unreliable across machines/CI runners, but the RATIO between "how long
    did the unrelated cases take" and "how long did the hot case's own
    backlog take" is exactly the signal that distinguishes "unrelated
    cases proceed independently" (small ratio) from "unrelated cases
    queued behind the hot backlog" (ratio near/at 1.0, the pre-fix
    behavior) regardless of the absolute numbers this run happens to
    produce.
    """
    hot_case_id = uuid.uuid4()
    hot_hold_seconds = 0.05
    hot_message_count = 12
    unrelated_case_count = 8

    unrelated_case_ids = [uuid.uuid4() for _ in range(unrelated_case_count)]
    unrelated_done_at: list[float] = []

    async def _unrelated(case_id: uuid.UUID) -> None:
        await _hold_case_lock_briefly(case_id, hold_seconds=0.01)
        unrelated_done_at.append(time.monotonic())

    hot_done_at: list[float] = []

    async def _hot() -> None:
        await _hold_case_lock_briefly(hot_case_id, hold_seconds=hot_hold_seconds)
        hot_done_at.append(time.monotonic())

    start = time.monotonic()
    results = await asyncio.gather(
        *(_hot() for _ in range(hot_message_count)),
        *(_unrelated(cid) for cid in unrelated_case_ids),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"unexpected failures: {failures!r}"
    assert len(unrelated_done_at) == unrelated_case_count
    assert len(hot_done_at) == hot_message_count

    last_unrelated_elapsed = max(unrelated_done_at) - start
    last_hot_elapsed = max(hot_done_at) - start

    # The unrelated cases must finish having taken meaningfully LESS time
    # than the hot case's own full serialized chain -- proving they were
    # never queued BEHIND the hot backlog, whatever the hot chain's own
    # absolute duration (dominated by this environment's connection
    # -establishment latency, not by this fix's own logic) turns out to be.
    assert last_unrelated_elapsed < last_hot_elapsed * 0.75, (
        f"unrelated cases took {last_unrelated_elapsed:.2f}s of the hot case's own "
        f"{last_hot_elapsed:.2f}s total -- too close to 1.0x, suggesting they were "
        "starved behind the hot backlog rather than proceeding independently"
    )
    # The hot case's own chain still eventually, correctly, fully drains --
    # genuine serialization under contention still works (every one of the
    # 12 completed, asserted above), this fix did not trade correctness
    # for the head-of-line fix.


# ---------------------------------------------------------------------------
# Bounded acquisition failure -- genuine, sustained same-key contention
# still eventually raises rather than waiting forever.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sustained_same_case_contention_eventually_raises_acquisition_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single, long-held lock on one case_id, plus a waiter bounded by a
    SHORT ``_CASE_LOCK_MAX_WAIT_SECONDS``, must raise
    ``CaseLockAcquisitionTimeoutError`` once that bound elapses -- never
    hang indefinitely. Uses its own even-shorter bound (not the module
    -level fixture's 5s) so this specific test stays fast."""
    monkeypatch.setattr(graph_mod, "_CASE_LOCK_MAX_WAIT_SECONDS", 0.3)
    case_id = uuid.uuid4()

    async def _hold_for_a_while() -> None:
        async with graph_mod._case_lock(case_id):  # noqa: SLF001
            await asyncio.sleep(2.0)

    holder_task = asyncio.create_task(_hold_for_a_while())
    await asyncio.sleep(0.05)  # let the holder genuinely acquire first

    try:
        with pytest.raises(graph_mod.CaseLockAcquisitionTimeoutError) as exc_info:
            async with graph_mod._case_lock(case_id):  # noqa: SLF001
                pass
        assert exc_info.value.case_id == case_id
        assert exc_info.value.attempts >= 1
        assert exc_info.value.elapsed_seconds >= 0.3
    finally:
        holder_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder_task


# ---------------------------------------------------------------------------
# Genuine dedicated-pool exhaustion (ADVISORY-4(b)'s own prerequisite) --
# proves the EXACT exception type the webhook-layer regression test
# (tests/test_webhooks_twilio_approve_by_sms.py::
# test_reply_1_when_handle_reply_raises_pages_sentry_and_falls_back_to_needs_eyes)
# simulates is what a genuinely exhausted pool actually raises.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_saturating_the_dedicated_pool_raises_sqlalchemy_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holds every connection the dedicated pool has (its own peak,
    ``_LOCK_POOL_SIZE + _LOCK_MAX_OVERFLOW``) open via a SEPARATE path
    (plain ``engine.connect()`` calls, not ``_case_lock`` itself), then
    asserts the very next ``_case_lock`` acquisition attempt raises a real
    ``sqlalchemy.exc.TimeoutError`` -- not a mocked stand-in. Monkeypatches
    the pool's own checkout timeout down to a fraction of a second so this
    test does not need to wait out the real (10s) production timeout.
    """
    engine = case_lock_pool_mod.case_lock_engine
    monkeypatch.setattr(engine.pool, "_timeout", 0.2)  # noqa: SLF001

    peak = case_lock_pool_mod._LOCK_POOL_SIZE + case_lock_pool_mod._LOCK_MAX_OVERFLOW  # noqa: SLF001
    held_connections: list[Any] = []
    try:
        for _ in range(peak):
            conn = await engine.connect()
            await conn.execute(text("SELECT 1"))
            held_connections.append(conn)

        with pytest.raises(SQLATimeoutError):
            async with graph_mod._case_lock(uuid.uuid4()):  # noqa: SLF001
                pass  # never reached -- the checkout itself times out first
    finally:
        for conn in held_connections:
            await conn.close()
