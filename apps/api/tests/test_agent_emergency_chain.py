"""Tests for ``app/agent/emergency_chain.py`` (#108) — the emergency
escalation chain: category-templated tenant safety SMS, landlord/backup
voice calls + SMS, the T+0/2/5/10/15/20m+ schedule, and acknowledgment.

Pure functions (templates, schedule arithmetic, TwiML builders) are
``unit``-marked, no DB. Everything else is ``integration`` (real Postgres
via docker-compose + ``alembic upgrade head``), with a FAKE Twilio sender
injected via ``app.integrations.twilio_send.set_twilio_sender_for_tests``
— there is NO code path in this file that ever touches the real Twilio
API (never-break: "NO LIVE SENDS in tests").

Run with:
    export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop
    uv run pytest tests/test_agent_emergency_chain.py -m integration -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent import emergency_chain
from app.agent.schemas import PrefilterResult
from app.integrations.twilio_send import set_twilio_sender_for_tests
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


# ---------------------------------------------------------------------------
# Fake Twilio sender — records every call, NEVER touches the network.
# ---------------------------------------------------------------------------


@dataclass
class _RecordedSend:
    kind: str  # "sms" | "call"
    to: str
    from_: str
    body: str | None = None
    twiml_url: str | None = None


@dataclass
class FakeTwilioSender:
    calls: list[_RecordedSend] = field(default_factory=list)
    fail_calls: bool = False
    fail_sms: bool = False

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        if self.fail_sms:
            raise RuntimeError("fake sms failure")
        self.calls.append(_RecordedSend(kind="sms", to=to, from_=from_, body=body))
        return f"SM{uuid.uuid4().hex}"

    async def create_call(self, *, to: str, from_: str, twiml_url: str) -> str:
        if self.fail_calls:
            raise RuntimeError("fake call failure")
        self.calls.append(_RecordedSend(kind="call", to=to, from_=from_, twiml_url=twiml_url))
        return f"CA{uuid.uuid4().hex}"


@pytest.fixture
def fake_sender() -> FakeTwilioSender:
    sender = FakeTwilioSender()
    set_twilio_sender_for_tests(sender)
    return sender


_LANDLORD_PHONE = "+14165550100"
_PROPERTY_TWILIO_NUMBER = "+14165559999"
_BACKUP_PHONE = "+14165550199"


async def _seed(
    session: AsyncSession,
    *,
    full_name: str | None = None,
    tenant_name: str | None = None,
    backup_contact: dict[str, str] | None = None,
    with_tenant: bool = True,
) -> tuple[str, str, str | None]:
    """Shared seed: one landlord (phone set), one property (twilio_number
    set, optional backup_contact), one tenant (unless ``with_tenant`` is
    False — the "unrecognized sender" scenario)."""
    landlord_id = await factories.insert_landlord(
        session, full_name=full_name, phone=_LANDLORD_PHONE
    )
    property_id = await factories.insert_property(
        session, landlord_id, twilio_number=_PROPERTY_TWILIO_NUMBER, backup_contact=backup_contact
    )
    tenant_id = None
    if with_tenant:
        tenant_id = await factories.insert_tenant(
            session, landlord_id, property_id, name=tenant_name
        )
    return landlord_id, property_id, tenant_id


# ---------------------------------------------------------------------------
# Local helpers — seed an emergency_call notification.
#
# ``_insert_emergency_call_notification`` deliberately mirrors the webhook's
# PRE-N1 shape (no ``next_attempt_at``, no ``ack_token``) -- a legacy/edge
# row that has NOT been born-enriched. Kept on purpose (not updated to the
# new shape) so it keeps exercising this module's OWN belt-2 healing (the
# sweep's ``next_attempt_at IS NULL`` clause + ``_CLAIM_STEP_SQL``'s
# fallback ack_token) independently of the webhook's belt-1 fix. Tests
# that specifically exercise belt 1 (the webhook's born-enriched INSERT)
# use ``_insert_born_enriched_emergency_call_notification`` below instead.
# ---------------------------------------------------------------------------


async def _insert_emergency_call_notification(
    session: AsyncSession,
    *,
    landlord_id: str,
    message_id: str,
    property_id: str,
    categories: list[str],
) -> str:
    notification_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO notifications (id, landlord_id, case_id, type, channel, status, payload) "
            "VALUES (:id, :landlord_id, NULL, 'emergency_call', 'voice', 'pending', "
            "CAST(:payload AS jsonb))"
        ),
        {
            "id": notification_id,
            "landlord_id": landlord_id,
            "payload": json.dumps(
                {"message_id": message_id, "property_id": property_id, "categories": categories}
            ),
        },
    )
    await session.commit()
    return notification_id


async def _insert_born_enriched_emergency_call_notification(
    session: AsyncSession,
    *,
    landlord_id: str,
    message_id: str,
    property_id: str,
    categories: list[str],
    ack_token: str,
    next_attempt_at: datetime,
) -> str:
    """Seed a row EXACTLY as the (post-N1) webhook's own INSERT now does —
    ``app/routers/webhooks/twilio.py::_INSERT_EMERGENCY_NOTIFICATION_SQL``:
    ``next_attempt_at`` and ``ack_token`` both set in the SAME statement
    that creates the row, "born enriched" — sweep-recoverable the instant
    it is durable, with no dependency on ``handle_emergency_trigger`` (or
    even ``fire_emergency_protocol``) ever being invoked at all."""
    notification_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO notifications "
            "(id, landlord_id, case_id, type, channel, status, payload, next_attempt_at) "
            "VALUES (:id, :landlord_id, NULL, 'emergency_call', 'voice', 'pending', "
            "CAST(:payload AS jsonb), :next_attempt_at)"
        ),
        {
            "id": notification_id,
            "landlord_id": landlord_id,
            "payload": json.dumps(
                {
                    "message_id": message_id,
                    "property_id": property_id,
                    "categories": categories,
                    "ack_token": ack_token,
                }
            ),
            "next_attempt_at": next_attempt_at,
        },
    )
    await session.commit()
    return notification_id


async def _fetch_notification(session: AsyncSession, notification_id: str) -> dict[str, object]:
    row = (
        (
            await session.execute(
                text(
                    "SELECT status, attempt, next_attempt_at, acknowledged_at, payload "
                    "FROM notifications WHERE id = :id"
                ),
                {"id": notification_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def _fetch_attempt_audit_rows(
    session: AsyncSession, landlord_id: str
) -> list[dict[str, object]]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT payload FROM audit_log WHERE landlord_id = :lid "
                    "AND action = 'emergency_call_attempt' ORDER BY id"
                ),
                {"lid": landlord_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


_CLEANUP_TABLES: tuple[str, ...] = (
    "audit_log",
    "notifications",
    "messages",
    "tenants",
    "properties",
)


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    for table in _CLEANUP_TABLES:
        await session.execute(
            text(f"DELETE FROM {table} WHERE landlord_id = :lid"),  # noqa: S608
            {"lid": landlord_id},
        )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


# ---------------------------------------------------------------------------
# Pure functions — schedule arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attempt", "expected_minutes"),
    [(0, 0), (1, 2), (2, 5), (3, 10), (4, 15), (5, 20), (6, 35), (7, 50), (8, 65)],
)
def test_next_offset_minutes(attempt: int, expected_minutes: int) -> None:
    assert emergency_chain.next_offset_minutes(attempt) == expected_minutes


@pytest.mark.unit
@pytest.mark.parametrize(
    ("step", "expected_actions"),
    [
        (0, ("landlord_call", "tenant_safety_sms")),
        (1, ("landlord_sms",)),
        (2, ("landlord_call",)),
        (3, ("backup_call", "backup_sms")),
        (4, ("landlord_call", "tenant_status_sms")),
        (5, ("landlord_call", "backup_call", "backup_sms")),
        (9, ("landlord_call", "backup_call", "backup_sms")),
    ],
)
def test_actions_for_step(step: int, expected_actions: tuple[str, ...]) -> None:
    assert emergency_chain.actions_for_step(step) == expected_actions


# ---------------------------------------------------------------------------
# Pure functions — category selection + templates (plain-language-rules.md)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        (["fire"], "fire"),
        (["water"], "water"),
        (["fire", "gas_co"], "fire"),
        (["water", "person"], "person"),
        (["security", "water"], "security"),
        ([], "unknown"),
    ],
)
def test_choose_primary_category(categories: list[str], expected: str) -> None:
    assert emergency_chain.choose_primary_category(categories) == expected


@pytest.mark.unit
@pytest.mark.parametrize("category", ["fire", "gas_co", "water", "security", "person"])
def test_tenant_safety_sms_is_three_numbered_lines_grade_five(category: str) -> None:
    chosen_category, body = emergency_chain.render_tenant_safety_sms([category])
    assert chosen_category == category
    lines = body.split("\n")
    assert len(lines) == 3, f"expected exactly 3 numbered lines, got {len(lines)}"
    for i, line in enumerate(lines, start=1):
        assert line.startswith(f"{i}. "), f"line {i} not numbered: {line!r}"
        words = line.removeprefix(f"{i}. ").split()
        assert len(words) <= 15, f"line {i} exceeds 15 words: {line!r}"


@pytest.mark.unit
@pytest.mark.parametrize("category", ["fire", "gas_co", "security", "person", "water"])
def test_tenant_safety_sms_mentions_911_for_every_category(category: str) -> None:
    """Rubric judgment call #1: fire/medical/crime → 911 first. ``water``'s
    EMERGENCY case (active/uncontained water, electrical contact) also
    ends on an unconditional "call 911 now" per the 2026-07-12
    copy-guardian ruling (finding C1) — the earlier hedged "if you're not
    sure it's safe" wording is removed."""
    _, body = emergency_chain.render_tenant_safety_sms([category])
    assert "911" in body


