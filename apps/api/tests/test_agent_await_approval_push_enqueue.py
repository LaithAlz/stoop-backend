"""Tests for the push-notification enqueue seam in
``app/agent/nodes/await_approval.py::mark_awaiting_approval`` (#210 M3,
schema-v1.md's v1.13 amendments).

Calls ``mark_awaiting_approval`` DIRECTLY (bypassing the full LangGraph
machinery — no Anthropic mocking needed, mirroring
``tests/test_agent_finalize_draft_decision.py``'s "seed a case directly"
convention) with a minimal, hand-built ``AgentState``-shaped dict, so
these tests focus purely on the enqueue seam without dragging in the
whole graph/checkpointer stack.

Marker: ``integration`` — real Postgres, same docker-compose harness
every other integration test module here uses.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.nodes.await_approval import mark_awaiting_approval
from app.agent.schemas import CaseContext
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
    """``mark_awaiting_approval`` uses ``get_admin_session`` — the app's
    own module-level engine, separate from this file's ``db_engine``
    fixture. Same cross-event-loop hazard as
    ``tests/test_property_provisioning.py``'s fixture of this name."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.rollback()
    for table in ("push_outbox", "push_tokens", "drafts", "cases", "tenants", "properties"):
        await session.execute(
            text(f"DELETE FROM {table} WHERE landlord_id = :lid"),  # noqa: S608
            {"lid": landlord_id},
        )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


async def _seed_open_case_with_pending_draft(session: AsyncSession) -> tuple[str, str, str]:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id)
    case_id = await factories.insert_case(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="open",
        severity="routine",
    )
    draft_id = await factories.insert_draft(session, landlord_id=landlord_id, case_id=case_id)
    return landlord_id, case_id, draft_id


def _state_for(case_id: str) -> dict[str, Any]:
    return {
        "message_id": UUID("00000000-0000-0000-0000-000000000001"),
        "case_context": CaseContext(case_id=UUID(case_id)),
        "reasoning_log": [],
    }


async def _outbox_rows(session: AsyncSession, landlord_id: str) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT device_token_id, kind, payload, status FROM push_outbox "
                    "WHERE landlord_id = :lid"
                ),
                {"lid": landlord_id},
            )
        )
        .mappings()
        .all()
    )
    # device_token_id comes back as a real uuid.UUID from asyncpg -- stringify
    # so callers can compare directly against the str ids factories.* return.
    return [{**dict(row), "device_token_id": str(row["device_token_id"])} for row in rows]


# ---------------------------------------------------------------------------
# 1. One active device -> exactly one push_outbox row
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_one_active_device_enqueues_one_row(db_session: AsyncSession) -> None:
    landlord_id, case_id, draft_id = await _seed_open_case_with_pending_draft(db_session)
    try:
        push_token_id = await factories.insert_push_token(db_session, landlord_id=landlord_id)

        result = await mark_awaiting_approval(_state_for(case_id))

        assert "Your reply is ready" in result["reasoning_log"][-1]

        status = (
            await db_session.execute(
                text("SELECT status FROM cases WHERE id = :id"), {"id": case_id}
            )
        ).scalar_one()
        assert status == "awaiting_approval"

        rows = await _outbox_rows(db_session, landlord_id)
        assert len(rows) == 1
        assert rows[0]["device_token_id"] == push_token_id
        assert rows[0]["kind"] == "draft_awaiting_approval"
        assert rows[0]["status"] == "pending"
        assert rows[0]["payload"] == {"case_id": case_id, "draft_id": draft_id}
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_redelivered_run_never_double_enqueues_same_device_and_draft(
    db_session: AsyncSession,
) -> None:
    """Crash-then-redelivery (app/agent/graph_entry.py's own "Crash-window
    coherence with #43's mark_awaiting_approval") can genuinely re-run this
    node for the SAME message/case — the NOT EXISTS guard on
    _ENQUEUE_PUSH_OUTBOX_SQL must make a second run a no-op for any
    (device, draft) pair already enqueued."""
    landlord_id, case_id, _draft_id = await _seed_open_case_with_pending_draft(db_session)
    try:
        await factories.insert_push_token(db_session, landlord_id=landlord_id)

        await mark_awaiting_approval(_state_for(case_id))
        await mark_awaiting_approval(_state_for(case_id))  # simulated redelivery

        rows = await _outbox_rows(db_session, landlord_id)
        assert len(rows) == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2. Zero devices -> zero rows; status flip still happens (byte-identical)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_zero_devices_enqueues_zero_rows(db_session: AsyncSession) -> None:
    landlord_id, case_id, _draft_id = await _seed_open_case_with_pending_draft(db_session)
    try:
        result = await mark_awaiting_approval(_state_for(case_id))

        assert "Your reply is ready" in result["reasoning_log"][-1]
        status = (
            await db_session.execute(
                text("SELECT status FROM cases WHERE id = :id"), {"id": case_id}
            )
        ).scalar_one()
        assert status == "awaiting_approval"

        assert await _outbox_rows(db_session, landlord_id) == []
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 3. A revoked device is never enqueued
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_revoked_device_is_never_enqueued(db_session: AsyncSession) -> None:
    landlord_id, case_id, _draft_id = await _seed_open_case_with_pending_draft(db_session)
    try:
        await factories.insert_push_token(
            db_session, landlord_id=landlord_id, revoked_at=datetime.now(UTC)
        )

        await mark_awaiting_approval(_state_for(case_id))

        assert await _outbox_rows(db_session, landlord_id) == []
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 4. No pending draft -> zero rows even with an active device (the rare
#    draft_response race-exhausted path — see that module's docstring)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_pending_draft_enqueues_zero_rows(db_session: AsyncSession) -> None:
    landlord_id, case_id, draft_id = await _seed_open_case_with_pending_draft(db_session)
    try:
        await factories.insert_push_token(db_session, landlord_id=landlord_id)
        # Mark the only draft non-pending -- simulates the race-exhausted
        # path where draft_response never actually inserted a live draft.
        await db_session.execute(
            text("UPDATE drafts SET status = 'stale' WHERE id = :id"), {"id": draft_id}
        )
        await db_session.commit()

        await mark_awaiting_approval(_state_for(case_id))

        assert await _outbox_rows(db_session, landlord_id) == []
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 5. Multiple active devices -> one row per device (fan-out)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_multiple_active_devices_fan_out_one_row_each(db_session: AsyncSession) -> None:
    landlord_id, case_id, draft_id = await _seed_open_case_with_pending_draft(db_session)
    try:
        token_1 = await factories.insert_push_token(db_session, landlord_id=landlord_id)
        token_2 = await factories.insert_push_token(
            db_session, landlord_id=landlord_id, platform="android"
        )

        await mark_awaiting_approval(_state_for(case_id))

        rows = await _outbox_rows(db_session, landlord_id)
        assert {row["device_token_id"] for row in rows} == {token_1, token_2}
        for row in rows:
            assert row["payload"] == {"case_id": case_id, "draft_id": draft_id}
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 6. case_id is None (unknown-sender path) -> node behaves exactly as
#    before, no push_outbox statement even attempted (nothing to enqueue
#    against).
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_missing_case_id_skips_enqueue_entirely() -> None:
    state: dict[str, Any] = {
        "message_id": UUID("00000000-0000-0000-0000-000000000002"),
        "case_context": CaseContext(case_id=None),
        "reasoning_log": ["existing line"],
    }
    result = await mark_awaiting_approval(state)
    assert result == {"reasoning_log": ["existing line"]}
