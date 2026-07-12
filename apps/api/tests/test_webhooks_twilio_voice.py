"""Integration tests for POST /webhooks/twilio/voice (#108) — the TwiML
callback for the emergency escalation chain's landlord/backup voice calls.

Marker: ``integration`` — requires docker-compose Postgres + alembic
upgrade head. Same harness convention as ``tests/test_webhooks_twilio_sms.py``
(``httpx.AsyncClient`` + ``ASGITransport`` against the live app, requests
signed with the app's own ``settings.twilio_auth_token``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.config import settings
from app.integrations.twilio import compute_signature
from app.main import app
from tests import factories

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"
_VOICE_PATH = "/webhooks/twilio/voice"


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


async def _insert_emergency_call_notification(
    session: AsyncSession, *, landlord_id: str, message_id: str, property_id: str
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
                {"message_id": message_id, "property_id": property_id, "categories": ["fire"]}
            ),
        },
    )
    await session.commit()
    return notification_id


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    for table in ("audit_log", "notifications", "messages", "tenants", "properties"):
        await session.execute(
            text(f"DELETE FROM {table} WHERE landlord_id = :lid"),  # noqa: S608
            {"lid": landlord_id},
        )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


def _call_params(
    *, call_sid: str, from_number: str, to_number: str, digits: str | None = None
) -> dict[str, str]:
    params = {
        "CallSid": call_sid,
        "AccountSid": "AC" + "0" * 32,
        "From": from_number,
        "To": to_number,
        "CallStatus": "in-progress",
        "Direction": "outbound-api",
    }
    if digits is not None:
        params["Digits"] = digits
    return params


async def _post_voice(
    query: str, params: dict[str, str], *, signature: str | None | object = "__default__"
) -> httpx.Response:
    url = f"http://test{_VOICE_PATH}{query}"
    if signature == "__default__":
        assert settings.twilio_auth_token is not None
        signature = compute_signature(url, params, settings.twilio_auth_token)

    headers: dict[str, str] = {}
    if signature is not None:
        headers["X-Twilio-Signature"] = str(signature)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(f"{_VOICE_PATH}{query}", data=params, headers=headers)


@pytest.mark.integration
async def test_missing_signature_returns_403(db_session: AsyncSession) -> None:
    notification_id = uuid.uuid4()
    response = await _post_voice(
        f"?notification_id={notification_id}",
        _call_params(call_sid="CA1", from_number="+14165550100", to_number="+14165559999"),
        signature=None,
    )
    assert response.status_code == 403


@pytest.mark.integration
async def test_initial_fetch_returns_gather_twiml_with_property_and_category(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session, phone="+14165550100")
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165559999"
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
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
    )

    try:
        response = await _post_voice(
            f"?notification_id={notification_id}",
            _call_params(call_sid="CA1", from_number="+14165550100", to_number="+14165559999"),
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/xml")
        assert "<Gather" in response.text
        assert "Press 1 to acknowledge" in response.text
        assert "Test Property" in response.text

        notif = (
            (
                await db_session.execute(
                    text("SELECT acknowledged_at FROM notifications WHERE id = :id"),
                    {"id": notification_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif["acknowledged_at"] is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_digits_one_acknowledges_and_stops_the_chain(db_session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(db_session, phone="+14165550100")
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165559999"
    )
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
    )

    try:
        response = await _post_voice(
            f"?notification_id={notification_id}",
            _call_params(
                call_sid="CA1", from_number="+14165550100", to_number="+14165559999", digits="1"
            ),
        )
        assert response.status_code == 200
        assert "Thanks" in response.text

        notif = (
            (
                await db_session.execute(
                    text("SELECT status, acknowledged_at FROM notifications WHERE id = :id"),
                    {"id": notification_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif["status"] == "acknowledged"
        assert notif["acknowledged_at"] is not None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_wrong_digit_does_not_acknowledge(db_session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(db_session, phone="+14165550100")
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165559999"
    )
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
    )

    try:
        response = await _post_voice(
            f"?notification_id={notification_id}",
            _call_params(
                call_sid="CA1", from_number="+14165550100", to_number="+14165559999", digits="9"
            ),
        )
        assert response.status_code == 200

        notif = (
            (
                await db_session.execute(
                    text("SELECT acknowledged_at FROM notifications WHERE id = :id"),
                    {"id": notification_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif["acknowledged_at"] is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_missing_notification_id_returns_200_error_twiml(db_session: AsyncSession) -> None:
    response = await _post_voice(
        "",
        _call_params(call_sid="CA1", from_number="+14165550100", to_number="+14165559999"),
    )
    assert response.status_code == 200
    assert "<Say>" in response.text


@pytest.mark.integration
async def test_malformed_notification_id_returns_200_error_twiml(db_session: AsyncSession) -> None:
    response = await _post_voice(
        "?notification_id=not-a-uuid",
        _call_params(call_sid="CA1", from_number="+14165550100", to_number="+14165559999"),
    )
    assert response.status_code == 200
    assert "<Say>" in response.text


@pytest.mark.integration
async def test_unknown_notification_id_returns_200_error_twiml(db_session: AsyncSession) -> None:
    response = await _post_voice(
        f"?notification_id={uuid.uuid4()}",
        _call_params(call_sid="CA1", from_number="+14165550100", to_number="+14165559999"),
    )
    assert response.status_code == 200
    assert "<Say>" in response.text
