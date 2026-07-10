"""Integration tests for #43 — shadow mode: ``interrupt()`` before send.

Marker: ``integration`` — requires a running Postgres instance (docker
-compose) + ``alembic upgrade head``. The Anthropic SDK is ALWAYS mocked
(``app.integrations.anthropic.get_client`` monkeypatched) — no real API
calls anywhere in this suite. Seeding helpers come from ``tests/factories.py``
(same shared-factories convention as ``tests/test_agent_graph.py``); the
fake-Anthropic-client machinery and ``_cleanup``/``_case_thread_id`` stay
local, mirroring that module.

This module's centerpiece is the interaction the #34 spec review flagged
as UNVERIFIED and PINNED: what does re-invoking the case-scoped graph do
when its thread is currently paused at ``await_approval``'s
``interrupt()``? Both orderings are tested directly, against the real
``AsyncPostgresSaver``/Postgres checkpointer (never mocked):

1. ``test_new_inbound_on_paused_case_marks_old_draft_stale_and_pauses_fresh``
   — stale-after-pause: a second message arrives while the first draft is
   still awaiting approval.
2. ``test_resume_after_superseded_by_stale_draft_rejects_safely`` —
   resume-after-stale: a (late) resume attempt referencing the NOW-STALE
   draft id must reject, leaving the CURRENT (fresh) pause completely
   undisturbed.

Also covers: durable pause across a simulated process restart, the
completion-gate interaction with ``app/agent/graph_entry.py`` (a paused
run already has its ``'drafted'`` completion marker written, so a
redelivery is correctly a no-op), and that the EMERGENCY/``degraded_mode``
interim edge fires independently of (never trapped behind) this pause.
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

import pytest
import pytest_asyncio
from anthropic.types import ToolUseBlock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.agent.checkpointer as cp_mod
import app.db.session as db_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.agent.graph import (
    CaseNotAwaitingApprovalError,
    DraftStaleError,
    compile_case_graph,
    resume_case_thread,
    run_graph,
)
from app.agent.graph_entry import enqueue_classification
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
    """Ordering contract (``app/agent/checkpointer.py``): ``setup_checkpointer()``
    must run before ``get_checkpointer()`` is ever used."""
    await setup_checkpointer()
    yield
    await close_checkpointer()


# ---------------------------------------------------------------------------
# Local-only helpers (mirrors tests/test_agent_graph.py's own local section).
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


async def _case_row(session: AsyncSession, *, landlord_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text("SELECT id, status, langgraph_thread_id FROM cases WHERE landlord_id = :lid"),
                {"lid": landlord_id},
            )
        )
        .mappings()
        .one()
    )
    # Normalize the driver's own UUID type (asyncpg.pgproto.pgproto.UUID) to
    # a plain str -- uuid.UUID(...) only accepts str/bytes/int, not asyncpg's
    # own UUID wrapper.
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "langgraph_thread_id": str(row["langgraph_thread_id"]),
    }


async def _pending_draft_id(session: AsyncSession, *, case_id: str) -> uuid.UUID:
    row = (
        (
            await session.execute(
                text("SELECT id FROM drafts WHERE case_id = :cid AND status = 'pending'"),
                {"cid": case_id},
            )
        )
        .mappings()
        .one()
    )
    return uuid.UUID(str(row["id"]))


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
        tool_input={"intent": "maintenance", "is_new_issue": True, "summary": "Heat is out"},
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


def _draft_response_message(
    *, body: str = "Thanks for letting me know, I'll look into it."
) -> SimpleNamespace:
    return _fake_message(
        tool_input={"body": body, "refusal_templates_used": []},
        tool_name="draft_message",
    )


def _happy_path_fake_messages(
    *, severity: str = "URGENT", body: str | None = None
) -> _FakeMessages:
    kwargs = {"body": body} if body is not None else {}
    return _FakeMessages(
        responses=[
            _intent_response(),
            _severity_response(severity=severity),
            _draft_response_message(**kwargs),
        ]
    )


# ---------------------------------------------------------------------------
# 1. Happy path — the pause itself.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_urgent_draft_pauses_at_interrupt_and_sets_awaiting_approval(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat has been out since this morning",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages())

    try:
        final_state = await run_graph(uuid.UUID(message_id))

        # ainvoke returns normally (never raises) with LangGraph's own
        # __interrupt__ marker — the pause is a normal return, not an
        # exception (verified against the real checkpointer).
        assert "__interrupt__" in final_state

        case = await _case_row(db_session, landlord_id=landlord_id)
        assert case["status"] == "awaiting_approval"

        draft_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid AND status = 'pending'"),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert draft_count == 1

        assert any("waiting for your approval" in line for line in final_state["reasoning_log"]), (
            final_state["reasoning_log"]
        )

        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ("await_approval",)
        assert len(snapshot.interrupts) == 1
        assert snapshot.interrupts[0].value["case_id"] == case["id"]
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2. Durable pause across a simulated process restart.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_pause_is_durable_across_restart_then_resumable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat has been out since this morning",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages())

    try:
        await run_graph(uuid.UUID(message_id))
        case = await _case_row(db_session, landlord_id=landlord_id)
        draft_id = await _pending_draft_id(db_session, case_id=case["id"])

        # Simulate a process restart exactly like tests/test_agent_graph.py's
        # own restart test: close + reopen the checkpointer pool.
        await close_checkpointer()
        assert cp_mod._pool is None  # noqa: SLF001
        await setup_checkpointer()

        fresh_case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await fresh_case_graph.aget_state(config)
        assert snapshot.next == ("await_approval",)
        assert len(snapshot.interrupts) == 1
        assert snapshot.interrupts[0].value["draft_id"] == str(draft_id)

        # Resumable post-restart via the #44/#45 seam.
        result = await resume_case_thread(
            case_id=uuid.UUID(case["id"]), draft_id=draft_id, resume_value={"action": "approved"}
        )
        assert "__interrupt__" not in result

        final_snapshot = await fresh_case_graph.aget_state(config)
        assert final_snapshot.next == ()
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 3. THE pinned interaction — stale-after-pause: a new inbound while the
#    thread is paused must mark the old draft stale and produce a FRESH
#    pause, without corrupting the thread.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_new_inbound_on_paused_case_marks_old_draft_stale_and_pauses_fresh(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    first_message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat has been out since this morning",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages(body="I'll take a look today."))

    try:
        await run_graph(uuid.UUID(first_message_id))
        case = await _case_row(db_session, landlord_id=landlord_id)
        first_draft_id = await _pending_draft_id(db_session, case_id=case["id"])
        assert case["status"] == "awaiting_approval"

        # A second tenant message lands WHILE the first draft is still
        # awaiting approval -- the exact interaction the #34 spec review
        # flagged as unverified.
        second_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="still no heat, it's getting cold in here",
        )
        _patch_client(
            monkeypatch,
            _happy_path_fake_messages(body="I hear you, sending someone out shortly."),
        )

        second_final_state = await run_graph(uuid.UUID(second_message_id))
        assert "__interrupt__" in second_final_state

        # Still exactly one case -- the second message attached to the
        # SAME case (identify_case's ambiguity rule: one open case).
        case_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert case_count == 1

        case_after = await _case_row(db_session, landlord_id=landlord_id)
        assert case_after["id"] == case["id"]
        assert case_after["status"] == "awaiting_approval"  # still awaiting -- the FRESH draft

        draft_rows = (
            (
                await db_session.execute(
                    text(
                        "SELECT id, status FROM drafts WHERE landlord_id = :lid ORDER BY created_at"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(draft_rows) == 2
        assert uuid.UUID(str(draft_rows[0]["id"])) == first_draft_id
        assert draft_rows[0]["status"] == "stale"  # the OLD draft, marked stale
        assert draft_rows[1]["status"] == "pending"  # the FRESH draft
        second_draft_id = uuid.UUID(str(draft_rows[1]["id"]))
        assert second_draft_id != first_draft_id

        draft_stale_audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid "
                    "AND action = 'draft_stale'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert draft_stale_audit_count == 1

        # The thread is NOT corrupted -- it holds exactly ONE live
        # interrupt, for the FRESH draft.
        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ("await_approval",)
        assert len(snapshot.interrupts) == 1
        assert snapshot.interrupts[0].value["draft_id"] == str(second_draft_id)
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 4. THE other ordering — resume-after-stale: a resume attempt referencing
#    the NOW-STALE draft id must reject safely, leaving the fresh pause
#    completely undisturbed.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resume_after_superseded_by_stale_draft_rejects_safely(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    first_message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat has been out since this morning",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages(body="I'll take a look today."))

    try:
        await run_graph(uuid.UUID(first_message_id))
        case = await _case_row(db_session, landlord_id=landlord_id)
        first_draft_id = await _pending_draft_id(db_session, case_id=case["id"])

        second_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="still no heat, it's getting cold in here",
        )
        _patch_client(
            monkeypatch,
            _happy_path_fake_messages(body="I hear you, sending someone out shortly."),
        )
        await run_graph(uuid.UUID(second_message_id))
        second_draft_id = await _pending_draft_id(db_session, case_id=case["id"])
        assert second_draft_id != first_draft_id

        # A late resume attempt -- e.g. a landlord's approve tap for the
        # FIRST draft that raced against the second tenant message and lost
        # (conversation-model.md: "staleness wins"). Must reject, not
        # resolve the CURRENT (fresh) interrupt with the wrong value.
        with pytest.raises(DraftStaleError) as exc_info:
            await resume_case_thread(
                case_id=uuid.UUID(case["id"]),
                draft_id=first_draft_id,
                resume_value={"action": "approved"},
            )
        assert exc_info.value.fresh_draft_id == second_draft_id
        assert exc_info.value.draft_id == first_draft_id

        # The thread is completely UNDISTURBED -- still exactly one live
        # interrupt, still for the fresh (second) draft.
        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ("await_approval",)
        assert len(snapshot.interrupts) == 1
        assert snapshot.interrupts[0].value["draft_id"] == str(second_draft_id)

        case_after = await _case_row(db_session, landlord_id=landlord_id)
        assert case_after["status"] == "awaiting_approval"

        # The CURRENT (fresh) draft can still be resumed normally
        # afterward -- the rejected attempt left nothing broken.
        result = await resume_case_thread(
            case_id=uuid.UUID(case["id"]),
            draft_id=second_draft_id,
            resume_value={"action": "approved"},
        )
        assert "__interrupt__" not in result
        final_snapshot = await case_graph.aget_state(config)
        assert final_snapshot.next == ()
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 5. Completion-gating (app/agent/graph_entry.py) still behaves for a
#    PAUSED run -- the 'drafted' marker is already written before the
#    pause, so a redelivery of the SAME message is correctly a no-op.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_paused_run_counts_as_complete_for_redelivery_via_enqueue_classification(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat has been out since this morning",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages())

    try:
        await enqueue_classification(uuid.UUID(message_id), uuid.UUID(landlord_id))

        case = await _case_row(db_session, landlord_id=landlord_id)
        assert case["status"] == "awaiting_approval"

        drafted_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid AND action = 'drafted'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert drafted_count == 1

        # Redelivery of the SAME message (e.g. a Twilio retry) -- the
        # completion marker ('drafted') already exists, so this must be a
        # pure no-op: no second run_graph invocation, no second drain, no
        # duplicated draft/case-status churn.
        await enqueue_classification(uuid.UUID(message_id), uuid.UUID(landlord_id))

        draft_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert draft_count == 1  # still exactly one -- no re-run happened

        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ("await_approval",)
        assert len(snapshot.interrupts) == 1  # the SAME pause, not a fresh one
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 6. The EMERGENCY/degraded_mode interim edge must fire independently of
#    (never trapped behind) this pause.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_llm_emergency_bypasses_the_pause_never_traps_needs_eyes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    # Deliberately NOT a Tier-0 HARD-hit phrase -- prefilter defaults to
    # hard_hit=False, exactly the "Tier-0 missed it, the model caught it"
    # scenario (mirrors tests/test_agent_graph.py's own EMERGENCY test).
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="my elderly mother lives here alone and hasn't been able to reach anyone for days",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages(severity="EMERGENCY"))

    try:
        final_state = await run_graph(uuid.UUID(message_id))
        assert "__interrupt__" not in final_state  # never paused -- degraded_mode -> END

        case = await _case_row(db_session, landlord_id=landlord_id)
        assert case["status"] != "awaiting_approval"  # await_approval never ran

        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ()  # thread ran to completion, nothing paused
        assert snapshot.interrupts == ()

        notif_row = (
            (
                await db_session.execute(
                    text("SELECT type, status FROM notifications WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif_row["type"] == "needs_eyes"
        assert notif_row["status"] == "pending"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_draft_guard_failed_bypasses_the_pause(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="can you knock 50 dollars off my rent for the noise",
    )
    unsafe_body = "Sure, I'll knock $50 off your rent this month for the trouble."
    fake_messages = _FakeMessages(
        responses=[
            _intent_response(),
            _severity_response(severity="ROUTINE"),
            _draft_response_message(body=unsafe_body),
            _draft_response_message(body=unsafe_body),
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        final_state = await run_graph(uuid.UUID(message_id))
        assert "__interrupt__" not in final_state
        assert final_state.get("draft_guard_failed") is True

        case = await _case_row(db_session, landlord_id=landlord_id)
        assert case["status"] != "awaiting_approval"

        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ()
        assert snapshot.interrupts == ()
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 7. resume_case_thread's own defensive checks (unit-adjacent, but needs
#    the real graph/checkpointer to exercise honestly).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resume_case_thread_rejects_when_no_pending_draft_at_all(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat has been out since this morning",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages())

    try:
        await run_graph(uuid.UUID(message_id))
        case = await _case_row(db_session, landlord_id=landlord_id)

        bogus_draft_id = uuid.uuid4()
        with pytest.raises(DraftStaleError) as exc_info:
            await resume_case_thread(
                case_id=uuid.UUID(case["id"]),
                draft_id=bogus_draft_id,
                resume_value={"action": "approved"},
            )
        real_draft_id = await _pending_draft_id(db_session, case_id=case["id"])
        assert exc_info.value.fresh_draft_id == real_draft_id
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_resume_case_thread_rejects_when_draft_pending_but_never_paused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discovered gap documented on ``CaseNotAwaitingApprovalError``:
    the EMERGENCY/``draft_guard_failed`` exit still inserts a ``pending``
    draft, but the thread is never actually paused behind an interrupt
    (routes to ``degraded_mode`` instead) -- ``resume_case_thread`` must
    reject this distinctly from a stale draft, never silently no-op."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="my elderly mother lives here alone and hasn't been able to reach anyone for days",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages(severity="EMERGENCY"))

    try:
        await run_graph(uuid.UUID(message_id))
        case = await _case_row(db_session, landlord_id=landlord_id)
        draft_id = await _pending_draft_id(db_session, case_id=case["id"])

        with pytest.raises(CaseNotAwaitingApprovalError):
            await resume_case_thread(
                case_id=uuid.UUID(case["id"]),
                draft_id=draft_id,
                resume_value={"action": "approved"},
            )
    finally:
        await _cleanup(db_session, landlord_id)
