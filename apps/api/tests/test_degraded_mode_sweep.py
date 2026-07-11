"""Tests for ``app/agent/degraded_mode_sweep.py`` (#109) — the
degraded-mode re-classification sweep driving the "no keywords at all"
leg's 1/5/15-minute retry schedule.

``next_retry_at`` is pure/unit-level (no DB, no network). Every other test
here is ``integration`` (real Postgres via docker-compose + ``alembic
upgrade head``) with the Anthropic SDK ALWAYS mocked
(``app.integrations.anthropic.get_client`` monkeypatched) — no real API
calls anywhere in this suite, matching ``tests/test_agent_graph.py``'s own
convention (this module directly exercises ``run_graph`` via the sweep, so
it needs the SAME checkpointer-lifecycle + fake-Anthropic-client fixtures).

Run with:
    export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop
    uv run pytest tests/test_degraded_mode_sweep.py -m integration -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from anthropic.types import ToolUseBlock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.agent.degraded_mode_sweep import next_retry_at, sweep_degraded_mode_retries
from app.agent.nodes.degraded_mode import RETRY_SCHEDULE
from app.integrations import anthropic as anthropic_mod
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


@pytest.fixture(autouse=True)
def _reset_anthropic_client() -> None:
    anthropic_mod.reset_client_for_tests()
    yield
    anthropic_mod.reset_client_for_tests()


@pytest_asyncio.fixture(autouse=True)
async def _checkpointer_lifecycle(_migrate_once: None) -> AsyncGenerator[None, None]:
    """Ordering contract (``app/agent/checkpointer.py``) — see
    ``tests/test_agent_graph.py``'s identical fixture."""
    await setup_checkpointer()
    yield
    await close_checkpointer()


# ---------------------------------------------------------------------------
# Local-only helpers
# ---------------------------------------------------------------------------


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
    await session.execute(text("DELETE FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id})
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


async def _insert_degraded_retry(
    session: AsyncSession,
    *,
    landlord_id: str,
    case_id: str | None,
    message_id: str,
    attempt: int,
    next_attempt_at: datetime,
    failed_at: datetime,
) -> str:
    """Directly seeds a ``degraded_retry`` notification row (bypassing
    ``degraded_mode()`` itself) so each test can place the row at whatever
    attempt/schedule point it wants to exercise."""
    notification_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO notifications "
            "(id, landlord_id, case_id, type, channel, status, attempt, next_attempt_at, payload) "
            "VALUES (:id, :landlord_id, :case_id, 'degraded_retry', 'push', 'pending', "
            ":attempt, :next_attempt_at, CAST(:payload AS jsonb))"
        ),
        {
            "id": notification_id,
            "landlord_id": landlord_id,
            "case_id": case_id,
            "attempt": attempt,
            "next_attempt_at": next_attempt_at,
            "payload": (
                '{"message_id": "'
                + message_id
                + '", "case_id": '
                + (f'"{case_id}"' if case_id else "null")
                + ', "reasons": ["classification_failed"], "leg": "queued_for_retry", '
                '"failed_at": "' + failed_at.isoformat() + '"}'
            ),
        },
    )
    await session.commit()
    return notification_id


# ---------------------------------------------------------------------------
# Fake Anthropic client -- identical shape to test_agent_graph.py's own
# local copy (not extracted to factories.py, per that module's own
# docstring on scope).
# ---------------------------------------------------------------------------


def _fake_message(*, tool_input: dict[str, Any], tool_name: str = "tool") -> SimpleNamespace:
    block = ToolUseBlock(id="toolu_test", input=tool_input, name=tool_name, type="tool_use")
    usage = SimpleNamespace(input_tokens=150, output_tokens=40)
    return SimpleNamespace(content=[block], usage=usage, model="claude-sonnet-5")


