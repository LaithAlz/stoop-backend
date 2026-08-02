"""Tests for app/unrouted_maintenance.py (#231, follow-up from #170/PR
#230) — the unrouted_inbound retention sweep (deletes resolved-and-old
rows only, in bounded/deadline-checked batches) and the once-per-UTC-day
operator digest (Sentry WARNING summarizing unresolved rows).

Pure-function tests (``unit``, no DB): the retention cutoff boundary and
the digest day-key. Everything that touches ``unrouted_inbound`` itself is
``integration`` — real Postgres, same docker-compose harness every other
integration test module here uses; every Sentry call is mocked, zero real
network.

Scripted time throughout: every test that cares about "how old" passes an
explicit ``now=``, and the deadline/batch tests use an injectable
``time_source`` (never real ``asyncio.sleep``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
import app.unrouted_maintenance as unrouted_maintenance_mod
from app.unrouted_maintenance import (
    _digest_day_key,
    _retention_cutoff,
    run_unrouted_maintenance_sweep,
)
from tests import factories

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"


def _get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", _DB_URL_DEFAULT)
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


def _alembic(*args: str) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env={**os.environ, "DATABASE_URL": _get_db_url()},
    )
    if result.returncode != 0:
        cmd = " ".join(args)
        raise RuntimeError(
            f"alembic {cmd!r} failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    _alembic("upgrade", "head")
    yield


@pytest_asyncio.fixture
async def db_engine(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine) as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def dispose_app_engine() -> AsyncGenerator[None, None]:
    """``run_unrouted_maintenance_sweep`` uses ``get_admin_session`` — the
    app's own module-level engine, separate from this file's ``db_engine``
    fixture. Same cross-event-loop hazard as
    ``tests/test_push_outbox_sweep.py``'s fixture of this name."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


async def _cleanup(session: AsyncSession, ids: list[str]) -> None:
    await session.rollback()
    if ids:
        await session.execute(
            text("DELETE FROM unrouted_inbound WHERE id = ANY(:ids)"), {"ids": ids}
        )
    await session.commit()


async def _existing_ids(session: AsyncSession, ids: list[str]) -> set[str]:
    rows = (
        (
            await session.execute(
                text("SELECT id FROM unrouted_inbound WHERE id = ANY(:ids)"), {"ids": ids}
            )
        )
        .scalars()
        .all()
    )
    return {str(r) for r in rows}


class _FakeClock:
    """A mutable, injectable time source — mirrors
    ``tests/test_push_outbox_sweep.py``'s own ``_FakeClock``. Never
    advances on its own; a test mutates ``.now`` explicitly."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _StepClock:
    """Advances by *step* every call — simulates wall-clock time passing
    ACROSS loop iterations (the retention loop has no external hook to
    mutate a clock mid-batch, since each batch is one DB round trip with
    no per-row callback), so this is what makes the "deadline trips after
    N batches" case scriptable without seeding hundreds of rows."""

    def __init__(self, start: float = 0.0, step: float = 0.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


# ---------------------------------------------------------------------------
# 1. Pure functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retention_cutoff_is_30_days_before_now() -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    assert _retention_cutoff(now) == now - timedelta(days=30)


@pytest.mark.unit
def test_digest_day_key_is_utc_calendar_date() -> None:
    now = datetime(2026, 8, 1, 23, 59, 59, tzinfo=UTC)
    assert _digest_day_key(now) == "2026-08-01"


@pytest.mark.unit
def test_digest_day_key_normalizes_non_utc_tzinfo_to_utc_date() -> None:
    """A timestamp that is 2026-08-02 in a +05:00 zone but still
    2026-08-01 in UTC must key on the UTC date, not the local one —
    unambiguous regardless of the caller's own tzinfo (module docstring)."""
    from datetime import timezone

    tz_plus5 = timezone(timedelta(hours=5))
    now = datetime(2026, 8, 2, 2, 0, 0, tzinfo=tz_plus5)  # 2026-08-01 21:00 UTC
    assert _digest_day_key(now) == "2026-08-01"


# ---------------------------------------------------------------------------
# 2. Retention — narrowing: resolved-and-old only, unresolved NEVER deleted
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_retention_deletes_resolved_row_older_than_30_days(
    db_session: AsyncSession,
) -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    old_resolved_id = await factories.insert_unrouted_inbound(
        db_session,
        received_at=now - timedelta(days=40),
        resolved_at=now - timedelta(days=31),
    )
    try:
        outcome = await run_unrouted_maintenance_sweep(now=now)
        assert str(old_resolved_id) in {str(i) for i in outcome.deleted_ids}

        remaining = await _existing_ids(db_session, [old_resolved_id])
        assert remaining == set()
    finally:
        await _cleanup(db_session, [old_resolved_id])


@pytest.mark.integration
async def test_retention_never_deletes_unresolved_row_regardless_of_age(
    db_session: AsyncSession,
) -> None:
    """The deliberate narrowing (module docstring): an unresolved row is
    NEVER auto-deleted, no matter how old — even 400 days old."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    very_old_unresolved_id = await factories.insert_unrouted_inbound(
        db_session,
        received_at=now - timedelta(days=400),
        resolved_at=None,
    )
    try:
        outcome = await run_unrouted_maintenance_sweep(now=now)
        assert str(very_old_unresolved_id) not in {str(i) for i in outcome.deleted_ids}

        remaining = await _existing_ids(db_session, [very_old_unresolved_id])
        assert remaining == {very_old_unresolved_id}
    finally:
        await _cleanup(db_session, [very_old_unresolved_id])


@pytest.mark.integration
async def test_retention_does_not_delete_recently_resolved_row(
    db_session: AsyncSession,
) -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    recently_resolved_id = await factories.insert_unrouted_inbound(
        db_session,
        received_at=now - timedelta(days=2),
        resolved_at=now - timedelta(days=1),
    )
    try:
        outcome = await run_unrouted_maintenance_sweep(now=now)
        assert str(recently_resolved_id) not in {str(i) for i in outcome.deleted_ids}

        remaining = await _existing_ids(db_session, [recently_resolved_id])
        assert remaining == {recently_resolved_id}
    finally:
        await _cleanup(db_session, [recently_resolved_id])


@pytest.mark.integration
async def test_retention_boundary_exactly_30_days_is_retained(
    db_session: AsyncSession,
) -> None:
    """Boundary semantics (module docstring, exclusive): a row resolved
    EXACTLY 30 days before `now` is retained -- only STRICTLY older than 30
    days is eligible."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    exactly_30_days_id = await factories.insert_unrouted_inbound(
        db_session,
        received_at=now - timedelta(days=31),
        resolved_at=now - timedelta(days=30),
    )
    try:
        outcome = await run_unrouted_maintenance_sweep(now=now)
        assert str(exactly_30_days_id) not in {str(i) for i in outcome.deleted_ids}

        remaining = await _existing_ids(db_session, [exactly_30_days_id])
        assert remaining == {exactly_30_days_id}
    finally:
        await _cleanup(db_session, [exactly_30_days_id])


