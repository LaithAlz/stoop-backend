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

import json
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


async def _link_message_to_case(session: AsyncSession, *, message_id: str, case_id: str) -> None:
    await session.execute(
        text(
            "INSERT INTO message_cases (message_id, case_id) VALUES (:mid, :cid) "
            "ON CONFLICT (message_id, case_id) DO NOTHING"
        ),
        {"mid": message_id, "cid": case_id},
    )
    await session.commit()


async def _set_case_status(session: AsyncSession, *, case_id: str, status: str) -> None:
    await session.execute(
        text("UPDATE cases SET status = :status, updated_at = now() WHERE id = :cid"),
        {"status": status, "cid": case_id},
    )
    await session.commit()


async def _insert_degraded_retry(
    session: AsyncSession,
    *,
    landlord_id: str,
    case_id: str | None,
    message_id: str,
    attempt: int,
    next_attempt_at: datetime,
    failed_at: datetime,
    case_status_at_failure: str | None = "open",
) -> str:
    """Directly seeds a ``degraded_retry`` notification row PLUS its
    companion ``tenant_ack`` row (bypassing ``degraded_mode()`` itself, but
    matching what it ALWAYS creates together in real production -- see
    that function's own no-keyword branch) so each test can place the row
    at whatever attempt/schedule point it wants to exercise. Seeding the
    ``tenant_ack`` row too matters: without it, a sweep tick whose
    ``run_graph`` call fails classification AGAIN re-enters
    ``degraded_mode()``'s OWN no-keyword branch, which would then create a
    genuinely NEW ``tenant_ack`` row (none existed) and fire ITS OWN
    "queued_for_retry" activation alert — a test artifact that would
    contaminate assertions about the SWEEP's own alerts/side effects.
    ``case_status_at_failure`` defaults to ``"open"`` — matching every
    existing test's case (seeded 'open' via ``_insert_case`` and never
    changed) so the re-animation guard
    (:func:`app.agent.degraded_mode_sweep._case_has_moved_on`) is a no-op
    by default; tests exercising that guard pass a different value or
    actually change the case's status before sweeping."""
    notification_id = str(uuid.uuid4())
    payload = {
        "message_id": message_id,
        "case_id": case_id,
        "reasons": ["classification_failed"],
        "leg": "queued_for_retry",
        "failed_at": failed_at.isoformat(),
        "case_status_at_failure": case_status_at_failure,
    }
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
            "payload": json.dumps(payload),
        },
    )
    tenant_ack_payload = {
        "message_id": message_id,
        "case_id": case_id,
        "reasons": ["classification_failed"],
        "body": "Got your message -- it's been passed to your landlord. If this is a "
        "life-threatening emergency, call 911.",
    }
    await session.execute(
        text(
            "INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload) "
            "VALUES (:landlord_id, :case_id, 'tenant_ack', 'sms', 'pending', "
            "CAST(:payload AS jsonb))"
        ),
        {
            "landlord_id": landlord_id,
            "case_id": case_id,
            "payload": json.dumps(tenant_ack_payload),
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
                    text(
                        "SELECT status, attempt FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'degraded_retry'"
                    ),
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
async def test_sweep_escalation_fires_sentry_activation_alert(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chaos test (issue #109 AC line 5 / safety-review round): the
    sweep's OWN 15-minute escalation ALSO pages Sentry at
    ``level="warning"`` — see ``degraded_mode_sweep.py``'s module
    docstring "Sentry activation alert"."""
    import app.agent.degraded_mode_sweep as sweep_mod

    calls: list[dict[str, object]] = []

    def _fake_capture_message(
        message: str, *, level: str | None = None, extras: dict[str, object] | None = None
    ) -> None:
        calls.append({"message": message, "level": level, "extras": extras})

    monkeypatch.setattr(sweep_mod.sentry_sdk, "capture_message", _fake_capture_message)

    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await factories.insert_message(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    failed_at = datetime.now(UTC) - timedelta(minutes=16)
    await _insert_degraded_retry(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        message_id=message_id,
        attempt=2,
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

        assert len(calls) == 1
        assert calls[0]["level"] == "warning"
        extras = calls[0]["extras"]
        assert isinstance(extras, dict)
        assert extras["leg"] == "retry_exhausted"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Re-animation guard (safety review HIGH/MAJOR)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sweep_supersedes_when_case_resolved_by_newer_message(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1 was queued for retry while the case was 'open'; M2 arrives and
    the case is resolved before the retry becomes due. The due retry must
    no-op (never re-run classification, never touch ``cases``) with a
    durable audit trail."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    m1_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="m1: not sure what's going on",
    )
    await _link_message_to_case(db_session, message_id=m1_id, case_id=case_id)

    # M2 arrives later, is handled normally (not modeled here -- this test
    # seeds only the END state), and the case is resolved.
    m2_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="m2: actually nevermind, it's fixed now, thanks",
    )
    await _link_message_to_case(db_session, message_id=m2_id, case_id=case_id)
    await _set_case_status(db_session, case_id=case_id, status="resolved")

    failed_at = datetime.now(UTC) - timedelta(minutes=2)
    await _insert_degraded_retry(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        message_id=m1_id,
        attempt=0,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        failed_at=failed_at,
        case_status_at_failure="open",
    )

    import app.agent.degraded_mode_sweep as sweep_mod

    async def _run_graph_must_not_be_called(message_id: uuid.UUID) -> None:
        raise AssertionError("run_graph must not be called for a superseded candidate")

    monkeypatch.setattr(sweep_mod, "run_graph", _run_graph_must_not_be_called)

    try:
        outcomes = await sweep_degraded_mode_retries()
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "superseded"

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
        assert retry_row["payload"]["outcome"] == "superseded"

        # The case is UNTOUCHED -- still 'resolved', never dragged back.
        case_status = (
            await db_session.execute(
                text("SELECT status FROM cases WHERE id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert case_status == "resolved"

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
        assert "retry_superseded" in audit_legs
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_supersedes_when_newer_inbound_exists_status_unchanged(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Condition B alone: the case is STILL 'open' (status unchanged), but
    a newer inbound message already exists on it -- M1 is no longer the
    case's latest unhandled inbound, so the retry must still supersede."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    m1_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="m1: not sure what's going on",
    )
    await _link_message_to_case(db_session, message_id=m1_id, case_id=case_id)

    m2_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="m2: also, the bathroom fan is loud",
    )
    await _link_message_to_case(db_session, message_id=m2_id, case_id=case_id)
    # Case status deliberately LEFT as 'open' -- only condition B fires.

    failed_at = datetime.now(UTC) - timedelta(minutes=2)
    await _insert_degraded_retry(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        message_id=m1_id,
        attempt=0,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        failed_at=failed_at,
        case_status_at_failure="open",
    )

    import app.agent.degraded_mode_sweep as sweep_mod

    async def _run_graph_must_not_be_called(message_id: uuid.UUID) -> None:
        raise AssertionError("run_graph must not be called for a superseded candidate")

    monkeypatch.setattr(sweep_mod, "run_graph", _run_graph_must_not_be_called)

    try:
        outcomes = await sweep_degraded_mode_retries()
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "superseded"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_proceeds_normally_when_case_unchanged_and_no_newer_message(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline/regression: neither re-animation signal fires -- the sweep
    proceeds to reclassify exactly as before this guard existed."""
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
    await _link_message_to_case(db_session, message_id=message_id, case_id=case_id)

    failed_at = datetime.now(UTC) - timedelta(minutes=2)
    await _insert_degraded_retry(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        message_id=message_id,
        attempt=0,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        failed_at=failed_at,
        case_status_at_failure="open",
    )

    fake_messages = _FakeMessages(
        responses=[_intent_response(), _garbage_severity_response(), _garbage_severity_response()]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        outcomes = await sweep_degraded_mode_retries()
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "rescheduled"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_case_has_moved_on_ignores_landlord_authored_linked_message(
    db_session: AsyncSession,
) -> None:
    """#122 safety-review pin (forward risk recorded on #40): a
    LANDLORD-authored message (approve-by-SMS command-channel reply) must
    NEVER count as "a newer inbound message exists" (Condition B) --
    only a genuinely new TENANT message is evidence the case moved on.
    Approve-by-SMS's own reply handler does not in fact link its messages
    into ``message_cases`` (it sets ``messages.case_id`` directly instead
    -- see ``app/agent/approve_by_sms.py``'s own module docstring), so this
    is belt-and-braces against a future change, exercised directly here at
    the ``_case_has_moved_on`` level (no Anthropic mocking needed)."""
    import app.agent.degraded_mode_sweep as sweep_mod

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
        body="m1: not sure what's going on",
    )
    await _link_message_to_case(db_session, message_id=message_id, case_id=case_id)

    # A LANDLORD-authored message, linked into message_cases for the SAME
    # case -- must be ignored by Condition B.
    landlord_message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=None,
        body="1",
        party="landlord",
    )
    await _link_message_to_case(db_session, message_id=landlord_message_id, case_id=case_id)

    candidate = sweep_mod.DegradedRetryCandidate(
        notification_id=uuid.uuid4(),
        message_id=uuid.UUID(message_id),
        case_id=uuid.UUID(case_id),
        landlord_id=uuid.UUID(landlord_id),
        attempt=0,
        failed_at=datetime.now(UTC) - timedelta(minutes=1),
        case_status_at_failure="open",
    )

    try:
        moved_on = await sweep_mod._case_has_moved_on(db_session, candidate)  # noqa: SLF001
        assert moved_on is False  # the landlord's own reply must NOT count as "moved on"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Exception handling never silently loops forever (safety HIGH + spec
# MAJOR -- THE blocker finding)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sweep_pages_sentry_on_every_persistent_exception_and_escalates_after_bound(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate whose ``run_graph`` raises PERSISTENTLY must page
    Sentry on EVERY tick (never a silent loop) and, once
    ``_MAX_CANDIDATE_EXCEPTIONS`` is reached, force-escalate to a genuine
    ``needs_eyes`` row via the SAME ``_escalate`` path."""
    import app.agent.degraded_mode_sweep as sweep_mod

    sentry_calls: list[dict[str, object]] = []

    def _fake_capture_message(
        message: str, *, level: str | None = None, extras: dict[str, object] | None = None
    ) -> None:
        sentry_calls.append({"message": message, "level": level, "extras": extras})

    monkeypatch.setattr(sweep_mod.sentry_sdk, "capture_message", _fake_capture_message)

    async def _boom(message_id: uuid.UUID) -> None:
        raise RuntimeError("simulated run_graph crash")

    monkeypatch.setattr(sweep_mod, "run_graph", _boom)

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
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        failed_at=failed_at,
        case_status_at_failure="open",
    )

    try:
        for tick in range(sweep_mod._MAX_CANDIDATE_EXCEPTIONS):  # noqa: SLF001
            outcomes = await sweep_degraded_mode_retries()
            # error-level pages accumulate one per tick regardless of
            # outcome -- checked precisely after the loop below.
            if tick < sweep_mod._MAX_CANDIDATE_EXCEPTIONS - 1:  # noqa: SLF001
                assert outcomes == []

        # After the bound: the last tick force-escalated.
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "escalated_by_exception"

        error_calls = [c for c in sentry_calls if c["level"] == "error"]
        assert len(error_calls) == sweep_mod._MAX_CANDIDATE_EXCEPTIONS  # noqa: SLF001
        for call in error_calls:
            extras = call["extras"]
            assert isinstance(extras, dict)
            assert extras["exc_type"] == "RuntimeError"

        warning_calls = [c for c in sentry_calls if c["level"] == "warning"]
        assert len(warning_calls) == 1
        warning_extras = warning_calls[0]["extras"]
        assert isinstance(warning_extras, dict)
        assert warning_extras["leg"] == "retry_exhausted_exception"

        retry_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, next_attempt_at FROM notifications "
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

        needs_eyes_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT payload FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'needs_eyes'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert needs_eyes_row["payload"]["leg"] == "retry_exhausted_exception"
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
                    text(
                        "SELECT status, attempt FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'degraded_retry'"
                    ),
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