class _FakeMessages:
    def __init__(self, *, responses: list[Any]) -> None:
        self._responses = list(responses)

    async def create(self, **kwargs: Any) -> Any:
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake_messages: _FakeMessages) -> None:
    monkeypatch.setattr(anthropic_mod, "get_client", lambda: _FakeClient(fake_messages))


def _intent_response() -> SimpleNamespace:
    return _fake_message(
        tool_input={"intent": "maintenance", "is_new_issue": False, "summary": "Heat is out"},
        tool_name="classify_intent",
    )


def _severity_response(*, severity: str = "URGENT") -> SimpleNamespace:
    return _fake_message(
        tool_input={
            "severity": severity,
            "rules_fired": ["No heat, mild weather"],
            "modifier": None,
            "refusal_flags": [],
            "reasoning": ["Tenant reports no heat."],
        },
        tool_name="classify_severity",
    )


def _garbage_severity_response() -> SimpleNamespace:
    return _fake_message(tool_input={"severity": "NOT_A_REAL_SEVERITY"})


def _draft_response_message() -> SimpleNamespace:
    return _fake_message(
        tool_input={
            "body": "Thanks for letting me know, I'll look into it.",
            "refusal_templates_used": [],
        },
        tool_name="draft_message",
    )


# ---------------------------------------------------------------------------
# next_retry_at -- pure, unit-level
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_next_retry_at_schedule() -> None:
    failed_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert next_retry_at(attempt=0, failed_at=failed_at) == failed_at + RETRY_SCHEDULE[0]
    assert next_retry_at(attempt=1, failed_at=failed_at) == failed_at + RETRY_SCHEDULE[1]
    assert next_retry_at(attempt=2, failed_at=failed_at) == failed_at + RETRY_SCHEDULE[2]


@pytest.mark.unit
def test_next_retry_at_exhausted_after_three_attempts() -> None:
    failed_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert next_retry_at(attempt=3, failed_at=failed_at) is None
    assert next_retry_at(attempt=4, failed_at=failed_at) is None