@pytest.mark.integration
async def test_retention_boundary_one_second_past_30_days_is_deleted(
    db_session: AsyncSession,
) -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    just_past_30_days_id = await factories.insert_unrouted_inbound(
        db_session,
        received_at=now - timedelta(days=31),
        resolved_at=now - timedelta(days=30, seconds=1),
    )
    try:
        outcome = await run_unrouted_maintenance_sweep(now=now)
        assert str(just_past_30_days_id) in {str(i) for i in outcome.deleted_ids}

        remaining = await _existing_ids(db_session, [just_past_30_days_id])
        assert remaining == set()
    finally:
        await _cleanup(db_session, [just_past_30_days_id])


@pytest.mark.integration
async def test_retention_mixed_batch_deletes_only_eligible_rows(
    db_session: AsyncSession,
) -> None:
    """One sweep, three rows, three different fates -- proves the WHERE
    clause is exactly `resolved_at IS NOT NULL AND resolved_at < cutoff`,
    not some looser approximation."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    old_resolved_id = await factories.insert_unrouted_inbound(
        db_session, received_at=now - timedelta(days=40), resolved_at=now - timedelta(days=35)
    )
    recent_resolved_id = await factories.insert_unrouted_inbound(
        db_session, received_at=now - timedelta(days=2), resolved_at=now - timedelta(hours=1)
    )
    old_unresolved_id = await factories.insert_unrouted_inbound(
        db_session, received_at=now - timedelta(days=90), resolved_at=None
    )
    ids = [old_resolved_id, recent_resolved_id, old_unresolved_id]
    try:
        outcome = await run_unrouted_maintenance_sweep(now=now)
        deleted = {str(i) for i in outcome.deleted_ids}
        assert old_resolved_id in deleted
        assert recent_resolved_id not in deleted
        assert old_unresolved_id not in deleted

        remaining = await _existing_ids(db_session, ids)
        assert remaining == {recent_resolved_id, old_unresolved_id}
    finally:
        await _cleanup(db_session, ids)


# ---------------------------------------------------------------------------
# 3. Retention — bounded batches + deadline discipline
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_retention_batch_limit_is_respected(db_session: AsyncSession) -> None:
    """Three eligible rows, ``retention_batch_limit=1``, a clock that never
    trips the deadline -- the sweep must loop batch-by-batch until every
    eligible row is gone, never in one oversized statement."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    ids = [
        await factories.insert_unrouted_inbound(
            db_session,
            received_at=now - timedelta(days=40),
            resolved_at=now - timedelta(days=31),
        )
        for _ in range(3)
    ]
    try:
        clock = _FakeClock(start=0.0)
        outcome = await run_unrouted_maintenance_sweep(
            now=now, deadline_seconds=25.0, time_source=clock, retention_batch_limit=1
        )
        assert {str(i) for i in outcome.deleted_ids} == set(ids)

        remaining = await _existing_ids(db_session, ids)
        assert remaining == set()
    finally:
        await _cleanup(db_session, ids)


