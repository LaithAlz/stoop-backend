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
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    tenant_id: str,
    severity: str | None = None,
) -> str:
    case_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO cases (id, landlord_id, property_id, tenant_id, status, "
            "severity, langgraph_thread_id) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, 'open', :severity, "
            ":thread_id)"
        ),
        {
            "id": case_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "severity": severity,
            "thread_id": str(uuid.uuid4()),
        },
    )
    await session.commit()
    return case_id


async def _get_case_severity(session: AsyncSession, case_id: str) -> str | None:
    row = (
        (await session.execute(text("SELECT severity FROM cases WHERE id = :id"), {"id": case_id}))
        .mappings()
        .one()
    )
    severity: str | None = row["severity"]
    return severity


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
    body = "fridge just completely died, got a week of groceries in there"
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body=body,
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
        # schema-v1 v1.7: `summary` carries the model's own case-specific
        # reasoning sentence (already appended to reasoning_log verbatim),
        # not the content-free generic severity line -- `GET /v1/queue`'s
        # `why` margin note needs the REASON, not a restatement of the
        # severity chip it renders separately (spec-guardian review, #56).
        assert payload["summary"] == "The fridge is completely dead and groceries will spoil."
        assert payload["summary"] in update["reasoning_log"]
        assert payload["model"] == "claude-sonnet-5"
        assert payload["tokens_in"] == 200
        assert payload["tokens_out"] == 60
        assert payload["prompt_version"] == "v2"
        assert isinstance(payload["cost_cents"], (int, float))
        assert "rules_fired" in payload
        assert "modifier" in payload
        assert "refusal_flags" in payload
        # Never the RAW tenant message body verbatim in the audit payload
        # -- `summary`/`rules_fired` are the model's own PARAPHRASED
        # reasoning about the issue (landlord-facing copy, already shown on
        # the approval card via reasoning_log) and may legitimately reuse
        # issue-specific words ("fridge") without that being the same thing
        # as leaking the verbatim inbound SMS text.
        assert body not in str(payload)
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
        # The persisted summary is the DETERMINISTIC clamped-severity line,
        # even though the model returned non-empty reasoning ("Seems
        # minor.") -- that reasoning reflects the model's own pre-clamp,
        # non-emergency judgment call, and persisting it verbatim would
        # read as de-escalating relative to the clamped EMERGENCY severity
        # this same row records. A clamp always wins over model reasoning.
        assert audit_row["payload"]["summary"] == "I'm treating this as an emergency."
        assert "minor" not in audit_row["payload"]["summary"]
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


@pytest.mark.integration
async def test_classify_severity_summary_is_case_specific_not_generic_template(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """schema-v1 v1.7 / #56 spec-guardian fix: the persisted `summary` must
    be the model's own substantive, case-specific reasoning -- the margin
    note adds the REASON, since the severity chip already shows the label
    -- never just the content-free ``f"I'm treating this as {severity}."``
    restatement."""
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
        body="heat has been out since 10pm, we have the baby this week",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "severity": "EMERGENCY",
                    "rules_fired": ["no heat overnight + infant present"],
                    "modifier": "vulnerable-occupant bump: infant",
                    "refusal_flags": [],
                    "reasoning": ["No heat on a cold night with a baby in the unit can't wait."],
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
        await classify_severity(state)

        audit_row = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()
        )
        summary = audit_row["payload"]["summary"]
        assert summary == "No heat on a cold night with a baby in the unit can't wait."
        # Never the content-free generic template when the model gave
        # substantive reasoning.
        assert summary != "I'm treating this as an emergency."
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_summary_falls_back_to_generic_line_when_reasoning_empty(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No clamp, but the model returned an empty ``reasoning`` list -- there
    is nothing case-specific to persist, so `summary` falls back to the
    same deterministic severity line always appended to reasoning_log."""
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
        body="parking question, nothing urgent",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "severity": "ROUTINE",
                    "rules_fired": [],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": [],
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

        audit_row = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["payload"]["summary"] == "I'm treating this as routine."
        assert audit_row["payload"]["summary"] in update["reasoning_log"]
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# #197 -- cases.severity is now written, post-clamp, never downgraded away
# from 'emergency'.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_classify_severity_writes_severity_onto_case_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain, un-clamped classification writes the SAME severity onto
    ``cases.severity`` that the audit row records -- the case starts with
    ``severity IS NULL`` (never classified before), matching every real
    case before its first classification run."""
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
        assert await _get_case_severity(db_session, case_id) is None

        state = _base_state(
            message_id=message_id,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            case_id=case_id,
        )
        await classify_severity(state)

        assert await _get_case_severity(db_session, case_id) == "urgent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_tier0_clamp_writes_emergency_onto_case_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value written to ``cases.severity`` is the POST-clamp severity --
    the LLM said ROUTINE, Tier-0 hard-fired, so the row gets 'emergency',
    never the model's own pre-clamp value."""
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
        assert await _get_case_severity(db_session, case_id) == "emergency"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_reclassification_updates_case_severity(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later message on the SAME case re-runs classification (e.g. the
    stale-draft re-run) and the case's severity moves to the new post-clamp
    value -- reclassification is a genuine update, not a write-once."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        severity="routine",
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="actually the leak has gotten a lot worse since this morning",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "severity": "URGENT",
                    "rules_fired": ["Active water leak worsening"],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["The leak has escalated since the last message."],
                }
            )
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        assert await _get_case_severity(db_session, case_id) == "routine"

        state = _base_state(
            message_id=message_id,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            case_id=case_id,
        )
        await classify_severity(state)

        assert await _get_case_severity(db_session, case_id) == "urgent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_never_downgrades_case_from_emergency(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case-level mirror of the Tier-0 clamp: a case already at 'emergency'
    (set by an earlier message) must never be overwritten by a LATER
    message's own, wholly legitimate ROUTINE classification -- no Tier-0
    hard hit on THIS message, so nothing clamps update['severity'] itself
    to EMERGENCY, but the case row must stay 'emergency' regardless."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        severity="emergency",
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="did you get my message about the gas smell?",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "severity": "ROUTINE",
                    "rules_fired": [],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["Just a follow-up question, nothing new."],
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

        # The returned state/audit trail reflect the model's OWN, honest
        # ROUTINE call for this message -- the never-downgrade rule is a
        # CASE-level pointer guard, not a rewrite of this call's own result.
        assert update["severity"].severity.value == "ROUTINE"
        audit_row = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["payload"]["severity"] == "routine"

        # But the CASE row itself never moves off 'emergency'.
        assert await _get_case_severity(db_session, case_id) == "emergency"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_severity_skips_case_update_when_no_case_yet(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unknown-sender fallback thread has no case to attach to
    (``case_context.case_id is None``) -- the audit row is still written
    (with a NULL ``case_id``), but there is no ``cases`` row to update and
    this node must not raise trying."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="hello is this the right number for maintenance",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "severity": "ROUTINE",
                    "rules_fired": [],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": [],
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
            case_id=None,
        )
        update = await classify_severity(state)

        assert update["classification_failed"] is False
        audit_row = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE payload ->> 'message_id' = :mid"),
                    {"mid": message_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["payload"]["case_id"] is None
    finally:
        await _cleanup(db_session, landlord_id)
