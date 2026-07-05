"""Integration tests for POST /webhooks/twilio/sms (issue #40).

Marker: ``integration`` — requires docker-compose Postgres + alembic
upgrade head. Self-contained per the project convention (helpers
duplicated, not imported, from ``tests/test_me.py`` / ``tests/
test_rls_isolation_matrix.py`` — see those files' own module docstrings).

Harness
-------
- ``httpx.AsyncClient`` with ``ASGITransport`` hits the live FastAPI app
  against the real docker-compose Postgres.
- Every request is signed with ``app.integrations.twilio.compute_signature``
  using the SAME ``settings.twilio_auth_token`` the app reads (the
  conftest.py placeholder), against the exact URL the app will reconstruct
  (``http://test`` + path — no ``PUBLIC_BASE_URL`` set in tests, so the
  fallback honors the ``Host`` header httpx sets to ``test``).
- Each test seeds its own landlord/property(/tenant) rows with fresh
  uuid4-derived phone numbers and cleans them up in FK-safe order at
  teardown, so the suite is re-runnable without wiping the DB.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent import prefilter
from app.config import settings
from app.integrations.twilio import compute_signature
from app.main import app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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
    """Same rationale as ``tests/test_me.py``'s fixture of the same name:
    the module-level admin engine (``app.db.session.engine``, backing
    ``get_admin_session``) must not carry pooled connections across event
    loops between tests."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _fresh_phone() -> str:
    return f"+1416{uuid.uuid4().int % 10_000_000:07d}"


async def _insert_landlord(session: AsyncSession, *, phone: str | None = None) -> str:
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
    property_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO properties (id, landlord_id, label, address_line1, city, twilio_number) "
            "VALUES (:id, :landlord_id, 'Test Property', '123 Test St', 'Toronto', :twilio_number)"
        ),
        {"id": property_id, "landlord_id": landlord_id, "twilio_number": twilio_number},
    )
    await session.commit()
    return property_id


async def _insert_tenant(
    session: AsyncSession, landlord_id: str, property_id: str, *, phone: str, active: bool = True
) -> str:
    tenant_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO tenants (id, landlord_id, property_id, phone, active) "
            "VALUES (:id, :landlord_id, :property_id, :phone, :active)"
        ),
        {
            "id": tenant_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "phone": phone,
            "active": active,
        },
    )
    await session.commit()
    return tenant_id


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
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


async def _post_sms(
    params: dict[str, str], *, signature: str | None | object = "__default__"
) -> httpx.Response:
    headers = {}
    if signature == "__default__":
        headers["X-Twilio-Signature"] = _sign(params)
    elif signature is not None:
        headers["X-Twilio-Signature"] = str(signature)
    # signature is None -> header omitted entirely (missing-signature case)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/webhooks/twilio/sms", data=params, headers=headers)


# ---------------------------------------------------------------------------
# Signature / malformed-form tests (before any persistence)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_invalid_signature_returns_403_nothing_persisted(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=_fresh_phone(), to_number=to_number, body="hello"
    )

    try:
        response = await _post_sms(params, signature="totally-wrong-signature")
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "invalid_signature"

        row = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE twilio_sid = :sid"),
                {"sid": message_sid},
            )
        ).scalar_one()
        assert row == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_missing_signature_returns_403(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    params = _sms_params(
        message_sid=f"SM{uuid.uuid4().hex}",
        from_number=_fresh_phone(),
        to_number=to_number,
        body="hello",
    )

    try:
        response = await _post_sms(params, signature=None)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invalid_signature"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_tampered_param_after_signing_returns_403(db_session: AsyncSession) -> None:
    """A signature computed over the original params must be rejected if a
    param is mutated afterward (classic tampering scenario)."""
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    params = _sms_params(
        message_sid=f"SM{uuid.uuid4().hex}",
        from_number=_fresh_phone(),
        to_number=to_number,
        body="hello",
    )
    signature = _sign(params)
    params["Body"] = "goodbye"  # tampered AFTER signing

    try:
        response = await _post_sms(params, signature=signature)
        assert response.status_code == 403
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_malformed_form_missing_body_returns_400(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = {
        "MessageSid": message_sid,
        "From": _fresh_phone(),
        "To": to_number,
        # Body deliberately omitted.
    }

    try:
        response = await _post_sms(params)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "malformed_webhook"

        row = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE twilio_sid = :sid"),
                {"sid": message_sid},
            )
        ).scalar_one()
        assert row == 0
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Unknown To number
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unknown_to_number_returns_200_no_row_persisted() -> None:
    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=_fresh_phone(),
        to_number=_fresh_phone(),  # matches no property
        body="hello",
    )

    response = await _post_sms(params)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert response.text == "<Response/>"