@pytest.mark.integration
async def test_retention_stops_at_deadline_then_resumes_next_tick(
    db_session: AsyncSession,
) -> None:
    """Three eligible rows, ``retention_batch_limit=1`` (forces three
    separate DELETE batches to drain them all), and a clock that trips the
    deadline right after the first batch: only ONE row is deleted THIS
    tick -- the other two are left alone (still resolved-and-old, still
    due) for the very next tick, never abandoned or lost."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    ids = [
        await factories.insert_unrouted_inbound(
            db_session,
            received_at=now - timedelta(days=40),
            resolved_at=now - timedelta(days=31),
        )
        for _ in range(3)
    ]
    try:
        # call1 = tick_start -> 0.0 (elapsed baseline)
        # call2 = deadline check before batch 1 -> 3.0 (elapsed 3.0 < 5.0, proceed)
        # call3 = deadline check before batch 2 -> 6.0 (elapsed 6.0 >= 5.0, STOP)
        tripping_clock = _StepClock(start=0.0, step=3.0)
        outcome = await run_unrouted_maintenance_sweep(
            now=now,
            deadline_seconds=5.0,
            time_source=tripping_clock,
            retention_batch_limit=1,
        )
        assert len(outcome.deleted_ids) == 1  # bounded: NOT all three this tick

        remaining_after_first_tick = await _existing_ids(db_session, ids)
        assert len(remaining_after_first_tick) == 2

        # The next tick call (a clock that never trips the deadline) drains
        # the leftover rows -- nothing was lost, only deferred.
        resume_clock = _FakeClock(start=0.0)
        outcome2 = await run_unrouted_maintenance_sweep(
            now=now, deadline_seconds=25.0, time_source=resume_clock, retention_batch_limit=1
        )
        assert len(outcome2.deleted_ids) == 2

        remaining_after_second_tick = await _existing_ids(db_session, ids)
        assert remaining_after_second_tick == set()
    finally:
        await _cleanup(db_session, ids)


# ---------------------------------------------------------------------------
# 4. Operator digest
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_digest_fires_for_unresolved_row_past_grace_period(
    db_session: AsyncSession,
) -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    aged_unresolved_id = await factories.insert_unrouted_inbound(
        db_session, received_at=now - timedelta(hours=2), resolved_at=None
    )
    try:
        with patch("app.unrouted_maintenance.sentry_sdk") as mock_sentry:
            outcome = await run_unrouted_maintenance_sweep(now=now)
        assert outcome.digest_fired is True
        mock_sentry.capture_message.assert_called_once()
        _, kwargs = mock_sentry.capture_message.call_args
        assert kwargs["level"] == "warning"
        assert kwargs["extras"]["unresolved_count"] == 1
        assert kwargs["extras"]["oldest_age_hours"] == pytest.approx(2.0, abs=0.05)
    finally:
        await _cleanup(db_session, [aged_unresolved_id])


@pytest.mark.integration
async def test_digest_respects_1h_grace_period(db_session: AsyncSession) -> None:
    """A row that just arrived (well within the 1h grace) must not trip
    the digest -- an operator may already be mid-reconciliation."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    fresh_unresolved_id = await factories.insert_unrouted_inbound(
        db_session, received_at=now - timedelta(minutes=30), resolved_at=None
    )
    try:
        with patch("app.unrouted_maintenance.sentry_sdk") as mock_sentry:
            outcome = await run_unrouted_maintenance_sweep(now=now)
        assert outcome.digest_fired is False
        mock_sentry.capture_message.assert_not_called()
    finally:
        await _cleanup(db_session, [fresh_unresolved_id])


@pytest.mark.integration
async def test_digest_zero_unresolved_rows_does_not_fire(db_session: AsyncSession) -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    with patch("app.unrouted_maintenance.sentry_sdk") as mock_sentry:
        outcome = await run_unrouted_maintenance_sweep(now=now)
    assert outcome.digest_fired is False
    mock_sentry.capture_message.assert_not_called()


