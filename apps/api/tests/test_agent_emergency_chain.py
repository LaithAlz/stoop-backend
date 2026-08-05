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
from app.integrations import sms_segments
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
        # #111 cost metering (schema-v1.md v1.12): property_id rides along
        # at the top level (no case_id exists this early), and the SMS
        # action -- never the voice-call action -- carries segments/cost.
        assert attempts[0]["payload"]["property_id"] == property_id
        actions = attempts[0]["payload"]["actions"]
        call_action = next(a for a in actions if a["action"] == "landlord_call")
        sms_action = next(a for a in actions if a["action"] == "tenant_safety_sms")
        assert call_action["segments"] is None
        assert call_action["sms_cost_cents"] is None
        assert sms_action["segments"] >= 1
        assert sms_action["sms_cost_cents"] == pytest.approx(
            sms_action["segments"] * sms_segments.SEGMENT_PRICE_USD_CENTS
        )
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


@pytest.mark.integration
async def test_backup_step_skips_gracefully_after_backup_contact_cleared_via_patch(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """#268: a backup contact cleared through the REAL ``PATCH
    /v1/properties/{id}`` write path (``backup_contact: null`` ->
    ``json.dumps(None)`` + ``CAST(... AS jsonb)``, a JSON null literal, not
    SQL NULL -- see ``tests/test_properties_router.py``'s step-1
    precondition test) must make the T+10m step ``"skipped"``, never
    ``"failed"`` -- the concrete failure mode this issue's step 1 verifies
    against is ``_backup_phone`` receiving the *string* ``"null"``
    (``"null".strip()`` is truthy) and handing Twilio ``to="null"``."""
    from app.deps import Landlord
    from app.routers.properties import PropertyUpdateRequest, update_property

    landlord_id, property_id, tenant_id = await _seed(
        db_session, backup_contact={"name": "Ex-Partner", "phone": _BACKUP_PHONE}
    )

    # Clear it through the actual router handler -- not a raw SQL UPDATE --
    # so this test exercises the exact code path a real PATCH request runs.
    await update_property(
        uuid.UUID(property_id),
        PropertyUpdateRequest(backup_contact=None),
        (Landlord(id=uuid.UUID(landlord_id)), db_session),
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
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=2, seconds=1))
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=5, seconds=1))
        calls_before_backup_step = len(fake_sender.calls)

        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=10, seconds=1))

        # nothing sent -- cleared backup contact, same as never-configured.
        assert len(fake_sender.calls) == calls_before_backup_step
        attempts = await _fetch_attempt_audit_rows(db_session, landlord_id)
        backup_step = attempts[3]["payload"]["actions"]
        assert all(a["status"] == "skipped" for a in backup_step)
        assert all(a["reason"] == "no_backup_contact" for a in backup_step)
        # The failure mode this proves against: never "failed".
        assert not any(a["status"] == "failed" for a in backup_step)
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_backup_step_skips_gracefully_after_backup_contact_cleared_mid_flight(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """#268, api-contracts.md v1.25 amendment: unlike the test above (which
    clears the backup contact BEFORE the chain ever starts, the "easy"
    case), this proves the HARDER promise -- a contact cleared MID-FLIGHT,
    after the chain has already run several steps with it still live, is
    picked up "on its very next scheduled attempt" because the chain
    "re-reads `properties.backup_contact` fresh on every attempt"
    (``_load_context``, called per step inside ``_process_due_row``, never
    cached/snapshotted at trigger time).

    Starts with a LIVE backup contact, runs T+0/2/5m normally (no backup
    leg is due yet), clears the contact through the REAL ``PATCH
    /v1/properties/{id}`` handler mid-flight, then proves: (a) the T+10m
    backup step -- the very next scheduled attempt -- skips both backup
    legs with ``reason: "no_backup_contact"``, never "failed"; (b) the
    T+20m+ repeat cycle (``actions_for_step``'s step 5 and every repeat
    after it) keeps skipping the backup legs too, across more than one
    repeat; and (c) the landlord call/SMS legs, untouched by this PATCH,
    keep firing on schedule throughout -- clearing a backup contact must
    never weaken the landlord side of the chain."""
    from app.deps import Landlord
    from app.routers.properties import PropertyUpdateRequest, update_property

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

        # T+0: landlord call + tenant safety sms -- backup contact still live.
        await emergency_chain.handle_emergency_trigger(
            notification_id=uuid.UUID(notification_id),
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
        )
        assert len(fake_sender.calls) == 2

        # T+2m: landlord SMS with an ack link -- no backup leg is due yet,
        # so nothing about the still-live contact should surface here.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=2, seconds=1))
        assert len(fake_sender.calls) == 3
        assert fake_sender.calls[2].kind == "sms"
        assert fake_sender.calls[2].to == "+14165550100"  # landlord, not backup

        # T+5m: second landlord call -- again, no backup leg due yet.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=5, seconds=1))
        assert len(fake_sender.calls) == 4
        assert fake_sender.calls[3].kind == "call"
        assert fake_sender.calls[3].to == "+14165550100"

        calls_before_backup_step = len(fake_sender.calls)

        # Clear the backup contact MID-FLIGHT -- through the REAL PATCH
        # /v1/properties/{id} handler, not a raw SQL UPDATE -- proving the
        # landlord-facing capability reaches the already-running chain.
        # Three legs already sent above with the contact genuinely live.
        await update_property(
            uuid.UUID(property_id),
            PropertyUpdateRequest(backup_contact=None),
            (Landlord(id=uuid.UUID(landlord_id)), db_session),
        )
        # A separate admin-engine connection drives the sweep below -- make
        # the PATCH durable so it's actually visible cross-connection.
        await db_session.commit()

        # T+10m -- the very next scheduled attempt after the clear: both
        # backup legs must now skip, never fail, never dial/text the
        # cleared number.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=10, seconds=1))
        assert len(fake_sender.calls) == calls_before_backup_step  # nothing new sent

        attempts = await _fetch_attempt_audit_rows(db_session, landlord_id)
        backup_step = attempts[3]["payload"]["actions"]
        assert all(a["status"] == "skipped" for a in backup_step)
        assert all(a["reason"] == "no_backup_contact" for a in backup_step)
        assert not any(a["status"] == "failed" for a in backup_step)

        # T+15m: third landlord call + honest tenant status update -- this
        # leg is untouched by the PATCH and must keep firing on schedule.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=15, seconds=1))
        assert len(fake_sender.calls) == calls_before_backup_step + 2
        step4_calls = fake_sender.calls[-2:]
        assert any(c.kind == "call" and c.to == "+14165550100" for c in step4_calls)
        tenant_status = next(c for c in step4_calls if c.kind == "sms")
        assert "Still reaching Sam" in (tenant_status.body or "")

        calls_before_repeat = len(fake_sender.calls)

        # T+20m -- the first T+20m+ repeat. Per actions_for_step, step 5
        # returns (landlord_call, backup_call, backup_sms): the landlord
        # leg must still send; both backup legs must still skip.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=20, seconds=1))
        assert len(fake_sender.calls) == calls_before_repeat + 1  # landlord_call only
        assert fake_sender.calls[-1].kind == "call"
        assert fake_sender.calls[-1].to == "+14165550100"

        attempts = await _fetch_attempt_audit_rows(db_session, landlord_id)
        step5_actions = attempts[5]["payload"]["actions"]
        landlord_call_action = next(a for a in step5_actions if a["action"] == "landlord_call")
        backup_call_action = next(a for a in step5_actions if a["action"] == "backup_call")
        backup_sms_action = next(a for a in step5_actions if a["action"] == "backup_sms")
        assert landlord_call_action["status"] == "sent"
        assert backup_call_action["status"] == "skipped"
        assert backup_call_action["reason"] == "no_backup_contact"
        assert backup_sms_action["status"] == "skipped"
        assert backup_sms_action["reason"] == "no_backup_contact"

        calls_before_second_repeat = len(fake_sender.calls)

        # T+35m -- a SECOND repeat, proving "every repeat" (not just the
        # one immediately after the PATCH) keeps honoring the cleared
        # contact, and the landlord leg keeps firing.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=35, seconds=1))
        assert len(fake_sender.calls) == calls_before_second_repeat + 1  # landlord_call only
        assert fake_sender.calls[-1].kind == "call"
        assert fake_sender.calls[-1].to == "+14165550100"

        attempts = await _fetch_attempt_audit_rows(db_session, landlord_id)
        step6_actions = attempts[6]["payload"]["actions"]
        landlord_call_action_2 = next(a for a in step6_actions if a["action"] == "landlord_call")
        backup_call_action_2 = next(a for a in step6_actions if a["action"] == "backup_call")
        backup_sms_action_2 = next(a for a in step6_actions if a["action"] == "backup_sms")
        assert landlord_call_action_2["status"] == "sent"
        assert backup_call_action_2["status"] == "skipped"
        assert backup_call_action_2["reason"] == "no_backup_contact"
        assert backup_sms_action_2["status"] == "skipped"
        assert backup_sms_action_2["reason"] == "no_backup_contact"

        # Across the entire chain, zero calls/texts ever reached the
        # backup number once the contact was cleared.
        assert all(c.to != _BACKUP_PHONE for c in fake_sender.calls)
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


@pytest.mark.integration
async def test_landlord_and_backup_get_distinct_tokens_and_ack_attribution(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """#289 acceptance: "one token per (notification, recipient), not per
    notification". Runs the chain through the T+10m backup step (the
    first step that ever sends a backup SMS carrying ``ack_token_backup``)
    and proves: (a) the landlord's own SMS link (T+2m) and the backup's own
    SMS link (T+10m) carry two DIFFERENT tokens, both stored under distinct
    ``payload`` keys; (b) acknowledging via the BACKUP token records
    ``recipient_role: "backup"`` in the audit trail, never a phone number,
    role only; (c) acknowledging via the LANDLORD token (a separate,
    already-acknowledged-chain check below) records ``recipient_role:
    "landlord"``."""
    landlord_id, property_id, tenant_id = await _seed(
        db_session, backup_contact={"name": "Bob", "phone": _BACKUP_PHONE}
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
        # T+2m: landlord SMS carries the landlord's own ack link.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=2, seconds=1))
        # T+5m: second landlord call — no token involved.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=5, seconds=1))
        # T+10m: backup call + SMS — the backup SMS carries the backup's
        # own ack link, minted independently of the landlord's.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=10, seconds=1))

        notif = await _fetch_notification(db_session, notification_id)
        landlord_token = notif["payload"]["ack_token"]
        backup_token = notif["payload"]["ack_token_backup"]

        assert landlord_token is not None
        assert backup_token is not None
        assert landlord_token != backup_token, "landlord and backup must never share a token"

        # The backup SMS body actually embeds the backup token, never the
        # landlord's.
        backup_sms = next(
            c
            for c in fake_sender.calls
            if c.kind == "sms" and c.to == _BACKUP_PHONE and c.body is not None
        )
        assert backup_token in (backup_sms.body or "")
        assert landlord_token not in (backup_sms.body or "")

        # Acknowledging via the BACKUP token attributes to "backup".
        result = await emergency_chain.acknowledge_by_token(backup_token, channel="sms_link")
        assert result is not None

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT payload FROM audit_log WHERE landlord_id = :lid "
                        "AND action = 'acknowledged'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["payload"]["recipient_role"] == "backup"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_acknowledge_via_landlord_token_attributes_to_landlord(
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

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT payload FROM audit_log WHERE landlord_id = :lid "
                        "AND action = 'acknowledged'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["payload"]["recipient_role"] == "landlord"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_dashboard_ack_attributes_to_landlord(db_session: AsyncSession) -> None:
    """The authenticated dashboard surface (``POST /v1/notifications/{id}
    /ack``) is unambiguously the landlord — no token involved at all."""
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
        acknowledged_at = await emergency_chain.acknowledge_notification(
            uuid.UUID(notification_id),
            actor="landlord",
            channel="dashboard",
            recipient_role="landlord",
        )
        assert acknowledged_at is not None

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT payload FROM audit_log WHERE landlord_id = :lid "
                        "AND action = 'acknowledged'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["payload"]["recipient_role"] == "landlord"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_legacy_shared_token_still_acknowledges_after_upgrade(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """#289 "Existing tokens": a chain seeded EXACTLY as a pre-#289 row
    would have been (a single ``ack_token``, no ``ack_token_backup`` at
    all — mirrors the webhook's born-enriched shape from BEFORE this
    revision) must keep working after the upgrade: the old shared token
    still acknowledges the chain, with no migration/backfill needed. This
    is the "must not lose its ability to be acknowledged" guarantee for a
    chain already mid-flight when this ships."""
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    pre_289_token = "pre-289-shared-token"  # noqa: S105 -- test fixture, not a secret
    notification_id = await _insert_born_enriched_emergency_call_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        categories=["fire"],
        ack_token=pre_289_token,
        next_attempt_at=datetime.now(UTC),
    )

    try:
        result = await emergency_chain.acknowledge_by_token(pre_289_token, channel="sms_link")
        assert result is not None
        acked_notification_id, _acknowledged_at = result
        assert str(acked_notification_id) == notification_id
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_revoked_backup_token_cannot_ack_but_landlord_token_still_can(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """#289 acceptance, the required proof: "a revoked backup token cannot
    acknowledge, and the landlord's token for the same notification still
    can". Runs a live chain through T+10m (the backup contact's own ack
    token gets minted and texted to them), removes the backup contact
    through the REAL ``PATCH /v1/properties/{id}`` handler (same pattern
    ``test_backup_step_skips_gracefully_after_backup_contact_cleared_mid_
    flight`` already established), and proves the ex-partner scenario #289
    describes is closed: the link the removed backup contact already holds
    no longer silences the emergency, while the landlord's own link (a
    completely different token, on the SAME notification) keeps working."""
    from app.deps import Landlord
    from app.routers.properties import PropertyUpdateRequest, update_property

    landlord_id, property_id, tenant_id = await _seed(
        db_session, backup_contact={"name": "Ex-Partner", "phone": _BACKUP_PHONE}
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
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=2, seconds=1))
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=5, seconds=1))
        # T+10m: the backup contact's own ack token is minted and texted to
        # them for the first time here.
        await emergency_chain.run_emergency_chain_sweep(now=t0 + timedelta(minutes=10, seconds=1))

        notif = await _fetch_notification(db_session, notification_id)
        landlord_token = notif["payload"]["ack_token"]
        backup_token_before_removal = notif["payload"]["ack_token_backup"]
        assert backup_token_before_removal is not None

        # The ex-partner scenario: remove the backup contact through the
        # REAL PATCH handler -- not a raw SQL UPDATE -- exercising the
        # actual code path a real request runs.
        await update_property(
            uuid.UUID(property_id),
            PropertyUpdateRequest(backup_contact=None),
            (Landlord(id=uuid.UUID(landlord_id)), db_session),
        )
        await db_session.commit()

        # The link the removed backup contact already holds must no longer
        # acknowledge anything -- this is the "cannot permanently silence a
        # live emergency" guarantee #289 exists to build.
        revoked_result = await emergency_chain.acknowledge_by_token(
            backup_token_before_removal, channel="sms_link"
        )
        assert revoked_result is None

        chain_after_revoked_attempt = await _fetch_notification(db_session, notification_id)
        assert chain_after_revoked_attempt["status"] == "pending"
        assert chain_after_revoked_attempt["acknowledged_at"] is None

        # The landlord's own token for the SAME notification -- a
        # completely different value, never touched by the revocation --
        # still works.
        landlord_result = await emergency_chain.acknowledge_by_token(
            landlord_token, channel="sms_link"
        )
        assert landlord_result is not None
        acked_notification_id, _acknowledged_at = landlord_result
        assert str(acked_notification_id) == notification_id

        chain_after_landlord_ack = await _fetch_notification(db_session, notification_id)
        assert chain_after_landlord_ack["status"] == "acknowledged"
        assert chain_after_landlord_ack["acknowledged_at"] is not None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_clearing_backup_contact_with_no_in_flight_chain_is_a_safe_no_op(
    db_session: AsyncSession,
) -> None:
    """:func:`emergency_chain.revoke_backup_ack_tokens` must be a true
    no-op (no error, touches zero rows) when there is no live
    ``emergency_call`` chain for the property at all -- the common case
    for most ``PATCH`` requests that clear a backup contact."""
    from app.deps import Landlord
    from app.routers.properties import PropertyUpdateRequest, update_property

    landlord_id, property_id, _tenant_id = await _seed(
        db_session, backup_contact={"name": "Bob", "phone": _BACKUP_PHONE}, with_tenant=False
    )

    try:
        response = await update_property(
            uuid.UUID(property_id),
            PropertyUpdateRequest(backup_contact=None),
            (Landlord(id=uuid.UUID(landlord_id)), db_session),
        )
        assert response.backup_contact is None
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


async def _insert_emergency_sms_notification(
    session: AsyncSession,
    *,
    landlord_id: str,
    message_id: str,
    property_id: str,
    category: str,
    body: str,
) -> str:
    """Mirrors the SHAPE of ``emergency_chain.py``'s own
    ``_INSERT_EMERGENCY_SMS_SQL`` (same payload keys) so a test can seed an
    ``emergency_sms`` row directly, without needing a full ``emergency_call``
    chain / step-0 processing run first -- used to deterministically drive
    the cross-path claim sequence in
    ``test_inline_claim_after_sweep_failure_marks_sent_not_resent`` below."""
    notification_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO notifications (id, landlord_id, case_id, type, channel, status, payload) "
            "VALUES (:id, :landlord_id, NULL, 'emergency_sms', 'sms', 'pending', "
            "CAST(:payload AS jsonb))"
        ),
        {
            "id": notification_id,
            "landlord_id": landlord_id,
            "payload": json.dumps(
                {
                    "message_id": message_id,
                    "property_id": property_id,
                    "category": category,
                    "body": body,
                }
            ),
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
async def test_inline_claim_after_sweep_failure_marks_sent_not_resent(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """Regression for issue #186's confirmed live bug (evidence-based
    triage, cross-path duplicate-send): an ``emergency_sms`` row can be
    claimed and sent by TWO independent code paths over its lifetime --
    ``run_sms_drain_sweep``'s own drain (``_process_sms_drain_candidate``)
    and the inline claim ``_execute_action`` uses for step 0
    (``_claim_emergency_sms_for_send`` + ``_mark_emergency_sms_status``).
    Both share the SAME claim CAS (``_CLAIM_SMS_DRAIN_SQL``), which treats
    ``status IN ('pending', 'failed')`` as fair game -- so a row the drain
    sweep marked ``'failed'`` can still be picked up and successfully sent
    by the inline path afterwards. Before this fix,
    ``_MARK_EMERGENCY_SMS_SQL`` only matched ``status = 'pending'``, so
    that later successful send silently no-opped: the row stayed
    ``'failed'`` forever, and the NEXT drain tick resent the tenant safety
    SMS again, indefinitely."""
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
    )
    category, body = emergency_chain.render_tenant_safety_sms(["fire"])
    notification_id = await _insert_emergency_sms_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        category=category,
        body=body,
    )

    try:
        # 1. The drain sweep claims the row first and its send FAILS (fake
        #    sender raises) -- mirrors a sweep tick winning the race
        #    against the inline step-0 path, per
        #    _claim_emergency_sms_for_send's own docstring.
        fake_sender.fail_sms = True
        sweep_outcomes = await emergency_chain.run_sms_drain_sweep()
        assert len(sweep_outcomes) == 1
        assert sweep_outcomes[0].outcome == "failed"
        assert sweep_outcomes[0].notification_type == "emergency_sms"
        assert len(fake_sender.calls) == 0, "the fake sender raises before recording a call"

        failed_row = await _fetch_notification(db_session, notification_id)
        assert failed_row["status"] == "failed"
        assert failed_row["attempt"] == 1

        # 2. A SUBSEQUENT claim+send via the INLINE code path succeeds --
        #    the exact _claim_emergency_sms_for_send (inside
        #    _execute_action) + _mark_emergency_sms_status pair
        #    _process_due_row's step-0 handling uses.
        fake_sender.fail_sms = False
        ctx = await emergency_chain._load_context(  # noqa: SLF001
            db_session, uuid.UUID(message_id)
        )
        assert ctx is not None
        outcome = await emergency_chain._execute_action(  # noqa: SLF001
            fake_sender,
            emergency_chain._ACTION_TENANT_SAFETY_SMS,  # noqa: SLF001
            ctx,
            categories=["fire"],
            notification_id=uuid.uuid4(),
            ack_token="cross-path-test-token",  # noqa: S106 -- test fixture, not a secret
            ack_token_backup="cross-path-test-token-backup",  # noqa: S106 -- test fixture
            message_id=uuid.UUID(message_id),
        )
        assert outcome.status == "sent"
        assert len(fake_sender.calls) == 1

        await emergency_chain._mark_emergency_sms_status(  # noqa: SLF001
            db_session, uuid.UUID(message_id), [outcome]
        )
        await db_session.commit()

        healed_row = await _fetch_notification(db_session, notification_id)
        assert healed_row["status"] == "sent", (
            "a send that succeeds after a sweep-recorded failure must reach "
            "'sent', not stay stuck at 'failed' forever"
        )

        # 3. A further drain tick must send NOTHING -- the row is
        #    terminally 'sent' and excluded from the retry set; zero new
        #    fake-sender calls proves the duplicate-send loop is closed.
        final_outcomes = await emergency_chain.run_sms_drain_sweep()
        assert final_outcomes == []
        assert len(fake_sender.calls) == 1, "no new send may occur once the row is 'sent'"
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
        now = datetime.now(UTC)
        first_outcomes = await emergency_chain.run_sms_drain_sweep(now=now)
        assert first_outcomes[0].outcome == "failed"

        notif = await _fetch_notification(db_session, notification_id)
        assert notif["status"] == "failed"
        assert notif["attempt"] == 1

        # Past the tenant_ack backoff window (issue #229 safety re-review
        # round 2 -- capped at 1 hour).
        fake_sender.fail_sms = False
        second_outcomes = await emergency_chain.run_sms_drain_sweep(now=now + timedelta(hours=2))
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


# ---------------------------------------------------------------------------
# Wall-clock tick deadline (issue #229, PR #228 senior-review advisory 1) --
# mirrors tests/test_agent_draft_sender.py's / tests/test_push_outbox_sweep
# .py's own deadline test pattern exactly.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sms_drain_sweep_default_deadline_is_25_seconds() -> None:
    assert emergency_chain.DEFAULT_TICK_DEADLINE_SECONDS == 25.0


class _FakeClock:
    """A mutable, injectable time source for the sweep's deadline check —
    advanced explicitly by the fake sender below rather than sleeping for
    real seconds. Mirrors ``tests/test_agent_draft_sender.py``'s own
    ``_FakeClock``."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _DeadlineBlowingSmsSender:
    """Implements just the ``send_sms`` half of ``TwilioSender`` -- records
    every call (by recipient phone) and advances a shared :class:`_FakeClock`
    past the tick's deadline on its FIRST send, simulating a slow/hanging
    Twilio round-trip that must not be allowed to also delay claiming the
    OTHER due row in the same tick."""

    def __init__(self, clock: _FakeClock, *, advance_by: float) -> None:
        self._clock = clock
        self._advance_by = advance_by
        self.calls: list[str] = []

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        self.calls.append(to)
        self._clock.now += self._advance_by
        return f"SM{uuid.uuid4().hex}"


