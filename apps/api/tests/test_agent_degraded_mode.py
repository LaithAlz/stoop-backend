"""Integration tests for ``app/agent/nodes/degraded_mode.py`` (#34 G1).

Marker: ``integration`` — requires a running Postgres instance (docker
-compose) + ``alembic upgrade head``. Seeding helpers (landlord/property/
tenant/message) come from ``tests/factories.py`` (senior review: shared
factories, not re-duplicated in every new test module); ``_insert_case``
and ``_cleanup`` stay local (not part of that extraction).

Run with:
    export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop
    uv run pytest tests/test_agent_degraded_mode.py -m integration -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.nodes.degraded_mode import (
    REASON_CLASSIFICATION_FAILED,
    REASON_DRAFT_GUARD_FAILED,
    REASON_SEVERITY_EMERGENCY,
    degraded_mode,
)
from app.agent.schemas import CaseContext, Severity, SeverityResult
from app.agent.state import AgentState
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
# Local-only helpers (NOT part of the tests/factories.py extraction)
# ---------------------------------------------------------------------------


async def _insert_case(
    session: AsyncSession, *, landlord_id: str, property_id: str, tenant_id: str
) -> str:
    case_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO cases (id, landlord_id, property_id, tenant_id, status, "
            "langgraph_thread_id) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, 'open', :thread_id)"
        ),
        {
            "id": case_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "thread_id": str(uuid.uuid4()),
        },
    )
    await session.commit()
    return case_id


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
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


def _emergency_severity() -> SeverityResult:
    return SeverityResult(severity=Severity.EMERGENCY, rules_fired=["test"], reasoning=["test"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_degraded_mode_classification_failed_writes_notification_and_audit(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
                case_id=uuid.UUID(case_id),
            ),
            "classification_failed": True,
            "reasoning_log": ["I couldn't finish classifying this message right now."],
        }
        update = await degraded_mode(state)

        assert any("sent you a notification" in line for line in update["reasoning_log"])

        notif_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT type, status, case_id, payload FROM notifications "
                        "WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif_row["type"] == "needs_eyes"
        assert notif_row["status"] == "pending"
        assert str(notif_row["case_id"]) == case_id
        assert notif_row["payload"]["reasons"] == [REASON_CLASSIFICATION_FAILED]
        assert notif_row["payload"]["message_id"] == message_id

        audit_row = (
            (
                await db_session.execute(
                    text("SELECT actor, action, payload FROM audit_log WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["actor"] == "system"
        assert audit_row["action"] == "degraded_mode"
        assert audit_row["payload"]["reasons"] == [REASON_CLASSIFICATION_FAILED]
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_degraded_mode_draft_guard_failed_reason_recorded(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
                case_id=uuid.UUID(case_id),
            ),
            "classification_failed": False,
            "draft_guard_failed": True,
            "reasoning_log": [],
        }
        await degraded_mode(state)

        payload = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()["payload"]
        )
        assert payload["reasons"] == [REASON_DRAFT_GUARD_FAILED]
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_degraded_mode_emergency_and_guard_failed_both_recorded(
    db_session: AsyncSession,
) -> None:
    """A model-classified EMERGENCY whose OWN draft acknowledgment also
    failed the hard guards records BOTH reasons — never just one (module
    docstring: "these two CAN co-occur")."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
                case_id=uuid.UUID(case_id),
            ),
            "classification_failed": False,
            "severity": _emergency_severity(),
            "draft_guard_failed": True,
            "reasoning_log": [],
        }
        await degraded_mode(state)

        payload = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()["payload"]
        )
        assert payload["reasons"] == [REASON_SEVERITY_EMERGENCY, REASON_DRAFT_GUARD_FAILED]
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_degraded_mode_is_idempotent_via_partial_unique_index(
    db_session: AsyncSession,
) -> None:
    """Two calls for the SAME message never produce two notifications or
    two degraded_mode audit rows — the ``uq_notifications_message_dedupe``
    partial unique index (migration 0006) is the enforcement mechanism,
    same pattern as the webhook's own emergency-artifact idempotency."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
                case_id=uuid.UUID(case_id),
            ),
            "classification_failed": True,
            "reasoning_log": [],
        }
        await degraded_mode(state)
        await degraded_mode(state)  # simulates a redelivered/retried graph run

        notif_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid"),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert notif_count == 1

        audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid "
                    "AND action = 'degraded_mode'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_degraded_mode_unknown_sender_case_id_null(db_session: AsyncSession) -> None:
    """No case exists (unknown sender) -- the notification/audit rows are
    still written, with ``case_id = NULL``, never raising."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    message_id = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=None
    )

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id), property_id=uuid.UUID(property_id)
            ),
            "classification_failed": True,
            "reasoning_log": [],
        }
        update = await degraded_mode(state)
        assert any("sent you a notification" in line for line in update["reasoning_log"])

        row = (
            (
                await db_session.execute(
                    text("SELECT case_id FROM notifications WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["case_id"] is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.unit
async def test_degraded_mode_missing_landlord_id_does_not_raise() -> None:
    """Defensive branch (should never happen in production —
    ``identify_property`` always sets ``landlord_id``): no DB access is
    attempted and the function returns gracefully."""
    state: AgentState = {
        "message_id": uuid.uuid4(),
        "case_context": CaseContext(),
        "classification_failed": True,
        "reasoning_log": [],
    }
    update = await degraded_mode(state)
    assert "reasoning_log" in update


@pytest.mark.unit
async def test_degraded_mode_write_failure_is_caught_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety review MEDIUM: a DB failure inside this node's OWN writes
    must never propagate out (it would otherwise silently abort the graph
    run with no notification and no Sentry page for the ONE node whose job
    is "make sure a person finds out"). Forces the failure by making the
    admin session a broken async generator."""
    import app.agent.nodes.degraded_mode as degraded_mode_mod

    async def _broken_get_admin_session() -> AsyncGenerator[None, None]:
        raise RuntimeError("forced DB failure for test")
        yield  # pragma: no cover -- unreachable, satisfies the generator shape

    monkeypatch.setattr(degraded_mode_mod, "get_admin_session", _broken_get_admin_session)

    state: AgentState = {
        "message_id": uuid.uuid4(),
        "case_context": CaseContext(landlord_id=uuid.uuid4(), property_id=uuid.uuid4()),
        "classification_failed": True,
        "reasoning_log": [],
    }
    update = await degraded_mode(state)  # must NOT raise
    assert "reasoning_log" in update
