"""Unit tests for ``app/scheduler.py`` (#108/#109) — the 60-second
lifespan-managed ticker driving the emergency chain sweep, the SMS drain
sweep, and the degraded-mode retry sweep.

No DB, no real sleep: ``_run_one_tick`` (the per-tick body) is tested
directly with all three sweep functions mocked — the ticker LOOP itself
(``asyncio.sleep`` + repeat) is intentionally not exercised end-to-end
here (that would mean a real-time test); ``start_scheduler``/
``stop_scheduler`` are tested for their task-lifecycle mechanics only.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import app.scheduler as scheduler_mod


@pytest.mark.unit
async def test_run_one_tick_calls_all_three_sweeps() -> None:
    fake_emergency = AsyncMock()
    fake_sms_drain = AsyncMock()
    fake_degraded = AsyncMock()
    with (
        patch("app.scheduler.run_emergency_chain_sweep", new=fake_emergency),
        patch("app.scheduler.run_sms_drain_sweep", new=fake_sms_drain),
        patch("app.scheduler.sweep_degraded_mode_retries", new=fake_degraded),
    ):
        await scheduler_mod._run_one_tick()  # noqa: SLF001

    fake_emergency.assert_awaited_once()
    fake_sms_drain.assert_awaited_once()
    fake_degraded.assert_awaited_once()


@pytest.mark.unit
async def test_run_one_tick_emergency_failure_does_not_prevent_other_sweeps() -> None:
    fake_emergency = AsyncMock(side_effect=RuntimeError("emergency sweep boom"))
    fake_sms_drain = AsyncMock()
    fake_degraded = AsyncMock()
    with (
        patch("app.scheduler.run_emergency_chain_sweep", new=fake_emergency),
        patch("app.scheduler.run_sms_drain_sweep", new=fake_sms_drain),
        patch("app.scheduler.sweep_degraded_mode_retries", new=fake_degraded),
        patch("app.scheduler.sentry_sdk") as mock_sentry,
    ):
        await scheduler_mod._run_one_tick()  # noqa: SLF001

    fake_sms_drain.assert_awaited_once()
    fake_degraded.assert_awaited_once()
    mock_sentry.capture_message.assert_called_once()


@pytest.mark.unit
async def test_run_one_tick_sms_drain_failure_does_not_prevent_other_sweeps() -> None:
    fake_emergency = AsyncMock()
    fake_sms_drain = AsyncMock(side_effect=RuntimeError("sms drain boom"))
    fake_degraded = AsyncMock()
    with (
        patch("app.scheduler.run_emergency_chain_sweep", new=fake_emergency),
        patch("app.scheduler.run_sms_drain_sweep", new=fake_sms_drain),
        patch("app.scheduler.sweep_degraded_mode_retries", new=fake_degraded),
        patch("app.scheduler.sentry_sdk") as mock_sentry,
    ):
        await scheduler_mod._run_one_tick()  # noqa: SLF001

    fake_emergency.assert_awaited_once()
    fake_degraded.assert_awaited_once()
    mock_sentry.capture_message.assert_called_once()


@pytest.mark.unit
async def test_run_one_tick_degraded_failure_is_isolated_from_other_sweeps() -> None:
    fake_emergency = AsyncMock()
    fake_sms_drain = AsyncMock()
    fake_degraded = AsyncMock(side_effect=RuntimeError("degraded sweep boom"))
    with (
        patch("app.scheduler.run_emergency_chain_sweep", new=fake_emergency),
        patch("app.scheduler.run_sms_drain_sweep", new=fake_sms_drain),
        patch("app.scheduler.sweep_degraded_mode_retries", new=fake_degraded),
        patch("app.scheduler.sentry_sdk") as mock_sentry,
    ):
        await scheduler_mod._run_one_tick()  # noqa: SLF001

    fake_emergency.assert_awaited_once()
    fake_sms_drain.assert_awaited_once()
    mock_sentry.capture_message.assert_called_once()


@pytest.mark.unit
async def test_run_one_tick_survives_a_raising_sentry_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety review, 2026-07-12 (finding 4, MEDIUM): if
    ``sentry_sdk.capture_message`` ITSELF raises (a broken transport) while
    reporting a sweep's failure, the tick must still complete normally —
    the failure-reporting path can never escape and kill the loop."""
    fake_emergency = AsyncMock(side_effect=RuntimeError("emergency sweep boom"))
    fake_sms_drain = AsyncMock()
    fake_degraded = AsyncMock()

    def _raising_capture_message(*args: object, **kwargs: object) -> None:
        raise RuntimeError("sentry transport is down")

    with (
        patch("app.scheduler.run_emergency_chain_sweep", new=fake_emergency),
        patch("app.scheduler.run_sms_drain_sweep", new=fake_sms_drain),
        patch("app.scheduler.sweep_degraded_mode_retries", new=fake_degraded),
        patch("app.scheduler.sentry_sdk.capture_message", side_effect=_raising_capture_message),
    ):
        # Must not raise -- this is the whole point of the test.
        await scheduler_mod._run_one_tick()  # noqa: SLF001

    fake_sms_drain.assert_awaited_once()
    fake_degraded.assert_awaited_once()