@pytest.mark.integration
async def test_sms_drain_sweep_stops_claiming_after_deadline_then_resumes_next_tick(
    db_session: AsyncSession,
) -> None:
    """Two due ``tenant_ack`` rows; the first send blows the (tiny,
    test-only) deadline. The SECOND due row must NOT be claimed in the same
    tick -- it stays 'pending' and due, claimed whole by the very next tick
    call. Nothing lost; never abandoned mid-claim -- this is what stops a
    hung Twilio call chain here from delaying the NEXT tick's
    ``run_emergency_chain_sweep``."""
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id_a = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id_b = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    notification_id_a = await _insert_tenant_ack_notification(
        db_session, landlord_id=landlord_id, message_id=message_id_a, body="Got your message A"
    )
    notification_id_b = await _insert_tenant_ack_notification(
        db_session, landlord_id=landlord_id, message_id=message_id_b, body="Got your message B"
    )
    clock = _FakeClock(start=0.0)
    sender = _DeadlineBlowingSmsSender(clock, advance_by=10.0)
    set_twilio_sender_for_tests(sender)

    try:
        outcomes = await emergency_chain.run_sms_drain_sweep(
            deadline_seconds=5.0, time_source=clock
        )
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "sent"
        assert len(sender.calls) == 1  # bounded: NOT both due rows attempted this tick

        notif_a = await _fetch_notification(db_session, notification_id_a)
        notif_b = await _fetch_notification(db_session, notification_id_b)
        statuses = {notif_a["status"], notif_b["status"]}
        assert statuses == {"sent", "pending"}  # exactly one sent, one left due

        # The next tick call (clock already past the first deadline window,
        # but the sweep recomputes its OWN start from time_source() every
        # call) claims and sends the leftover row.
        outcomes_second_tick = await emergency_chain.run_sms_drain_sweep(
            deadline_seconds=5.0, time_source=clock
        )
        assert len(outcomes_second_tick) == 1
        assert outcomes_second_tick[0].outcome == "sent"
        assert len(sender.calls) == 2

        notif_a_after = await _fetch_notification(db_session, notification_id_a)
        notif_b_after = await _fetch_notification(db_session, notification_id_b)
        assert notif_a_after["status"] == "sent"
        assert notif_b_after["status"] == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# TWO PASSES -- emergency_sms is never starved by a poisoned tenant_ack
