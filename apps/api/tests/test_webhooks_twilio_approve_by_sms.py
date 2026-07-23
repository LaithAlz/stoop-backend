"""Integration tests for approve-by-SMS (#122) — the landlord "1"/"2"/
"UNDO" reply flow on ``POST /webhooks/twilio/sms``.

Marker: ``integration`` — requires docker-compose Postgres + alembic
upgrade head. Self-contained per ``tests/test_webhooks_twilio_sms.py``'s
own stated convention (helpers duplicated, not imported) — this module
follows that same convention rather than importing from that sibling
file or from ``tests/factories.py``.

Cleanup ordering note (#122-specific): unlike every prior scenario in the
sibling webhooks test file, a landlord-channel message's ``case_id`` CAN
now be non-NULL (schema-v1.md v1.1: "case_id = the referenced draft's
case"), and ``messages.case_id`` has NO ``ON DELETE CASCADE`` — deleting a
``cases`` row while a ``messages`` row still references it raises a FK
violation. This file's own ``_cleanup`` therefore deletes ``messages``
BEFORE ``cases`` (the opposite order from the sibling file's, which never
needed to care because ``case_id`` was always ``NULL`` for every scenario
it covers).

Harness mirrors ``tests/test_webhooks_twilio_sms.py`` exactly (httpx +
ASGITransport against the real app, Twilio-signature-signed requests).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.config import settings
from app.integrations.twilio import compute_signature
from app.main import app

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"
_SIGNING_URL = "http://test/webhooks/twilio/sms"


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


@pytest_asyncio.fixture(autouse=True)
async def _checkpointer_lifecycle(_migrate_once: None) -> AsyncGenerator[None, None]:
    """``httpx.ASGITransport`` does NOT run FastAPI's own lifespan (no
    ``setup_checkpointer()`` call), but approve-by-SMS's approve/reject
    dispatch reaches ``resolve_draft_decision``/``resume_case_thread``,
    which need a live checkpointer pool — same ordering contract as
    ``tests/test_drafts_router.py``'s own identical fixture."""
    await setup_checkpointer()
    yield
    await close_checkpointer()


# ---------------------------------------------------------------------------
# Seeding helpers (self-contained, mirrors the sibling file's own shapes)
# ---------------------------------------------------------------------------


def _fresh_phone() -> str:
    return f"+1416{uuid.uuid4().int % 10_000_000:07d}"


async def _insert_landlord(session: AsyncSession, *, phone: str) -> str:
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO landlords (id, auth_user_id, email, phone) "
            "VALUES (:id, :auth_id, :email, :phone)"
        ),
        {
            "id": landlord_id,
            "auth_id": str(uuid.uuid4()),
            "email": f"{landlord_id}@example.com",
            "phone": phone,
        },
    )
    await session.commit()
    return landlord_id


async def _insert_property(session: AsyncSession, landlord_id: str, *, twilio_number: str) -> str:
    """``address_line1`` is derived from the freshly generated ``property_id``
    (globally unique) rather than a fixed literal — schema-v1.md's v1.15
    amendment (#203 item 2, migration 0013) added a landlord-scoped,
    normalized-address UNIQUE index on ``properties``, and this module's
    own "different property, same landlord" scenario (below) inserts two
    properties for one landlord in a single test; a fixed literal address
    would collide against that index. Matches ``tests/factories.py``'s own
    identical fix for the same migration."""
    property_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO properties (id, landlord_id, label, address_line1, city, twilio_number) "
            "VALUES (:id, :landlord_id, 'Palmerston', :address_line1, 'Toronto', :twilio_number)"
        ),
        {
            "id": property_id,
            "landlord_id": landlord_id,
            "address_line1": f"{property_id} Palmerston",
            "twilio_number": twilio_number,
        },
    )
    await session.commit()
    return property_id


async def _insert_tenant(
    session: AsyncSession, landlord_id: str, property_id: str, *, name: str = "Maria"
) -> str:
    tenant_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO tenants (id, landlord_id, property_id, phone, name, unit) "
            "VALUES (:id, :landlord_id, :property_id, :phone, :name, '2')"
        ),
        {
            "id": tenant_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "phone": _fresh_phone(),
            "name": name,
        },
    )
    await session.commit()
    return tenant_id


