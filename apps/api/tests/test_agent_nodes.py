"""Integration tests for the #30/#110 deterministic graph nodes:
``identify_property``, ``load_context``, ``identify_case``.

Marker: ``integration`` — requires a running Postgres instance (docker
-compose) + ``alembic upgrade head``. Self-contained per the project
convention (helpers duplicated, not imported, from other test modules).

Run with:
    export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop
    uv run pytest tests/test_agent_nodes.py -m integration -v
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

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.case_lifecycle import (
    RESOLUTION_PROPOSAL_WINDOW,
    STATUS_OPEN,
    STATUS_RESOLVED,
    RoutingSignal,
)
from app.agent.nodes import identify_case as identify_case_mod
from app.agent.nodes.identify_case import identify_case
from app.agent.nodes.identify_property import MessageNotFoundError, identify_property
from app.agent.nodes.load_context import load_context
from app.agent.schemas import CaseContext, PrefilterResult
from app.agent.state import AgentState

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
    """Same rationale as other integration test modules: the module-level
    admin engine must not carry pooled connections across event loops."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _fresh_phone() -> str:
    return f"+1416{uuid.uuid4().int % 10_000_000:07d}"


async def _insert_landlord(
    session: AsyncSession, *, voice_profile: dict[str, object] | None = None
) -> str:
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO landlords (id, auth_user_id, email, voice_profile) "
            "VALUES (:id, :auth_id, :email, CAST(:voice_profile AS jsonb))"
        ),
        {
            "id": landlord_id,
            "auth_id": str(uuid.uuid4()),
            "email": f"{landlord_id}@example.com",
            "voice_profile": json.dumps(voice_profile) if voice_profile is not None else None,
        },
    )
    await session.commit()
    return landlord_id


async def _insert_property(
    session: AsyncSession,
    landlord_id: str,
    *,
    house_rules: str | None = "No pets. Quiet after 9pm.",
    backup_contact: dict[str, object] | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> str:
    property_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO properties "
            "(id, landlord_id, label, address_line1, city, house_rules, backup_contact, lat, lon) "
            "VALUES (:id, :landlord_id, 'Test Property', '123 Test St', 'Toronto', "
            ":house_rules, CAST(:backup_contact AS jsonb), :lat, :lon)"
        ),
        {
            "id": property_id,
            "landlord_id": landlord_id,
            "house_rules": house_rules,
            "backup_contact": json.dumps(backup_contact) if backup_contact is not None else None,
            "lat": lat,
            "lon": lon,
        },
    )
    await session.commit()
    return property_id


async def _insert_tenant(
    session: AsyncSession,
    landlord_id: str,
    property_id: str,
    *,
    vulnerable_occupant: str | None = None,
) -> str:
    tenant_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO tenants (id, landlord_id, property_id, phone, vulnerable_occupant) "
            "VALUES (:id, :landlord_id, :property_id, :phone, :vulnerable_occupant)"
        ),
        {
            "id": tenant_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "phone": _fresh_phone(),
            "vulnerable_occupant": vulnerable_occupant,
        },
    )
    await session.commit()
    return tenant_id


_DEFAULT_NO_HIT_PREFILTER = PrefilterResult(hard_hit=False)


async def _insert_message(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    tenant_id: str | None,
    party: str = "tenant",
    direction: str = "inbound",
    body: str = "The heat is out.",
    prefilter: PrefilterResult | None = _DEFAULT_NO_HIT_PREFILTER,
    created_at: datetime | None = None,
) -> str:
    message_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, landlord_id, property_id, tenant_id, direction, party, body, twilio_sid, "
            " prefilter, created_at) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, :direction, :party, :body, "
            " :twilio_sid, CAST(:prefilter AS jsonb), COALESCE(:created_at, now()))"
        ),
        {
            "id": message_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "direction": direction,
            "party": party,
            "body": body,
            "twilio_sid": f"SM{uuid.uuid4().hex}",
            "prefilter": prefilter.model_dump_json() if prefilter is not None else None,
            "created_at": created_at,
        },
    )
    await session.commit()
    return message_id


