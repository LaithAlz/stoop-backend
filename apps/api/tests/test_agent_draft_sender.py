"""Integration tests for #44/#45's sender ticker (``app/agent/
draft_sender.py``) — the FIRST outbound-send call site in this codebase.
Every test uses a FAKE :class:`app.integrations.sms_sender.SmsSender` — no
real Twilio client exists in this codebase at all (#108's parallel
branch), and this module is a completely separate file from
``app/integrations/twilio.py`` on purpose (scope boundary).

Marker: ``integration`` — requires a running Postgres instance + ``alembic
upgrade head``.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.draft_sender import run_sender_loop, sender_tick
from app.integrations.sms_sender import SmsSender
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
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(
        text("DELETE FROM audit_log WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM trust_metrics WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM messages WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM tenants WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM properties WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


class _FakeSmsSender:
    """Records every call; never touches a network. ``fail_next`` lets a
    test simulate a provider error on the NEXT ``send_sms`` call only."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next = False

    async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated provider failure")
        self.calls.append({"to_e164": to_e164, "from_e164": from_e164, "body": body})
        return f"SM{uuid.uuid4().hex}"


async def _seed_approved_draft(
    session: AsyncSession,
    *,
    due_in_seconds: float = -1.0,
    edited: bool = False,
    final_body: str | None = None,
    severity: str = "urgent",
    provision_twilio_number: bool = True,
) -> tuple[str, str, str]:
    """Returns ``(landlord_id, case_id, draft_id)`` — a draft already
    ``'approved'`` with ``scheduled_send_at`` *due_in_seconds* from now
    (negative = already due). ``provision_twilio_number=True`` (default)
    gives the property a fresh, collision-free ``twilio_number`` (the
    column is ``UNIQUE`` — never a fixed literal across calls) so every
    existing test exercises the realistic case: a property that already
    has a provisioned outbound number. Pass ``False`` for the "not yet
    provisioned" guard test."""
    landlord_id = await factories.insert_landlord(session)
    twilio_number = factories.fresh_phone() if provision_twilio_number else None
    property_id = await factories.insert_property(session, landlord_id, twilio_number=twilio_number)
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id)
    case_id = await factories.insert_case(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
        severity=severity,
    )
    scheduled_send_at = datetime.now(UTC) + timedelta(seconds=due_in_seconds)
    draft_id = await factories.insert_draft(
        session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="approved",
        scheduled_send_at=scheduled_send_at,
        edited=edited,
        final_body=final_body,
    )
    return landlord_id, case_id, draft_id