async def _insert_case(
    session: AsyncSession, *, landlord_id: str, property_id: str, tenant_id: str, status: str
) -> str:
    case_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO cases (id, landlord_id, property_id, tenant_id, status, "
            "langgraph_thread_id) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, :status, :thread_id)"
        ),
        {
            "id": case_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "status": status,
            "thread_id": str(uuid.uuid4()),
        },
    )
    await session.commit()
    return case_id


async def _insert_draft(
    session: AsyncSession,
    *,
    landlord_id: str,
    case_id: str,
    status: str,
    scheduled_send_at: datetime | None = None,
    approved_via: str | None = None,
    body: str = "Hi Maria - so sorry to hear that. I'll have someone out first thing tomorrow.",
) -> str:
    draft_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO drafts (id, landlord_id, case_id, recipient, body, prompt_version, "
            "status, scheduled_send_at, approved_via) "
            "VALUES (:id, :landlord_id, :case_id, 'tenant', :body, 'v1', :status, "
            ":scheduled_send_at, :approved_via)"
        ),
        {
            "id": draft_id,
            "landlord_id": landlord_id,
            "case_id": case_id,
            "body": body,
            "status": status,
            "scheduled_send_at": scheduled_send_at,
            "approved_via": approved_via,
        },
    )
    await session.commit()
    return draft_id


