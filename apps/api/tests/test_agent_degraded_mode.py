"""Integration tests for ``app/agent/nodes/degraded_mode.py`` (#34 G1 seam,
#109 classification_failed leg).

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
    HOLDING_ACK_TEMPLATE,
    REASON_CLASSIFICATION_FAILED,
    REASON_DRAFT_GUARD_FAILED,
    REASON_SEVERITY_EMERGENCY,
    degraded_mode,
    render_holding_ack,
)
from app.agent.schemas import CaseContext, PrefilterResult, Severity, SeverityResult
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


async def _notifications_by_type(
    db_session: AsyncSession, *, landlord_id: str
) -> dict[str, list[dict[str, object]]]:
    rows = (
        (
            await db_session.execute(
                text(
                    "SELECT type, status, channel, case_id, attempt, next_attempt_at, payload "
                    "FROM notifications WHERE landlord_id = :lid ORDER BY created_at"
                ),
                {"lid": landlord_id},
            )
        )
        .mappings()
        .all()
    )
    by_type: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_type.setdefault(row["type"], []).append(dict(row))
    return by_type


# ---------------------------------------------------------------------------
# render_holding_ack — pure, unit-level (no DB)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_holding_ack_uses_landlord_first_name() -> None:
    assert render_holding_ack("Laith") == HOLDING_ACK_TEMPLATE.format(first_name="Laith")
    assert "Laith" in render_holding_ack("Laith")
    assert "911" in render_holding_ack("Laith")


@pytest.mark.unit
def test_render_holding_ack_falls_back_when_no_name() -> None:
    rendered = render_holding_ack(None)
    assert "your landlord" in rendered
    assert "911" in rendered


# ---------------------------------------------------------------------------
# classification_failed — Tier-0 HARD hit already handled (#109)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_degraded_mode_classification_failed_hard_hit_skips_new_artifacts(
    db_session: AsyncSession,
) -> None:
    """Doctrine: "Tier 0 HARD hit | already handled" — no tenant_ack, no
    needs_eyes, no degraded_retry. Only an audit_log row (unconditional,
    see module docstring)."""
    landlord_id = await factories.insert_landlord(db_session, full_name="Laith Alzoubi")
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="there is a fire in the kitchen",
        prefilter=PrefilterResult(hard_hit=True, categories=["fire"]).model_dump_json(),
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
        update = await degraded_mode(state)
        assert any("nothing extra to do" in line for line in update["reasoning_log"])

        notif_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid"),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert notif_count == 0

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
        assert audit_row["action"] == "degraded_mode"
        assert audit_row["payload"]["leg"] == "hard_hit_already_handled"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_degraded_mode_classification_failed_hard_hit_audit_not_deduped(
    db_session: AsyncSession,
) -> None:
    """Deliberately NOT idempotency-gated (module docstring) — repeated
    calls each append a fact, mirroring the webhook's own
    ``_alert_tenant_hard_fire`` "not noise-suppression" precedent."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        prefilter=PrefilterResult(hard_hit=True, categories=["fire"]).model_dump_json(),
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
        await degraded_mode(state)
        await degraded_mode(state)

        audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid "
                    "AND action = 'degraded_mode'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_count == 2
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# classification_failed — SOFT annotation present -> escalate blind (#109)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_degraded_mode_classification_failed_soft_annotation_escalates_blind(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session, full_name="Maria Chen")
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="there is a leak under the sink",
        prefilter=PrefilterResult(hard_hit=False, soft_annotations=["leak"]).model_dump_json(),
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
        update = await degraded_mode(state)
        assert any("flagged it for you right away" in line for line in update["reasoning_log"])

        by_type = await _notifications_by_type(db_session, landlord_id=landlord_id)

        assert len(by_type["needs_eyes"]) == 1
        needs_eyes = by_type["needs_eyes"][0]
        assert needs_eyes["status"] == "pending"
        assert needs_eyes["channel"] == "push"
        assert needs_eyes["payload"]["raw_text"] == "there is a leak under the sink"
        assert needs_eyes["payload"]["soft_annotations"] == ["leak"]
        assert needs_eyes["payload"]["leg"] == "soft_annotation_escalated"

        assert len(by_type["tenant_ack"]) == 1
        tenant_ack = by_type["tenant_ack"][0]
        assert tenant_ack["status"] == "pending"
        assert tenant_ack["channel"] == "sms"
        assert tenant_ack["payload"]["body"] == render_holding_ack("Maria")

        assert "degraded_retry" not in by_type

        audit_row = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["payload"]["reasons"] == [REASON_CLASSIFICATION_FAILED]
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# classification_failed — no keywords at all -> queue holding ack + retry
# (#109)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_degraded_mode_classification_failed_no_keywords_queues_retry(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session, full_name="Sam Okafor")
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="not sure what's going on but something seems off",
        prefilter=PrefilterResult(hard_hit=False).model_dump_json(),
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
        update = await degraded_mode(state)
        assert any("keep trying in the background" in line for line in update["reasoning_log"])

        by_type = await _notifications_by_type(db_session, landlord_id=landlord_id)

        assert "needs_eyes" not in by_type

        assert len(by_type["tenant_ack"]) == 1
        tenant_ack = by_type["tenant_ack"][0]
        assert tenant_ack["payload"]["body"] == render_holding_ack("Sam")

        assert len(by_type["degraded_retry"]) == 1
        retry_row = by_type["degraded_retry"][0]
        assert retry_row["status"] == "pending"
        assert retry_row["attempt"] == 0
        assert retry_row["next_attempt_at"] is not None
        assert "failed_at" in retry_row["payload"]

        audit_row = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["payload"]["leg"] == "queued_for_retry"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_degraded_mode_no_keywords_idempotent_via_partial_unique_indexes(
    db_session: AsyncSession,
) -> None:
    """Two calls for the SAME message never produce two tenant_ack or two
    degraded_retry rows, and never a second audit row — the new v1.8
    partial unique indexes (migration 0009), same discipline as the
    original #34 dedupe test."""
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

        by_type = await _notifications_by_type(db_session, landlord_id=landlord_id)
        assert len(by_type["tenant_ack"]) == 1
        assert len(by_type["degraded_retry"]) == 1

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
async def test_degraded_mode_no_keywords_unknown_sender_case_id_null(
    db_session: AsyncSession,
) -> None:
    """No case exists (unknown sender) -- tenant_ack/degraded_retry rows
    are still written, with ``case_id = NULL``, never raising."""
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
        assert any("keep trying in the background" in line for line in update["reasoning_log"])

        by_type = await _notifications_by_type(db_session, landlord_id=landlord_id)
        assert by_type["tenant_ack"][0]["case_id"] is None
        assert by_type["degraded_retry"][0]["case_id"] is None
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# draft_guard_failed / severity_emergency — UNCHANGED generic leg (#34)
# ---------------------------------------------------------------------------


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

        notif_row = (
            (
                await db_session.execute(
                    text("SELECT type FROM notifications WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif_row["type"] == "needs_eyes"
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
async def test_degraded_mode_generic_leg_is_idempotent_via_partial_unique_index(
    db_session: AsyncSession,
) -> None:
    """Two calls for the SAME message never produce two notifications or
    two degraded_mode audit rows on the UNCHANGED generic (severity_
    emergency/draft_guard_failed) leg — the original #34 regression test,
    now exercised explicitly against that leg rather than
    classification_failed (which #109 changed)."""
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
        await degraded_mode(state)

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


# ---------------------------------------------------------------------------
# Defensive/unit-level tests
# ---------------------------------------------------------------------------


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
    admin session a broken async generator. Exercised against the
    classification_failed leg (#109) — the SAME monkeypatched
    ``get_admin_session`` symbol every helper function in this module
    reads from, so this covers both legs."""
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