async def _insert_case(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    tenant_id: str,
    status: str = STATUS_OPEN,
    resolved_reason: str | None = None,
    resolved_at: datetime | None = None,
    last_activity_at: datetime | None = None,
    pending_resolved_at: datetime | None = None,
) -> str:
    case_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO cases "
            "(id, landlord_id, property_id, tenant_id, status, resolved_reason, resolved_at, "
            " langgraph_thread_id, last_activity_at, pending_resolved_at) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, :status, :resolved_reason, "
            " :resolved_at, :thread_id, COALESCE(:last_activity_at, now()), :pending_resolved_at)"
        ),
        {
            "id": case_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "status": status,
            "resolved_reason": resolved_reason,
            "resolved_at": resolved_at,
            "thread_id": str(uuid.uuid4()),
            "last_activity_at": last_activity_at,
            "pending_resolved_at": pending_resolved_at,
        },
    )
    await session.commit()
    return case_id


def _open_case_entry(
    *, case_id: str, last_activity_at: datetime, status: str = STATUS_OPEN
) -> dict[str, object]:
    """Build one ``state["open_cases"]`` entry — the same shape
    ``load_context``'s ``OpenCaseSummary.model_dump(mode="json")`` produces
    (#34 senior review: ``identify_case`` now consumes this from state
    instead of re-querying the ``cases`` table itself)."""
    return {
        "case_id": case_id,
        "status": status,
        "severity": None,
        "intent": None,
        "title": None,
        "last_activity_at": last_activity_at.isoformat(),
    }


