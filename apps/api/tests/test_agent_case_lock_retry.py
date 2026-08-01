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
async def test_chatty_case_backlog_does_not_starve_unrelated_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the reviewer's own repro shape, tuned to actually
    DISCRIMINATE (safety review, #186 follow-up round, ADVISORY-4
    remainder — the earlier relative-timing version of this test could
    pass even under the pre-BLOCKING-2 shape on a fast enough machine;
    this version makes the outcome BINARY instead of timing-dependent):
    12 messages racing for the SAME (hot) case_id, genuinely serialized
    (correct, each holding for 0.3s -- long enough that 12 of them
    cannot fully drain inside this test's shrunk pool-checkout window),
    running CONCURRENTLY with 8 UNRELATED, distinct-case messages that
    have nothing to do with the hot case's contention.

    The discriminating technique (same one already used at
    ``test_saturating_the_dedicated_pool_raises_sqlalchemy_timeout_error``
    above): shrink the DEDICATED POOL's own checkout timeout
    (``case_lock_engine.pool._timeout``) to 0.5s. Under THIS fix, waiting
    for a contended lock never pins a pool connection at all (module
    docstring "Bounded try-lock retries, not a blocking wait"), so the 8
    unrelated, uncontended cases each need the pool for only a brief,
    uncontended instant regardless of how deep the hot backlog is --
    all 8 succeed. Under the PRE-BLOCKING-2 shape (a single blocking
    ``pg_advisory_xact_lock`` call issued AFTER an already-checked-out
    connection), the 12 hot-case waiters alone would pin the pool for the
    hot chain's own multi-second drain, so an unrelated case's OWN
    checkout attempt would sit in the pool's queue past the shrunk 0.5s
    timeout and raise ``sqlalchemy.exc.TimeoutError`` -- 0/8 would
    succeed. Binary (8/8 vs 0/8), not a fuzzy timing ratio, and the whole
    test completes in well under 4s (12 * 0.3s hot chain + retry-loop
    overhead, comfortably bounded).

    Pool warm-up, BEFORE shrinking the timeout: ``tests/conftest.py``'s
    autouse ``_reset_case_lock_pool`` disposes this pool before every
    test, so it starts genuinely cold. Establishing a brand-new physical
    connection in this environment measurably takes real time (observed
    directly, several hundred ms — see ``tests/test_agent_case_lock_
    pool.py``'s own docstring on the ADVISORY-1 churn measurement); a
    burst of 20 simultaneous FIRST-ever checkouts against a cold pool can
    occasionally exceed a 0.5s timeout on connection-establishment
    latency ALONE, unrelated to this fix's own correctness — a genuine
    flake class, not a signal. Forcing all
    :data:`case_lock_pool_mod._LOCK_POOL_SIZE` connections to be
    established once, up front, under the pool's UNMODIFIED default
    timeout, isolates the actual variable under test (lock contention
    behavior) from one-time connection-establishment variance.
    """
    engine = case_lock_pool_mod.case_lock_engine
    warm_up_case_ids = [uuid.uuid4() for _ in range(case_lock_pool_mod._LOCK_POOL_SIZE)]  # noqa: SLF001
    await asyncio.gather(
        *(_hold_case_lock_briefly(cid, hold_seconds=0.01) for cid in warm_up_case_ids)
    )

    monkeypatch.setattr(engine.pool, "_timeout", 0.5)  # noqa: SLF001

    hot_case_id = uuid.uuid4()
    hot_hold_seconds = 0.3
    hot_message_count = 12
    unrelated_case_count = 8

    unrelated_case_ids = [uuid.uuid4() for _ in range(unrelated_case_count)]

    async def _unrelated(case_id: uuid.UUID) -> None:
        await _hold_case_lock_briefly(case_id, hold_seconds=0.01)

    async def _hot() -> None:
        await _hold_case_lock_briefly(hot_case_id, hold_seconds=hot_hold_seconds)

    start = time.monotonic()
    unrelated_results = await asyncio.gather(
        *(_hot() for _ in range(hot_message_count)),
        *(_unrelated(cid) for cid in unrelated_case_ids),
        return_exceptions=True,
    )
    elapsed = time.monotonic() - start

    hot_results = unrelated_results[:hot_message_count]
    unrelated_only_results = unrelated_results[hot_message_count:]

    unrelated_failures = [r for r in unrelated_only_results if isinstance(r, BaseException)]
    assert not unrelated_failures, (
        f"{len(unrelated_failures)}/{unrelated_case_count} unrelated, uncontended cases were "
        f"starved behind the hot case's own backlog: {unrelated_failures!r}"
    )

    # The hot case's own chain still eventually, correctly, fully drains --
    # genuine serialization under contention still works, this fix did not
    # trade correctness for the head-of-line fix. (A hot-case attempt
    # COULD, in principle, itself hit the shrunk 0.5s pool timeout while
    # WAITING for its own turn behind 11 others under the bounded retry
    # loop's own -- separate -- _CASE_LOCK_MAX_WAIT_SECONDS bound, which
    # this test's autouse fixture already set to 5.0s, comfortably above
    # the ~3.6s the hot chain itself needs; the assertion below is the
    # actual regression proof, not a tautology.)
    hot_failures = [r for r in hot_results if isinstance(r, BaseException)]
    assert not hot_failures, f"hot-case messages unexpectedly failed: {hot_failures!r}"

    # Sanity bound, not the load-bearing assertion (the failure checks
    # above are): the hot chain's own theoretical minimum is
    # hot_message_count * hot_hold_seconds (~3.6s) -- this environment's
    # own one-time connection-establishment cost for a freshly-reset pool
    # (tests/conftest.py's autouse _reset_case_lock_pool disposes it
    # before every test) adds a further, empirically observed, ~0.5s on
    # top locally. A generous ceiling here still catches a genuine hang
    # (the OLD, pre-BLOCKING-2 shape would have FAILED fast with
    # TimeoutErrors well before this bound, never merely run long) without
    # being flaky against normal environment variance.
    assert elapsed < 6.0, f"test took {elapsed:.2f}s -- expected well under 6s"


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