@pytest.mark.unit
async def test_run_one_tick_survives_a_raising_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guarantee as above, for a broken ``log.error`` call itself."""
    fake_emergency = AsyncMock(side_effect=RuntimeError("emergency sweep boom"))
    fake_sms_drain = AsyncMock()
    fake_degraded = AsyncMock()

    with (
        patch("app.scheduler.run_emergency_chain_sweep", new=fake_emergency),
        patch("app.scheduler.run_sms_drain_sweep", new=fake_sms_drain),
        patch("app.scheduler.sweep_degraded_mode_retries", new=fake_degraded),
        patch("app.scheduler.log") as mock_log,
    ):
        mock_log.error.side_effect = RuntimeError("logging pipe is broken")
        await scheduler_mod._run_one_tick()  # noqa: SLF001

    fake_sms_drain.assert_awaited_once()
    fake_degraded.assert_awaited_once()


@pytest.mark.unit
async def test_on_ticker_task_done_ignores_clean_cancellation() -> None:
    """A ``stop_scheduler()``-initiated cancellation must never trigger a
    restart."""

    async def _never_ending() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_never_ending())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    with patch("app.scheduler.start_scheduler") as mock_start:
        scheduler_mod._on_ticker_task_done(task)  # noqa: SLF001
    mock_start.assert_not_called()


@pytest.mark.unit
async def test_ticker_task_auto_restarts_after_unexpected_exception() -> None:
    """Safety review, 2026-07-12 (finding 4, MEDIUM): if the ticker task
    ever exits non-cancelled (structurally shouldn't happen given
    ``_run_one_tick``'s own guarantees, but this is the defense-in-depth
    for "shouldn't happen" bugs), the done-callback restarts it rather
    than silently leaving the emergency chain unswept forever."""
    call_count = 0

    async def _flaky_loop(interval_seconds: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("the ticker loop itself blew up, just this once")
        await asyncio.Event().wait()  # the "restarted" task just waits to be cancelled

    with (
        patch("app.scheduler._ticker_loop", new=_flaky_loop),
        patch("app.scheduler.sentry_sdk") as mock_sentry,
    ):
        scheduler_mod.start_scheduler()
        first_task = scheduler_mod._task  # noqa: SLF001
        # Wait for the SECOND (restarted) task to actually start running --
        # not just for _task's identity to change, which happens as soon as
        # start_scheduler() reassigns it, before the new coroutine has had
        # a chance to run its own first line.
        for _ in range(50):
            await asyncio.sleep(0)
            if call_count >= 2:
                break

        try:
            assert scheduler_mod._task is not None  # noqa: SLF001
            assert scheduler_mod._task is not first_task  # noqa: SLF001
            assert not scheduler_mod._task.done()  # noqa: SLF001
            assert call_count == 2
            mock_sentry.capture_message.assert_called_once()
        finally:
            await scheduler_mod.stop_scheduler()


@pytest.mark.unit
async def test_start_scheduler_creates_a_running_task() -> None:
    scheduler_mod.start_scheduler()
    try:
        assert scheduler_mod._task is not None  # noqa: SLF001
        assert not scheduler_mod._task.done()  # noqa: SLF001
    finally:
        await scheduler_mod.stop_scheduler()


@pytest.mark.unit
async def test_start_scheduler_is_idempotent() -> None:
    scheduler_mod.start_scheduler()
    first_task = scheduler_mod._task  # noqa: SLF001
    try:
        scheduler_mod.start_scheduler()
        assert scheduler_mod._task is first_task  # noqa: SLF001
    finally:
        await scheduler_mod.stop_scheduler()


@pytest.mark.unit
async def test_stop_scheduler_cancels_and_clears_the_task() -> None:
    scheduler_mod.start_scheduler()
    task = scheduler_mod._task  # noqa: SLF001
    assert task is not None

    await scheduler_mod.stop_scheduler()

    assert task.done()
    assert task.cancelled()
    assert scheduler_mod._task is None  # noqa: SLF001


@pytest.mark.unit
async def test_stop_scheduler_when_never_started_is_a_safe_no_op() -> None:
    scheduler_mod.reset_scheduler_for_tests()
    await scheduler_mod.stop_scheduler()  # must not raise
    assert scheduler_mod._task is None  # noqa: SLF001


@pytest.mark.unit
async def test_ticker_loop_ticks_repeatedly_at_the_given_interval() -> None:
    """Proves the loop actually sleeps between ticks and calls the
    per-tick body each time — using a tiny REAL interval (0.01s), never
    the real 60s :data:`scheduler_mod.TICK_INTERVAL_SECONDS`, so this test
    stays fast without needing to monkeypatch ``asyncio.sleep`` itself
    (which would risk starving the event loop of real yield points)."""
    tick_count = 0

    async def _fake_tick() -> None:
        nonlocal tick_count
        tick_count += 1

    with patch("app.scheduler._run_one_tick", new=_fake_tick):
        task = asyncio.create_task(scheduler_mod._ticker_loop(0.01))  # noqa: SLF001
        await asyncio.sleep(0.1)  # real, tiny wait -- enough for several ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert tick_count >= 3


# ---------------------------------------------------------------------------
# Lifespan wiring — app.main._lifespan actually starts/stops the scheduler,
# in the right order relative to the checkpointer/role-separation checks.
# Mirrors tests/test_role_separation_check.py's own
# test_lifespan_calls_verify_request_engine_role_separation.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_lifespan_starts_scheduler_after_checkpointer_and_stops_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def _fake_verify() -> None:
        order.append("verify")

    async def _fake_setup_checkpointer() -> None:
        order.append("checkpointer_setup")

    def _fake_start_scheduler() -> None:
        order.append("scheduler_start")

    async def _fake_stop_scheduler() -> None:
        order.append("scheduler_stop")

    async def _fake_close_checkpointer() -> None:
        order.append("checkpointer_close")

    monkeypatch.setattr("app.main.verify_request_engine_role_separation", _fake_verify)
    monkeypatch.setattr("app.main.setup_checkpointer", _fake_setup_checkpointer)
    monkeypatch.setattr("app.main.start_scheduler", _fake_start_scheduler)
    monkeypatch.setattr("app.main.stop_scheduler", _fake_stop_scheduler)
    monkeypatch.setattr("app.main.close_checkpointer", _fake_close_checkpointer)

    from app.main import _lifespan
    from app.main import app as fastapi_app

    async with _lifespan(fastapi_app):
        pass

    assert order == [
        "verify",
        "checkpointer_setup",
        "scheduler_start",
        "scheduler_stop",
        "checkpointer_close",
    ]
