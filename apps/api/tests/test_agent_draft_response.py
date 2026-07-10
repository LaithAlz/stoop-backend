"""Integration tests for ``app/agent/nodes/draft_response.py`` (#33).

Marker: ``integration`` — requires a running Postgres instance (docker
-compose) + ``alembic upgrade head``, same as ``tests/test_agent_nodes.py``.
The Anthropic SDK itself is ALWAYS mocked (``app.integrations.anthropic
.get_client`` monkeypatched) — no real API calls anywhere in this suite.

Run with:
    export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop
    uv run pytest tests/test_agent_draft_response.py -m integration -v
"""

from __future__ import annotations

import asyncio
import json
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

import app.agent.nodes.draft_response as node_mod
import app.db.session as db_mod
from app.agent.nodes.draft_response import draft_response
from app.agent.prompts.v2 import REFUSAL_TEMPLATES
from app.agent.schemas import CaseContext, RefusalFlag, Severity, SeverityResult
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
    session: AsyncSession, landlord_id: str, *, house_rules: str | None = "No pets."
) -> str:
    property_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO properties (id, landlord_id, label, address_line1, city, house_rules) "
            "VALUES (:id, :landlord_id, 'Test Property', '123 Test St', 'Toronto', :house_rules)"
        ),
        {"id": property_id, "landlord_id": landlord_id, "house_rules": house_rules},
    )
    await session.commit()
    return property_id


async def _insert_tenant(
    session: AsyncSession, landlord_id: str, property_id: str, *, name: str | None = "Maria"
) -> str:
    tenant_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO tenants (id, landlord_id, property_id, phone, name) "
            "VALUES (:id, :landlord_id, :property_id, :phone, :name)"
        ),
        {
            "id": tenant_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "phone": _fresh_phone(),
            "name": name,
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
    session: AsyncSession, *, landlord_id: str, property_id: str, tenant_id: str, body: str
) -> str:
    message_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, landlord_id, property_id, tenant_id, direction, party, body, twilio_sid) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, 'inbound', 'tenant', :body, "
            " :twilio_sid)"
        ),
        {
            "id": message_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "body": body,
            "twilio_sid": f"SM{uuid.uuid4().hex}",
        },
    )
    await session.commit()
    return message_id