@pytest.mark.unit
def test_water_template_verbatim_after_c1_hedge_removal() -> None:
    """Copy finding C1 (2026-07-12): the hedge is gone; the third line is
    an unconditional, concrete instruction."""
    _, body = emergency_chain.render_tenant_safety_sms(["water"])
    assert body == (
        "1. Stay away from the water.\n"
        "2. Don't touch outlets or switches near it.\n"
        "3. Call 911 now."
    )


@pytest.mark.unit
def test_tenant_status_sms_verbatim_template() -> None:
    body = emergency_chain.render_tenant_status_sms("Maria")
    assert body == "Still reaching Maria — if the situation is getting dangerous, call 911."


@pytest.mark.unit
def test_landlord_alert_sms_contains_property_and_ack_url() -> None:
    body = emergency_chain.render_landlord_alert_sms(
        property_label="41 Palmerston",
        category_label="a fire",
        tenant_label="Maria",
        ack_url="https://stoop.example/ack/tok123",
    )
    assert "41 Palmerston" in body
    assert "Maria" in body
    assert "https://stoop.example/ack/tok123" in body
    assert "EMERGENCY" in body


@pytest.mark.unit
def test_backup_alert_sms_notes_landlord_unanswered() -> None:
    body = emergency_chain.render_backup_alert_sms(
        property_label="41 Palmerston",
        category_label="a fire",
        landlord_label="Sam",
        tenant_label="Maria",
        ack_url="https://stoop.example/ack/tok123",
    )
    assert "Sam" in body
    assert "hasn't answered" in body