async def _insert_ready_notification(
    session: AsyncSession,
    *,
    landlord_id: str,
    case_id: str,
    draft_id: str,
    created_at: datetime | None = None,
) -> str:
    notification_id = str(uuid.uuid4())
    payload = {"draft_id": draft_id, "kind": "ready", "body": "Draft ready..."}
    await session.execute(
        text(
            "INSERT INTO notifications "
            "(id, landlord_id, case_id, type, channel, status, payload, created_at) "
            "VALUES (:id, :landlord_id, :case_id, 'draft_ready', 'sms', 'pending', "
            "CAST(:payload AS jsonb), COALESCE(:created_at, now()))"
        ),
        {
            "id": notification_id,
            "landlord_id": landlord_id,
            "case_id": case_id,
            "payload": json.dumps(payload),
            "created_at": created_at,
        },
    )
    await session.commit()
    return notification_id


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    """Deletes ``messages`` BEFORE ``cases`` — see module docstring
    "Cleanup ordering note"."""
    await session.execute(
        text(
            "DELETE FROM message_cases WHERE case_id IN "
            "(SELECT id FROM cases WHERE landlord_id = :lid)"
        ),
        {"lid": landlord_id},
    )
    await session.execute(text("DELETE FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text(
            "DELETE FROM message_status_events WHERE message_id IN "
            "(SELECT id FROM messages WHERE landlord_id = :lid)"
        ),
        {"lid": landlord_id},
    )
    await session.execute(
        text("DELETE FROM audit_log WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM notifications WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
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


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _sms_params(*, message_sid: str, from_number: str, to_number: str, body: str) -> dict[str, str]:
    return {
        "MessageSid": message_sid,
        "SmsSid": message_sid,
        "AccountSid": "AC" + "0" * 32,
        "From": from_number,
        "To": to_number,
        "Body": body,
        "NumMedia": "0",
    }


def _sign(params: dict[str, str]) -> str:
    assert settings.twilio_auth_token is not None
    return compute_signature(_SIGNING_URL, params, settings.twilio_auth_token)


async def _post_sms(params: dict[str, str]) -> httpx.Response:
    headers = {"X-Twilio-Signature": _sign(params)}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/webhooks/twilio/sms", data=params, headers=headers)


async def _fetch_draft(session: AsyncSession, draft_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text("SELECT status, scheduled_send_at, approved_via FROM drafts WHERE id = :id"),
                {"id": draft_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def _fetch_landlord_sms_rows(
    session: AsyncSession, *, landlord_id: str
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT payload FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'draft_ready' AND channel = 'sms' "
                    "ORDER BY created_at"
                ),
                {"lid": landlord_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 1 -> approve
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reply_1_approves_pending_draft_with_5min_window(db_session: AsyncSession) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
    )
    draft_id = await _insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, status="pending"
    )
    await _insert_ready_notification(
        db_session, landlord_id=landlord_id, case_id=case_id, draft_id=draft_id
    )

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=landlord_phone, to_number=to_number, body="1"
    )

    try:
        before = datetime.now(UTC)
        response = await _post_sms(params)
        assert response.status_code == 200

        draft = await _fetch_draft(db_session, draft_id)
        assert draft["status"] == "approved"
        assert draft["approved_via"] == "sms"
        scheduled_send_at = draft["scheduled_send_at"]
        assert scheduled_send_at is not None
        delta = scheduled_send_at - before
        # ~5 minutes, not the dashboard's ~5 seconds.
        assert timedelta(minutes=4) < delta < timedelta(minutes=6)

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor FROM audit_log WHERE case_id = :cid AND action = 'approved'"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["actor"] == "landlord"

        # The landlord's own reply message is durably stored, case_id set,
        # never forwarded to the tenant (party stays 'landlord', direction
        # 'inbound' -- no outbound message was created FOR this reply).
        message_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT party, tenant_id, vendor_id, case_id, direction FROM messages "
                        "WHERE twilio_sid = :sid"
                    ),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["party"] == "landlord"
        assert message_row["tenant_id"] is None
        assert message_row["vendor_id"] is None
        assert str(message_row["case_id"]) == case_id
        assert message_row["direction"] == "inbound"

        # A confirmation SMS was enqueued (durable outbox), never sent inline.
        rows = await _fetch_landlord_sms_rows(db_session, landlord_id=landlord_id)
        kinds = [row["payload"]["kind"] for row in rows]
        assert "approved" in kinds
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_reply_1_twice_is_idempotent_never_reruns_writer(db_session: AsyncSession) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
    )
    draft_id = await _insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, status="pending"
    )
    await _insert_ready_notification(
        db_session, landlord_id=landlord_id, case_id=case_id, draft_id=draft_id
    )

    try:
        first = _sms_params(
            message_sid=f"SM{uuid.uuid4().hex}",
            from_number=landlord_phone,
            to_number=to_number,
            body="1",
        )
        r1 = await _post_sms(first)
        assert r1.status_code == 200
        draft_after_first = await _fetch_draft(db_session, draft_id)

        # A SECOND, distinct reply ("1" again) must re-confirm idempotently,
        # never re-run the approve writer (which would raise trying to
        # transition an already-'approved' row a second time via the
        # pending-only WHERE clause -- it simply wouldn't match, and this
        # path short-circuits before even trying).
        second = _sms_params(
            message_sid=f"SM{uuid.uuid4().hex}",
            from_number=landlord_phone,
            to_number=to_number,
            body="1",
        )
        r2 = await _post_sms(second)
        assert r2.status_code == 200

        draft_after_second = await _fetch_draft(db_session, draft_id)
        assert draft_after_second["status"] == "approved"
        assert draft_after_second["scheduled_send_at"] == draft_after_first["scheduled_send_at"]

        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid AND action = 'approved'"),
                {"cid": case_id},
            )
        ).scalar_one()
        assert audit_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2 -> reject
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reply_2_rejects_pending_draft_case_stays_open(db_session: AsyncSession) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
    )
    draft_id = await _insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, status="pending"
    )
    await _insert_ready_notification(
        db_session, landlord_id=landlord_id, case_id=case_id, draft_id=draft_id
    )

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=landlord_phone, to_number=to_number, body="2"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        draft = await _fetch_draft(db_session, draft_id)
        assert draft["status"] == "rejected"

        case_row = (
            (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :id"), {"id": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert case_row["status"] == "open"

        rows = await _fetch_landlord_sms_rows(db_session, landlord_id=landlord_id)
        kinds = [row["payload"]["kind"] for row in rows]
        assert "rejected" in kinds
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# UNDO
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reply_undo_within_window_reverts_to_pending(db_session: AsyncSession) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
    )
    draft_id = await _insert_draft(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="approved",
        scheduled_send_at=datetime.now(UTC) + timedelta(minutes=4),
        approved_via="sms",
    )
    await _insert_ready_notification(
        db_session, landlord_id=landlord_id, case_id=case_id, draft_id=draft_id
    )

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=landlord_phone, to_number=to_number, body="undo"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        draft = await _fetch_draft(db_session, draft_id)
        assert draft["status"] == "pending"
        assert draft["scheduled_send_at"] is None
        assert draft["approved_via"] is None

        rows = await _fetch_landlord_sms_rows(db_session, landlord_id=landlord_id)
        kinds = [row["payload"]["kind"] for row in rows]
        assert "undo" in kinds
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_reply_undo_when_already_sent_cannot_undo(db_session: AsyncSession) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_tenant",
    )
    draft_id = await _insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, status="sent", approved_via="sms"
    )
    await _insert_ready_notification(
        db_session, landlord_id=landlord_id, case_id=case_id, draft_id=draft_id
    )

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=landlord_phone, to_number=to_number, body="undo"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        draft = await _fetch_draft(db_session, draft_id)
        assert draft["status"] == "sent"  # unchanged
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Stale-draft race (AC 4)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_stale_draft_replies_with_fresh_draft_notice_and_does_not_send(
    db_session: AsyncSession,
) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id, name="Maria")
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
    )
    # The draft the landlord was notified about has since gone stale (a
    # newer tenant message arrived) -- simulated directly.
    draft_id = await _insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, status="stale"
    )
    await _insert_ready_notification(
        db_session, landlord_id=landlord_id, case_id=case_id, draft_id=draft_id
    )

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=landlord_phone, to_number=to_number, body="1"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        draft = await _fetch_draft(db_session, draft_id)
        assert draft["status"] == "stale"  # untouched -- nothing was sent

        rows = await _fetch_landlord_sms_rows(db_session, landlord_id=landlord_id)
        stale_rows = [row for row in rows if row["payload"]["kind"] == "stale"]
        assert len(stale_rows) == 1
        assert "Maria" in stale_rows[0]["payload"]["body"]
        assert "fresh draft coming" in stale_rows[0]["payload"]["body"]
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Disambiguation (AC: multiple pending drafts, most-recent-notification wins)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_disambiguation_replies_map_to_most_recent_notification(
    db_session: AsyncSession,
) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)

    case_1 = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
    )
    draft_1 = await _insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_1, status="pending"
    )
    await _insert_ready_notification(
        db_session,
        landlord_id=landlord_id,
        case_id=case_1,
        draft_id=draft_1,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    case_2 = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
    )
    draft_2 = await _insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_2, status="pending"
    )
    await _insert_ready_notification(
        db_session,
        landlord_id=landlord_id,
        case_id=case_2,
        draft_id=draft_2,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=landlord_phone, to_number=to_number, body="1"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        draft_1_after = await _fetch_draft(db_session, draft_1)
        draft_2_after = await _fetch_draft(db_session, draft_2)
        assert draft_1_after["status"] == "pending"  # untouched
        assert draft_2_after["status"] == "approved"  # the MOST RECENT notice
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Unrecognized replies -- never silently dropped
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unrecognized_reply_falls_back_to_needs_eyes_never_dropped(
    db_session: AsyncSession,
) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
    )
    draft_id = await _insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, status="pending"
    )
    await _insert_ready_notification(
        db_session, landlord_id=landlord_id, case_id=case_id, draft_id=draft_id
    )

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=landlord_phone,
        to_number=to_number,
        body="can you call me instead?",
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        # The pending draft is completely untouched.
        draft = await _fetch_draft(db_session, draft_id)
        assert draft["status"] == "pending"

        needs_eyes_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'needs_eyes'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert needs_eyes_row["status"] == "pending"

        message_row = (
            (
                await db_session.execute(
                    text("SELECT party, case_id FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["party"] == "landlord"
        assert message_row["case_id"] is None  # unrecognized -- never correlated
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Property-scoped correlation -- never leaks a different property's draft
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reply_on_different_property_number_does_not_correlate(
    db_session: AsyncSession,
) -> None:
    """api-contracts.md: correlation is scoped to the property owning the
    `To` number. A landlord who manages two properties replying "1" on
    property B's number must never act on a draft that belongs to
    property A, even though it's the same landlord's most recent
    draft-ready notice overall."""
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)

    to_number_a = _fresh_phone()
    property_a = await _insert_property(db_session, landlord_id, twilio_number=to_number_a)
    tenant_a = await _insert_tenant(db_session, landlord_id, property_a)
    case_a = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_a,
        tenant_id=tenant_a,
        status="awaiting_approval",
    )
    draft_a = await _insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_a, status="pending"
    )
    await _insert_ready_notification(
        db_session, landlord_id=landlord_id, case_id=case_a, draft_id=draft_a
    )

    to_number_b = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number_b)
    # No tenant, no case, no draft, no draft-ready notice for property B.

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=landlord_phone, to_number=to_number_b, body="1"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        # Property A's draft is completely untouched.
        draft_a_after = await _fetch_draft(db_session, draft_a)
        assert draft_a_after["status"] == "pending"

        # Falls back to needs_eyes -- nothing to correlate against on B.
        needs_eyes_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'needs_eyes'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert needs_eyes_row["status"] == "pending"
    finally:
        await _cleanup(db_session, landlord_id)