# backlog (safety re-review, blocking finding 1, 2026-08-01).
# ---------------------------------------------------------------------------


class _PoisonedTenantAckSender:
    """Fails (raising, simulating a hung Twilio call) for every ``send_sms``
    whose body matches *poison_body*; succeeds instantly for anything else
    (the emergency_sms body). Advances a shared :class:`_FakeClock` on
    every failure — mirrors a real Twilio HTTP timeout burning wall-clock
    time, the exact PoC'd starvation mechanism this test regresses."""

    def __init__(self, clock: _FakeClock, *, poison_body: str, advance_by: float) -> None:
        self._clock = clock
        self._poison_body = poison_body
        self._advance_by = advance_by
        self.calls: list[str] = []

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        self.calls.append(body)
        if body == self._poison_body:
            self._clock.now += self._advance_by
            raise RuntimeError("simulated twilio timeout")
        return f"SM{uuid.uuid4().hex}"


@pytest.mark.integration
async def test_sms_drain_sweep_emergency_sms_not_starved_by_poisoned_tenant_ack_rows(
    db_session: AsyncSession,
) -> None:
    """Safety re-review, blocking finding 1, 2026-08-01 -- regression for a
    reproduced starvation bug: an EARLIER revision of this sweep's 25s
    deadline drained ``tenant_ack`` and ``emergency_sms`` from ONE shared
    ``ORDER BY created_at`` queue under the SAME budget. Three OLDER,
    permanently-failing ``tenant_ack`` rows (each risking the full Twilio
    timeout) could consume the entire deadline before the loop ever
    reached a NEWER, genuinely-due ``emergency_sms`` row — the one
    non-redundant tenant-facing message in the whole chain (see module
    docstring "Idempotency") — silently never attempted, tick after tick.

    Fixed with two SEPARATE passes (see module docstring "TWO PASSES"):
    ``emergency_sms`` is now UNBOUNDED and runs FIRST, so it is sent on the
    very FIRST sweep call regardless of how many poisoned ``tenant_ack``
    rows sort ahead of it in ``created_at`` order, and regardless of how
    tiny *deadline_seconds* is (the deadline only ever bounds the
    ``tenant_ack`` pass, which runs second).

    Note (reviewer advisory): this test does not itself pin WHICH pass
    runs first -- that ordering is a LATENCY property (which row type gets
    attempted soonest within a tick), not a safety one; the actual safety
    guarantee under test is that emergency_sms is never starved, which
    holds regardless of pass order."""
    landlord_id, property_id, safe_tenant_id = await _seed(db_session)

    poison_notification_ids: list[str] = []
    for _ in range(3):
        poison_tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
        poison_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=poison_tenant_id,
        )
        poison_notification_ids.append(
            await _insert_tenant_ack_notification(
                db_session, landlord_id=landlord_id, message_id=poison_message_id, body="poison"
            )
        )

    # Force these OLDER in created_at than the emergency_sms row below --
    # ORDER BY created_at is exactly what let a stale tenant_ack backlog
    # sort ahead of a fresh emergency_sms under the (now-removed)
    # single-queue version of this deadline.
    for notification_id in poison_notification_ids:
        await db_session.execute(
            text("UPDATE notifications SET created_at = now() - interval '2 hours' WHERE id = :id"),
            {"id": notification_id},
        )
    await db_session.commit()

    safe_message_id = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=safe_tenant_id
    )
    category, body = emergency_chain.render_tenant_safety_sms(["fire"])
    emergency_notification_id = await _insert_emergency_sms_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=safe_message_id,
        property_id=property_id,
        category=category,
        body=body,
    )

    clock = _FakeClock(start=0.0)
    sender = _PoisonedTenantAckSender(clock, poison_body="poison", advance_by=10.0)
    set_twilio_sender_for_tests(sender)

    try:
        # A tiny 5s deadline -- three poison sends alone (10s each,
        # matching the real Twilio HTTP timeout) would blow it many times
        # over if emergency_sms shared the same budget/queue as an earlier
        # revision of this fix did. It does not: this ONE sweep call still
        # sends emergency_sms, on the first call, despite the deadline.
        outcomes = await emergency_chain.run_sms_drain_sweep(
            deadline_seconds=5.0, time_source=clock
        )

        emergency_outcome = next(
            o for o in outcomes if str(o.notification_id) == emergency_notification_id
        )
        assert emergency_outcome.outcome == "sent"

        notif = await _fetch_notification(db_session, emergency_notification_id)
        assert notif["status"] == "sent"
        assert notif["attempt"] == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Exponential backoff on tenant_ack's next_attempt_at (issue #229 safety