@pytest.mark.unit
def test_build_voice_twiml_has_gather_and_press_one_instruction() -> None:
    xml = emergency_chain.build_voice_twiml(
        property_label="41 Palmerston",
        category_label="a fire",
        action_url="https://stoop.example/webhooks/twilio/voice?notification_id=abc",
    )
    assert "<Gather" in xml
    assert 'numDigits="1"' in xml
    assert 'action="https://stoop.example/webhooks/twilio/voice?notification_id=abc"' in xml
    assert "Press 1 to acknowledge" in xml
    assert "41 Palmerston" in xml


@pytest.mark.unit
def test_build_ack_confirmation_twiml() -> None:
    xml = emergency_chain.build_ack_confirmation_twiml()
    assert "<Say>" in xml
    assert "Thanks" in xml


@pytest.mark.unit
def test_render_voice_action_url_falls_back_without_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(emergency_chain.settings, "public_base_url", None)
    notification_id = uuid.uuid4()
    url = emergency_chain.render_voice_action_url(notification_id)
    assert url == f"http://localhost:8000/webhooks/twilio/voice?notification_id={notification_id}"


@pytest.mark.unit
def test_render_voice_action_url_uses_public_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emergency_chain.settings, "public_base_url", "https://api.stoop.example/")
    notification_id = uuid.uuid4()
    url = emergency_chain.render_voice_action_url(notification_id)
    assert (
        url == f"https://api.stoop.example/webhooks/twilio/voice?notification_id={notification_id}"
    )


