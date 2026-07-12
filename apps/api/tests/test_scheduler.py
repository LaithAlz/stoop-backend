"""Unit tests for ``app/scheduler.py`` (#108/#109) — the 60-second
lifespan-managed ticker driving both the emergency chain sweep and the
degraded-mode retry sweep.

No DB, no real sleep: ``_run_one_tick`` (the per-tick body) is tested
directly with both sweep functions mocked — the ticker LOOP itself
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
async def test_run_one_tick_calls_both_sweeps() -> None:
    fake_emergency = AsyncMock()
    fake_degraded = AsyncMock()
    with (
        patch("app.scheduler.run_emergency_chain_sweep", new=fake_emergency),
        patch("app.scheduler.sweep_degraded_mode_retries", new=fake_degraded),
    ):
        await scheduler_mod._run_one_tick()  # noqa: SLF001

    fake_emergency.assert_awaited_once()
    fake_degraded.assert_awaited_once()


@pytest.mark.unit
async def test_run_one_tick_emergency_failure_does_not_prevent_degraded_sweep() -> None:
    fake_emergency = AsyncMock(side_effect=RuntimeError("emergency sweep boom"))
    fake_degraded = AsyncMock()
    with (
        patch("app.scheduler.run_emergency_chain_sweep", new=fake_emergency),
        patch("app.scheduler.sweep_degraded_mode_retries", new=fake_degraded),
        patch("app.scheduler.sentry_sdk") as mock_sentry,
    ):
        await scheduler_mod._run_one_tick()  # noqa: SLF001

    fake_degraded.assert_awaited_once()
    mock_sentry.capture_message.assert_called_once()


@pytest.mark.unit
async def test_run_one_tick_degraded_failure_is_isolated_from_emergency_sweep() -> None:
    fake_emergency = AsyncMock()
    fake_degraded = AsyncMock(side_effect=RuntimeError("degraded sweep boom"))
    with (
        patch("app.scheduler.run_emergency_chain_sweep", new=fake_emergency),
        patch("app.scheduler.sweep_degraded_mode_retries", new=fake_degraded),
        patch("app.scheduler.sentry_sdk") as mock_sentry,
    ):
        await scheduler_mod._run_one_tick()  # noqa: SLF001

    fake_emergency.assert_awaited_once()
    mock_sentry.capture_message.assert_called_once()


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