# re-review round 2, blocking finding, 2026-08-01 -- SYMMETRY with the
# identical landlord_sms.py fix); emergency_sms is explicitly PINNED to have
# NO backoff filter at all -- it must keep its unconditional every-tick
# retry forever (see module docstring "TWO PASSES").
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attempt", "expected_seconds"),
    [
        (1, 60.0),
        (2, 120.0),
        (3, 240.0),
        (4, 480.0),
        (5, 960.0),
        (6, 1920.0),
        (7, 3600.0),  # 60 * 2**6 = 3840, capped
        (8, 3600.0),  # stays capped for every further attempt
    ],
)
def test_tenant_ack_backoff_progression(attempt: int, expected_seconds: float) -> None:
    """Pins the exact progression -- same formula and constants as
    ``app/agent/landlord_sms.py``'s own ``_landlord_sms_backoff_seconds``
    (issue #229 safety re-review round 2's reviewer-specified formula):
    ``60s * 2**(attempt - 1)``, capped at 3600s."""
    assert (
        emergency_chain._tenant_ack_backoff_seconds(attempt)  # noqa: SLF001
        == expected_seconds
    )


@pytest.mark.unit
def test_emergency_sms_pass_select_has_no_backoff_filter() -> None:
    """Structural pin (per the reviewer's own advisory): a future
    "cleanup" harmonizing the two drain-pass SELECTs must never add a
    ``next_attempt_at`` filter to the ``emergency_sms`` pass -- that row
    type must retry EVERY tick, unconditionally (see
    ``_SELECT_DUE_EMERGENCY_SMS_DRAIN_SQL``'s own comment). Doing so would
    silently reopen blocking finding 1's own starvation direction (a stuck
    emergency_sms candidate quietly backing off instead of being retried
    every tick -- the one non-redundant tenant-facing message in the whole
    chain)."""
    assert "next_attempt_at" not in str(
        emergency_chain._SELECT_DUE_EMERGENCY_SMS_DRAIN_SQL  # noqa: SLF001
    )