@pytest.mark.unit
def test_render_ack_url_uses_token_not_notification_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emergency_chain.settings, "public_base_url", "https://api.stoop.example")
    url = emergency_chain.render_ack_url(uuid.uuid4(), "tok-abc123")
    assert url == "https://api.stoop.example/ack/tok-abc123"


# ---------------------------------------------------------------------------
# Integration — T+0 (handle_emergency_trigger)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_handle_emergency_trigger_calls_landlord_and_texts_tenant(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    landlord_id, property_id, tenant_id = await _seed(
        db_session, full_name="Sam Lee", tenant_name="Maria"
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="there is a fire!",
        prefilter=PrefilterResult(hard_hit=True, categories=["fire"]).model_dump_json(),
    )
    notification_id = await _insert_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
    )

    try:
        await emergency_chain.handle_emergency_trigger(
            notification_id=uuid.UUID(notification_id),
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
        )

        assert len(fake_sender.calls) == 2
        call_actions = {c.kind: c for c in fake_sender.calls}
        assert call_actions["call"].to == "+14165550100"  # landlord
        assert call_actions["call"].from_ == "+14165559999"
        assert call_actions["sms"].to != "+14165550100"  # tenant, not landlord
        assert "Get out" in (call_actions["sms"].body or "")

        notif = await _fetch_notification(db_session, notification_id)
        assert notif["status"] == "pending"
        assert notif["attempt"] == 1
        assert notif["next_attempt_at"] is not None
        assert notif["payload"]["ack_token"]

        sms_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, payload FROM notifications WHERE type = 'emergency_sms' "
                        "AND landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert sms_row["status"] == "sent"
        assert sms_row["payload"]["category"] == "fire"

        attempts = await _fetch_attempt_audit_rows(db_session, landlord_id)
        assert len(attempts) == 1
        assert attempts[0]["payload"]["step"] == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_handle_emergency_trigger_is_idempotent_on_second_call(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    notification_id = await _insert_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
    )

    try:
        args = {
            "notification_id": uuid.UUID(notification_id),
            "message_id": uuid.UUID(message_id),
            "property_id": uuid.UUID(property_id),
            "categories": ["fire"],
        }
        await emergency_chain.handle_emergency_trigger(**args)
        first_call_count = len(fake_sender.calls)
        await emergency_chain.handle_emergency_trigger(**args)

        assert len(fake_sender.calls) == first_call_count, "second call must not re-send anything"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_unrecognized_tenant_number_skips_safety_sms_but_still_calls_landlord(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """See emergency_chain.py's module docstring "Known limitation" — a
    message with no resolvable tenant_id has no stored phone to text the
    safety instructions to; the landlord escalation must still run in full."""
    landlord_id, property_id, _tenant_id = await _seed(db_session, with_tenant=False)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=None,
    )
    notification_id = await _insert_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
    )

    try:
        await emergency_chain.handle_emergency_trigger(
            notification_id=uuid.UUID(notification_id),
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
        )

        assert len(fake_sender.calls) == 1
        assert fake_sender.calls[0].kind == "call"

        attempts = await _fetch_attempt_audit_rows(db_session, landlord_id)
        actions = attempts[0]["payload"]["actions"]
        sms_action = next(a for a in actions if a["action"] == "tenant_safety_sms")
        assert sms_action["status"] == "skipped"
        assert sms_action["reason"] == "no_tenant_phone"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Integration — the periodic sweep (T+2m/+5m/+10m/+15m/+20m+)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sweep_advances_through_the_full_schedule_in_order(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    landlord_id, property_id, tenant_id = await _seed(
        db_session,
        full_name="Sam",
        tenant_name="Maria",
        backup_contact={"name": "Bob", "phone": _BACKUP_PHONE},
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    notification_id = await _insert_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
    )

    try:
        t0 = datetime.now(UTC)
        await emergency_chain.handle_emergency_trigger(
            notification_id=uuid.UUID(notification_id),
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
        )
        assert len(fake_sender.calls) == 2  # T+0: landlord call + tenant safety sms

        # T+2m: landlord SMS with an ack link.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=2, seconds=1))
        assert len(fake_sender.calls) == 3
        assert fake_sender.calls[2].kind == "sms"
        assert fake_sender.calls[2].to == "+14165550100"
        assert "EMERGENCY" in (fake_sender.calls[2].body or "")

        # T+5m: second landlord call.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=5, seconds=1))
        assert len(fake_sender.calls) == 4
        assert fake_sender.calls[3].kind == "call"

        # T+10m: backup contact call + sms.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=10, seconds=1))
        assert len(fake_sender.calls) == 6
        backup_actions = {c.kind: c for c in fake_sender.calls[4:6]}
        assert backup_actions["call"].to == "+14165550199"
        assert backup_actions["sms"].to == "+14165550199"

        # T+15m: third landlord call + honest tenant status update.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=15, seconds=1))
        assert len(fake_sender.calls) == 8
        step4 = fake_sender.calls[6:8]
        tenant_status = next(c for c in step4 if c.kind == "sms")
        assert "Still reaching Sam" in (tenant_status.body or "")

        # T+20m+: repeat cycle (landlord call + backup call + backup sms).
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=20, seconds=1))
        assert len(fake_sender.calls) == 11

        notif = await _fetch_notification(db_session, notification_id)
        assert notif["attempt"] == 6
        assert notif["status"] == "pending"  # never acknowledged in this test
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_backup_step_skips_gracefully_when_not_configured(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    notification_id = await _insert_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
    )

    try:
        t0 = datetime.now(UTC)
        await emergency_chain.handle_emergency_trigger(
            notification_id=uuid.UUID(notification_id),
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
        )
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=2, seconds=1))
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=5, seconds=1))
        calls_before_backup_step = len(fake_sender.calls)

        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=10, seconds=1))

        # nothing sent -- no backup contact configured for this property
        assert len(fake_sender.calls) == calls_before_backup_step
        attempts = await _fetch_attempt_audit_rows(db_session, landlord_id)
        backup_step = attempts[3]["payload"]["actions"]
        assert all(a["status"] == "skipped" for a in backup_step)
        assert all(a["reason"] == "no_backup_contact" for a in backup_step)
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Integration — acknowledgment stops the chain, from all three surfaces
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_acknowledge_stops_the_chain(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    notification_id = await _insert_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
    )

    try:
        t0 = datetime.now(UTC)
        await emergency_chain.handle_emergency_trigger(
            notification_id=uuid.UUID(notification_id),
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
        )
        calls_before_ack = len(fake_sender.calls)

        acknowledged_at = await emergency_chain.acknowledge_notification(
            uuid.UUID(notification_id), actor="system", channel="voice_keypress"
        )
        assert acknowledged_at is not None

        # Even far in the future, an acknowledged chain never fires again.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(hours=5))

        assert len(fake_sender.calls) == calls_before_ack
        notif = await _fetch_notification(db_session, notification_id)
        assert notif["status"] == "acknowledged"
        assert notif["acknowledged_at"] is not None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_acknowledge_is_idempotent_across_concurrent_surfaces(
    db_session: AsyncSession,
) -> None:
    landlord_id, property_id, _tenant_id = await _seed(db_session, with_tenant=False)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=None,
    )
    notification_id = await _insert_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
    )

    try:
        first = await emergency_chain.acknowledge_notification(
            uuid.UUID(notification_id), actor="system", channel="voice_keypress"
        )
        second = await emergency_chain.acknowledge_notification(
            uuid.UUID(notification_id), actor="landlord", channel="dashboard"
        )
        assert first == second

        audit_rows = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid "
                    "AND action = 'acknowledged'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_rows == 1, "only the FIRST ack may write an audit_log row"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_acknowledge_notification_unknown_id_returns_none(db_session: AsyncSession) -> None:
    result = await emergency_chain.acknowledge_notification(
        uuid.uuid4(), actor="system", channel="voice_keypress"
    )
    assert result is None


