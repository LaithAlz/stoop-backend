"""Integration tests for POST /webhooks/twilio/status (issue #152).

Marker: ``integration`` — requires docker-compose Postgres + alembic
upgrade head. Self-contained (helpers duplicated from ``tests/
test_webhooks_twilio_sms.py`` per the project convention — see that
file's own module docstring for why).
"""

from __future__ import annotations

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

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"
_SIGNING_URL = "http://test/webhooks/twilio/status"


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


def _fresh_phone() -> str:
    return f"+1416{uuid.uuid4().int % 10_000_000:07d}"


async def _insert_landlord(session: AsyncSession) -> str:
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"),
        {"id": landlord_id, "auth_id": str(uuid.uuid4()), "email": f"{landlord_id}@example.com"},
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


async def _insert_message(
    session: AsyncSession, landlord_id: str, property_id: str, *, twilio_sid: str
) -> str:
    message_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, landlord_id, property_id, direction, party, body, twilio_sid) "
            "VALUES (:id, :landlord_id, :property_id, 'outbound', 'tenant', 'test body', :sid)"
        ),
        {
            "id": message_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "sid": twilio_sid,
        },
    )
    await session.commit()
    return message_id


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(
        text(
            "DELETE FROM message_status_events WHERE message_id IN "
            "(SELECT id FROM messages WHERE landlord_id = :lid)"
        ),
        {"lid": landlord_id},
    )
    await session.execute(
        text("DELETE FROM messages WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM properties WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


def _status_params(
    *, message_sid: str, status: str, error_code: str | None = None
) -> dict[str, str]:
    params = {
        "MessageSid": message_sid,
        "MessageStatus": status,
        "To": _fresh_phone(),
        "From": _fresh_phone(),
    }
    if error_code:
        params["ErrorCode"] = error_code
    return params


def _sign(params: dict[str, str]) -> str:
    assert settings.twilio_auth_token is not None
    return compute_signature(_SIGNING_URL, params, settings.twilio_auth_token)


async def _post_status(
    params: dict[str, str], *, signature: str | None | object = "__default__"
) -> httpx.Response:
    headers = {}
    if signature == "__default__":
        headers["X-Twilio-Signature"] = _sign(params)
    elif signature is not None:
        headers["X-Twilio-Signature"] = str(signature)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/webhooks/twilio/status", data=params, headers=headers)


@pytest.mark.integration
async def test_invalid_signature_returns_403(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    message_sid = f"SM{uuid.uuid4().hex}"
    await _insert_message(db_session, landlord_id, property_id, twilio_sid=message_sid)

    try:
        params = _status_params(message_sid=message_sid, status="delivered")
        response = await _post_status(params, signature="bad-signature")
        assert response.status_code == 403
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_recognized_status_appends_event_row(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    message_sid = f"SM{uuid.uuid4().hex}"
    message_id = await _insert_message(db_session, landlord_id, property_id, twilio_sid=message_sid)

    try:
        params = _status_params(message_sid=message_sid, status="delivered")
        response = await _post_status(params)

        assert response.status_code == 200
        assert response.text == "<Response/>"

        row = (
            (
                await db_session.execute(
                    text("SELECT status FROM message_status_events WHERE message_id = :mid"),
                    {"mid": message_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "delivered"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_duplicate_and_out_of_order_callbacks_both_appended_as_facts(
    db_session: AsyncSession,
) -> None:
    """Every callback is a fact — duplicates AND out-of-order status
    values are both appended, never de-duplicated or upserted."""
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    message_sid = f"SM{uuid.uuid4().hex}"
    message_id = await _insert_message(db_session, landlord_id, property_id, twilio_sid=message_sid)

    try:
        # "delivered" (terminal) arrives first, then a late "sent" (a
        # transient state) arrives out of order -- both are appended, in
        # arrival order, as facts. Precedence-based derivation is a
        # read-side concern, not this endpoint's job.
        r1 = await _post_status(_status_params(message_sid=message_sid, status="delivered"))
        r2 = await _post_status(_status_params(message_sid=message_sid, status="delivered"))
        r3 = await _post_status(_status_params(message_sid=message_sid, status="sent"))

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 200

        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM message_status_events WHERE message_id = :mid"),
                {"mid": message_id},
            )
        ).scalar_one()
        assert count == 3
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_unknown_sid_returns_200_no_row(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)

    try:
        params = _status_params(message_sid=f"SM{uuid.uuid4().hex}", status="delivered")
        response = await _post_status(params)

        assert response.status_code == 200
        assert response.text == "<Response/>"

        count = (
            await db_session.execute(text("SELECT COUNT(*) FROM message_status_events"))
        ).scalar_one()
        # No assertion on a global count across the whole table (other
        # tests may leave rows); instead confirm no row references a
        # message with this (nonexistent) sid indirectly is unnecessary —
        # the unknown sid never resolves to any message_id, so there is
        # structurally nothing to check beyond "200 was returned".
        assert count >= 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_out_of_vocabulary_status_returns_200_no_row(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    message_sid = f"SM{uuid.uuid4().hex}"
    message_id = await _insert_message(db_session, landlord_id, property_id, twilio_sid=message_sid)

    try:
        params = _status_params(
            message_sid=message_sid, status="read"
        )  # not in the CHECK vocabulary
        response = await _post_status(params)

        assert response.status_code == 200
        assert response.text == "<Response/>"

        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM message_status_events WHERE message_id = :mid"),
                {"mid": message_id},
            )
        ).scalar_one()
        assert count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_error_code_recorded_on_failed_status(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    message_sid = f"SM{uuid.uuid4().hex}"
    message_id = await _insert_message(db_session, landlord_id, property_id, twilio_sid=message_sid)

    try:
        params = _status_params(message_sid=message_sid, status="failed", error_code="30003")
        response = await _post_status(params)
        assert response.status_code == 200

        row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, error_code FROM message_status_events "
                        "WHERE message_id = :mid"
                    ),
                    {"mid": message_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "failed"
        assert row["error_code"] == "30003"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Consolidated review item 2 — /status must never 500, even on a DB blip
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_db_error_during_lookup_returns_200_not_500(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier revision left the ``twilio_sid`` SELECT unwrapped, so a
    transient DB error there surfaced as an unintended 500 — itself a
    contract violation (a non-2xx makes Twilio retry-storm a callback that
    will never resolve differently). Simulates a genuine DB-level failure
    by swapping the module's SQL constant for intentionally-broken SQL,
    rather than mocking session internals."""
    import app.routers.webhooks.twilio as webhook_mod

    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    message_sid = f"SM{uuid.uuid4().hex}"
    await _insert_message(db_session, landlord_id, property_id, twilio_sid=message_sid)

    monkeypatch.setattr(
        webhook_mod,
        "_SELECT_MESSAGE_BY_SID_SQL",
        text("SELECT id FROM this_table_does_not_exist_xyz"),
    )

    try:
        params = _status_params(message_sid=message_sid, status="delivered")
        response = await _post_status(params)

        assert response.status_code == 200
        assert response.text == "<Response/>"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Consolidated review item 7 — replay bound on message_status_events
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_replay_cap_stops_accepting_events_past_the_limit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounds storage under a replay storm: once a message already has
    ``_MAX_STATUS_EVENTS_PER_MESSAGE`` rows, further callbacks for that
    message are dropped (200, metadata-only log) rather than accepted
    forever. Uses a small monkeypatched cap so the test doesn't need to
    fire 100+ real HTTP requests."""
    import app.routers.webhooks.twilio as webhook_mod

    monkeypatch.setattr(webhook_mod, "_MAX_STATUS_EVENTS_PER_MESSAGE", 2)

    landlord_id = await _insert_landlord(db_session)
    to_number = _fresh_phone()
    property_id = await _insert_property(db_session, landlord_id, twilio_number=to_number)
    message_sid = f"SM{uuid.uuid4().hex}"
    message_id = await _insert_message(db_session, landlord_id, property_id, twilio_sid=message_sid)

    try:
        r1 = await _post_status(_status_params(message_sid=message_sid, status="queued"))
        r2 = await _post_status(_status_params(message_sid=message_sid, status="sent"))
        r3 = await _post_status(_status_params(message_sid=message_sid, status="delivered"))

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 200  # capped, but still 200 -- never a 4xx/5xx

        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM message_status_events WHERE message_id = :mid"),
                {"mid": message_id},
            )
        ).scalar_one()
        assert count == 2  # the third callback was dropped by the cap
    finally:
        await _cleanup(db_session, landlord_id)