# ---------------------------------------------------------------------------
# Sweep -- integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sweep_ignores_not_yet_due_rows(db_session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    now = datetime.now(UTC)
    await _insert_degraded_retry(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        message_id=message_id,
        attempt=0,
        next_attempt_at=now + timedelta(minutes=1),  # not due yet
        failed_at=now,
    )

    try:
        outcomes = await sweep_degraded_mode_retries(now=now)
        assert outcomes == []

        row = (
            (
                await db_session.execute(
                    text("SELECT status, attempt FROM notifications WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "pending"
        assert row["attempt"] == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_reschedules_on_repeated_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
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
        body="not sure what's going on",
    )
    failed_at = datetime.now(UTC) - timedelta(minutes=2)
    await _insert_degraded_retry(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        message_id=message_id,
        attempt=0,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),  # due
        failed_at=failed_at,
    )

    # classify_intent succeeds; both classify_severity attempts fail
    # (garbage) -- classification_failed=True again on this retry.
    fake_messages = _FakeMessages(
        responses=[_intent_response(), _garbage_severity_response(), _garbage_severity_response()]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        outcomes = await sweep_degraded_mode_retries()
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "rescheduled"

        row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, attempt, next_attempt_at FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'degraded_retry'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "pending"
        assert row["attempt"] == 1
        assert row["next_attempt_at"] == failed_at + RETRY_SCHEDULE[1]

        # No needs_eyes escalation yet.
        needs_eyes_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid "
                    "AND type = 'needs_eyes'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert needs_eyes_count == 0

        audit_legs = (
            (
                await db_session.execute(
                    text(
                        "SELECT payload ->> 'leg' FROM audit_log WHERE landlord_id = :lid "
                        "AND action = 'degraded_mode'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .scalars()
            .all()
        )
        assert "retry_attempt_failed" in audit_legs
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_escalates_after_third_failed_attempt(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
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
    )
    failed_at = datetime.now(UTC) - timedelta(minutes=16)
    await _insert_degraded_retry(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        message_id=message_id,
        attempt=2,  # the 15-minute attempt is due now
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        failed_at=failed_at,
    )

    fake_messages = _FakeMessages(
        responses=[_intent_response(), _garbage_severity_response(), _garbage_severity_response()]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        outcomes = await sweep_degraded_mode_retries()
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "escalated"

        retry_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, attempt, next_attempt_at, payload FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'degraded_retry'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert retry_row["status"] == "exhausted"
        assert retry_row["attempt"] == 3
        assert retry_row["next_attempt_at"] is None
        assert retry_row["payload"]["outcome"] == "escalated"

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
        assert needs_eyes_row["payload"]["raw_text"] == (
            "not sure what's going on but something seems off"
        )
        assert needs_eyes_row["payload"]["leg"] == "retry_exhausted"

        audit_legs = (
            (
                await db_session.execute(
                    text(
                        "SELECT payload ->> 'leg' FROM audit_log WHERE landlord_id = :lid "
                        "AND action = 'degraded_mode'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .scalars()
            .all()
        )
        assert "retry_exhausted" in audit_legs
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_resolves_when_reclassification_succeeds(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
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
        body="the heat has been out since this morning",
    )
    failed_at = datetime.now(UTC) - timedelta(minutes=2)
    await _insert_degraded_retry(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        message_id=message_id,
        attempt=0,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        failed_at=failed_at,
    )

    # This time the LLM is healthy again -- classification succeeds and the
    # rest of the pipeline runs normally.
    fake_messages = _FakeMessages(
        responses=[_intent_response(), _severity_response(), _draft_response_message()]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        outcomes = await sweep_degraded_mode_retries()
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "resolved"

        retry_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, next_attempt_at, payload FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'degraded_retry'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert retry_row["status"] == "exhausted"
        assert retry_row["next_attempt_at"] is None
        assert retry_row["payload"]["outcome"] == "resolved"

        needs_eyes_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid "
                    "AND type = 'needs_eyes'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert needs_eyes_count == 0

        # The rest of the pipeline actually ran -- a draft now exists.
        draft_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert draft_count == 1

        audit_legs = (
            (
                await db_session.execute(
                    text(
                        "SELECT payload ->> 'leg' FROM audit_log WHERE landlord_id = :lid "
                        "AND action = 'degraded_mode'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .scalars()
            .all()
        )
        assert "retry_resolved" in audit_legs
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_one_candidate_failure_never_aborts_the_tick(
    db_session: AsyncSession,
) -> None:
    """``run_graph`` raising for one candidate (here: a ``message_id`` with
    no persisted ``messages`` row at all -- ``identify_property``'s
    ``MessageNotFoundError``) must not prevent OTHER due candidates in the
    same tick from being processed."""
    landlord_id = await factories.insert_landlord(db_session)

    # notifications has no FK to properties -- no property row is needed
    # for this test's bogus, message-less notification row.
    bogus_message_id = str(uuid.uuid4())
    await _insert_degraded_retry(
        db_session,
        landlord_id=landlord_id,
        case_id=None,
        message_id=bogus_message_id,
        attempt=0,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        failed_at=datetime.now(UTC) - timedelta(minutes=2),
    )

    try:
        outcomes = await sweep_degraded_mode_retries()
        # The broken candidate is logged and skipped -- never raised, never
        # included in the returned outcomes.
        assert outcomes == []

        row = (
            (
                await db_session.execute(
                    text("SELECT status, attempt FROM notifications WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        # Untouched -- the guarded UPDATE never ran for this candidate.
        assert row["status"] == "pending"
        assert row["attempt"] == 0
    finally:
        await db_session.execute(
            text("DELETE FROM notifications WHERE landlord_id = :lid"), {"lid": landlord_id}
        )
        await db_session.execute(
            text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id}
        )
        await db_session.commit()
