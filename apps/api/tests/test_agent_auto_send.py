"""Integration tests for #60's auto-send exit — ``app/agent/nodes/
auto_send.py`` + ``app/agent/graph.py``'s ``_route_after_draft_response``/
``_route_after_auto_send_draft`` wiring.

This is the ONLY sanctioned exception to landlord approval besides the
emergency safety path (CLAUDE.md rule 3) — every test here exercises it
against the REAL graph (``run_graph``), never a shortcut, so a routing
regression that accidentally widened the auto-send gate would actually be
caught.

Marker: ``integration`` — requires a running Postgres instance + ``alembic
upgrade head``. Harness mirrors ``tests/test_agent_finalize_draft_
decision.py`` (same fake-Anthropic-client machinery, same fixtures) since
this module picks up right where that one's ROUTINE-severity coverage
would naturally continue.
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

import app.agent.graph as graph_mod
import app.db.session as db_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.agent.draft_sender import sender_tick
from app.agent.graph import run_graph
from app.deps import Landlord
from app.integrations import anthropic as anthropic_mod
from app.routers.drafts import undo_approval
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
    await setup_checkpointer()
    yield
    await close_checkpointer()


# ---------------------------------------------------------------------------
# Local helpers — mirrors tests/test_agent_finalize_draft_decision.py.
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
    await session.execute(
        text("DELETE FROM trust_metrics WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM messages WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM tenants WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM properties WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


async def _find_case_id(session: AsyncSession, landlord_id: str) -> str:
    row = (
        (
            await session.execute(
                text("SELECT id FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        )
        .mappings()
        .one()
    )
    return str(row["id"])


async def _draft_row(session: AsyncSession, *, case_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    "SELECT id, status, auto_send, scheduled_send_at, edited "
                    "FROM drafts WHERE case_id = :cid ORDER BY created_at DESC LIMIT 1"
                ),
                {"cid": case_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def _case_row(session: AsyncSession, *, case_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text("SELECT status, langgraph_thread_id FROM cases WHERE id = :cid"),
                {"cid": case_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def _audit_actions(session: AsyncSession, *, case_id: str) -> list[str]:
    rows = (
        (
            await session.execute(
                text("SELECT action FROM audit_log WHERE case_id = :cid ORDER BY id"),
                {"cid": case_id},
            )
        )
        .mappings()
        .all()
    )
    return [row["action"] for row in rows]


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


def _happy_path_fake_messages(
    *, body: str = "I'll take a look at that this week.", severity: str = "ROUTINE"
) -> _FakeMessages:
    return _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "intent": "maintenance",
                    "is_new_issue": True,
                    "summary": "Squeaky door hinge",
                },
                tool_name="classify_intent",
            ),
            _fake_message(
                tool_input={
                    "severity": severity,
                    "rules_fired": ["Cosmetic, no safety impact"],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["A squeaky hinge is not urgent."],
                },
                tool_name="classify_severity",
            ),
            _fake_message(
                tool_input={"body": body, "refusal_templates_used": []}, tool_name="draft_message"
            ),
        ]
    )


class _FakeSmsSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str:
        self.calls.append({"to_e164": to_e164, "from_e164": from_e164, "body": body})
        return f"SM{uuid.uuid4().hex}"


async def _seed_landlord_property_tenant(session: AsyncSession) -> tuple[str, str, str]:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(
        session, landlord_id, twilio_number=factories.fresh_phone()
    )
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id)
    return landlord_id, property_id, tenant_id


# ---------------------------------------------------------------------------
# 1. Happy path — routine draft on an unlocked property auto-sends, no
#    interrupt, `auto_sent` audit row, sender tick delivers exactly once.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_routine_draft_on_unlocked_property_auto_sends_end_to_end(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id, property_id, tenant_id = await _seed_landlord_property_tenant(db_session)
    await factories.insert_trust_metrics(
        db_session, landlord_id=landlord_id, property_id=property_id, autonomy_unlocked=True
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the front door hinge squeaks a bit",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages())
    try:
        final_state = await run_graph(uuid.UUID(message_id))
        assert "__interrupt__" not in final_state  # never paused

        case_id = await _find_case_id(db_session, landlord_id)
        case = await _case_row(db_session, case_id=case_id)
        # Never set to awaiting_approval -- nothing for a landlord to approve.
        assert case["status"] == "open"

        draft = await _draft_row(db_session, case_id=case_id)
        assert draft["status"] == "approved"
        assert draft["auto_send"] is True
        assert draft["edited"] is False
        assert draft["scheduled_send_at"] is not None

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "auto_sent" in actions
        assert "approved" not in actions

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor FROM audit_log WHERE case_id = :cid AND action = 'auto_sent'"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["actor"] == "agent"

        # The undo window (+5s) hasn't elapsed yet -- backdate it the same
        # way every other sender_tick test in this codebase simulates
        # "already due" (tests/test_agent_draft_sender.py's own
        # `_seed_approved_draft`), rather than sleeping for real seconds.
        await db_session.execute(
            text(
                "UPDATE drafts SET scheduled_send_at = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": str(draft["id"])},
        )
        await db_session.commit()

        # The EXISTING sender ticker drains it -- no new send path.
        sender = _FakeSmsSender()
        claimed = await sender_tick(sender=sender)
        assert claimed == 1
        assert len(sender.calls) == 1

        draft_after = await _draft_row(db_session, case_id=case_id)
        assert draft_after["status"] == "sent"

        case_after = await _case_row(db_session, case_id=case_id)
        assert case_after["status"] == "awaiting_tenant"

        # Exactly once -- a second tick finds nothing left to claim.
        claimed_again = await sender_tick(sender=sender)
        assert claimed_again == 0
        assert len(sender.calls) == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2. Undo works on an auto-sent draft within the window.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_undo_works_on_auto_sent_draft_within_window(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id, property_id, tenant_id = await _seed_landlord_property_tenant(db_session)
    await factories.insert_trust_metrics(
        db_session, landlord_id=landlord_id, property_id=property_id, autonomy_unlocked=True
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the front door hinge squeaks a bit",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages())
    try:
        await run_graph(uuid.UUID(message_id))
        case_id = await _find_case_id(db_session, landlord_id)
        draft = await _draft_row(db_session, case_id=case_id)
        assert draft["status"] == "approved"

        landlord = Landlord(id=uuid.UUID(landlord_id))
        result = await undo_approval(draft["id"], (landlord, db_session))
        assert result.status == "pending"

        draft_after = await _draft_row(db_session, case_id=case_id)
        assert draft_after["status"] == "pending"
        assert draft_after["scheduled_send_at"] is None

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "send_cancelled" in actions

        # The sender ticker must never touch a pending (un-approved) draft.
        sender = _FakeSmsSender()
        claimed = await sender_tick(sender=sender)
        assert claimed == 0
        assert sender.calls == []
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 3. Unlocked-but-revoked falls back to the normal approval interrupt.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unlocked_but_revoked_property_falls_to_normal_approval(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id, property_id, tenant_id = await _seed_landlord_property_tenant(db_session)
    await factories.insert_trust_metrics(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        autonomy_unlocked=False,
        revoked_at=datetime.now(UTC),
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the front door hinge squeaks a bit",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages())
    try:
        final_state = await run_graph(uuid.UUID(message_id))
        assert "__interrupt__" in final_state  # paused -- normal approval flow

        case_id = await _find_case_id(db_session, landlord_id)
        case = await _case_row(db_session, case_id=case_id)
        assert case["status"] == "awaiting_approval"

        draft = await _draft_row(db_session, case_id=case_id)
        assert draft["status"] == "pending"
        assert draft["auto_send"] is False

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "auto_sent" not in actions
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 4. Trust-lookup failure -- fail-closed to the normal approval interrupt,
#    never fail-open to auto-send.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_trust_lookup_failure_falls_back_to_normal_approval(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id, property_id, tenant_id = await _seed_landlord_property_tenant(db_session)
    # Genuinely unlocked -- WOULD auto-send if the lookup succeeded.
    await factories.insert_trust_metrics(
        db_session, landlord_id=landlord_id, property_id=property_id, autonomy_unlocked=True
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the front door hinge squeaks a bit",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages())

    async def _raising_lookup(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("simulated trust_metrics lookup failure")

    monkeypatch.setattr(graph_mod, "is_routine_autonomy_unlocked", _raising_lookup)

    try:
        final_state = await run_graph(uuid.UUID(message_id))
        assert "__interrupt__" in final_state  # fail-closed, never auto-sent

        case_id = await _find_case_id(db_session, landlord_id)
        case = await _case_row(db_session, case_id=case_id)
        assert case["status"] == "awaiting_approval"

        draft = await _draft_row(db_session, case_id=case_id)
        assert draft["status"] == "pending"

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "auto_sent" not in actions
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 5. CRITICAL FENCES -- urgent/emergency severities never auto-send, even
#    when the property's own 'routine' row is unlocked.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_urgent_severity_never_auto_sends_even_when_routine_unlocked(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id, property_id, tenant_id = await _seed_landlord_property_tenant(db_session)
    await factories.insert_trust_metrics(
        db_session, landlord_id=landlord_id, property_id=property_id, autonomy_unlocked=True
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat has been out since this morning",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages(severity="URGENT"))
    try:
        final_state = await run_graph(uuid.UUID(message_id))
        assert "__interrupt__" in final_state  # normal pause, never auto-sent

        case_id = await _find_case_id(db_session, landlord_id)
        draft = await _draft_row(db_session, case_id=case_id)
        assert draft["status"] == "pending"
        assert draft["auto_send"] is False

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "auto_sent" not in actions
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_emergency_severity_never_auto_sends_even_when_routine_unlocked(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id, property_id, tenant_id = await _seed_landlord_property_tenant(db_session)
    await factories.insert_trust_metrics(
        db_session, landlord_id=landlord_id, property_id=property_id, autonomy_unlocked=True
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="there's smoke and I smell gas, please help",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages(severity="EMERGENCY"))
    try:
        final_state = await run_graph(uuid.UUID(message_id))
        assert "__interrupt__" not in final_state  # degraded_mode -> END, never a pause

        case_id = await _find_case_id(db_session, landlord_id)
        case = await _case_row(db_session, case_id=case_id)
        # Never awaiting_approval (degraded-mode path) and never auto-sent.
        assert case["status"] != "awaiting_approval"

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "auto_sent" not in actions

        draft = await _draft_row(db_session, case_id=case_id)
        assert draft["status"] == "pending"  # degraded-path drafts stay pending, unsent
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 6. Safety review MEDIUM-1 (supersession) — a newer inbound message must
#    cancel a still-unsent auto-sent draft, never let both go out.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_newer_inbound_cancels_unsent_auto_sent_draft_exactly_one_send(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact burst scenario (safety review MEDIUM-1): msg1 arrives and
    auto-approves D1; msg2 arrives on the SAME case before any sender tick
    runs; the tick that follows must send EXACTLY ONE reply (D2's content)
    and D1 must be cancelled with a supersession-tagged audit row — never
    two unattended sends for one burst."""
    landlord_id, property_id, tenant_id = await _seed_landlord_property_tenant(db_session)
    await factories.insert_trust_metrics(
        db_session, landlord_id=landlord_id, property_id=property_id, autonomy_unlocked=True
    )
    first_message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the front door hinge squeaks a bit",
    )
    _patch_client(
        monkeypatch, _happy_path_fake_messages(body="I'll take a look at that this week.")
    )
    try:
        await run_graph(uuid.UUID(first_message_id))
        case_id = await _find_case_id(db_session, landlord_id)
        first_draft = await _draft_row(db_session, case_id=case_id)
        assert first_draft["status"] == "approved"
        assert first_draft["auto_send"] is True
        first_draft_id = str(first_draft["id"])

        # msg2 arrives BEFORE any sender tick — D1's undo window (+5s)
        # hasn't elapsed, exactly the reviewer's scenario.
        second_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="also the mailbox lid is loose",
        )
        _patch_client(monkeypatch, _happy_path_fake_messages(body="I'll fix the mailbox too."))
        await run_graph(uuid.UUID(second_message_id))

        # D1 must be cancelled -- never left dangling to also send.
        first_draft_after = (
            (
                await db_session.execute(
                    text("SELECT status FROM drafts WHERE id = :id"), {"id": first_draft_id}
                )
            )
            .mappings()
            .one()
        )
        assert first_draft_after["status"] == "cancelled"

        cancel_audit = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor, payload FROM audit_log "
                        "WHERE case_id = :cid AND action = 'send_cancelled'"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert cancel_audit["actor"] == "agent"
        assert cancel_audit["payload"]["reason"] == "superseded_by_newer_message"
        assert cancel_audit["payload"]["draft_id"] == first_draft_id

        second_draft = await _draft_row(db_session, case_id=case_id)  # latest by created_at
        assert second_draft["id"] != uuid.UUID(first_draft_id)
        assert second_draft["status"] == "approved"
        assert second_draft["auto_send"] is True

        # Backdate for the tick (same convention as every other sender_tick
        # test in this codebase -- never sleep for real seconds).
        await db_session.execute(
            text(
                "UPDATE drafts SET scheduled_send_at = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": str(second_draft["id"])},
        )
        await db_session.commit()

        sender = _FakeSmsSender()
        claimed = await sender_tick(sender=sender)
        assert claimed == 1
        assert len(sender.calls) == 1
        assert sender.calls[0]["body"] == "I'll fix the mailbox too."

        final_first = (
            (
                await db_session.execute(
                    text("SELECT status FROM drafts WHERE id = :id"), {"id": first_draft_id}
                )
            )
            .mappings()
            .one()
        )
        assert final_first["status"] == "cancelled"  # never claimed/sent

        second_final = await _draft_row(db_session, case_id=case_id)
        assert second_final["status"] == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sender_cancels_auto_send_draft_when_newer_inbound_exists(
    db_session: AsyncSession,
) -> None:
    """Belt-and-braces (safety review MEDIUM-1): even bypassing draft_
    response's own primary fix entirely (seeded directly, no graph run),
    the sender's OWN claim guard refuses to send a superseded auto_send
    draft — cancelling it instead of claiming it. A landlord-approved
    (``auto_send=false``) draft in the identical situation is UNTOUCHED."""
    landlord_id, property_id, tenant_id = await _seed_landlord_property_tenant(db_session)
    case_id = await factories.insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="open",
        severity="routine",
    )
    auto_draft_id = await factories.insert_draft(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="approved",
        scheduled_send_at=datetime.now(UTC) - timedelta(seconds=1),
        auto_send=True,
    )

    # A landlord-approved draft on a DIFFERENT case, same shape otherwise --
    # must be sent normally, never cancelled by this guard.
    other_case_id = await factories.insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="open",
        severity="routine",
    )
    landlord_draft_id = await factories.insert_draft(
        db_session,
        landlord_id=landlord_id,
        case_id=other_case_id,
        status="approved",
        scheduled_send_at=datetime.now(UTC) - timedelta(seconds=1),
        auto_send=False,
    )

    try:
        # A newer inbound message lands on the auto_send draft's OWN case,
        # correlated via message_cases (messages.case_id stays NULL in
        # production -- the same convention every other correlation in
        # this codebase relies on).
        newer_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="one more thing",
        )
        await factories.insert_message_case(
            db_session, message_id=newer_message_id, case_id=case_id
        )
        # Same-shaped newer inbound on the OTHER case too, to prove a
        # landlord-approved draft is sent regardless.
        newer_message_id_2 = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="one more thing on the other case",
        )
        await factories.insert_message_case(
            db_session, message_id=newer_message_id_2, case_id=other_case_id
        )

        sender = _FakeSmsSender()
        claimed = await sender_tick(sender=sender)
        assert claimed == 1  # only the landlord-approved draft
        assert len(sender.calls) == 1

        auto_draft_after = (
            (
                await db_session.execute(
                    text("SELECT status FROM drafts WHERE id = :id"), {"id": auto_draft_id}
                )
            )
            .mappings()
            .one()
        )
        assert auto_draft_after["status"] == "cancelled"

        cancel_audit = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor, payload FROM audit_log "
                        "WHERE case_id = :cid AND action = 'send_cancelled'"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert cancel_audit["actor"] == "agent"
        assert cancel_audit["payload"]["reason"] == "superseded_by_newer_message"

        landlord_draft_after = (
            (
                await db_session.execute(
                    text("SELECT status FROM drafts WHERE id = :id"), {"id": landlord_draft_id}
                )
            )
            .mappings()
            .one()
        )
        assert landlord_draft_after["status"] == "sent"  # untouched by the auto_send-only guard
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 7. Safety review MEDIUM-2 (rate cap) — a case cannot receive unlimited
#    auto-sends; at/over the cap, auto-send falls back to the approval
#    interrupt.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_auto_send_daily_case_cap_falls_back_to_approval(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 auto-sends (the default cap) then a 6th routine inbound on the
    SAME case must produce a normal approval card, never a 6th send."""
    from app.config import settings

    monkeypatch.setattr(settings, "auto_send_daily_case_cap", 5)

    landlord_id, property_id, tenant_id = await _seed_landlord_property_tenant(db_session)
    await factories.insert_trust_metrics(
        db_session, landlord_id=landlord_id, property_id=property_id, autonomy_unlocked=True
    )
    # One open case, pre-seeded with 5 `auto_sent` audit rows -- seeded
    # directly (cheap, deterministic) rather than 5 full graph runs; the
    # router's own count read doesn't care how the rows got there, only
    # that they exist within the trailing 24h.
    case_id = await factories.insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="open",
        severity="routine",
    )
    for _ in range(5):
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            actor="agent",
            action="auto_sent",
            payload={"draft_id": str(uuid.uuid4())},
        )
    try:
        # The 6th routine message on the SAME conversation -- identify_case
        # attaches it to the tenant's one open case (conversation-model.md's
        # own routing rule: exactly one open case -> attach). Must land in
        # the approval queue, never auto-send.
        sixth_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="one more routine thing",
        )
        _patch_client(monkeypatch, _happy_path_fake_messages(body="Got it, I'll take a look."))

        final_state = await run_graph(uuid.UUID(sixth_message_id))
        assert "__interrupt__" in final_state  # approval card, not a send

        case_after = (
            (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert case_after["status"] == "awaiting_approval"

        draft = await _draft_row(db_session, case_id=case_id)
        assert draft["status"] == "pending"
        assert draft["auto_send"] is False

        auto_sent_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE case_id = :cid AND action = 'auto_sent'"
                ),
                {"cid": case_id},
            )
        ).scalar_one()
        assert auto_sent_count == 5  # unchanged -- the 6th never auto-sent
    finally:
        await _cleanup(db_session, landlord_id)
