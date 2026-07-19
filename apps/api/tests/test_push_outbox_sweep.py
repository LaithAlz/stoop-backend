"""Tests for app/push_outbox.py (#210 M3) — the push-notification outbox
sweep: CAS claim (+ double-claim race), Expo's DeviceNotRegistered
pruning, bounded-retry exhaustion, and payload safety (never tenant
names/phones/message bodies).

Marker: ``integration`` — real Postgres, same docker-compose harness every
other integration test module here uses. Every Expo call is faked via
``set_expo_push_sender_for_tests`` — zero real network, ever.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
import app.push_outbox as push_outbox_mod
from app.integrations.expo_push import (
    DEVICE_NOT_REGISTERED_ERROR_CODE,
    ExpoPushMessage,
    ExpoPushTicket,
    set_expo_push_sender_for_tests,
)
from app.push_outbox import run_push_outbox_sweep
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
    """``run_push_outbox_sweep`` uses ``get_admin_session`` — the app's own
    module-level engine, separate from this file's ``db_engine`` fixture.
    Same cross-event-loop hazard as
    ``tests/test_property_provisioning.py``'s fixture of this name."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


@dataclass
class _FakeExpoPushSender:
    """Records every call; per-token canned outcome (a ticket or an
    exception to raise), default 'ok'."""

    responses: dict[str, ExpoPushTicket | Exception] = field(default_factory=dict)
    calls: list[ExpoPushMessage] = field(default_factory=list)

    async def send_push(self, message: ExpoPushMessage) -> ExpoPushTicket:
        self.calls.append(message)
        outcome = self.responses.get(message.to, ExpoPushTicket(status="ok"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake_sender() -> Generator[_FakeExpoPushSender, None, None]:
    fake = _FakeExpoPushSender()
    set_expo_push_sender_for_tests(fake)
    try:
        yield fake
    finally:
        set_expo_push_sender_for_tests(None)


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.rollback()
    await session.execute(
        text("DELETE FROM push_outbox WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM push_tokens WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


async def _seed_pending_row(
    session: AsyncSession, *, revoked_at: object = None
) -> tuple[str, str, str]:
    landlord_id = await factories.insert_landlord(session)
    push_token_id = await factories.insert_push_token(
        session, landlord_id=landlord_id, revoked_at=revoked_at
    )
    outbox_id = await factories.insert_push_outbox(
        session,
        landlord_id=landlord_id,
        device_token_id=push_token_id,
        payload={"case_id": str(uuid.uuid4()), "draft_id": str(uuid.uuid4())},
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    return landlord_id, push_token_id, outbox_id


async def _row_status(session: AsyncSession, outbox_id: str) -> tuple[str, int]:
    row = (
        (
            await session.execute(
                text("SELECT status, attempt FROM push_outbox WHERE id = :id"), {"id": outbox_id}
            )
        )
        .mappings()
        .one()
    )
    return row["status"], row["attempt"]


# ---------------------------------------------------------------------------
# 1. Success path
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sweep_sends_pending_row_and_marks_sent(
    db_session: AsyncSession, fake_sender: _FakeExpoPushSender
) -> None:
    landlord_id, _push_token_id, outbox_id = await _seed_pending_row(db_session)
    try:
        outcomes = await run_push_outbox_sweep()
        assert [o.outcome for o in outcomes] == ["sent"]
        assert len(fake_sender.calls) == 1

        status, attempt = await _row_status(db_session, outbox_id)
        assert status == "sent"
        assert attempt == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_generic_title_and_body_never_reflect_payload(
    db_session: AsyncSession, fake_sender: _FakeExpoPushSender
) -> None:
    """The push notification's own title/body must be FIXED, generic
    copy — never derived from payload (schema-v1.md's v1.13 amendments)."""
    landlord_id, _push_token_id, _outbox_id = await _seed_pending_row(db_session)
    try:
        await run_push_outbox_sweep()
        assert len(fake_sender.calls) == 1
        sent = fake_sender.calls[0]
        assert sent.title == "Stoop"
        assert sent.body == "A reply is waiting for your approval."
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2. Double-claim race — asyncio.gather, house pattern
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_double_claim_race_exactly_one_sweep_wins(
    db_session: AsyncSession, fake_sender: _FakeExpoPushSender
) -> None:
    landlord_id, _push_token_id, outbox_id = await _seed_pending_row(db_session)
    try:
        results = await asyncio.gather(run_push_outbox_sweep(), run_push_outbox_sweep())
        combined = results[0] + results[1]
        outcomes_for_row = sorted(o.outcome for o in combined if str(o.outbox_id) == outbox_id)
        assert outcomes_for_row == ["lost_race", "sent"]
        assert len(fake_sender.calls) == 1  # Expo's send endpoint hit exactly once

        status, attempt = await _row_status(db_session, outbox_id)
        assert status == "sent"
        assert attempt == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 3. DeviceNotRegistered — device revoked, row terminally failed
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_device_not_registered_revokes_token_and_fails_row(
    db_session: AsyncSession, fake_sender: _FakeExpoPushSender
) -> None:
    landlord_id, push_token_id, outbox_id = await _seed_pending_row(db_session)
    try:
        row = (
            (
                await db_session.execute(
                    text("SELECT token FROM push_tokens WHERE id = :id"), {"id": push_token_id}
                )
            )
            .mappings()
            .one()
        )
        fake_sender.responses[row["token"]] = ExpoPushTicket(
            status="error", error_code=DEVICE_NOT_REGISTERED_ERROR_CODE
        )

        outcomes = await run_push_outbox_sweep()
        assert [o.outcome for o in outcomes] == ["failed_device_not_registered"]

        status, _attempt = await _row_status(db_session, outbox_id)
        assert status == "failed"

        revoked_at = (
            await db_session.execute(
                text("SELECT revoked_at FROM push_tokens WHERE id = :id"), {"id": push_token_id}
            )
        ).scalar_one()
        assert revoked_at is not None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_already_revoked_device_skips_expo_call_and_fails_row(
    db_session: AsyncSession, fake_sender: _FakeExpoPushSender
) -> None:
    """A device revoked BETWEEN enqueue and sweep (by an earlier tick) —
    never even attempt the Expo call."""
    landlord_id, _push_token_id, outbox_id = await _seed_pending_row(
        db_session, revoked_at=datetime.now(UTC)
    )
    try:
        outcomes = await run_push_outbox_sweep()
        assert [o.outcome for o in outcomes] == ["failed_revoked_device"]
        assert fake_sender.calls == []

        status, _attempt = await _row_status(db_session, outbox_id)
        assert status == "failed"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 4. Bounded-retry exhaustion — transient failures, no Sentry page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_transient_failures_exhaust_after_max_attempts(
    db_session: AsyncSession, fake_sender: _FakeExpoPushSender
) -> None:
    landlord_id, push_token_id, outbox_id = await _seed_pending_row(db_session)
    try:
        row = (
            (
                await db_session.execute(
                    text("SELECT token FROM push_tokens WHERE id = :id"), {"id": push_token_id}
                )
            )
            .mappings()
            .one()
        )
        fake_sender.responses[row["token"]] = RuntimeError("transient expo failure")

        current_now = datetime.now(UTC) - timedelta(seconds=1)
        max_attempts = push_outbox_mod._PUSH_MAX_ATTEMPTS  # noqa: SLF001
        retry_interval = push_outbox_mod._PUSH_RETRY_INTERVAL_SECONDS  # noqa: SLF001

        last_outcomes = []
        for _ in range(max_attempts):
            last_outcomes = await run_push_outbox_sweep(now=current_now)
            current_now = current_now + timedelta(seconds=retry_interval + 1)

        assert [o.outcome for o in last_outcomes] == ["exhausted"]
        status, attempt = await _row_status(db_session, outbox_id)
        assert status == "exhausted"
        assert attempt == max_attempts
        assert len(fake_sender.calls) == max_attempts
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_transient_failure_before_last_attempt_reschedules_not_exhausts(
    db_session: AsyncSession, fake_sender: _FakeExpoPushSender
) -> None:
    landlord_id, push_token_id, outbox_id = await _seed_pending_row(db_session)
    try:
        row = (
            (
                await db_session.execute(
                    text("SELECT token FROM push_tokens WHERE id = :id"), {"id": push_token_id}
                )
            )
            .mappings()
            .one()
        )
        fake_sender.responses[row["token"]] = RuntimeError("transient expo failure")

        outcomes = await run_push_outbox_sweep()
        assert [o.outcome for o in outcomes] == ["rescheduled"]

        status, attempt = await _row_status(db_session, outbox_id)
        assert status == "pending"
        assert attempt == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.unit
def test_push_outbox_module_never_pages_sentry_on_exhaustion() -> None:
    """Deliberate divergence from every other sweep in this codebase
    (module docstring "Deliberate divergence") — proved structurally: this
    module never even imports/references sentry_sdk, so there is nothing
    that could page on exhaustion."""
    source = inspect.getsource(push_outbox_mod)
    assert "sentry_sdk" not in source


# ---------------------------------------------------------------------------
# 5. Payload safety — never tenant names/phones/message bodies
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_message_never_forwards_anything_but_uuids() -> None:
    """Even if ``payload`` somehow carried sensitive keys (a hypothetical
    future bug elsewhere), ``_build_message`` reads ONLY ``case_id``/
    ``draft_id`` — everything else is silently dropped, never forwarded to
    Expo's servers."""
    dirty_payload = {
        "case_id": "11111111-1111-1111-1111-111111111111",
        "draft_id": "22222222-2222-2222-2222-222222222222",
        "tenant_name": "Maria Gonzalez",
        "tenant_phone": "+14165551234",
        "message_body": "the toilet is overflowing help",
    }
    message = push_outbox_mod._build_message(  # noqa: SLF001
        kind="draft_awaiting_approval",
        payload=dirty_payload,
        token="ExponentPushToken[abc]",  # noqa: S106 -- test fixture, not a secret
    )

    assert message.title == "Stoop"
    assert message.body == "A reply is waiting for your approval."
    assert message.data == {
        "kind": "draft_awaiting_approval",
        "case_id": dirty_payload["case_id"],
        "draft_id": dirty_payload["draft_id"],
    }
    assert "tenant_name" not in message.data.values()
    assert "tenant_phone" not in message.data.values()
    for value in message.data.values():
        assert "Maria" not in value
        assert "+1416" not in value
        assert "overflowing" not in value