async def _insert_pending_draft(
    session: AsyncSession, *, landlord_id: str, case_id: str, body: str = "old draft"
) -> str:
    row = (
        (
            await session.execute(
                text(
                    "INSERT INTO drafts (landlord_id, case_id, recipient, body, prompt_version, "
                    "status) VALUES (:landlord_id, :case_id, 'tenant', :body, 'v1', 'pending') "
                    "RETURNING id"
                ),
                {"landlord_id": landlord_id, "case_id": case_id, "body": body},
            )
        )
        .mappings()
        .one()
    )
    await session.commit()
    draft_id: str = str(row["id"])
    return draft_id


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(
        text("DELETE FROM audit_log WHERE landlord_id = :lid"), {"lid": landlord_id}
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


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------


def _fake_message(*, body: str, refusal_templates_used: list[str] | None = None) -> SimpleNamespace:
    tool_input: dict[str, Any] = {"body": body}
    if refusal_templates_used is not None:
        tool_input["refusal_templates_used"] = refusal_templates_used
    block = ToolUseBlock(id="toolu_test", input=tool_input, name="draft_message", type="tool_use")
    usage = SimpleNamespace(input_tokens=150, output_tokens=40)
    return SimpleNamespace(content=[block], usage=usage, model="claude-sonnet-5")


class _FakeMessages:
    def __init__(self, *, responses: list[Any] | None = None, delay: float = 0.0) -> None:
        self._responses = list(responses or [])
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
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


def _routine_severity(refusal_flags: list[RefusalFlag] | None = None) -> SeverityResult:
    return SeverityResult(
        severity=Severity.ROUTINE,
        rules_fired=[],
        modifier=None,
        refusal_flags=refusal_flags or [],
        reasoning=["Minor issue."],
    )


def _severity_result(severity: Severity, *, rules_fired: list[str] | None = None) -> SeverityResult:
    return SeverityResult(
        severity=severity,
        rules_fired=rules_fired or [],
        modifier=None,
        refusal_flags=[],
        reasoning=["Reasoning."],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_draft_response_double_timeout_shares_one_end_to_end_deadline(
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
            _fake_message(body="draft one", refusal_templates_used=[]),
            _fake_message(body="draft two", refusal_templates_used=[]),
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
                case_id=uuid.UUID(case_id),
            ),
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        start = time.monotonic()
        update = await draft_response(state)
        elapsed = time.monotonic() - start

        assert update["draft_guard_failed"] is True  # both attempts failed -> safe fallback
        assert len(fake_messages.calls) == 2
        assert elapsed < 0.6
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_retry_skipped_when_budget_exhausted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the first attempt's timeout already consumes the (tiny) shared
    budget down to below the retry floor, the regeneration attempt is
    skipped entirely -- only ONE call is ever made."""
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
            _fake_message(body="draft one", refusal_templates_used=[]),
            _fake_message(body="draft two", refusal_templates_used=[]),
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
                case_id=uuid.UUID(case_id),
            ),
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is True
        assert len(fake_messages.calls) == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_success_inserts_pending_draft_and_audit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(
        db_session, voice_profile={"tone": "warm, direct", "samples": ["Hey! No worries."]}
    )
    property_id = await _insert_property(db_session, landlord_id, house_rules="No smoking.")
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id, name="Maria")
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="kitchen faucet has a slow drip",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                body="Hi Maria — thanks for the heads up, I'll get someone out this week.",
                refusal_templates_used=[],
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
                house_rules="No smoking.",
                voice_profile={"tone": "warm, direct", "samples": ["Hey! No worries."]},
            ),
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is False
        assert "Maria" in update["draft"].body
        assert any("drafted a reply" in line for line in update["reasoning_log"])

        draft_rows = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, recipient, prompt_version, body FROM drafts "
                        "WHERE case_id = :cid"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(draft_rows) == 1
        assert draft_rows[0]["status"] == "pending"
        assert draft_rows[0]["recipient"] == "tenant"
        assert draft_rows[0]["prompt_version"] == "v2"

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
        assert audit_rows[0]["actor"] == "agent"
        assert audit_rows[0]["action"] == "drafted"
        payload = audit_rows[0]["payload"]
        assert payload["guard_failed"] is False
        assert payload["model"] == "claude-sonnet-5"
        assert payload["tokens_in"] == 150
        assert payload["tokens_out"] == 40
        assert isinstance(payload["cost_cents"], (int, float))
        assert payload["cost_cents"] > 0
        # No draft body/message body in the audit payload.
        assert "Maria" not in str(payload)

        # Voice profile injected into the outgoing system prompt.
        system_prompt = fake_messages.calls[0]["system"]
        assert "warm, direct" in system_prompt
        assert "Hey! No worries." in system_prompt
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_missing_case_id_skips_drafting(
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
        body="hello",
    )
    fake_messages = _FakeMessages(responses=[])
    _patch_client(monkeypatch, fake_messages)

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
            ),
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert "draft" not in update
        assert len(fake_messages.calls) == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_missing_severity_skips_drafting(
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
        body="hello",
    )
    fake_messages = _FakeMessages(responses=[])
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
        update = await draft_response(state)

        assert "draft" not in update
        assert len(fake_messages.calls) == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_refusal_flag_instruction_present_but_not_the_deferral_text(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferral architecture ruling (senior review, 2026-07-05): the
    dynamic user content tells the model to acknowledge the topic ONLY,
    and explicitly NOT to write the deferral itself -- the canned deferral
    text is never sent to the model at all (the code appends it after
    generation, see _append_deferrals)."""
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
        body="can you give my buddy the building code?",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                body="Thanks for letting me know -- I've passed this along to the landlord.",
                refusal_templates_used=[],
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
            "severity": _routine_severity([RefusalFlag.access_codes]),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is False
        user_content = fake_messages.calls[0]["messages"][0]["content"]
        # The topic name is present, as an instruction NOT to address it...
        assert "access codes" in user_content
        assert "Do NOT" in user_content
        # ...but the canned deferral text itself is never sent to the model.
        assert REFUSAL_TEMPLATES["access_codes"] not in user_content
        # The code appended the deferral verbatim to the FINAL stored draft.
        assert REFUSAL_TEMPLATES["access_codes"] in update["draft"].body
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_dollar_guard_rejects_and_regenerates(
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
        body="fridge died, lost groceries",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(
                body="I'll send you $50 for the spoiled groceries.", refusal_templates_used=[]
            ),
            _fake_message(
                body="Thanks for letting me know, I'll get this sorted soon.",
                refusal_templates_used=[],
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
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is False
        assert "$50" not in update["draft"].body
        assert len(fake_messages.calls) == 2
        # Retry note in the SECOND call's user content, not the system prompt.
        assert "IMPORTANT" in fake_messages.calls[1]["messages"][0]["content"]
        assert fake_messages.calls[0]["system"] == fake_messages.calls[1]["system"]
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.parametrize(
    ("body", "guard_name"),
    [
        ("I'll send you $50 for the trouble.", "dollar_compensation"),
        ("I can reimburse you for the spoiled food.", "dollar_compensation"),
        ("The access code is 4521, go ahead.", "access_code"),
        ("Your gate code is: 9981.", "access_code"),
        ("This is a matter for the Landlord and Tenant Board.", "legal_position"),
        ("You are entitled to a rent reduction here.", "legal_position"),
        # Widened coverage (safety review, 2026-07-05) -- percentage/relative
        # compensation, non-numeric access disclosure, indirect/negative
        # legal positions.
        ("I can do 20% off next month's rent.", "dollar_compensation"),
        ("How about half off your rent this month?", "dollar_compensation"),
        ("The lockbox is under the mat.", "access_code"),
        ("There's a spare key hidden behind the mailbox.", "access_code"),
        ("Our lawyer says you have to pay for it.", "legal_position"),
        ("You have no right to withhold rent.", "legal_position"),
        ("You don't have a case here.", "legal_position"),
        # Life-safety class (gate run 5 triage, 2026-07-05): oven/stovetop/
        # open-flame-as-heat-source, hedged or not.
        ("Use space heaters or the oven (off) for warmth", "unsafe_heat_source"),
        ("You could turn on the oven for heat tonight.", "unsafe_heat_source"),
        ("Try the stovetop to warm up the kitchen.", "unsafe_heat_source"),
        ("Light some candles to stay warm until then.", "unsafe_heat_source"),
    ],
)
@pytest.mark.integration
async def test_draft_response_hard_guard_positive_cases(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, body: str, guard_name: str
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
        body="tenant message",
    )

    violations = node_mod._check_hard_guards(body=body)
    assert guard_name in violations

    # Also exercise the full node: BOTH attempts violate -> safe fallback used.
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(body=body, refusal_templates_used=[]),
            _fake_message(body=body, refusal_templates_used=[]),
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
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is True
        assert update["draft"].body != body
        assert any("safer standard reply" in line for line in update["reasoning_log"])
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.parametrize(
    "body",
    [
        "Thanks for letting me know, I'll take a look this week.",
        "Tony can come by Thursday morning between 8 and 11.",
        "No worries at all, I'll follow up soon.",
        # A broken oven/stove (repair topic, not a heat-source suggestion)
        # must never trip the new unsafe_heat_source guard.
        "Sorry to hear the oven is broken, someone will look at it tomorrow.",
        "The stove is dead -- I'll get a repair person out by 9am.",
        "Use a space heater and extra blankets to stay warm tonight.",
    ],
)
@pytest.mark.unit
def test_draft_response_hard_guard_negative_cases(body: str) -> None:
    """Clean, ordinary replies must never trip a hard guard (false-positive
    check)."""
    violations = node_mod._check_hard_guards(body=body)
    assert violations == []


# ---------------------------------------------------------------------------
# Guard/deferral self-collision fix (HIGH, safety review 2026-07-05):
# mandated REFUSAL_TEMPLATES text must never trip its OWN guard, and every
# template + the generic fallback must stay clean under the WIDENED guards.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("flag_value", [flag.value for flag in RefusalFlag])
def test_draft_response_every_refusal_template_stays_clean_under_widened_guards(
    flag_value: str,
) -> None:
    template_text = REFUSAL_TEMPLATES[flag_value]
    violations = node_mod._check_hard_guards(body=template_text)
    assert violations == []


@pytest.mark.unit
def test_draft_response_generic_fallback_stays_clean_under_widened_guards() -> None:
    violations = node_mod._check_hard_guards(body=node_mod._GENERIC_SAFE_FALLBACK)
    assert violations == []


@pytest.mark.unit
def test_draft_response_strip_mandated_templates_removes_exact_template_text() -> None:
    template_text = REFUSAL_TEMPLATES["cost_compensation"]
    body = f"Hi Maria, {template_text} Talk soon!"
    scrubbed = node_mod._strip_mandated_templates(body)
    assert template_text not in scrubbed
    assert "Hi Maria," in scrubbed
    assert "Talk soon!" in scrubbed


@pytest.mark.integration
async def test_draft_response_paraphrase_of_topic_accepted_not_generic_fallback(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferral architecture ruling (senior review, 2026-07-05): a
    paraphrase-shaped model acknowledgment that merely mentions the TOPIC
    vocabulary ("rent discount"), with no actual commitment attached, must
    be ACCEPTED on the first attempt -- never degraded to the generic
    fallback purely because of topical wording."""
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
        body="can I get a discount on rent since the heat was out last month?",
    )

    ack_body = "About the rent discount you mentioned — Laith will sort that out separately."
    fake_messages = _FakeMessages(
        responses=[_fake_message(body=ack_body, refusal_templates_used=[])]
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
            "severity": _routine_severity([RefusalFlag.cost_compensation]),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is False
        assert len(fake_messages.calls) == 1  # accepted first try -- no retry, no fallback
        assert ack_body in update["draft"].body
        assert update["draft"].body != node_mod._GENERIC_SAFE_FALLBACK
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_appends_cost_compensation_deferral_verbatim(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model writes ONLY a brief neutral acknowledgment (never the
    deferral itself); the code appends the canned cost_compensation
    deferral verbatim afterward."""
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
        body="can you cover the cost of my ruined couch from the leak?",
    )

    deferral_text = REFUSAL_TEMPLATES["cost_compensation"]
    ack_body = "Thanks for flagging this — I've passed it along to the landlord."
    fake_messages = _FakeMessages(
        responses=[_fake_message(body=ack_body, refusal_templates_used=[])]
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
            "severity": _routine_severity([RefusalFlag.cost_compensation]),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is False
        assert len(fake_messages.calls) == 1
        assert ack_body in update["draft"].body
        assert deferral_text in update["draft"].body
        # Deferral is APPENDED after the model's own acknowledgment.
        assert update["draft"].body.index(ack_body) < update["draft"].body.index(deferral_text)
        assert update["draft"].refusal_templates_used == [RefusalFlag.cost_compensation]
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_appends_legal_rent_ltb_deferral_verbatim(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as cost_compensation, for legal_rent_ltb (whose template text
    contains "Landlord and Tenant Board", which would trip its own
    legal_position guard if the MODEL wrote it -- but the model never
    does; the code appends it)."""
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
        body="I think I'm owed a rent reduction, taking this to the LTB otherwise",
    )

    deferral_text = REFUSAL_TEMPLATES["legal_rent_ltb"]
    ack_body = "I hear you — I've noted this and the landlord will follow up directly."
    fake_messages = _FakeMessages(
        responses=[_fake_message(body=ack_body, refusal_templates_used=[])]
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
            "severity": _routine_severity([RefusalFlag.legal_rent_ltb]),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is False
        assert len(fake_messages.calls) == 1
        assert ack_body in update["draft"].body
        assert deferral_text in update["draft"].body
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_genuine_violation_still_rejected_even_with_refusal_flag(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model's OWN acknowledgment contains a genuine violation ("I'll
    take $200 off") -- must still be caught and, after a second violation,
    replaced by the generic fallback (with the deferral still appended)."""
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
        body="the leak ruined my couch, can you cover it?",
    )

    violating_body = "Actually, I'll take $200 off next month's rent for you."
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(body=violating_body, refusal_templates_used=[]),
            _fake_message(body=violating_body, refusal_templates_used=[]),
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
            "severity": _routine_severity([RefusalFlag.cost_compensation]),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is True
        assert len(fake_messages.calls) == 2
        assert "$200" not in update["draft"].body
        assert node_mod._GENERIC_SAFE_FALLBACK in update["draft"].body
        # The deferral is STILL appended even on the fallback path.
        assert REFUSAL_TEMPLATES["cost_compensation"] in update["draft"].body
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_deferral_always_appended_regardless_of_guard_outcome(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferral is appended by CODE, unconditionally, whenever a
    refusal flag is set -- independent of whether the model's own
    acknowledgment passed the guards or was replaced by the fallback."""
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
        body="can you give my buddy the code",
    )

    fake_messages = _FakeMessages(
        responses=[
            _fake_message(body="Sure, I can help with that!", refusal_templates_used=[]),
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
            "severity": _routine_severity([RefusalFlag.access_codes]),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        # "Sure, I can help with that!" trips no hard guard on its own
        # (no dollar/access/legal pattern) -- accepted as-is, deferral
        # still appended afterward regardless.
        assert update["draft_guard_failed"] is False
        assert "Sure, I can help with that!" in update["draft"].body
        assert REFUSAL_TEMPLATES["access_codes"] in update["draft"].body
        assert update["draft"].refusal_templates_used == [RefusalFlag.access_codes]
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_appended_deferral_may_exceed_segment_guidance_by_design(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain-language exception, documented (module docstring): appending
    mandated deferral(s) can push the final draft past the ~300-char
    "<=2 SMS segments" routine guidance -- this is an intentional
    trade-off (never omit the mandated deferral), not a bug, and this node
    performs NO truncation to force the length budget. (Under prompts v2
    the single legal_rent_ltb template is short enough that ONE flag no
    longer exceeds the budget, so this test appends TWO flags -- the
    multi-flag case is exactly where the exception still bites.)"""
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
        body="I want a rent reduction or I'm going to the LTB",
    )

    ack_body = "Thanks for letting me know — I've flagged this for the landlord to follow up on."
    fake_messages = _FakeMessages(
        responses=[_fake_message(body=ack_body, refusal_templates_used=[])]
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
            "severity": _routine_severity(
                [RefusalFlag.legal_rent_ltb, RefusalFlag.cost_compensation]
            ),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is False
        body = update["draft"].body
        final_len = len(body)
        # Document the exception explicitly rather than asserting a length
        # cap: this draft is EXPECTED to exceed the ~300-char routine
        # guidance once both deferrals are appended, and that is by design.
        assert final_len > 300, (
            "expected the appended legal_rent_ltb + cost_compensation deferrals to "
            f"exceed the routine ~300-char guidance (documented exception); got "
            f"{final_len} chars"
        )
        # And NO truncation happened: both templates are present verbatim.
        assert REFUSAL_TEMPLATES["legal_rent_ltb"] in body
        assert REFUSAL_TEMPLATES["cost_compensation"] in body
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_stale_then_insert_respects_partial_unique_index(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    old_draft_id = await _insert_pending_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, body="earlier draft"
    )
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="also, the hallway light is out",
    )

    fake_messages = _FakeMessages(
        responses=[_fake_message(body="Got it, thanks for the update!", refusal_templates_used=[])]
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
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)
        assert update["draft_guard_failed"] is False

        rows = (
            (
                await db_session.execute(
                    text("SELECT id, status FROM drafts WHERE case_id = :cid ORDER BY created_at"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 2
        assert str(rows[0]["id"]) == old_draft_id
        assert rows[0]["status"] == "stale"
        assert rows[1]["status"] == "pending"

        # Exactly one 'pending' draft ever exists for the case -- the
        # partial unique index (uq_drafts_one_pending) is never violated.
        pending_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE case_id = :cid AND status = 'pending'"),
                {"cid": case_id},
            )
        ).scalar_one()
        assert pending_count == 1

        stale_audit = (
            (
                await db_session.execute(
                    text(
                        "SELECT action, payload FROM audit_log WHERE case_id = :cid "
                        "AND action = 'draft_stale'"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert stale_audit["payload"]["draft_id"] == old_draft_id

        drafted_audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid AND action = 'drafted'"),
                {"cid": case_id},
            )
        ).scalar_one()
        assert drafted_audit_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_no_stale_draft_when_none_pending(
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
        body="the hallway light is out",
    )

    fake_messages = _FakeMessages(
        responses=[_fake_message(body="Thanks, I'll take care of it!", refusal_templates_used=[])]
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
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        await draft_response(state)

        rows = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert rows == 1

        stale_audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE case_id = :cid AND action = 'draft_stale'"
                ),
                {"cid": case_id},
            )
        ).scalar_one()
        assert stale_audit_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Severity-aware next-step guidance (senior review, 2026-07-05) -- pure
# _build_user_content tests, no DB/Anthropic needed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_user_content_urgent_heating_topic_gets_self_help_and_time_guidance() -> None:
    content = node_mod._build_user_content(
        body="the heat hasn't worked since 10pm and it's getting really cold",
        tenant_name="Maria",
        house_rules=None,
        severity_result=_severity_result(
            Severity.URGENT, rules_fired=["No heat (outdoor temperature above -10C)"]
        ),
        refusal_flags=[],
    )
    assert "self-help check" in content
    assert "breaker" in content.lower()
    assert "bounded" in content.lower()


@pytest.mark.unit
def test_build_user_content_urgent_appliance_topic_gets_self_help_and_time_guidance() -> None:
    content = node_mod._build_user_content(
        body="fridge just completely died, light's off and it's not cold at all",
        tenant_name="Sam",
        house_rules=None,
        severity_result=_severity_result(
            Severity.URGENT, rules_fired=["Refrigerator dead (food spoilage clock is running)"]
        ),
        refusal_flags=[],
    )
    assert "plugged in" in content
    assert "breaker" in content.lower()


@pytest.mark.unit
def test_build_user_content_one_question_hard_cap_is_explicit() -> None:
    """Gate run 5 triage: u2's draft asked two questions despite the
    original "at most ONE question" line -- the reminder now spells out
    the exact failure pattern (an "or"-joined double question) as a HARD
    CAP, and the appliance/heating guidance's own illustrative example no
    longer uses that "or"-question shape."""
    content = node_mod._build_user_content(
        body="fridge just completely died",
        tenant_name="Sam",
        house_rules=None,
        severity_result=_severity_result(
            Severity.URGENT, rules_fired=["Refrigerator dead (food spoilage clock is running)"]
        ),
        refusal_flags=[],
    )
    assert "HARD CAP" in content
    assert "plugged in and the breaker" in content
    assert "plugged in properly, or" not in content


@pytest.mark.unit
def test_build_user_content_urgent_security_topic_gets_bounded_window_not_self_help() -> None:
    content = node_mod._build_user_content(
        body="the deadbolt on my unit door stopped catching",
        tenant_name="Dev",
        house_rules=None,
        severity_result=_severity_result(
            Severity.URGENT,
            rules_fired=[
                "Door or window lock broken -- unit is currently securable but compromised"
            ],
        ),
        refusal_flags=[],
    )
    assert "security issue" in content
    assert "within 24 hours" in content
    # The POSITIVE self-help instruction is absent -- the guidance text
    # itself mentions "self-help" only to say a lock has none, which is the
    # point of this test (a security issue gets no self-help instruction).
    assert "Include ONE quick self-help check" not in content


@pytest.mark.unit
def test_build_user_content_urgent_generic_topic_gets_concrete_time_only() -> None:
    content = node_mod._build_user_content(
        body="the toilet keeps running",
        tenant_name="Dev",
        house_rules=None,
        severity_result=_severity_result(Severity.URGENT, rules_fired=["Some other URGENT rule"]),
        refusal_flags=[],
    )
    assert "bounded next step" in content
    assert "self-help" not in content


@pytest.mark.unit
def test_build_user_content_emergency_gets_structure_guidance() -> None:
    content = node_mod._build_user_content(
        body="water is coming through the ceiling light",
        tenant_name="Dev",
        house_rules=None,
        severity_result=_severity_result(
            Severity.EMERGENCY, rules_fired=["Active, uncontained water"]
        ),
        refusal_flags=[],
    )
    assert "safety instruction(s) first" in content
    assert "what you (the landlord) are doing right now" in content


@pytest.mark.unit
def test_build_user_content_emergency_requires_numbered_list_max_three_steps() -> None:
    """Gate run 5 finding: e1's draft crammed 5-6 instructions into one
    dense paragraph even though it was well under the length budget --
    plain-language-rules.md rule 2 ("instructions come as a numbered
    list, most important first, max three steps") was never actually
    encoded in the EMERGENCY guidance until now."""
    content = node_mod._build_user_content(
        body="water is coming through the ceiling light",
        tenant_name="Dev",
        house_rules=None,
        severity_result=_severity_result(
            Severity.EMERGENCY, rules_fired=["Active, uncontained water"]
        ),
        refusal_flags=[],
    )
    assert "NUMBERED LIST" in content
    assert "AT MOST" in content
    assert "3 steps" in content
    assert "most important" in content.lower()
    assert "plain-language-rules.md rule 2" in content


@pytest.mark.unit
def test_build_user_content_routine_gets_no_severity_specific_structure_guidance() -> None:
    content = node_mod._build_user_content(
        body="dripping faucet",
        tenant_name="Sam",
        house_rules=None,
        severity_result=_routine_severity(),
        refusal_flags=[],
    )
    assert "safety instruction(s) first" not in content
    assert "self-help check" not in content


@pytest.mark.unit
def test_build_user_content_routine_gets_concrete_time_commitment_guidance() -> None:
    """Gate run 5 triage: r1's draft said "this week"/"soon" -- ROUTINE now
    gets the same concrete-over-relative next-step reminder EMERGENCY/
    URGENT already had (plain-language-rules.md rule 4 is unconditional)."""
    content = node_mod._build_user_content(
        body="kitchen faucet has a slow drip",
        tenant_name="Sam",
        house_rules=None,
        severity_result=_routine_severity(),
        refusal_flags=[],
    )
    assert "bounded next step" in content
    assert "bare" in content.lower()


@pytest.mark.unit
def test_build_user_content_emergency_heating_topic_gets_unsafe_heat_source_warning() -> None:
    """Gate run 5 triage: e3's draft suggested the oven for warmth -- the
    EMERGENCY path now gets the same oven/stovetop/open-flame warning the
    URGENT heating branch gets, whenever the topic is heating-related."""
    content = node_mod._build_user_content(
        body="the heat hasn't worked since 10pm and it's freezing, we have the baby this week",
        tenant_name="Maria",
        house_rules=None,
        severity_result=_severity_result(Severity.EMERGENCY, rules_fired=["No heat at/below -10C"]),
        refusal_flags=[],
    )
    assert "oven" in content.lower()
    assert "open flame" in content.lower()
    assert "space heater" in content.lower()


@pytest.mark.unit
def test_build_user_content_refusal_ack_forbids_handoff_duplication_and_signoff() -> None:
    """Gate run 6 triage: f1's combined draft read as two texts glued
    together -- the model's ack said "passed this to the landlord, he'll
    follow up ... talk soon" and then the appended deferral template said
    the hand-off AGAIN. The instruction itself mandated that duplication
    ("ONE brief, neutral sentence noting you've passed it along"). The
    refusal guidance now forbids the ack from stating the hand-off,
    promising follow-up, or signing off, because the appended note owns
    all of that."""
    content = node_mod._build_user_content(
        body="last winter the heat was broken for weeks, I want a rent reduction or LTB",
        tenant_name="Maria",
        house_rules=None,
        severity_result=_severity_result(
            Severity.ROUTINE, rules_fired=["Refusal topic: rent reduction / LTB"]
        ),
        refusal_flags=[RefusalFlag.legal_rent_ltb],
    )
    assert "must NOT say you've passed" in content
    assert "must NOT promise follow-up" in content
    assert "must NOT sign off" in content
    assert "more text follows yours" in content
    # The old duplication-mandating instruction is gone.
    assert "sentence noting you've passed it along" not in content
    # The core prohibitions survive the rewrite.
    assert "pre-approved note" in content
    assert "paraphrase" in content


@pytest.mark.unit
def test_build_user_content_emergency_non_heating_topic_gets_no_heat_source_warning() -> None:
    content = node_mod._build_user_content(
        body="water is coming through the ceiling light",
        tenant_name="Dev",
        house_rules=None,
        severity_result=_severity_result(
            Severity.EMERGENCY, rules_fired=["Active, uncontained water"]
        ),
        refusal_flags=[],
    )
    assert "oven" not in content.lower()


@pytest.mark.unit
def test_build_user_content_access_codes_gets_alternative_guidance() -> None:
    content = node_mod._build_user_content(
        body="can you give my buddy the code",
        tenant_name="Sam",
        house_rules=None,
        severity_result=_routine_severity([RefusalFlag.access_codes]),
        refusal_flags=[RefusalFlag.access_codes],
    )
    assert "arrange this themselves directly" in content


@pytest.mark.unit
def test_build_user_content_non_access_refusal_flag_does_not_get_alternative_guidance() -> None:
    content = node_mod._build_user_content(
        body="I think I'm owed a rent reduction",
        tenant_name="Maria",
        house_rules=None,
        severity_result=_routine_severity([RefusalFlag.legal_rent_ltb]),
        refusal_flags=[RefusalFlag.legal_rent_ltb],
    )
    assert "arrange this themselves directly" not in content


# ---------------------------------------------------------------------------
# Length-budget character hint (senior review, 2026-07-05) -- pure
# _build_user_content / helper tests.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_user_content_includes_available_character_budget_hint() -> None:
    content = node_mod._build_user_content(
        body="dripping faucet",
        tenant_name="Sam",
        house_rules=None,
        severity_result=_routine_severity(),
        refusal_flags=[],
    )
    assert f"at most {node_mod._LENGTH_BUDGET_CHARS} characters" in content


@pytest.mark.unit
def test_build_user_content_available_chars_shrinks_when_deferral_will_be_appended() -> None:
    available = node_mod._available_ack_chars([RefusalFlag.access_codes])
    assert available < node_mod._LENGTH_BUDGET_CHARS
    content = node_mod._build_user_content(
        body="can you give my buddy the code",
        tenant_name="Sam",
        house_rules=None,
        severity_result=_routine_severity([RefusalFlag.access_codes]),
        refusal_flags=[RefusalFlag.access_codes],
    )
    assert f"at most {available} characters" in content


@pytest.mark.unit
def test_available_ack_chars_never_below_floor_for_a_very_long_deferral_combo() -> None:
    available = node_mod._available_ack_chars(list(RefusalFlag))
    assert available == node_mod._MIN_ACK_CHARS_FLOOR


@pytest.mark.unit
def test_available_ack_chars_full_budget_when_no_refusal_flags() -> None:
    assert node_mod._available_ack_chars([]) == node_mod._LENGTH_BUDGET_CHARS


# ---------------------------------------------------------------------------
# Length discipline: regenerate once, then flag -- TRUNCATION IS FORBIDDEN
# (senior review, 2026-07-05).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_draft_response_length_violation_regenerates_once_then_succeeds(
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
        body="dripping faucet",
    )

    long_body = "This is a very long reply. " * 20  # well over 300 chars, guard-clean
    short_body = "Thanks, I'll send someone by Thursday morning."
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(body=long_body, refusal_templates_used=[]),
            _fake_message(body=short_body, refusal_templates_used=[]),
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
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update.get("length_over_budget", False) is False
        assert update["draft_guard_failed"] is False
        assert update["draft"].body == short_body
        assert len(fake_messages.calls) == 2
        # The SECOND call's user content carries the shorten note, not the
        # hard-guard violation note.
        second_call_content = fake_messages.calls[1]["messages"][0]["content"]
        assert "too long" in second_call_content
        assert "IMPORTANT" in second_call_content
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_length_violation_persists_kept_not_truncated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TRUNCATION IS FORBIDDEN: a guard-clean draft that is STILL too long
    after the one regeneration attempt is kept exactly as generated (never
    cut), and state["length_over_budget"] is set."""
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
        body="dripping faucet",
    )

    long_body_1 = "This is a very long reply, attempt one. " * 15
    long_body_2 = "This is a very long reply, attempt two, still long. " * 15
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(body=long_body_1, refusal_templates_used=[]),
            _fake_message(body=long_body_2, refusal_templates_used=[]),
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
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["length_over_budget"] is True
        assert update["draft_guard_failed"] is False  # independent dimensions
        # Kept EXACTLY as generated -- never truncated, never replaced with
        # the generic fallback.
        assert update["draft"].body == long_body_2
        assert len(fake_messages.calls) == 2
        assert any("longer than usual" in line for line in update["reasoning_log"])

        draft_row = (
            (
                await db_session.execute(
                    text("SELECT body FROM drafts WHERE case_id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert draft_row["body"] == long_body_2
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_length_check_exempt_when_refusal_flags_present(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented exception: a refusal-flagged message never triggers the
    length-driven retry, no matter how long the guard-clean ack is."""
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
        body="can you give my buddy the code",
    )

    long_ack = "Thanks for letting me know about this. " * 10  # well over 300 chars
    fake_messages = _FakeMessages(
        responses=[_fake_message(body=long_ack, refusal_templates_used=[])]
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
            "severity": _routine_severity([RefusalFlag.access_codes]),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["length_over_budget"] is False
        assert len(fake_messages.calls) == 1  # no length-driven retry at all
        assert long_ack.rstrip() in update["draft"].body
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_guard_violation_takes_priority_over_length_violation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a candidate is BOTH a hard-guard violation AND too long, the
    retry note names the guard violation, not the length issue -- safety
    takes priority."""
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
        body="fridge died, lost groceries",
    )

    long_and_violating = ("I'll send you $50 for the trouble. " * 10) + "extra padding text"
    short_and_clean = "Thanks, I'll check the breaker and follow up by 9am tomorrow."
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(body=long_and_violating, refusal_templates_used=[]),
            _fake_message(body=short_and_clean, refusal_templates_used=[]),
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
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)

        assert update["draft_guard_failed"] is False
        assert update["length_over_budget"] is False
        assert update["draft"].body == short_and_clean
        second_call_content = fake_messages.calls[1]["messages"][0]["content"]
        assert "dollar_compensation" in second_call_content
        assert "too long" not in second_call_content
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_length_over_budget_cost_accumulates_across_both_calls(
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
        body="dripping faucet",
    )

    long_body = "This is a very long reply. " * 20
    short_body = "Thanks, I'll send someone Thursday between 10 and 12."
    fake_messages = _FakeMessages(
        responses=[
            _fake_message(body=long_body, refusal_templates_used=[]),
            _fake_message(body=short_body, refusal_templates_used=[]),
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
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        await draft_response(state)

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT payload FROM audit_log WHERE case_id = :cid AND action = 'drafted'"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        # 150 tokens_in / 40 tokens_out PER call (see _fake_message) -- summed
        # across both attempts, never just the last one.
        assert audit_row["payload"]["tokens_in"] == 300
        assert audit_row["payload"]["tokens_out"] == 80
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# G2 (#34 spec review, MAJOR): the ON CONFLICT/retry path in
# _stale_then_insert_draft had no test that would fail if it were reverted
# to a naive INSERT -- every existing test runs a single writer sequentially,
# so the conflict branch never engages. These tests force it deterministically
# by wrapping a REAL session and making its FIRST (or every) _INSERT_DRAFT_SQL
# call look like it lost the uq_drafts_one_pending race (RETURNING produced
# no row) -- no genuine concurrent transactions needed.
# ---------------------------------------------------------------------------


class _NoRowResult:
    """Duck-types the tiny slice of a SQLAlchemy ``CursorResult`` that
    ``_stale_then_insert_draft`` actually calls: ``.mappings().one_or_none()``
    returning ``None`` -- exactly what a losing ``ON CONFLICT ... DO
    NOTHING`` INSERT reports."""

    def mappings(self) -> _NoRowResult:
        return self

    def one_or_none(self) -> None:
        return None


class _ConflictOnceSession:
    """Wraps a REAL ``AsyncSession``, forcing the first *conflicts*
    executions of ``_INSERT_DRAFT_SQL`` to look like they lost the
    ``uq_drafts_one_pending`` race, then delegating every call --
    including every OTHER statement, and any ``_INSERT_DRAFT_SQL`` call
    past *conflicts* -- to the real session unchanged. Proves the retry
    loop in ``_stale_then_insert_draft`` actually engages (and, when
    *conflicts* is below ``_MAX_DRAFT_INSERT_ATTEMPTS``, succeeds) without
    needing genuinely concurrent transactions.
    """

    def __init__(self, real_session: AsyncSession, *, conflicts: int) -> None:
        self._real = real_session
        self._conflicts_remaining = conflicts
        self.insert_draft_attempts = 0

    async def execute(self, statement: Any, params: Any = None) -> Any:
        if statement is node_mod._INSERT_DRAFT_SQL:  # noqa: SLF001
            self.insert_draft_attempts += 1
            if self._conflicts_remaining > 0:
                self._conflicts_remaining -= 1
                return _NoRowResult()
        return await self._real.execute(statement, params)


@pytest.mark.integration
async def test_stale_then_insert_draft_retries_when_insert_loses_the_race(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        wrapped = _ConflictOnceSession(db_session, conflicts=1)
        reasoning_log: list[str] = []

        new_draft_id = await node_mod._stale_then_insert_draft(  # noqa: SLF001
            wrapped,
            landlord_id=uuid.UUID(landlord_id),
            case_id=uuid.UUID(case_id),
            body="a real reply, composed before the race",
            prompt_version="v2",
            reasoning_log=reasoning_log,
        )

        # Attempt 1 lost the race (forced); attempt 2 actually inserted.
        assert wrapped.insert_draft_attempts == 2
        assert new_draft_id is not None

        row = (
            (
                await db_session.execute(
                    text("SELECT status FROM drafts WHERE id = :id"), {"id": str(new_draft_id)}
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "pending"

        # Exactly one pending draft for the case -- the retry never left a
        # duplicate or a partially-applied stale-mark behind.
        pending_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE case_id = :cid AND status = 'pending'"),
                {"cid": case_id},
            )
        ).scalar_one()
        assert pending_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_stale_then_insert_draft_raises_after_exhausting_all_attempts(
    db_session: AsyncSession,
) -> None:
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        wrapped = _ConflictOnceSession(
            db_session,
            conflicts=node_mod._MAX_DRAFT_INSERT_ATTEMPTS,  # noqa: SLF001
        )
        reasoning_log: list[str] = []

        with pytest.raises(node_mod.DraftInsertRaceExhaustedError):
            await node_mod._stale_then_insert_draft(  # noqa: SLF001
                wrapped,
                landlord_id=uuid.UUID(landlord_id),
                case_id=uuid.UUID(case_id),
                body="a real reply, always loses the race",
                prompt_version="v2",
                reasoning_log=reasoning_log,
            )

        assert wrapped.insert_draft_attempts == node_mod._MAX_DRAFT_INSERT_ATTEMPTS  # noqa: SLF001

        # Every attempt was skipped (ON CONFLICT DO NOTHING) -- nothing was
        # ever actually persisted for this case.
        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_response_returns_gracefully_when_insert_race_exhausted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Node-level counterpart to the two tests above: when
    ``_stale_then_insert_draft`` raises ``DraftInsertRaceExhaustedError``
    (forced here directly, rather than re-deriving the race), the NODE
    must catch it and return a normal partial state update -- never an
    unhandled raise that would crash the whole graph run."""
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
        body="the hallway light is out",
    )

    fake_messages = _FakeMessages(
        responses=[_fake_message(body="Thanks, I'll take care of it!", refusal_templates_used=[])]
    )
    _patch_client(monkeypatch, fake_messages)

    async def _always_exhausted(*_args: Any, **_kwargs: Any) -> uuid.UUID:
        raise node_mod.DraftInsertRaceExhaustedError("forced for test")

    monkeypatch.setattr(node_mod, "_stale_then_insert_draft", _always_exhausted)  # noqa: SLF001

    try:
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
                case_id=uuid.UUID(case_id),
            ),
            "severity": _routine_severity(),
            "reasoning_log": [],
        }
        update = await draft_response(state)  # must NOT raise

        assert update["draft"] is not None  # the reply WAS composed
        assert any("conflicting update" in line for line in update["reasoning_log"])

        # The helper raised before any INSERT could commit -- nothing
        # persisted for this case.
        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert count == 0
    finally:
        await _cleanup(db_session, landlord_id)