# ---------------------------------------------------------------------------
# Dedupe idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_duplicate_message_sid_idempotent_one_row_one_notification_one_audit(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    from_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=from_number,
        to_number=to_number,
        body="there is a fire!",
    )

    try:
        r1 = await _post_sms(params)
        r2 = await _post_sms(params)  # identical redelivery

        assert r1.status_code == 200
        assert r2.status_code == 200

        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE twilio_sid = :sid"),
                {"sid": message_sid},
            )
        ).scalar_one()
        assert message_count == 1

        notification_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'emergency_call'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert notification_count == 1

        audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'emergency_triggered'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Tier-0 HARD hit — tenant path
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_hard_hit_tenant_message_creates_emergency_artifacts_exactly_once(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    from_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=from_number,
        to_number=to_number,
        body="there is a fire in the kitchen!",
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        message_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT id, party, tenant_id, prefilter FROM messages "
                        "WHERE twilio_sid = :sid"
                    ),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["party"] == "tenant"
        assert message_row["tenant_id"] is None  # unrecognized tenant phone
        assert message_row["prefilter"]["hard_hit"] is True
        assert "fire" in message_row["prefilter"]["categories"]

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor, payload FROM audit_log "
                        "WHERE landlord_id = :lid AND action = 'emergency_triggered'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["actor"] == "prefilter"
        assert "fire" in audit_row["payload"]["rules_fired"]

        notification_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT type, channel, status FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'emergency_call'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert notification_row["channel"] == "voice"
        assert notification_row["status"] == "pending"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_routine_tenant_message_creates_no_emergency_artifacts(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=_fresh_phone(),
        to_number=to_number,
        body="hey, when will the plumber come by?",
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        message_row = (
            (
                await db_session.execute(
                    text("SELECT prefilter FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["prefilter"]["hard_hit"] is False

        audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'emergency_triggered'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_count == 0

        notification_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'emergency_call'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert notification_count == 0

        # AC #4: the background classification stub still ran (message_received).
        received_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'message_received' "
                    "AND payload ->> 'message_id' = "
                    "(SELECT id::text FROM messages WHERE twilio_sid = :sid)"
                ),
                {"lid": landlord_id, "sid": message_sid},
            )
        ).scalar_one()
        assert received_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_soft_annotation_alone_creates_no_emergency_artifacts(
    db_session: AsyncSession,
) -> None:
    """A SOFT annotation (e.g. "no heat") never fires the protocol alone."""
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=_fresh_phone(),
        to_number=to_number,
        body="hi, we have no heat since this morning",
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        message_row = (
            (
                await db_session.execute(
                    text("SELECT prefilter FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["prefilter"]["hard_hit"] is False
        assert "no_heat" in message_row["prefilter"]["soft_annotations"]

        audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'emergency_triggered'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Tier-0 HARD hit — landlord-authored path
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_landlord_authored_hard_hit_creates_needs_eyes_not_emergency_protocol(
    db_session: AsyncSession,
) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)
    # No tenant shares this phone -> no collision -> landlord command channel.

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=landlord_phone,
        to_number=to_number,
        body="there is a fire at the property!",
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        message_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT party, tenant_id, prefilter FROM messages WHERE twilio_sid = :sid"
                    ),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["party"] == "landlord"
        assert message_row["tenant_id"] is None
        assert message_row["prefilter"]["hard_hit"] is True

        # No tenant emergency protocol artifacts:
        audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'emergency_triggered'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_count == 0

        emergency_notification_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'emergency_call'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert emergency_notification_count == 0

        # Instead: exactly one needs_eyes notification, never silently dropped.
        needs_eyes_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, payload FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'needs_eyes'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert needs_eyes_row["status"] == "pending"
        assert needs_eyes_row["payload"]["prefilter_hard_hit"] is True
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Routing predicate
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_landlord_channel_routing_when_from_matches_landlord_phone(
    db_session: AsyncSession,
) -> None:
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=landlord_phone, to_number=to_number, body="1"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        row = (
            (
                await db_session.execute(
                    text("SELECT party FROM messages WHERE twilio_sid = :sid"), {"sid": message_sid}
                )
            )
            .mappings()
            .one()
        )
        assert row["party"] == "landlord"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_collision_self_managing_landlord_who_is_active_tenant_routes_to_tenant(
    db_session: AsyncSession,
) -> None:
    """api-contracts.md: on collision (self-managing landlord living
    in-unit as an active tenant), the TENANT pipeline wins — an emergency
    can never be bypassed by the landlord-channel routing."""
    shared_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=shared_phone)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(
        db_session, landlord_id, property_id, phone=shared_phone, active=True
    )

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=shared_phone, to_number=to_number, body="hello"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        row = (
            (
                await db_session.execute(
                    text("SELECT party, tenant_id FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert row["party"] == "tenant"
        assert str(row["tenant_id"]) == tenant_id
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_tenant_channel_unknown_from_persists_with_null_tenant_id(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)  # no phone set
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    # A different, unrelated active tenant on the same property.
    await _insert_tenant(db_session, landlord_id, property_id, phone=_fresh_phone(), active=True)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=_fresh_phone(),  # matches no tenant
        to_number=to_number,
        body="hello",
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        row = (
            (
                await db_session.execute(
                    text("SELECT party, tenant_id FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert row["party"] == "tenant"
        assert row["tenant_id"] is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_tenant_channel_recognized_active_tenant_resolves_tenant_id(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    tenant_phone = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await _insert_tenant(
        db_session, landlord_id, property_id, phone=tenant_phone, active=True
    )

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=tenant_phone, to_number=to_number, body="hello"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        row = (
            (
                await db_session.execute(
                    text("SELECT party, tenant_id FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert row["party"] == "tenant"
        assert str(row["tenant_id"]) == tenant_id
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_inactive_tenant_not_matched_persists_with_null_tenant_id(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    tenant_phone = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    await _insert_tenant(db_session, landlord_id, property_id, phone=tenant_phone, active=False)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=tenant_phone, to_number=to_number, body="hello"
    )

    try:
        response = await _post_sms(params)
        assert response.status_code == 200

        row = (
            (
                await db_session.execute(
                    text("SELECT party, tenant_id FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert row["party"] == "tenant"
        assert row["tenant_id"] is None
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Consolidated review regressions — transaction-design fix (item 1)
#
# Both reviewers independently reproduced a silent-message-loss bug in an
# earlier revision: a shared session/transaction across the message INSERT
# and every post-persist side effect meant a failure in ANY side effect
# poisoned the whole transaction, so the final teardown commit silently
# rolled back the message row too, even though the caller already got a
# 200. These tests pin the fix: the message row survives a side-effect
# failure (i), a crash between persist and artifacts is recovered by the
# next retry (ii), and repeated redeliveries after artifacts already exist
# never create a second set (iii, in addition to the dedupe test above).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_i_injected_artifact_failure_returns_200_and_message_row_survives(
    db_session: AsyncSession,
) -> None:
    """(i) Injecting a failure into the post-persist emergency-artifact
    side effect must NOT roll back the already-committed message row —
    the regression both reviewers reproduced against an earlier revision."""
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    from_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=from_number,
        to_number=to_number,
        body="there is a fire!",
    )

    try:
        with patch(
            "app.routers.webhooks.twilio._ensure_tenant_emergency_artifacts",
            new=AsyncMock(side_effect=RuntimeError("simulated artifact failure")),
        ):
            response = await _post_sms(params)

        assert response.status_code == 200
        assert response.text == "<Response/>"

        # The message row survives despite the injected failure -- this is
        # exactly the row that an earlier (buggy) revision silently lost.
        row = (
            (
                await db_session.execute(
                    text("SELECT id, body FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert row["body"] == "there is a fire!"

        # No partial artifacts from the failed attempt (the injected
        # failure replaced the whole function, so none were attempted).
        notif_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'emergency_call'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert notif_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_i_injected_needs_eyes_failure_returns_200_and_message_row_survives(
    db_session: AsyncSession,
) -> None:
    """Same regression as above, landlord/needs_eyes side effect instead of
    the tenant emergency-artifact one."""
    landlord_phone = _fresh_phone()
    landlord_id = await _insert_landlord(db_session, phone=landlord_phone)
    to_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=landlord_phone, to_number=to_number, body="1"
    )

    try:
        with patch(
            "app.routers.webhooks.twilio._ensure_needs_eyes_notification",
            new=AsyncMock(side_effect=RuntimeError("simulated needs_eyes failure")),
        ):
            response = await _post_sms(params)

        assert response.status_code == 200

        row = (
            (
                await db_session.execute(
                    text("SELECT id, party FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert row["party"] == "landlord"

        needs_eyes_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'needs_eyes'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert needs_eyes_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_ii_retry_after_crash_between_persist_and_artifacts_creates_artifacts_once(
    db_session: AsyncSession,
) -> None:
    """(ii) Simulate a crash between the message commit and its artifacts:
    directly insert the message row (as the FIRST delivery's own commit
    would have, per the transaction redesign) WITHOUT creating any
    artifacts, then replay the identical webhook (Twilio's at-least-once
    redelivery) and assert the retry recovers the existing row and creates
    the missing artifacts exactly once."""
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    from_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    body = "there is a fire!"
    prefilter_result = prefilter.check(body)
    assert prefilter_result.hard_hit is True  # precondition for this scenario

    # Simulate: an earlier delivery already committed the message row (per
    # the transaction redesign, step 1) but the process died before any
    # post-persist side effect ran.
    await db_session.execute(
        text(
            "INSERT INTO messages (landlord_id, property_id, tenant_id, case_id, "
            "direction, party, body, twilio_sid, prefilter) "
            "VALUES (:landlord_id, :property_id, NULL, NULL, 'inbound', 'tenant', "
            ":body, :sid, CAST(:prefilter AS jsonb))"
        ),
        {
            "landlord_id": landlord_id,
            "property_id": property_id,
            "body": body,
            "sid": message_sid,
            "prefilter": prefilter_result.model_dump_json(),
        },
    )
    await db_session.commit()

    # Precondition: no artifacts exist yet (the simulated crash happened
    # before they were created).
    pre_notif_count = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM notifications "
                "WHERE landlord_id = :lid AND type = 'emergency_call'"
            ),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert pre_notif_count == 0

    params = _sms_params(
        message_sid=message_sid, from_number=from_number, to_number=to_number, body=body
    )

    try:
        response = await _post_sms(params)  # the "retry" delivery
        assert response.status_code == 200

        # Still exactly one message row (the INSERT conflicted, no second
        # row was created).
        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE twilio_sid = :sid"), {"sid": message_sid}
            )
        ).scalar_one()
        assert message_count == 1

        # The retry recovered the existing row and created the missing
        # artifacts -- exactly once.
        notif_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'emergency_call'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert notif_count == 1

        audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'emergency_triggered'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_iii_repeated_redeliveries_after_artifacts_exist_stay_exactly_once(
    db_session: AsyncSession,
) -> None:
    """(iii) A THIRD delivery, arriving after the artifacts already exist
    (either from a normal first delivery or a prior crash-recovery retry),
    must not create a second set -- the idempotent ``WHERE NOT EXISTS``
    gate holds across arbitrarily many redeliveries, not just two."""
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    from_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=from_number,
        to_number=to_number,
        body="there is a fire!",
    )

    try:
        r1 = await _post_sms(params)
        r2 = await _post_sms(params)
        r3 = await _post_sms(params)
        assert r1.status_code == r2.status_code == r3.status_code == 200

        notif_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'emergency_call'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert notif_count == 1

        audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'emergency_triggered'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Ops-visibility alerts (consolidated review items 3/4)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unknown_to_number_alerts_loudly() -> None:
    """Consolidated review item 3: an unrecognized `To` number now goes
    LOUD (log.error + Sentry), not a quiet info log."""
    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=_fresh_phone(),
        to_number=_fresh_phone(),  # matches no property
        body="hello",
    )

    with patch("app.routers.webhooks.twilio.sentry_sdk.capture_message") as mock_capture:
        response = await _post_sms(params)

    assert response.status_code == 200
    mock_capture.assert_called_once()
    call_kwargs = mock_capture.call_args.kwargs
    assert call_kwargs["extras"]["twilio_sid"] == message_sid
    # Never the raw phone number -- only a digest.
    assert params["To"] not in str(mock_capture.call_args)
    assert params["From"] not in str(mock_capture.call_args)


@pytest.mark.integration
async def test_tenant_hard_fire_alerts_loudly(db_session: AsyncSession) -> None:
    """Consolidated review item 4: every tenant Tier-0 HARD fire alerts via
    Sentry -- a 'pending' notification row alone pages nobody until #108."""
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    from_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=from_number,
        to_number=to_number,
        body="there is a fire!",
    )

    try:
        with patch("app.routers.webhooks.twilio.sentry_sdk.capture_message") as mock_capture:
            response = await _post_sms(params)

        assert response.status_code == 200
        assert mock_capture.call_count >= 1
        found = any(
            "fire" in call.kwargs.get("extras", {}).get("categories", [])
            for call in mock_capture.call_args_list
        )
        assert found, (
            f"expected a Sentry alert with categories=['fire'], got: {mock_capture.call_args_list}"
        )
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_tenant_hard_fire_alert_only_on_creation_not_on_duplicate(
    db_session: AsyncSession,
) -> None:
    """Consolidated review item 3: the Sentry alert fires on the delivery
    that actually CREATES the escalation artifacts -- a subsequent
    duplicate delivery (artifacts already exist) must NOT re-alert."""
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    from_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=from_number,
        to_number=to_number,
        body="there is a fire!",
    )

    try:
        with patch("app.routers.webhooks.twilio.sentry_sdk.capture_message") as mock_capture:
            r1 = await _post_sms(params)  # creates the escalation
            first_call_count = mock_capture.call_count
            r2 = await _post_sms(params)  # duplicate -- must not re-alert
            second_call_count = mock_capture.call_count

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert first_call_count >= 1
        assert second_call_count == first_call_count, (
            "duplicate delivery must not fire an additional tenant-hard-fire alert"
        )
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Consolidated review item 2 (BLOCKING) — conflict-path recovery failures
# must 5xx, not 200, so Twilio's retry can complete the recovery.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recovery_select_failure_returns_5xx_then_retry_completes_artifacts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB error while recovering an existing row's data (the conflict
    path) must surface as a 5xx -- NOT a 200 -- so Twilio retries. A
    subsequent, unimpaired redelivery must then complete the recovery and
    create the artifacts exactly once."""
    import app.routers.webhooks.twilio as webhook_mod

    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    from_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    body = "there is a fire!"
    prefilter_result = prefilter.check(body)

    # Simulate: an earlier delivery already committed the message row but
    # died before any post-persist side effect ran (same setup as the
    # crash-recovery test above).
    await db_session.execute(
        text(
            "INSERT INTO messages (landlord_id, property_id, tenant_id, case_id, "
            "direction, party, body, twilio_sid, prefilter) "
            "VALUES (:landlord_id, :property_id, NULL, NULL, 'inbound', 'tenant', "
            ":body, :sid, CAST(:prefilter AS jsonb))"
        ),
        {
            "landlord_id": landlord_id,
            "property_id": property_id,
            "body": body,
            "sid": message_sid,
            "prefilter": prefilter_result.model_dump_json(),
        },
    )
    await db_session.commit()

    params = _sms_params(
        message_sid=message_sid, from_number=from_number, to_number=to_number, body=body
    )

    try:
        with monkeypatch.context() as m:
            m.setattr(
                webhook_mod,
                "_SELECT_MESSAGE_FOR_RECOVERY_SQL",
                text("SELECT * FROM nonexistent_table_xyz_recovery"),
            )
            response1 = await _post_sms(params)
        assert response1.status_code >= 500, (
            f"expected a 5xx on recovery failure, got {response1.status_code}"
        )

        # No artifacts were created by the failed attempt.
        notif_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'emergency_call'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert notif_count == 0

        # SQL restored (monkeypatch.context() exited) -- the next
        # redelivery succeeds and completes the recovery.
        response2 = await _post_sms(params)
        assert response2.status_code == 200

        notif_count_after = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'emergency_call'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert notif_count_after == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_recovery_row_missing_returns_5xx(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structurally-unexpected "conflict but no row found" case must
    also 5xx, not 200 -- forced here by pointing the recovery SELECT at a
    twilio_sid that will never match (simulating `.one()` raising
    NoResultFound without needing an actual race)."""
    import app.routers.webhooks.twilio as webhook_mod

    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    from_number = _fresh_phone()
    await _insert_property(db_session, landlord_id, twilio_number=to_number)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=from_number, to_number=to_number, body="hello"
    )

    try:
        # First delivery persists the row normally.
        r1 = await _post_sms(params)
        assert r1.status_code == 200

        # Force the recovery SELECT (only reached on a conflict) to match
        # nothing, simulating the "row missing" branch.
        with monkeypatch.context() as m:
            m.setattr(
                webhook_mod,
                "_SELECT_MESSAGE_FOR_RECOVERY_SQL",
                text(
                    "SELECT id, landlord_id, property_id, party, tenant_id, prefilter "
                    "FROM messages WHERE twilio_sid = :sid AND false"
                ),
            )
            response2 = await _post_sms(params)  # duplicate -> conflict -> recovery -> forced-empty

        assert response2.status_code >= 500
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Consolidated review item 4 — _digest is keyed (HMAC), not a bare hash
# ---------------------------------------------------------------------------


def test_digest_is_keyed_and_deterministic() -> None:
    """_digest must be deterministic for the same input, must not equal
    the raw input, and must change if the key (twilio_auth_token) changes
    -- proving it is actually KEYED, not a plain unkeyed hash."""
    from app.routers.webhooks import twilio as webhook_mod

    phone = "+14165551234"
    d1 = webhook_mod._digest(phone)  # noqa: SLF001
    d2 = webhook_mod._digest(phone)  # noqa: SLF001
    assert d1 == d2
    assert d1 != phone

    with patch.object(webhook_mod.settings, "twilio_auth_token", "a-different-token"):
        d3 = webhook_mod._digest(phone)  # noqa: SLF001
    assert d3 != d1, "digest must change when the keying secret changes -- proves it's keyed"