@pytest.mark.integration
async def test_acknowledge_by_token_resolves_and_acks(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    landlord_id, property_id, _tenant_id = await _seed(db_session, with_tenant=False)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=None,
    )
    notification_id = await _insert_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
    )

    try:
        await emergency_chain.handle_emergency_trigger(
            notification_id=uuid.UUID(notification_id),
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
        )
        notif = await _fetch_notification(db_session, notification_id)
        token = notif["payload"]["ack_token"]

        result = await emergency_chain.acknowledge_by_token(token, channel="sms_link")
        assert result is not None
        acked_notification_id, _acknowledged_at = result
        assert str(acked_notification_id) == notification_id

        unknown = await emergency_chain.acknowledge_by_token("not-a-real-token", channel="sms_link")
        assert unknown is None
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Integration — crash-safety: the chain resumes from durable rows, never
# from in-process state.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_crash_in_the_pre_enrich_window_is_recovered_by_the_next_sweep_tick(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """Safety review, 2026-07-12 (finding N1, BLOCKING) — extends the crash
    test to the "pre-enrich window" itself: the row is seeded EXACTLY as
    the (post-fix) webhook's own INSERT now does —
    ``app/routers/webhooks/twilio.py::_INSERT_EMERGENCY_NOTIFICATION_SQL``
    sets ``next_attempt_at = now()`` and a fresh ``ack_token`` in the SAME
    statement that creates the row — and NEITHER
    ``app.agent.emergency.fire_emergency_protocol`` NOR
    ``emergency_chain.handle_emergency_trigger`` is EVER invoked, simulating
    a crash strictly BEFORE either one runs (the earliest possible crash
    point, one step earlier than the previous revision's own separate
    enrich transaction could reach). Proves belt 1 alone — durable at
    INSERT time — is sufficient: the very next sweep tick still performs
    the T+0 landlord call AND the tenant safety SMS with zero dependency on
    this module's own T+0 code path ever having run."""
    landlord_id, property_id, tenant_id = await _seed(
        db_session, full_name="Sam Lee", tenant_name="Maria"
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    now = datetime.now(UTC)
    notification_id = await _insert_born_enriched_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
        ack_token="born-enriched-crash-test-token",  # noqa: S106 -- test fixture, not a secret
        next_attempt_at=now,
    )

    try:
        assert len(fake_sender.calls) == 0, (
            "no send should have happened yet -- handle_emergency_trigger was never called"
        )

        # "process restarts" — the scheduler's next tick finds the row due,
        # with no help from handle_emergency_trigger at all.
        outcomes = await emergency_chain.run_emergency_chain_sweep(now=now + timedelta(seconds=1))

        assert len(outcomes) == 1
        assert outcomes[0].outcome == "processed"
        assert len(fake_sender.calls) == 2
        kinds = {c.kind for c in fake_sender.calls}
        assert kinds == {"call", "sms"}

        notif = await _fetch_notification(db_session, notification_id)
        assert notif["attempt"] == 1
        assert notif["payload"]["ack_token"] == "born-enriched-crash-test-token"  # noqa: S105

        sms_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status FROM notifications WHERE type = 'emergency_sms' "
                        "AND landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert sms_row["status"] == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_forcing_the_residual_header_read_to_fail_is_recovered_by_next_sweep_tick(
    db_session: AsyncSession, fake_sender: FakeTwilioSender, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety review, 2026-07-12 (finding N1) — ``handle_emergency_trigger``
    no longer does any durable write of its own; its only remaining DB
    touch is the (now RESIDUAL — belt 1 already enriched the row before
    this ever runs) read-only ``_load_trigger_header``. Force THAT to
    raise, simulating a crash/DB hiccup inside ``handle_emergency_trigger``
    itself, and prove the next sweep tick still calls the landlord and
    texts the tenant regardless — the row was already durably due before
    this function was ever invoked, so its failure changes nothing about
    recoverability."""
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    now = datetime.now(UTC)
    notification_id = await _insert_born_enriched_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
        ack_token="residual-path-test-token",  # noqa: S106 -- test fixture, not a secret
        next_attempt_at=now,
    )

    async def _boom(_notification_id: uuid.UUID) -> tuple[uuid.UUID, datetime]:
        raise RuntimeError("simulated failure reading the notification header")

    monkeypatch.setattr(emergency_chain, "_load_trigger_header", _boom)

    try:
        with pytest.raises(RuntimeError, match="simulated failure"):
            await emergency_chain.handle_emergency_trigger(
                notification_id=uuid.UUID(notification_id),
                message_id=uuid.UUID(message_id),
                property_id=uuid.UUID(property_id),
                categories=["fire"],
            )
        assert len(fake_sender.calls) == 0, "the header read raised before any send was attempted"

        outcomes = await emergency_chain.run_emergency_chain_sweep(now=now + timedelta(seconds=1))

        assert len(outcomes) == 1
        assert outcomes[0].outcome == "processed"
        assert len(fake_sender.calls) == 2
        kinds = {c.kind for c in fake_sender.calls}
        assert kinds == {"call", "sms"}
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_lost_race_on_concurrent_claim_never_double_sends(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """Two overlapping attempts to process the SAME due step (e.g. the T+0
    immediate call racing the sweep's very first tick) — only one may
    win; the loser must be a silent, safe no-op, never a duplicate
    call/SMS."""
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    now = datetime.now(UTC)
    notification_id = await _insert_born_enriched_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
        ack_token="race-test-ack-token",  # noqa: S106 -- test fixture, not a secret
        next_attempt_at=now,
    )

    try:
        candidate = emergency_chain.EmergencyCallCandidate(
            notification_id=uuid.UUID(notification_id),
            landlord_id=uuid.UUID(landlord_id),
            attempt=0,
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
            ack_token="race-test-ack-token",  # noqa: S106 -- test fixture, not a secret
            chain_started_at=now,
        )

        outcome_a = await emergency_chain._process_due_row(candidate)  # noqa: SLF001
        outcome_b = await emergency_chain._process_due_row(candidate)  # noqa: SLF001

        outcomes = {outcome_a, outcome_b}
        assert "processed" in outcomes
        assert "lost_race" in outcomes
        # step 0 has TWO actions (landlord_call + tenant_safety_sms) -- the
        # winner performs both, the loser performs neither.
        assert len(fake_sender.calls) == 2, "only the winner's two actions may have been sent"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Integration — the SMS drain sweep (safety review, 2026-07-12: spec
# finding S1 / safety finding 3) -- resends tenant_ack/emergency_sms rows
# until genuinely delivered, closing #109's deployment gate.
# ---------------------------------------------------------------------------


async def _insert_tenant_ack_notification(
    session: AsyncSession, *, landlord_id: str, message_id: str, body: str
) -> str:
    notification_id = str(uuid.uuid4())
    payload = {"message_id": message_id, "reasons": ["classification_failed"], "body": body}
    await session.execute(
        text(
            "INSERT INTO notifications (id, landlord_id, case_id, type, channel, status, payload) "
            "VALUES (:id, :landlord_id, NULL, 'tenant_ack', 'sms', 'pending', "
            "CAST(:payload AS jsonb))"
        ),
        {
            "id": notification_id,
            "landlord_id": landlord_id,
            "payload": json.dumps(payload),
        },
    )
    await session.commit()
    return notification_id


@pytest.mark.integration
async def test_sms_drain_sweep_sends_pending_tenant_ack(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    notification_id = await _insert_tenant_ack_notification(
        db_session, landlord_id=landlord_id, message_id=message_id, body="Got your message..."
    )

    try:
        outcomes = await emergency_chain.run_sms_drain_sweep()

        assert len(outcomes) == 1
        assert outcomes[0].outcome == "sent"
        assert outcomes[0].notification_type == "tenant_ack"
        assert len(fake_sender.calls) == 1
        assert fake_sender.calls[0].kind == "sms"
        assert fake_sender.calls[0].body == "Got your message..."

        notif = await _fetch_notification(db_session, notification_id)
        assert notif["status"] == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sms_drain_sweep_resends_emergency_sms_after_initial_send_failure(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """Safety finding 3: the tenant safety SMS is the one non-redundant
    message in the whole chain -- a failed first attempt (at T+0) must be
    retried by the drain sweep, not left at-most-once."""
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    notification_id = await _insert_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
    )

    try:
        fake_sender.fail_sms = True
        await emergency_chain.handle_emergency_trigger(
            notification_id=uuid.UUID(notification_id),
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
        )
        # T+0: landlord call succeeded, tenant safety sms FAILED.
        assert len(fake_sender.calls) == 1
        assert fake_sender.calls[0].kind == "call"

        sms_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status FROM notifications WHERE type = 'emergency_sms' "
                        "AND landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert sms_row["status"] == "failed"

        # Next tick: the fault clears, the drain sweep resends successfully.
        fake_sender.fail_sms = False
        outcomes = await emergency_chain.run_sms_drain_sweep()

        assert len(outcomes) == 1
        assert outcomes[0].outcome == "sent"
        assert outcomes[0].notification_type == "emergency_sms"
        assert len(fake_sender.calls) == 2
        assert fake_sender.calls[1].kind == "sms"

        sms_row_after = (
            (
                await db_session.execute(
                    text(
                        "SELECT status FROM notifications WHERE type = 'emergency_sms' "
                        "AND landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert sms_row_after["status"] == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sms_drain_sweep_marks_failed_and_retries_on_the_next_tick(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    notification_id = await _insert_tenant_ack_notification(
        db_session, landlord_id=landlord_id, message_id=message_id, body="Got your message..."
    )

    try:
        fake_sender.fail_sms = True
        first_outcomes = await emergency_chain.run_sms_drain_sweep()
        assert first_outcomes[0].outcome == "failed"

        notif = await _fetch_notification(db_session, notification_id)
        assert notif["status"] == "failed"
        assert notif["attempt"] == 1

        fake_sender.fail_sms = False
        second_outcomes = await emergency_chain.run_sms_drain_sweep()
        assert second_outcomes[0].outcome == "sent"

        notif_after = await _fetch_notification(db_session, notification_id)
        assert notif_after["status"] == "sent"
        assert notif_after["attempt"] == 2
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sms_drain_sweep_no_tenant_phone_is_terminal_exhausted(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """Safety review, 2026-07-12 (finding N2) — ``no_tenant_phone`` is
    TERMINAL, not transient: it must land on ``'exhausted'`` (schema-v1.md's
    CHECK already allows it), never ``'failed'`` — ``'failed'`` stays in
    ``_SELECT_DUE_SMS_DRAIN_SQL``'s own retry set, so marking a genuinely
    unfixable row ``'failed'`` would have the sweep silently re-attempt (and
    re-fail) it forever. A SECOND tick must be a true no-op."""
    landlord_id, property_id, _tenant_id = await _seed(db_session, with_tenant=False)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=None,
    )
    notification_id = await _insert_tenant_ack_notification(
        db_session, landlord_id=landlord_id, message_id=message_id, body="Got your message..."
    )

    try:
        outcomes = await emergency_chain.run_sms_drain_sweep()
        assert outcomes[0].outcome == "no_tenant_phone"
        assert len(fake_sender.calls) == 0

        notif = await _fetch_notification(db_session, notification_id)
        assert notif["status"] == "exhausted"

        # Second tick: 'exhausted' is excluded from the sweep's own
        # status IN ('pending', 'failed') selection -- nothing to do.
        second_outcomes = await emergency_chain.run_sms_drain_sweep()
        assert second_outcomes == []
        assert len(fake_sender.calls) == 0

        notif_after = await _fetch_notification(db_session, notification_id)
        assert notif_after["status"] == "exhausted"
        assert notif_after["attempt"] == notif["attempt"], "a no-op tick must not re-claim the row"
    finally:
        await _cleanup(db_session, landlord_id)