async def _insert_needs_eyes_or_emergency_notification(
    session: AsyncSession, *, landlord_id: str, message_id: str, notif_type: str = "emergency_call"
) -> None:
    channel = "voice" if notif_type == "emergency_call" else "push"
    await session.execute(
        text(
            "INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload) "
            "VALUES (:landlord_id, NULL, :type, :channel, 'pending', CAST(:payload AS jsonb))"
        ),
        {
            "landlord_id": landlord_id,
            "type": notif_type,
            "channel": channel,
            "payload": json.dumps({"message_id": message_id}),
        },
    )
    await session.commit()


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(
        text(
            "DELETE FROM message_cases WHERE case_id IN "
            "(SELECT id FROM cases WHERE landlord_id = :lid)"
        ),
        {"lid": landlord_id},
    )
    await session.execute(
        text("DELETE FROM audit_log WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM notifications WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id})
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
# identify_property
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_identify_property_known_tenant(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {"message_id": uuid.UUID(message_id)}
        update = await identify_property(state)

        case_context = update["case_context"]
        assert case_context.property_id == uuid.UUID(property_id)
        assert case_context.tenant_id == uuid.UUID(tenant_id)
        assert case_context.landlord_id == uuid.UUID(landlord_id)
        # Warm, plain-English copy -- no node-name prefix, no raw ids.
        assert any("Test Property" in line for line in update["reasoning_log"])
        assert not any("identify_property:" in line for line in update["reasoning_log"])
        assert not any(str(property_id) in line for line in update["reasoning_log"])
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_property_unknown_tenant_sender_creates_needs_eyes(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=None, party="tenant"
    )

    try:
        state: AgentState = {"message_id": uuid.UUID(message_id)}
        update = await identify_property(state)

        assert update["case_context"].tenant_id is None
        assert any(
            "New sender" in line and "Test Property" in line for line in update["reasoning_log"]
        )

        rows = (
            (
                await db_session.execute(
                    text("SELECT type FROM notifications WHERE payload ->> 'message_id' = :mid"),
                    {"mid": message_id},
                )
            )
            .mappings()
            .all()
        )
        assert [r["type"] for r in rows] == ["needs_eyes"]

        # Idempotent: calling again does not create a second notification.
        await identify_property(state)
        rows_again = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM notifications WHERE payload ->> 'message_id' = :mid"),
                {"mid": message_id},
            )
        ).scalar_one()
        assert rows_again == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_property_unknown_landlord_channel_no_extra_notification(
    db_session: AsyncSession,
) -> None:
    """A landlord command-channel message (tenant_id NULL, party='landlord')
    is NOT this node's concern (the webhook already handles it) — no
    duplicate notification, no "unknown sender" note."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=None,
        party="landlord",
    )

    try:
        state: AgentState = {"message_id": uuid.UUID(message_id)}
        update = await identify_property(state)

        assert not any("unknown sender" in line for line in update["reasoning_log"])
        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM notifications WHERE payload ->> 'message_id' = :mid"),
                {"mid": message_id},
            )
        ).scalar_one()
        assert count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_property_missing_message_raises(db_session: AsyncSession) -> None:
    with pytest.raises(MessageNotFoundError):
        await identify_property({"message_id": uuid.uuid4()})


# ---------------------------------------------------------------------------
# load_context
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_load_context_loads_property_landlord_tenant_fields(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session, voice_profile={"tone": "friendly"})
    property_id = await _insert_property(
        db_session,
        landlord_id,
        house_rules="No smoking.",
        backup_contact={"name": "Sam", "phone": "+14165551234"},
    )
    tenant_id = await _insert_tenant(
        db_session, landlord_id, property_id, vulnerable_occupant="elderly"
    )

    try:
        state: AgentState = {
            "case_context": CaseContext(
                property_id=uuid.UUID(property_id),
                landlord_id=uuid.UUID(landlord_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        update = await load_context(state)

        cc = update["case_context"]
        assert cc.house_rules == "No smoking."
        assert cc.backup_contact == {"name": "Sam", "phone": "+14165551234"}
        assert cc.voice_profile == {"tone": "friendly"}
        assert cc.vulnerable_occupant is not None
        assert cc.vulnerable_occupant.value == "elderly"
        assert cc.quiet_hours == {"start": "21:00", "end": "08:00"}
        assert cc.heating_season == {"start": "09-15", "end": "06-01"}
        assert update["open_cases"] == []
        assert update["channel_history"] == []
        assert update["weather"] is None
        assert any("can't check the" in line for line in update["reasoning_log"])
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_load_context_open_cases_excludes_resolved_and_orders_recent_first(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)

    now = datetime.now(UTC)
    older_open = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=now - timedelta(days=2),
    )
    newer_open = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=now - timedelta(hours=1),
    )
    await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status=STATUS_RESOLVED,
        resolved_reason="landlord",
        resolved_at=now - timedelta(days=1),
        last_activity_at=now - timedelta(days=1),
    )

    try:
        state: AgentState = {
            "case_context": CaseContext(
                property_id=uuid.UUID(property_id),
                landlord_id=uuid.UUID(landlord_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        update = await load_context(state)

        open_case_ids = [row["case_id"] for row in update["open_cases"]]
        assert open_case_ids == [newer_open, older_open]
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_load_context_channel_history_chronological_and_role_mapped(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)

    now = datetime.now(UTC)
    await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        direction="inbound",
        body="The heat is out.",
        created_at=now - timedelta(minutes=10),
    )
    await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        direction="outbound",
        body="Sorry to hear that, looking into it.",
        created_at=now - timedelta(minutes=5),
    )
    # A landlord command-channel message must never appear in the tenant's
    # channel history (tenant_id is NULL for these, per schema-v1.md).
    await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=None,
        party="landlord",
        direction="inbound",
        body="1",
        created_at=now - timedelta(minutes=1),
    )

    try:
        state: AgentState = {
            "case_context": CaseContext(
                property_id=uuid.UUID(property_id),
                landlord_id=uuid.UUID(landlord_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        update = await load_context(state)

        history = update["channel_history"]
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["body"] == "The heat is out."
        assert history[1]["role"] == "assistant"
        assert history[1]["body"] == "Sorry to hear that, looking into it."
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_load_context_weather_loaded_when_coordinates_present(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agent.schemas import WeatherSnapshot

    async def _fake_weather(lat: float | None, lon: float | None) -> WeatherSnapshot | None:
        assert lat == 43.65
        assert lon == -79.38
        return WeatherSnapshot(current_temp_c=-12.0, overnight_low_c=-15.0, heat_warning=False)

    monkeypatch.setattr("app.agent.nodes.load_context.get_weather_snapshot", _fake_weather)

    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id, lat=43.65, lon=-79.38)

    try:
        state: AgentState = {
            "case_context": CaseContext(
                property_id=uuid.UUID(property_id), landlord_id=uuid.UUID(landlord_id)
            ),
            "reasoning_log": [],
        }
        update = await load_context(state)

        assert update["weather"] is not None
        assert update["weather"].overnight_low_c == -15.0
        assert any("-12.0" in line and "overnight low" in line for line in update["reasoning_log"])
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_load_context_weather_unavailable_on_provider_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_weather_none(lat: float | None, lon: float | None) -> None:
        return None

    monkeypatch.setattr("app.agent.nodes.load_context.get_weather_snapshot", _fake_weather_none)

    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id, lat=43.65, lon=-79.38)

    try:
        state: AgentState = {
            "case_context": CaseContext(
                property_id=uuid.UUID(property_id), landlord_id=uuid.UUID(landlord_id)
            ),
            "reasoning_log": [],
        }
        update = await load_context(state)

        assert update["weather"] is None
        assert any("couldn't reach the weather service" in line for line in update["reasoning_log"])
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# identify_case
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_identify_case_no_open_cases_opens_new_case(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {"message_id": uuid.UUID(message_id), "reasoning_log": []}
        update = await identify_case(state)

        case_id = update["case_context"].case_id
        assert case_id is not None

        case_row = (
            (
                await db_session.execute(
                    text("SELECT status, tenant_id FROM cases WHERE id = :cid"),
                    {"cid": str(case_id)},
                )
            )
            .mappings()
            .one()
        )
        assert case_row["status"] == STATUS_OPEN
        assert str(case_row["tenant_id"]) == tenant_id

        audit_rows = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE case_id = :cid"),
                    {"cid": str(case_id)},
                )
            )
            .mappings()
            .all()
        )
        assert [r["action"] for r in audit_rows] == ["case_opened"]

        link_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM message_cases WHERE message_id = :mid AND case_id = :cid"
                ),
                {"mid": message_id, "cid": str(case_id)},
            )
        ).scalar_one()
        assert link_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_case_one_open_case_attaches_and_bumps_activity(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    old_activity = datetime.now(UTC) - timedelta(days=1)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=old_activity,
    )
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "open_cases": [_open_case_entry(case_id=case_id, last_activity_at=old_activity)],
            "reasoning_log": [],
        }
        update = await identify_case(state)

        assert str(update["case_context"].case_id) == case_id

        case_row = (
            (
                await db_session.execute(
                    text("SELECT last_activity_at FROM cases WHERE id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert case_row["last_activity_at"] > old_activity

        # No new case was created.
        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_case_multiple_open_cases_attaches_most_recent(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    now = datetime.now(UTC)
    older_case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=now - timedelta(days=3),
    )
    newer_case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=now - timedelta(hours=1),
    )
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "open_cases": [
                _open_case_entry(case_id=older_case_id, last_activity_at=now - timedelta(days=3)),
                _open_case_entry(case_id=newer_case_id, last_activity_at=now - timedelta(hours=1)),
            ],
            "reasoning_log": [],
        }
        update = await identify_case(state)

        assert str(update["case_context"].case_id) == newer_case_id
        assert any("more than one open conversation" in line for line in update["reasoning_log"])
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_case_unknown_sender_opens_no_case(db_session: AsyncSession) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=None
    )

    try:
        state: AgentState = {"message_id": uuid.UUID(message_id), "reasoning_log": []}
        update = await identify_case(state)

        assert update["case_context"].case_id is None
        assert any("don't recognize this sender" in line for line in update["reasoning_log"])

        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_case_missing_prefilter_snapshot_logs_and_continues(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        prefilter=None,
    )

    try:
        state: AgentState = {"message_id": uuid.UUID(message_id), "reasoning_log": []}
        update = await identify_case(state)

        # Never re-runs Tier-0; falls back gracefully and still routes the
        # message to a case (the missing-snapshot anomaly is structlog-only,
        # not landlord-facing copy -- see identify_case.py's docstring).
        assert update["case_context"].case_id is not None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_case_hard_hit_backfills_notification_case_id(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        prefilter=PrefilterResult(hard_hit=True, categories=["fire"]),
    )
    await _insert_needs_eyes_or_emergency_notification(
        db_session, landlord_id=landlord_id, message_id=message_id, notif_type="emergency_call"
    )

    try:
        state: AgentState = {"message_id": uuid.UUID(message_id), "reasoning_log": []}
        update = await identify_case(state)

        case_id = update["case_context"].case_id
        assert case_id is not None

        row = (
            (
                await db_session.execute(
                    text("SELECT case_id FROM notifications WHERE payload ->> 'message_id' = :mid"),
                    {"mid": message_id},
                )
            )
            .mappings()
            .one()
        )
        assert str(row["case_id"]) == str(case_id)
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_case_reopen_within_30_days(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    resolved_case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status=STATUS_RESOLVED,
        resolved_reason="landlord",
        resolved_at=datetime.now(UTC) - timedelta(days=10),
    )
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    def _fake_signals(state: AgentState) -> list[RoutingSignal]:
        return [RoutingSignal(is_new_issue=False, matched_case_id=uuid.UUID(resolved_case_id))]

    monkeypatch.setattr(identify_case_mod, "_extract_signals", _fake_signals)

    try:
        state: AgentState = {"message_id": uuid.UUID(message_id), "reasoning_log": []}
        update = await identify_case(state)

        assert str(update["case_context"].case_id) == resolved_case_id

        row = (
            (
                await db_session.execute(
                    text("SELECT status, resolved_reason, resolved_at FROM cases WHERE id = :cid"),
                    {"cid": resolved_case_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "reopened"
        assert row["resolved_reason"] is None
        assert row["resolved_at"] is None

        audit_actions = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE case_id = :cid ORDER BY id"),
                    {"cid": resolved_case_id},
                )
            )
            .mappings()
            .all()
        )
        assert [r["action"] for r in audit_actions] == ["case_reopened"]
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_case_reopen_past_30_days_creates_related_case(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    resolved_case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status=STATUS_RESOLVED,
        resolved_reason="auto_stale",
        resolved_at=datetime.now(UTC) - timedelta(days=31),
    )
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    def _fake_signals(state: AgentState) -> list[RoutingSignal]:
        return [RoutingSignal(is_new_issue=False, matched_case_id=uuid.UUID(resolved_case_id))]

    monkeypatch.setattr(identify_case_mod, "_extract_signals", _fake_signals)

    try:
        state: AgentState = {"message_id": uuid.UUID(message_id), "reasoning_log": []}
        update = await identify_case(state)

        new_case_id = update["case_context"].case_id
        assert new_case_id is not None
        assert str(new_case_id) != resolved_case_id

        new_case_row = (
            (
                await db_session.execute(
                    text("SELECT status, related_case_id FROM cases WHERE id = :cid"),
                    {"cid": str(new_case_id)},
                )
            )
            .mappings()
            .one()
        )
        assert new_case_row["status"] == STATUS_OPEN
        assert str(new_case_row["related_case_id"]) == resolved_case_id

        # The OLD case is untouched (still resolved).
        old_case_row = (
            (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :cid"), {"cid": resolved_case_id}
                )
            )
            .mappings()
            .one()
        )
        assert old_case_row["status"] == STATUS_RESOLVED
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_identify_case_trusts_state_open_cases_over_a_resolved_db_row(
    db_session: AsyncSession,
) -> None:
    """G3 discriminating test (#34 spec review, MAJOR): without
    ``_fake_signals`` (the deterministic, no-signals ambiguity path), seed
    a case that is ALREADY 'resolved' in the DB, but present in
    ``state["open_cases"]`` — proving ``identify_case`` treats it as a
    routing CANDIDATE from state, never by re-querying `cases` for open
    ones itself.

    If ``identify_case`` instead re-queried the DB for open cases (ignoring
    state), a REAL query would find ZERO open cases for this tenant (the
    seeded case's status is 'resolved', excluded from every OPEN_STATUSES
    query) — it would fall to the "no open cases" branch and open a
    brand-new, UNRELATED case, leaving the old one untouched. Supplying the
    resolved case via ``state["open_cases"]`` instead makes
    ``route_inbound_message``'s ambiguity rule treat it as the (only)
    candidate, so ``identify_case``'s own re-check of that SPECIFIC case's
    real DB status (which it still legitimately does, to decide reopen-vs-
    new — a per-target check, not an open-cases re-query) reopens the SAME
    case within the 30-day window. That reopening is the discriminating
    signal: it can only happen if state's open_cases was actually consulted
    as a routing candidate.
    """
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    resolved_activity = datetime.now(UTC) - timedelta(days=10)
    resolved_case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status=STATUS_RESOLVED,
        resolved_reason="landlord",
        resolved_at=resolved_activity,
        last_activity_at=resolved_activity,
    )
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "open_cases": [
                _open_case_entry(
                    case_id=resolved_case_id,
                    last_activity_at=resolved_activity,
                    status=STATUS_RESOLVED,
                )
            ],
            "reasoning_log": [],
        }
        update = await identify_case(state)

        # Attached to (and reopened) the SAME resolved case from state --
        # not a fresh, unrelated new case a real "open cases" query would
        # have produced (that query would have found none).
        assert str(update["case_context"].case_id) == resolved_case_id

        row = (
            (
                await db_session.execute(
                    text("SELECT status, resolved_reason, resolved_at FROM cases WHERE id = :cid"),
                    {"cid": resolved_case_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "reopened"
        assert row["resolved_reason"] is None
        assert row["resolved_at"] is None

        # Exactly one case exists for this landlord -- no unrelated new
        # case was created alongside it.
        case_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert case_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# sweep_cases() — the DB entrypoint for the time-driven sweep
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sweep_cases_auto_stales_inactive_case_in_the_real_database(
    db_session: AsyncSession,
) -> None:
    from app.agent.case_lifecycle import sweep_cases

    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    now = datetime.now(UTC)

    stale_case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=now - timedelta(days=20),
    )
    fresh_case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=now - timedelta(hours=1),
    )

    try:
        actions = await sweep_cases(now=now)
        assert stale_case_id in {str(a.case_id) for a in actions}

        stale_row = (
            (
                await db_session.execute(
                    text("SELECT status, resolved_reason, resolved_at FROM cases WHERE id = :cid"),
                    {"cid": stale_case_id},
                )
            )
            .mappings()
            .one()
        )
        assert stale_row["status"] == STATUS_RESOLVED
        assert stale_row["resolved_reason"] == "auto_stale"
        assert stale_row["resolved_at"] is not None

        fresh_row = (
            (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :cid"), {"cid": fresh_case_id}
                )
            )
            .mappings()
            .one()
        )
        assert fresh_row["status"] == STATUS_OPEN

        audit_rows = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE case_id = :cid"),
                    {"cid": stale_case_id},
                )
            )
            .mappings()
            .all()
        )
        assert [r["action"] for r in audit_rows] == ["case_resolved"]

        # #110 review, advisory: auto-stale must never close a case silently
        # -- a needs_eyes notification is created too.
        notif_rows = (
            (
                await db_session.execute(
                    text("SELECT type, channel, payload FROM notifications WHERE case_id = :cid"),
                    {"cid": stale_case_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(notif_rows) == 1
        assert notif_rows[0]["type"] == "needs_eyes"
        assert notif_rows[0]["channel"] == "push"
        assert notif_rows[0]["payload"]["reason"] == "auto_stale"
        assert notif_rows[0]["payload"]["case_id"] == stale_case_id
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Tenant-confirmed resolution — full cycle (schema-v1.md v1.5, migration
# 0008): propose -> contradict OR auto-apply, against the real database.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_tenant_confirms_resolved_sets_pending_resolved_at(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    def _fake_signals(state: AgentState) -> list[RoutingSignal]:
        return [
            RoutingSignal(
                is_new_issue=False,
                matched_case_id=uuid.UUID(case_id),
                tenant_confirms_resolved=True,
            )
        ]

    monkeypatch.setattr(identify_case_mod, "_extract_signals", _fake_signals)

    try:
        before = datetime.now(UTC)
        state: AgentState = {"message_id": uuid.UUID(message_id), "reasoning_log": []}
        update = await identify_case(state)
        after = datetime.now(UTC)

        assert any("mark it resolved automatically" in line for line in update["reasoning_log"])

        row = (
            (
                await db_session.execute(
                    text("SELECT status, pending_resolved_at FROM cases WHERE id = :cid"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        # Status is untouched (still open-family) -- only the column marks
        # the pending proposal; no audit_log entry is written for it either
        # (only visible via the column + reasoning_log, per design).
        assert row["status"] == STATUS_OPEN
        assert row["pending_resolved_at"] is not None
        assert (
            before + RESOLUTION_PROPOSAL_WINDOW
            <= row["pending_resolved_at"]
            <= (after + RESOLUTION_PROPOSAL_WINDOW)
        )

        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert audit_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_new_message_before_deadline_contradicts_and_clears_pending(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_activity = datetime.now(UTC)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=case_activity,
        pending_resolved_at=datetime.now(UTC) + timedelta(hours=1),
    )
    message_id = await _insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "open_cases": [_open_case_entry(case_id=case_id, last_activity_at=case_activity)],
            "reasoning_log": [],
        }
        update = await identify_case(state)

        assert str(update["case_context"].case_id) == case_id
        assert any("held off" in line for line in update["reasoning_log"])

        row = (
            (
                await db_session.execute(
                    text("SELECT status, pending_resolved_at FROM cases WHERE id = :cid"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["pending_resolved_at"] is None
        assert row["status"] == STATUS_OPEN  # case stays open, not resolved
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_cases_applies_tenant_confirmed_resolution_past_deadline(
    db_session: AsyncSession,
) -> None:
    from app.agent.case_lifecycle import sweep_cases

    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    now = datetime.now(UTC)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        pending_resolved_at=now - timedelta(minutes=1),
    )

    try:
        actions = await sweep_cases(now=now)
        assert case_id in {str(a.case_id) for a in actions}

        row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, resolved_reason, resolved_at, pending_resolved_at "
                        "FROM cases WHERE id = :cid"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == STATUS_RESOLVED
        assert row["resolved_reason"] == "tenant_confirmed"
        assert row["resolved_at"] is not None
        assert row["pending_resolved_at"] is None

        audit_rows = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .all()
        )
        assert [r["action"] for r in audit_rows] == ["case_resolved"]

        # The tenant-confirmed leg does NOT create a needs_eyes notification
        # -- the landlord already saw the proposal via reasoning_log.
        notification_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM notifications WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert notification_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_cases_leaves_pending_resolution_untouched_before_deadline(
    db_session: AsyncSession,
) -> None:
    from app.agent.case_lifecycle import sweep_cases

    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    now = datetime.now(UTC)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        pending_resolved_at=now + timedelta(hours=1),
    )

    try:
        actions = await sweep_cases(now=now)
        assert case_id not in {str(a.case_id) for a in actions}

        row = (
            (
                await db_session.execute(
                    text("SELECT status, pending_resolved_at FROM cases WHERE id = :cid"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == STATUS_OPEN
        assert row["pending_resolved_at"] is not None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_cases_pending_resolution_prevents_auto_stale_interplay(
    db_session: AsyncSession,
) -> None:
    """A case with a pending (not-yet-due) resolution must NOT auto-stale
    even though its last_activity_at is far past the 14-day threshold —
    precedence: pending-resolution wins (case_lifecycle.py's module
    docstring)."""
    from app.agent.case_lifecycle import sweep_cases

    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    now = datetime.now(UTC)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=now - timedelta(days=100),
        pending_resolved_at=now + timedelta(hours=1),
    )

    try:
        actions = await sweep_cases(now=now)
        assert case_id not in {str(a.case_id) for a in actions}

        row = (
            (
                await db_session.execute(
                    text("SELECT status, resolved_reason FROM cases WHERE id = :cid"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == STATUS_OPEN
        assert row["resolved_reason"] is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_toctou_contradiction_between_select_and_update_is_not_overwritten(
    db_session: AsyncSession,
) -> None:
    """#110 review, BLOCKING (proven live: "tenant said it is back, but
    case = resolved/tenant_confirmed"). Reproduces the exact race: seed a
    due case, compute the sweep's decision phase (a now-stale action) via
    ``apply_time_transitions``, THEN apply a contradiction directly
    (simulating ``identify_case`` running concurrently and clearing
    ``pending_resolved_at`` because a new tenant message just arrived),
    THEN attempt the guarded UPDATE with the stale action — assert it
    safely no-ops: the case stays exactly as the contradiction left it, and
    NO audit row is written for a resolution that didn't actually happen.
    """
    from app.agent.case_lifecycle import (
        AUTO_STALE_INACTIVITY,
        CaseSnapshot,
        _apply_sweep_action,
        apply_time_transitions,
    )

    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    now = datetime.now(UTC)

    # Seed a case whose tenant-confirmed resolution is already due.
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        pending_resolved_at=now - timedelta(minutes=1),
    )

    try:
        # 1. The sweep's DECISION phase: a snapshot read "as of now" (the
        #    row is still due at this instant) produces a resolve action.
        snapshot = CaseSnapshot(
            case_id=uuid.UUID(case_id),
            status=STATUS_OPEN,
            resolved_reason=None,
            resolved_at=None,
            last_activity_at=now,
            pending_resolved_at=now - timedelta(minutes=1),
        )
        actions = apply_time_transitions([snapshot], now)
        assert len(actions) == 1
        stale_action = actions[0]

        # 2. A concurrent contradiction lands BETWEEN the decision above
        #    and the write below (e.g. identify_case() reacting to a new
        #    tenant message that just came in).
        await db_session.execute(
            text("UPDATE cases SET pending_resolved_at = NULL WHERE id = :cid"),
            {"cid": case_id},
        )
        await db_session.commit()

        # 3. The (now-stale) guarded write attempt.
        applied = await _apply_sweep_action(
            db_session,
            stale_action,
            effective_now=now,
            stale_threshold=now - AUTO_STALE_INACTIVITY,
        )
        await db_session.commit()

        assert applied is False

        row = (
            (
                await db_session.execute(
                    text("SELECT status, pending_resolved_at FROM cases WHERE id = :cid"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == STATUS_OPEN
        assert row["pending_resolved_at"] is None

        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert audit_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_toctou_new_message_between_select_and_update_prevents_auto_stale(
    db_session: AsyncSession,
) -> None:
    """Same TOCTOU class as the tenant-confirmed race above, for the
    auto-stale leg: a new tenant message bumping ``last_activity_at``
    between the sweep's decision phase and its write must prevent the
    stale-action write, not silently overwrite the fresh activity.
    """
    from app.agent.case_lifecycle import CaseSnapshot, _apply_sweep_action, apply_time_transitions

    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    now = datetime.now(UTC)
    stale_since = now - timedelta(days=20)

    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        last_activity_at=stale_since,
    )

    try:
        snapshot = CaseSnapshot(
            case_id=uuid.UUID(case_id),
            status=STATUS_OPEN,
            resolved_reason=None,
            resolved_at=None,
            last_activity_at=stale_since,
            pending_resolved_at=None,
        )
        actions = apply_time_transitions([snapshot], now)
        assert len(actions) == 1
        stale_action = actions[0]

        # A new tenant message lands concurrently, bumping activity.
        await db_session.execute(
            text("UPDATE cases SET last_activity_at = :now WHERE id = :cid"),
            {"now": now, "cid": case_id},
        )
        await db_session.commit()

        applied = await _apply_sweep_action(
            db_session,
            stale_action,
            effective_now=now,
            stale_threshold=now - timedelta(days=14),
        )
        await db_session.commit()

        assert applied is False

        row = (
            (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == STATUS_OPEN

        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert audit_count == 0

        notification_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM notifications WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert notification_count == 0
    finally:
        await _cleanup(db_session, landlord_id)