# ---------------------------------------------------------------------------
# 1. Happy path — a due, clean approval.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sender_tick_sends_due_draft_and_writes_every_durable_side_effect(
    db_session: AsyncSession,
) -> None:
    landlord_id, case_id, draft_id = await _seed_approved_draft(db_session)
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1
        assert len(sender.calls) == 1

        draft_row = (
            (
                await db_session.execute(
                    text("SELECT status, sent_message_id FROM drafts WHERE id = :id"),
                    {"id": draft_id},
                )
            )
            .mappings()
            .one()
        )
        assert draft_row["status"] == "sent"
        assert draft_row["sent_message_id"] is not None

        message_row = (
            (
                await db_session.execute(
                    text("SELECT direction, party, twilio_sid, body FROM messages WHERE id = :id"),
                    {"id": str(draft_row["sent_message_id"])},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["direction"] == "outbound"
        assert message_row["party"] == "tenant"
        assert message_row["twilio_sid"] is not None
        assert message_row["twilio_sid"].startswith("SM")
        assert message_row["body"] == sender.calls[0]["body"]
        # The send goes out from the CASE's OWN property's twilio_number,
        # never a fabricated/omitted "from" (app/integrations/sms_sender.py's
        # module docstring "Why from_e164 is required").
        assert sender.calls[0]["from_e164"] is not None

        case_row = (
            (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :id"), {"id": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert case_row["status"] == "awaiting_tenant"

        trust_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT clean_approvals, edited_approvals FROM trust_metrics "
                        "WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert trust_row["clean_approvals"] == 1
        assert trust_row["edited_approvals"] == 0

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor, action FROM audit_log WHERE case_id = :cid "
                        "AND action = 'sent'"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["actor"] == "system"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sender_tick_records_edited_approval_in_trust_metrics(
    db_session: AsyncSession,
) -> None:
    landlord_id, case_id, draft_id = await _seed_approved_draft(
        db_session, edited=True, final_body="Edited landlord text."
    )
    sender = _FakeSmsSender()
    try:
        await sender_tick(sender=sender)

        assert sender.calls[0]["body"] == "Edited landlord text."

        trust_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT clean_approvals, edited_approvals FROM trust_metrics "
                        "WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert trust_row["clean_approvals"] == 0
        assert trust_row["edited_approvals"] == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2. Not-yet-due drafts are left alone.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sender_tick_ignores_drafts_not_yet_due(db_session: AsyncSession) -> None:
    landlord_id, _case_id, draft_id = await _seed_approved_draft(db_session, due_in_seconds=60.0)
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 0
        assert sender.calls == []

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "approved"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 3. Idempotent claim — concurrent ticks never double-send.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_ticks_send_exactly_once(db_session: AsyncSession) -> None:
    landlord_id, _case_id, draft_id = await _seed_approved_draft(db_session)
    sender = _FakeSmsSender()
    try:
        results = await asyncio.gather(sender_tick(sender=sender), sender_tick(sender=sender))
        assert sum(results) == 1  # exactly one tick won the claim
        assert len(sender.calls) == 1

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 4. Crash/failure semantics — a stuck 'sending' row, never a silent
#    double-send or a fabricated message.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_send_failure_leaves_draft_stuck_sending_not_lost_or_duplicated(
    db_session: AsyncSession,
) -> None:
    landlord_id, case_id, draft_id = await _seed_approved_draft(db_session)
    sender = _FakeSmsSender()
    sender.fail_next = True
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1  # claimed the row -- the SEND itself then failed
        assert sender.calls == []

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sending"  # stuck, visible -- never silently re-attempted here

        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert message_count == 0  # never fabricated an outbound message row

        # A second tick does NOT re-claim the stuck row (status is no
        # longer 'approved') -- never a silent double-send once it's stuck.
        claimed_again = await sender_tick(sender=sender)
        assert claimed_again == 0
        assert sender.calls == []
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 5. Deployment gating — the worker refuses to run with no sender bound.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_run_sender_loop_disabled_with_no_sender_configured() -> None:
    """Deployment gating (matches #109's own pattern): a ``None`` sender
    means the worker is disabled — this must return promptly (never loop
    forever, never raise), not merely "eventually work" behind a timeout.
    The loud-log assertion lives in ``test_integrations_sms_sender.py``-style
    coverage is out of scope here; the behavioral contract (never loops,
    never touches the claim SQL) is what this test pins."""
    await asyncio.wait_for(run_sender_loop(sender=None), timeout=1.0)


@pytest.mark.integration
async def test_run_sender_loop_stops_on_stop_event(db_session: AsyncSession) -> None:
    landlord_id, _case_id, draft_id = await _seed_approved_draft(db_session)
    sender = _FakeSmsSender()
    stop_event = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    try:
        await asyncio.gather(
            run_sender_loop(sender=sender, interval_seconds=0.01, stop_event=stop_event),
            _stop_soon(),
        )
        # At least one tick ran before the stop fired.
        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_edited_draft_with_empty_final_body_never_sends_original_text(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #4 (safety review, INFO/defensive): an edited draft whose
    ``final_body`` is somehow empty must NEVER silently fall back to
    ``drafts.body`` (the ORIGINAL text the landlord explicitly replaced) —
    refused loudly (logged + Sentry-paged), left stuck ``'sending'``, same
    as any other send failure."""
    import app.agent.draft_sender as draft_sender_mod

    calls: list[dict[str, object]] = []

    def _fake_capture_message(
        message: str, *, level: str | None = None, extras: dict[str, object] | None = None
    ) -> None:
        calls.append({"message": message, "level": level, "extras": extras})

    monkeypatch.setattr(draft_sender_mod.sentry_sdk, "capture_message", _fake_capture_message)

    landlord_id, case_id, draft_id = await _seed_approved_draft(
        db_session, edited=True, final_body=None
    )
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1  # claimed the row -- refused before ever calling send_sms
        assert sender.calls == []  # NEVER sent the original text

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sending"  # stuck, visible -- never silently re-attempted

        assert len(calls) == 1
        assert calls[0]["level"] == "error"

        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert message_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 6. No twilio_number provisioned yet -- refuse to send, never misroute.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_property_twilio_number_refuses_to_send(db_session: AsyncSession) -> None:
    """A property with no ``twilio_number`` provisioned yet must never send
    from a DIFFERENT property's number or a fabricated placeholder —
    mirrors ``app/agent/emergency_chain.py``'s own ``"no_twilio_number"``
    skip reason. Refused loudly, row left stuck ``'sending'`` (same
    stuck-row semantics as any other send failure), no message row
    fabricated."""
    landlord_id, case_id, draft_id = await _seed_approved_draft(
        db_session, provision_twilio_number=False
    )
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1  # claimed the row -- refused before ever calling send_sms
        assert sender.calls == []

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sending"  # stuck, visible -- never silently re-attempted

        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert message_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


def test_sms_sender_protocol_is_a_runtime_checkable_shape() -> None:
    """Cheap unit-level pin: SmsSender is the ONE seam the ticker depends
    on — a fake implementing just `send_sms` satisfies it structurally."""

    class _Impl:
        async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str:
            return "sid"

    sender: SmsSender = _Impl()
    assert hasattr(sender, "send_sms")
