"""Integration tests for ``app/agent/nodes/classify_severity.py`` (#32).

Marker: ``integration`` — requires a running Postgres instance (docker
-compose) + ``alembic upgrade head``, same as ``tests/test_agent_nodes.py``.
The Anthropic SDK itself is ALWAYS mocked (``app.integrations.anthropic
.get_client`` monkeypatched) — no real API calls anywhere in this suite.

Run with:
    export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop
    uv run pytest tests/test_agent_classify_severity.py -m integration -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from anthropic.types import ToolUseBlock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.nodes.classify_severity import classify_severity
from app.agent.schemas import CaseContext, PrefilterResult, WeatherSnapshot
from app.agent.state import AgentState
from app.integrations import anthropic as anthropic_mod

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


# ---------------------------------------------------------------------------
# Seeding helpers (duplicated per project convention)
# ---------------------------------------------------------------------------


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


async def _insert_property(session: AsyncSession, landlord_id: str) -> str:
    property_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO properties (id, landlord_id, label, address_line1, city) "
            "VALUES (:id, :landlord_id, 'Test Property', '123 Test St', 'Toronto')"
        ),
        {"id": property_id, "landlord_id": landlord_id},
    )
    await session.commit()
    return property_id


async def _insert_tenant(session: AsyncSession, landlord_id: str, property_id: str) -> str:
    tenant_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO tenants (id, landlord_id, property_id, phone) "
            "VALUES (:id, :landlord_id, :property_id, :phone)"
        ),
        {
            "id": tenant_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "phone": _fresh_phone(),
        },
    )
    await session.commit()
    return tenant_id


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


async def _insert_message(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    tenant_id: str,
    body: str,
    prefilter: PrefilterResult | None = None,
) -> str:
    message_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, landlord_id, property_id, tenant_id, direction, party, body, twilio_sid, "
            " prefilter) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, 'inbound', 'tenant', :body, "
            " :twilio_sid, CAST(:prefilter AS jsonb))"
        ),
        {
            "id": message_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "body": body,
            "twilio_sid": f"SM{uuid.uuid4().hex}",
            "prefilter": (prefilter or PrefilterResult(hard_hit=False)).model_dump_json(),
        },
    )
    await session.commit()
    return message_id


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(
        text("DELETE FROM audit_log WHERE landlord_id = :lid"), {"lid": landlord_id}
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
# Fake Anthropic client
# ---------------------------------------------------------------------------


def _fake_message(*, tool_input: dict[str, Any]) -> SimpleNamespace:
    block = ToolUseBlock(
        id="toolu_test", input=tool_input, name="classify_severity", type="tool_use"
    )
    usage = SimpleNamespace(input_tokens=200, output_tokens=60)
    return SimpleNamespace(content=[block], usage=usage, model="claude-sonnet-5")


class _FakeMessages:
    def __init__(self, *, responses: list[Any] | None = None, delay: float = 0.0) -> None:
        self._responses = list(responses or [])
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        import asyncio

        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake_messages: _FakeMessages) -> None:
    monkeypatch.setattr(anthropic_mod, "get_client", lambda: _FakeClient(fake_messages))


def _base_state(
    *,
    message_id: str,
    landlord_id: str,
    property_id: str,
    tenant_id: str,
    case_id: str | None = None,
) -> AgentState:
    return {
        "message_id": uuid.UUID(message_id),
        "case_context": CaseContext(
            landlord_id=uuid.UUID(landlord_id),
            property_id=uuid.UUID(property_id),
            tenant_id=uuid.UUID(tenant_id),
            case_id=uuid.UUID(case_id) if case_id is not None else None,
        ),
        "reasoning_log": [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_classify_severity_success_writes_state_and_audit_log(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="fridge just completely died, got a week of groceries in there",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "severity": "URGENT",
                    "rules_fired": ["Refrigerator dead — spoilage clock is running"],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["The fridge is completely dead and groceries will spoil."],
                }
            )
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state = _base_state(
            message_id=message_id,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            case_id=case_id,
        )
        update = await classify_severity(state)

        assert update["classification_failed"] is False
        assert update["severity"].severity.value == "URGENT"
        assert any("urgent" in line for line in update["reasoning_log"])
        assert any("groceries" in line for line in update["reasoning_log"])

        audit_rows = (
            (
                await db_session.execute(
                    text("SELECT actor, action, payload FROM audit_log WHERE case_id = :cid"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(audit_rows) == 1
        row = audit_rows[0]
        assert row["actor"] == "agent"
        assert row["action"] == "classified"
        payload = row["payload"]
        assert payload["severity"] == "urgent"  # lowercase db_value, not "URGENT"
        assert payload["message_id"] == message_id
        assert payload["case_id"] == case_id
        assert payload["model"] == "claude-sonnet-5"
        assert payload["tokens_in"] == 200
        assert payload["tokens_out"] == 60
        assert payload["prompt_version"] == "v2"
        assert isinstance(payload["cost_cents"], (int, float))
        assert "rules_fired" in payload
        assert "modifier" in payload
        assert "refusal_flags" in payload
        # Never a message body in the audit payload.
        assert "fridge" not in str(payload)
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_includes_weather_and_vulnerable_occupant_in_request(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat hasn't worked since 10pm and it's freezing, we have the baby this week",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "severity": "EMERGENCY",
                    "rules_fired": ["no heat at/below -10C"],
                    "modifier": "vulnerable-occupant bump: infant",
                    "refusal_flags": [],
                    "reasoning": ["No heat at -15C with an infant present."],
                }
            )
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        from app.agent.schemas import VulnerableOccupant

        state = _base_state(
            message_id=message_id,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
        )
        state["case_context"] = state["case_context"].model_copy(
            update={
                "case_id": uuid.UUID(case_id),
                "vulnerable_occupant": VulnerableOccupant.infant,
            }
        )
        state["weather"] = WeatherSnapshot(
            current_temp_c=-15.0, overnight_low_c=-17.0, heat_warning=False
        )
        await classify_severity(state)

        user_content = fake_messages.calls[0]["messages"][0]["content"]
        assert "-15.0" in user_content
        assert "-17.0" in user_content
        assert "infant" in user_content
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_double_failure_sets_classification_failed_flag(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="not sure what's going on but something seems off",
    )

    fake_messages = _FakeMessages(
        responses=[
            # Both attempts return a schema-invalid severity value.
            _fake_message(tool_input={"severity": "NOT_A_REAL_SEVERITY"}),
            _fake_message(tool_input={"severity": "ALSO_BAD"}),
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state = _base_state(
            message_id=message_id,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            case_id=case_id,
        )
        update = await classify_severity(state)

        assert update["classification_failed"] is True
        assert "severity" not in update
        assert any("couldn't finish classifying" in line for line in update["reasoning_log"])
        assert len(fake_messages.calls) == 2

        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert audit_count == 0  # no audit row on failure -- see module docstring
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_double_timeout_shares_one_end_to_end_deadline(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both attempts time out; the retry still fires (enough budget
    remains) and total elapsed stays bounded by the SHARED deadline --
    never 2x independent per-attempt budgets (spec-guardian ruling)."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="hello",
    )

    monkeypatch.setattr(anthropic_mod, "CLASSIFICATION_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(anthropic_mod, "FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS", 0.1)
    monkeypatch.setattr(anthropic_mod, "MIN_RETRY_BUDGET_SECONDS", 0.05)
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(tool_input={"severity": "ROUTINE"}),
            _fake_message(tool_input={"severity": "ROUTINE"}),
        ],
        delay=1.0,
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state = _base_state(
            message_id=message_id,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            case_id=case_id,
        )
        start = time.monotonic()
        update = await classify_severity(state)
        elapsed = time.monotonic() - start

        assert update["classification_failed"] is True
        assert len(fake_messages.calls) == 2
        assert elapsed < 0.6
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_retry_skipped_when_budget_exhausted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the first attempt's timeout already consumes the (tiny) shared
    budget down to below the retry floor, the retry is skipped entirely --
    only ONE call is ever made, and no audit row is written."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="hello",
    )

    monkeypatch.setattr(anthropic_mod, "CLASSIFICATION_BUDGET_SECONDS", 0.1)
    monkeypatch.setattr(anthropic_mod, "FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS", 0.1)
    monkeypatch.setattr(anthropic_mod, "MIN_RETRY_BUDGET_SECONDS", 0.5)
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(tool_input={"severity": "ROUTINE"}),
            _fake_message(tool_input={"severity": "ROUTINE"}),
        ],
        delay=1.0,
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state = _base_state(
            message_id=message_id,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            case_id=case_id,
        )
        update = await classify_severity(state)

        assert update["classification_failed"] is True
        assert len(fake_messages.calls) == 1

        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert audit_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_tier0_clamp_never_deescalates(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM says ROUTINE but the Tier-0 prefilter hard-fired -> clamped to
    EMERGENCY, with the mandated reasoning_log line and a structlog warning."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="there is a fire in the hallway",
        prefilter=PrefilterResult(hard_hit=True, categories=["fire"]),
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "severity": "ROUTINE",
                    "rules_fired": [],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["Seems minor."],
                }
            )
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state = _base_state(
            message_id=message_id,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            case_id=case_id,
        )
        update = await classify_severity(state)

        assert update["severity"].severity.value == "EMERGENCY"
        assert any(
            "The alarm phrasing already made this an emergency — I kept it there." == line
            for line in update["reasoning_log"]
        )

        audit_row = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["payload"]["severity"] == "emergency"
        assert any("Tier-0" in rule for rule in audit_row["payload"]["rules_fired"])
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_never_clamps_when_already_emergency(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No clamp note is added when the model already agrees with Tier-0."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="there is a fire in the hallway",
        prefilter=PrefilterResult(hard_hit=True, categories=["fire"]),
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "severity": "EMERGENCY",
                    "rules_fired": ["Fire"],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["Active fire hazard."],
                }
            )
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state = _base_state(
            message_id=message_id,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            case_id=case_id,
        )
        update = await classify_severity(state)

        assert update["severity"].severity.value == "EMERGENCY"
        assert not any("kept it there" in line for line in update["reasoning_log"])
    finally:
        await _cleanup(db_session, landlord_id)