@pytest.mark.integration
async def test_emergency_sms_pass_retries_every_tick_with_no_backoff(
    db_session: AsyncSession, fake_sender: FakeTwilioSender
) -> None:
    """Behavioral pin, complementing the structural one above: an
    ``emergency_sms`` row that keeps failing is retried on EVERY call, even
    when ``now`` never advances at all between calls -- proving no
    backoff/``next_attempt_at`` window ever gates it, unlike ``tenant_ack``
    (contrast
    ``test_sms_drain_sweep_marks_failed_and_retries_on_the_next_tick``
    above, which DOES need ``now`` to advance past its own backoff before
    a retry succeeds)."""
    landlord_id, property_id, tenant_id = await _seed(db_session)
    message_id = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    category, body = emergency_chain.render_tenant_safety_sms(["fire"])
    notification_id = await _insert_emergency_sms_notification(
        db_session,
        landlord_id=landlord_id,
        message_id=message_id,
        property_id=property_id,
        category=category,
        body=body,
    )

    try:
        fake_sender.fail_sms = True
        now = datetime.now(UTC)  # deliberately the SAME `now` every call
        for expected_attempt in range(1, 4):
            outcomes = await emergency_chain.run_sms_drain_sweep(now=now)
            assert [o.outcome for o in outcomes] == ["failed"]
            notif = await _fetch_notification(db_session, notification_id)
            assert notif["status"] == "failed"
            assert notif["attempt"] == expected_attempt
    finally:
        await _cleanup(db_session, landlord_id)