@pytest.mark.integration
async def test_digest_zero_count_does_not_consume_the_daily_stamp(
    db_session: AsyncSession,
) -> None:
    """A zero-count tick must NOT mark today as "already digested" -- a
    later same-day tick, once a row ages past the grace period, must still
    be able to fire."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    with patch("app.unrouted_maintenance.sentry_sdk") as mock_sentry_empty:
        outcome_empty = await run_unrouted_maintenance_sweep(now=now)
    assert outcome_empty.digest_fired is False
    mock_sentry_empty.capture_message.assert_not_called()

    aged_unresolved_id = await factories.insert_unrouted_inbound(
        db_session, received_at=now - timedelta(hours=2), resolved_at=None
    )
    try:
        later_same_day = now + timedelta(hours=1)
        with patch("app.unrouted_maintenance.sentry_sdk") as mock_sentry_later:
            outcome_later = await run_unrouted_maintenance_sweep(now=later_same_day)
        assert outcome_later.digest_fired is True
        mock_sentry_later.capture_message.assert_called_once()
    finally:
        await _cleanup(db_session, [aged_unresolved_id])


@pytest.mark.integration
async def test_digest_second_call_same_day_is_a_noop(db_session: AsyncSession) -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    aged_unresolved_id = await factories.insert_unrouted_inbound(
        db_session, received_at=now - timedelta(hours=2), resolved_at=None
    )
    try:
        with patch("app.unrouted_maintenance.sentry_sdk") as mock_sentry:
            outcome1 = await run_unrouted_maintenance_sweep(now=now)
            assert outcome1.digest_fired is True

            later_same_day = now + timedelta(hours=3)
            outcome2 = await run_unrouted_maintenance_sweep(now=later_same_day)
            assert outcome2.digest_fired is False

        mock_sentry.capture_message.assert_called_once()
    finally:
        await _cleanup(db_session, [aged_unresolved_id])


@pytest.mark.integration
async def test_digest_fires_again_the_next_utc_day(db_session: AsyncSession) -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    aged_unresolved_id = await factories.insert_unrouted_inbound(
        db_session, received_at=now - timedelta(hours=2), resolved_at=None
    )
    try:
        with patch("app.unrouted_maintenance.sentry_sdk") as mock_sentry:
            outcome1 = await run_unrouted_maintenance_sweep(now=now)
            assert outcome1.digest_fired is True

            next_day = now + timedelta(days=1)
            outcome2 = await run_unrouted_maintenance_sweep(now=next_day)
            assert outcome2.digest_fired is True

        assert mock_sentry.capture_message.call_count == 2
    finally:
        await _cleanup(db_session, [aged_unresolved_id])


@pytest.mark.integration
async def test_digest_never_counts_resolved_rows(db_session: AsyncSession) -> None:
    """A resolved row, however old, must never contribute to the digest's
    unresolved count -- the digest and retention read disjoint sets."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    old_resolved_id = await factories.insert_unrouted_inbound(
        db_session, received_at=now - timedelta(days=5), resolved_at=now - timedelta(hours=2)
    )
    try:
        with patch("app.unrouted_maintenance.sentry_sdk") as mock_sentry:
            outcome = await run_unrouted_maintenance_sweep(now=now)
        assert outcome.digest_fired is False
        mock_sentry.capture_message.assert_not_called()
    finally:
        await _cleanup(db_session, [old_resolved_id])


@pytest.mark.integration
async def test_digest_never_logs_phone_numbers_or_twilio_sid(db_session: AsyncSession) -> None:
    """Rule #5 -- the digest's own Sentry payload must carry only a count
    and a duration, never `from_number`/`to_number`/`twilio_sid`/payload
    content."""
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    aged_unresolved_id = await factories.insert_unrouted_inbound(
        db_session,
        twilio_sid="SMtestsid00000000000000000000000",
        from_number="+14165551234",
        to_number="+14165559999",
        received_at=now - timedelta(hours=2),
        resolved_at=None,
    )
    try:
        with patch("app.unrouted_maintenance.sentry_sdk") as mock_sentry:
            await run_unrouted_maintenance_sweep(now=now)
        _, kwargs = mock_sentry.capture_message.call_args
        extras = kwargs["extras"]
        assert set(extras.keys()) == {"unresolved_count", "oldest_age_hours"}
        assert "+14165551234" not in str(extras)
        assert "+14165559999" not in str(extras)
        assert "SMtestsid00000000000000000000000" not in str(extras)
    finally:
        await _cleanup(db_session, [aged_unresolved_id])


# ---------------------------------------------------------------------------
# 5. Digest dedupe stamp reset seam (conftest's autouse fixture covers this
# in every OTHER test file; this proves the seam itself works in isolation)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reset_for_tests_clears_the_daily_stamp() -> None:
    unrouted_maintenance_mod._digest_state.last_fired_day = "2026-08-01"  # noqa: SLF001
    unrouted_maintenance_mod.reset_for_tests()
    assert unrouted_maintenance_mod._digest_state.last_fired_day is None  # noqa: SLF001
