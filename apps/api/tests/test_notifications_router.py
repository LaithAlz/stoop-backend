"""Tests for ``app/routers/notifications.py`` (#108) — the two ack
surfaces: ``POST /v1/notifications/{id}/ack`` (dashboard/authenticated)
and ``GET /ack/{token}`` (public, tokenized SMS link).

``POST /v1/notifications/{id}/ack`` is tested by calling the handler
directly with a real ``(Landlord, AsyncSession)`` tuple obtained via
``app.deps.require_landlord`` — same precedent as
``tests/test_require_landlord.py``'s own module docstring ("prefer testing
the dependency directly ... FastAPI's Depends(...) wiring is just a
type-hint annotation"). Cross-tenant RLS enforcement itself (can landlord A
even SEE landlord B's ``notifications`` row) is proven generically for
EVERY table, including ``notifications``, by
``tests/test_rls_isolation_matrix.py`` — not duplicated here; this file
covers this endpoint's OWN logic (the re-SELECT-before-delegating guard,
idempotency, 404 shape).

``GET /ack/{token}`` needs no auth at all, so it IS tested via a real
HTTP round trip (``httpx.AsyncClient`` + ``ASGITransport``) — no JWT/JWKS
machinery needed.

Marker: ``integration`` — requires docker-compose Postgres + alembic
upgrade head.
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
from app.agent import emergency_chain
from app.deps import require_landlord
from app.errors import AppError
from app.integrations.supabase_auth import AuthUser
from app.integrations.twilio_send import set_twilio_sender_for_tests
from app.main import app
from tests import factories


class _NullTwilioSender:
    """A fake Twilio sender that accepts every call/SMS without touching
    the network — see never-break rule "NO LIVE SENDS in tests". This
    file only cares that ``GET /ack/{token}`` resolves and acknowledges
    correctly; the actual send content is exercised exhaustively in
    ``tests/test_agent_emergency_chain.py``."""

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        return f"SM{uuid.uuid4().hex}"

    async def create_call(self, *, to: str, from_: str, twiml_url: str) -> str:
        return f"CA{uuid.uuid4().hex}"


@pytest.fixture(autouse=True)
def _fake_twilio_sender() -> None:
    set_twilio_sender_for_tests(_NullTwilioSender())


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


def _auth_user_for(auth_user_id: str) -> AuthUser:
    return AuthUser(user_id=uuid.UUID(auth_user_id), email="test@example.com", full_name="Test")


async def _insert_landlord_with_auth_user(session: AsyncSession) -> tuple[str, str]:
    """A landlord whose ``auth_user_id`` we control (needed to construct a
    matching ``AuthUser`` for ``require_landlord``) — factories.insert_landlord
    generates a random one internally, so this is a small local variant."""
    auth_user_id = str(uuid.uuid4())
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"),
        {"id": landlord_id, "auth_id": auth_user_id, "email": f"{landlord_id}@example.com"},
    )
    await session.commit()
    return landlord_id, auth_user_id


async def _insert_emergency_call_notification(session: AsyncSession, *, landlord_id: str) -> str:
    notification_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO notifications (id, landlord_id, case_id, type, channel, status, payload) "
            "VALUES (:id, :landlord_id, NULL, 'emergency_call', 'voice', 'pending', '{}'::jsonb)"
        ),
        {"id": notification_id, "landlord_id": landlord_id},
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


# ---------------------------------------------------------------------------
# POST /v1/notifications/{id}/ack
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_ack_notification_happy_path(db_session: AsyncSession) -> None:
    from app.routers.notifications import ack_notification

    landlord_id, auth_user_id = await _insert_landlord_with_auth_user(db_session)
    notification_id = await _insert_emergency_call_notification(db_session, landlord_id=landlord_id)

    try:
        landlord_and_session = await require_landlord(_auth_user_for(auth_user_id), db_session)
        response = await ack_notification(uuid.UUID(notification_id), landlord_and_session)
        assert response.acknowledged_at is not None

        notif = (
            (
                await db_session.execute(
                    text("SELECT status FROM notifications WHERE id = :id"),
                    {"id": notification_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif["status"] == "acknowledged"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_ack_notification_is_idempotent(db_session: AsyncSession) -> None:
    from app.routers.notifications import ack_notification

    landlord_id, auth_user_id = await _insert_landlord_with_auth_user(db_session)
    notification_id = await _insert_emergency_call_notification(db_session, landlord_id=landlord_id)

    try:
        landlord_and_session = await require_landlord(_auth_user_for(auth_user_id), db_session)
        first = await ack_notification(uuid.UUID(notification_id), landlord_and_session)
        second = await ack_notification(uuid.UUID(notification_id), landlord_and_session)
        assert first.acknowledged_at == second.acknowledged_at
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_ack_notification_unknown_id_returns_404(db_session: AsyncSession) -> None:
    from app.routers.notifications import ack_notification

    landlord_id, auth_user_id = await _insert_landlord_with_auth_user(db_session)

    try:
        landlord_and_session = await require_landlord(_auth_user_for(auth_user_id), db_session)
        with pytest.raises(AppError) as exc_info:
            await ack_notification(uuid.uuid4(), landlord_and_session)
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "notification_not_found"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# GET /ack/{token} — public, real HTTP round trip
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_ack_by_token_http_round_trip(db_session: AsyncSession) -> None:
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
    notification_id = await _insert_emergency_call_notification(db_session, landlord_id=landlord_id)
    # Give it a proper emergency_call payload so handle_emergency_trigger can enrich it.
    await db_session.execute(
        text("UPDATE notifications SET payload = CAST(:payload AS jsonb) WHERE id = :id"),
        {
            "id": notification_id,
            "payload": json.dumps(
                {"message_id": message_id, "property_id": property_id, "categories": ["fire"]}
            ),
        },
    )
    await db_session.commit()

    try:
        await emergency_chain.handle_emergency_trigger(
            notification_id=uuid.UUID(notification_id),
            message_id=uuid.UUID(message_id),
            property_id=uuid.UUID(property_id),
            categories=["fire"],
        )
        notif = (
            (
                await db_session.execute(
                    text("SELECT payload FROM notifications WHERE id = :id"),
                    {"id": notification_id},
                )
            )
            .mappings()
            .one()
        )
        token = notif["payload"]["ack_token"]

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/ack/{token}")
        assert response.status_code == 200
        assert response.json()["acknowledged_at"] is not None

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            not_found = await client.get("/ack/not-a-real-token")
        assert not_found.status_code == 404
        assert not_found.json()["error"]["code"] == "notification_not_found"
    finally:
        await _cleanup(db_session, landlord_id)
