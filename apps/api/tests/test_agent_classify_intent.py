"""Integration tests for ``app/agent/nodes/classify_intent.py`` (#31).

Marker: ``integration`` — requires a running Postgres instance (docker
-compose) + ``alembic upgrade head``, same as ``tests/test_agent_nodes.py``.
The Anthropic SDK itself is ALWAYS mocked (``app.integrations.anthropic
.get_client`` monkeypatched) — no real API calls anywhere in this suite.

Run with:
    export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop
    uv run pytest tests/test_agent_classify_intent.py -m integration -v

Eval-scenario coverage note (#31 AC: "Covered by eval scenarios (R2 admin
vs U2 maintenance)"): the REAL eval harness/YAML scenarios (#35-37) don't
exist in this codebase yet, and this task explicitly must not run
``pytest -m eval`` (real API, costs money — the orchestrator runs that
separately). ``test_classify_intent_r2_admin_scenario`` and
``test_classify_intent_u2_maintenance_scenario`` below exercise this node
against the SAME message text as R2/U2 in
``docs/02-product/eval-scenarios-v1.md``, with a mocked (but scenario-
correct) Anthropic response — confirming the node's plumbing handles both
shapes correctly. They are not a substitute for the real eval run once
#35-37 land.
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
from app.agent.nodes.classify_intent import classify_intent
from app.agent.schemas import CaseContext, PrefilterResult
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
# Seeding helpers (duplicated from tests/test_agent_nodes.py per project
# convention — see that module's own docstring)
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


async def _insert_message(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    tenant_id: str,
    body: str,
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
            "prefilter": PrefilterResult(hard_hit=False).model_dump_json(),
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
    block = ToolUseBlock(id="toolu_test", input=tool_input, name="classify_intent", type="tool_use")
    usage = SimpleNamespace(input_tokens=80, output_tokens=20)
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_classify_intent_success_records_state_and_reasoning_log(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the kitchen faucet has a slow drip",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "intent": "maintenance",
                    "is_new_issue": True,
                    "summary": "Slow drip in the kitchen faucet",
                }
            )
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_intent(state)

        assert update["intent"] is not None
        assert update["intent"].intent.value == "maintenance"
        assert update["intent"].is_new_issue is True
        assert any("Slow drip" in line for line in update["reasoning_log"])
        assert len(fake_messages.calls) == 1
        assert "temperature" not in fake_messages.calls[0]
        assert fake_messages.calls[0]["tool_choice"] == {
            "type": "tool",
            "name": "classify_intent",
        }
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_intent_retries_once_on_validation_failure_then_succeeds(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="could I get rent receipts for March to May?",
    )

    fake_messages = _FakeMessages(
        responses=[
            # First attempt: invalid enum value -> Pydantic ValidationError.
            _fake_message(
                tool_input={
                    "intent": "not-a-real-category",
                    "is_new_issue": True,
                    "summary": "bad payload",
                }
            ),
            # Second attempt: valid.
            _fake_message(
                tool_input={
                    "intent": "admin",
                    "is_new_issue": True,
                    "summary": "Rent receipts for March to May",
                }
            ),
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_intent(state)

        assert update["intent"].intent.value == "admin"
        assert len(fake_messages.calls) == 2
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_intent_double_failure_leaves_intent_unset(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="anyone home? need to ask about parking",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(tool_input={"intent": "bogus", "is_new_issue": True, "summary": "x"}),
            _fake_message(tool_input={"intent": "bogus2", "is_new_issue": True, "summary": "y"}),
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_intent(state)

        assert update["intent"] is None
        assert any("couldn't figure out" in line for line in update["reasoning_log"])
        assert len(fake_messages.calls) == 2
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_intent_double_timeout_shares_one_end_to_end_deadline(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both attempts time out; the retry still fires (enough budget
    remains) and total elapsed stays bounded by the SHARED deadline --
    never 2x independent per-attempt budgets (spec-guardian ruling)."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="hello?",
    )

    monkeypatch.setattr(anthropic_mod, "CLASSIFICATION_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(anthropic_mod, "FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS", 0.1)
    monkeypatch.setattr(anthropic_mod, "MIN_RETRY_BUDGET_SECONDS", 0.05)
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(tool_input={"intent": "other", "is_new_issue": True, "summary": "x"}),
            _fake_message(tool_input={"intent": "other", "is_new_issue": True, "summary": "y"}),
        ],
        delay=1.0,  # far longer than any allotted timeout below
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        start = time.monotonic()
        update = await classify_intent(state)
        elapsed = time.monotonic() - start

        assert update["intent"] is None
        # Retry DID fire: 0.3s budget - 0.1s first-attempt cap = 0.2s
        # remaining, well above the 0.05s floor.
        assert len(fake_messages.calls) == 2
        # Total elapsed bounded by the SHARED 0.3s budget (plus modest
        # overhead) -- not ~2s (2 independent 1.0s-delay attempts) and not
        # 2x an independent per-attempt budget.
        assert elapsed < 0.6
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_intent_retry_skipped_when_budget_exhausted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the first attempt's timeout already consumes the (tiny) shared
    budget down to below the retry floor, the retry is skipped entirely --
    only ONE call is ever made."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="hello?",
    )

    monkeypatch.setattr(anthropic_mod, "CLASSIFICATION_BUDGET_SECONDS", 0.1)
    monkeypatch.setattr(anthropic_mod, "FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS", 0.1)
    monkeypatch.setattr(anthropic_mod, "MIN_RETRY_BUDGET_SECONDS", 0.5)
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(tool_input={"intent": "other", "is_new_issue": True, "summary": "x"}),
            _fake_message(tool_input={"intent": "other", "is_new_issue": True, "summary": "y"}),
        ],
        delay=1.0,
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_intent(state)

        assert update["intent"] is None
        assert len(fake_messages.calls) == 1  # retry skipped -- budget exhausted
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_intent_r2_admin_scenario(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """eval-scenarios-v1.md R2 — rent receipt, admin from records."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body=("hey, could I get rent receipts for March to May? accountant needs them for taxes"),
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "intent": "admin",
                    "is_new_issue": True,
                    "summary": "Tenant needs rent receipts for March to May",
                }
            )
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_intent(state)
        assert update["intent"].intent.value == "admin"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_intent_u2_maintenance_scenario(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """eval-scenarios-v1.md U2 — dead fridge, spoilage clock."""
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body=(
            "fridge just completely died, light's off and it's not cold at all. got a "
            "week of groceries in there :("
        ),
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "intent": "maintenance",
                    "is_new_issue": True,
                    "summary": "Fridge died, food at risk of spoiling",
                }
            )
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_intent(state)
        assert update["intent"].intent.value == "maintenance"
    finally:
        await _cleanup(db_session, landlord_id)
