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
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
import pytest_asyncio
from anthropic.types import ToolUseBlock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.agent.nodes.classify_intent as node_mod
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


class _ScriptedClock:
    """A controllable stand-in for ``time.monotonic()`` -- same pattern as
    ``tests/test_integrations_anthropic.py``'s ``_FakeClock``, which pins
    ``new_deadline``/``attempt_timeout``'s pure arithmetic in isolation.
    ``app/integrations/anthropic.py``'s ``_now()`` exists as a deliberately
    patchable seam precisely for this (module docstring: "tests monkeypatch
    this directly to simulate elapsed time deterministically, with no real
    sleeps").

    #212 item 2: the double-timeout test below used to assert a real
    ``time.monotonic()``-measured ``elapsed < 0.6`` bound -- exactly the
    assertion shape that flakes under CI scheduling load. Patching this
    clock (together with ``_record_call_tool_forced_timeouts`` below, which
    advances it by each attempt's allotted timeout) makes the shared
    -deadline arithmetic the node exercises fully deterministic: no real
    wall-clock reading is ever consulted for the assertion.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _record_call_tool_forced_timeouts(
    monkeypatch: pytest.MonkeyPatch, fake_clock: _ScriptedClock
) -> list[float]:
    """Wrap ``anthropic_mod.call_tool_forced`` to record the actual
    ``timeout_seconds`` each attempt used, then advance *fake_clock* by
    that same amount -- a deterministic stand-in for "this attempt
    consumed its full allotted timeout before failing", with zero
    dependence on real OS/event-loop timing precision. Returns the list
    that gets appended to (in call order) as the node under test runs."""
    recorded: list[float] = []
    real_call_tool_forced = anthropic_mod.call_tool_forced

    async def _wrapper(*, timeout_seconds: float, **kwargs: Any) -> Any:
        recorded.append(timeout_seconds)
        try:
            return await real_call_tool_forced(timeout_seconds=timeout_seconds, **kwargs)
        finally:
            fake_clock.advance(timeout_seconds)

    monkeypatch.setattr(anthropic_mod, "call_tool_forced", _wrapper)
    return recorded


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
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
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
                case_id=uuid.UUID(case_id),
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
        assert payload["kind"] == "intent"
        assert payload["intent"] == "maintenance"
        assert payload["summary"] == "Slow drip in the kitchen faucet"
        assert payload["model"] == "claude-sonnet-5"
        assert payload["tokens_in"] == 80
        assert payload["tokens_out"] == 20
        assert payload["prompt_version"] == "inline-v0"
        assert isinstance(payload["cost_cents"], (int, float))
        assert payload["cost_cents"] > 0
        # The RAW tenant message body never enters the payload (the agent's
        # own short summary is expected/allowed, per the payload shape).
        assert "the kitchen faucet has a slow drip" not in str(payload)
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
async def test_classify_intent_success_row_only_carries_winning_attempt_usage(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#208, "Scope, honestly stated": a first attempt that reaches the API
    and fails, followed by a second that succeeds, does NOT get its own
    failed-attempt tokens folded into the SUCCESS 'classified' row -- that
    row is BYTE-IDENTICAL to before #208, carrying only the winning
    attempt's usage (80/20, per _fake_message's default), never 160/40.
    This is a documented, accepted gap (see this node's own module
    docstring "Cost accounting on TOTAL failure (#208)"), not an oversight
    -- no separate audit row exists for the first attempt either, since the
    node ultimately succeeded."""
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
        body="could I get rent receipts for March to May?",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "intent": "not-a-real-category",
                    "is_new_issue": True,
                    "summary": "bad payload",
                }
            ),
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
                case_id=uuid.UUID(case_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_intent(state)
        assert update["intent"].intent.value == "admin"

        audit_rows = (
            (
                await db_session.execute(
                    text("SELECT payload FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .all()
        )
        # Exactly ONE row -- no separate row for the first, failed attempt.
        assert len(audit_rows) == 1
        payload = audit_rows[0]["payload"]
        assert payload["kind"] == "intent"
        assert payload["tokens_in"] == 80
        assert payload["tokens_out"] == 20
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_intent_double_failure_leaves_intent_unset(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#208: both attempts reach the API (real usage, per ``_fake_message``'s
    default 80/20 tokens) but fail OUR OWN ``IntentResult`` validation -- a
    'classified' audit row IS now written (unlike the pre-#208 behavior),
    but disambiguated as a FAILURE via ``payload.kind`` and carrying no
    ``intent``/``summary`` -- this row never claims a classification
    happened, only that an attempt was made and here is what it cost."""
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
                case_id=uuid.UUID(case_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_intent(state)

        assert update["intent"] is None
        assert any("couldn't figure out" in line for line in update["reasoning_log"])
        assert len(fake_messages.calls) == 2

        audit_row = (
            (
                await db_session.execute(
                    text("SELECT actor, action, payload FROM audit_log WHERE case_id = :cid"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["actor"] == "agent"
        assert audit_row["action"] == "classified"  # existing vocabulary, no migration
        payload = audit_row["payload"]
        assert payload["kind"] == "intent_classification_failed"
        assert "intent" not in payload
        assert "summary" not in payload
        assert "is_new_issue" not in payload
        assert payload["message_id"] == message_id
        assert payload["case_id"] == case_id
        assert payload["model"] == "claude-sonnet-5"
        # Both failed attempts reached the API -- 80/20 tokens EACH (see
        # _fake_message's default), summed, never just the last one.
        assert payload["tokens_in"] == 160
        assert payload["tokens_out"] == 40
        assert isinstance(payload["cost_cents"], (int, float))
        assert payload["cost_cents"] > 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_intent_double_failure_with_no_usage_writes_no_audit_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#208: when NEITHER attempt ever reaches the API (a pre-flight
    connection failure here; a timeout behaves identically -- see
    ``test_integrations_anthropic.py``), there is genuinely no billed cost
    to report, so no NEW audit row is written -- the pre-#208 "no audit row
    on failure" invariant still holds in this specific case."""
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
        body="anyone home? need to ask about parking",
    )

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_messages = _FakeMessages(
        responses=[
            anthropic.APIConnectionError(request=request),
            anthropic.APIConnectionError(request=request),
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
                case_id=uuid.UUID(case_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_intent(state)

        assert update["intent"] is None
        assert len(fake_messages.calls) == 2

        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert audit_count == 0  # no billed usage -- no fabricated record
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_classify_intent_failed_cost_audit_write_error_does_not_raise(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#208 safety review LOW-2: the failed-cost audit INSERT is best-effort
    -- it only RECORDS spend, it is not load-bearing for classification.
    A transient DB error while writing it (simulated here by making
    ``_insert_intent_classification_failed_audit`` itself raise) must be
    logged and swallowed, never propagated -- ``classify_intent`` still
    returns its normal double-failure result (``intent=None``, a plain
    reasoning_log line), so the graph can proceed on to
    ``classify_severity`` exactly as it would if the cost record had
    written successfully."""
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
        body="anyone home? need to ask about parking",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(tool_input={"intent": "bogus", "is_new_issue": True, "summary": "x"}),
            _fake_message(tool_input={"intent": "bogus2", "is_new_issue": True, "summary": "y"}),
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    async def _raising_insert(**kwargs: Any) -> None:
        raise RuntimeError("simulated transient DB failure")

    monkeypatch.setattr(node_mod, "_insert_intent_classification_failed_audit", _raising_insert)

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
                case_id=uuid.UUID(case_id),
            ),
            "reasoning_log": [],
        }
        # Must not raise -- the best-effort try/except swallows it.
        update = await classify_intent(state)

        assert update["intent"] is None
        assert any("couldn't figure out" in line for line in update["reasoning_log"])
        assert len(fake_messages.calls) == 2

        # The write genuinely failed (it was never actually persisted) --
        # this test proves the SWALLOW, not that the row still landed.
        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert audit_count == 0
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
    fake_clock = _ScriptedClock()
    monkeypatch.setattr(anthropic_mod, "_now", fake_clock.now)
    recorded_timeouts = _record_call_tool_forced_timeouts(monkeypatch, fake_clock)

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
        # Retry DID fire: 0.3s budget - 0.1s first-attempt cap = 0.2s
        # remaining, well above the 0.05s floor.
        assert len(fake_messages.calls) == 2
        # Deterministic behavioral pin (#212 item 2, replacing a real-
        # wall-clock ``elapsed < 0.6`` assertion): the SECOND attempt's
        # timeout is exactly the REMAINDER of the ONE shared 0.3s deadline
        # after the first attempt's capped 0.1s -- never a fresh,
        # independent per-attempt budget (which would make this ~2s: two
        # independent 1.0s-delay attempts), and never measured against
        # real elapsed time.
        assert recorded_timeouts == [
            pytest.approx(anthropic_mod.FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS),
            pytest.approx(
                anthropic_mod.CLASSIFICATION_BUDGET_SECONDS
                - anthropic_mod.FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS
            ),
        ]
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
