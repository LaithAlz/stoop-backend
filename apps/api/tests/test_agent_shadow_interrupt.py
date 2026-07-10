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

Review-round additions (this issue's own safety/spec review, second pass)
------------------------------------------------------------------------
- **Concurrency section** (below): the sequential tests above prove
  correctness under STRICT ordering, but not under real concurrency. These
  tests launch genuinely concurrent ``asyncio`` tasks — a double-resume
  race and a resume racing a fresh ``run_graph`` re-run — and verify
  ``app/agent/graph.py``'s per-case ``pg_advisory_xact_lock`` (module
  docstring "Per-case serialization") actually serializes them.
- ``test_crash_between_draft_response_and_mark_awaiting_approval_heals_on_redelivery``
  — the crash-window coherence fix in ``app/agent/graph_entry.py``.
- ``test_unknown_sender_never_pauses_at_interrupt`` — the corrected (no
  longer ``# pragma: no cover``) ``case_id is None`` path in
  ``app/agent/nodes/await_approval.py``.
- ``test_await_approval_skips_the_pause_when_no_pending_draft_exists`` —
  the ``draft_id is None`` defensive skip in the same module.
"""

from __future__ import annotations

import asyncio
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
import app.agent.graph as graph_mod
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


# ---------------------------------------------------------------------------
# 8. Crash-window coherence (app/agent/graph_entry.py's completion gate).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_crash_between_draft_response_and_mark_awaiting_approval_heals_on_redelivery(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``draft_response`` commits (draft row + 'drafted' audit marker)
    BEFORE ``mark_awaiting_approval`` ever runs -- a crash in that exact
    window must not leave the case stuck 'open' (auto-stale ELIGIBLE, per
    app/agent/case_lifecycle.py) with an orphaned pending draft and no way
    to ever re-run (the OLD completion gate would have skipped every
    future redelivery of this exact message forever)."""
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

    original_mark_awaiting_approval = graph_mod.mark_awaiting_approval

    async def _boom(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("simulated crash between draft_response and mark_awaiting_approval")

    monkeypatch.setattr(graph_mod, "mark_awaiting_approval", _boom)

    try:
        # First attempt: draft_response commits, then the simulated crash.
        await enqueue_classification(uuid.UUID(message_id), uuid.UUID(landlord_id))

        case = await _case_row(db_session, landlord_id=landlord_id)
        assert case["status"] == "open"  # never transitioned -- the crash window

        drafted_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid AND action = 'drafted'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert drafted_count == 1

        pending_draft_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid AND status = 'pending'"),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert pending_draft_count == 1  # orphaned -- draft exists, case never advanced

        # "Process restarts" -- mark_awaiting_approval works again.
        monkeypatch.setattr(graph_mod, "mark_awaiting_approval", original_mark_awaiting_approval)
        _patch_client(monkeypatch, _happy_path_fake_messages(body="second attempt draft"))

        # Redelivery of the SAME message (e.g. a Twilio retry) -- must NOT
        # be skipped by the completion gate; must self-heal.
        await enqueue_classification(uuid.UUID(message_id), uuid.UUID(landlord_id))

        case_after = await _case_row(db_session, landlord_id=landlord_id)
        assert case_after["status"] == "awaiting_approval"

        draft_rows = (
            (
                await db_session.execute(
                    text("SELECT status FROM drafts WHERE landlord_id = :lid ORDER BY created_at"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(draft_rows) == 2
        assert draft_rows[0]["status"] == "stale"  # the orphaned first draft, now superseded
        assert draft_rows[1]["status"] == "pending"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_second_message_crash_on_already_awaiting_approval_case_heals_on_redelivery(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-2 reproduction (safety review): the round-1 completion-gate
    fix inferred "this message's run completed" from the case's CURRENT
    status being non-'open' -- which can be true for a reason completely
    UNRELATED to the message being checked. Here M1 completes normally
    FIRST (case already 'awaiting_approval', D1 pending) -- THEN M2
    arrives and crashes between ITS OWN draft_response (D1 staled, D2
    pending, 'drafted' marker written for M2) and ITS OWN
    mark_awaiting_approval. The case's status is 'awaiting_approval' the
    whole time (leftover from M1), so a status-only check would wrongly
    call M2 already complete -- every redelivery of M2 would be a silent
    no-op forever, and D2 would be permanently unapprovable
    (``resume_case_thread`` for D2 would raise
    ``CaseNotAwaitingApprovalError``, since the thread genuinely has no
    live interrupt: it's stuck at ``next=('mark_awaiting_approval',)``).
    The fix keys completion to the THREAD's own checkpoint state instead
    -- proves the fix, not just the round-1 scenario it was already known
    to handle."""
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
        # M1 completes fully and normally -- case awaiting_approval, D1 pending.
        await run_graph(uuid.UUID(first_message_id))
        case = await _case_row(db_session, landlord_id=landlord_id)
        assert case["status"] == "awaiting_approval"
        first_draft_id = await _pending_draft_id(db_session, case_id=case["id"])

        # M2 arrives on the SAME (already awaiting_approval) case.
        second_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="still no heat, it's getting cold in here",
        )
        _patch_client(
            monkeypatch, _happy_path_fake_messages(body="I hear you, sending someone out.")
        )

        original_mark_awaiting_approval = graph_mod.mark_awaiting_approval

        async def _boom(state: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError(
                "simulated crash between M2's draft_response and mark_awaiting_approval"
            )

        monkeypatch.setattr(graph_mod, "mark_awaiting_approval", _boom)

        # M2's first attempt: its OWN draft_response commits (D1 staled, D2
        # pending, 'drafted' marker for M2), then the simulated crash.
        await enqueue_classification(uuid.UUID(second_message_id), uuid.UUID(landlord_id))

        case_mid = await _case_row(db_session, landlord_id=landlord_id)
        # Status is 'awaiting_approval' -- but LEFTOVER from M1, not proof
        # M2's own mark_awaiting_approval ran.
        assert case_mid["status"] == "awaiting_approval"

        second_drafted_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid "
                    "AND action = 'drafted' AND payload ->> 'message_id' = :mid"
                ),
                {"lid": landlord_id, "mid": second_message_id},
            )
        ).scalar_one()
        assert second_drafted_count == 1  # M2's own drafted marker exists

        second_draft_id = await _pending_draft_id(db_session, case_id=case["id"])
        assert second_draft_id != first_draft_id  # D2, not D1 -- D1 is now stale

        # Proves the bug WOULD be live without the fix: the thread has no
        # live interrupt right now (stuck at next=('mark_awaiting_approval',)),
        # so a resume attempt for D2 correctly finds nothing to resume --
        # if redelivery were (wrongly) skipped forever, D2 would be stuck
        # exactly like this permanently.
        with pytest.raises(CaseNotAwaitingApprovalError):
            await resume_case_thread(
                case_id=uuid.UUID(case["id"]),
                draft_id=second_draft_id,
                resume_value={"action": "approved"},
            )

        # "Process restarts" -- mark_awaiting_approval works again.
        monkeypatch.setattr(graph_mod, "mark_awaiting_approval", original_mark_awaiting_approval)
        _patch_client(monkeypatch, _happy_path_fake_messages(body="third attempt draft"))

        # Redelivery of M2 -- must NOT be silently skipped just because the
        # case already looks non-'open' (that's leftover from M1); must
        # self-heal.
        await enqueue_classification(uuid.UUID(second_message_id), uuid.UUID(landlord_id))

        case_after = await _case_row(db_session, landlord_id=landlord_id)
        assert case_after["status"] == "awaiting_approval"  # now genuinely from M2's own run

        draft_rows = (
            (
                await db_session.execute(
                    text("SELECT status FROM drafts WHERE landlord_id = :lid ORDER BY created_at"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        # D1 (stale from M1->M2), D2 (stale -- superseded by the healed
        # re-run's own stale-then-insert), D3 (pending -- the healed draft).
        assert len(draft_rows) == 3
        assert draft_rows[0]["status"] == "stale"
        assert draft_rows[1]["status"] == "stale"
        assert draft_rows[2]["status"] == "pending"

        third_draft_id = await _pending_draft_id(db_session, case_id=case["id"])
        assert third_draft_id not in (first_draft_id, second_draft_id)

        # The case is genuinely approvable again.
        result = await resume_case_thread(
            case_id=uuid.UUID(case["id"]),
            draft_id=third_draft_id,
            resume_value={"action": "approved"},
        )
        assert "__interrupt__" not in result
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 9. case_id is None is a REAL reachable path (unknown sender), not just a
#    defensive invariant -- the graph must end unpaused.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unknown_sender_never_pauses_at_interrupt(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=None,
        body="hi, is this the right number for 41 Palmerston?",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages(severity="ROUTINE"))

    try:
        final_state = await run_graph(uuid.UUID(message_id))

        assert "__interrupt__" not in final_state
        assert final_state.get("draft") is None  # draft_response returned early -- no case_id

        case_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert case_count == 0

        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": f"message:{message_id}"}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ()
        assert snapshot.interrupts == ()
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 10. draft_id is None at pause time -- defensive skip, never a stuck
#     unapprovable interrupt.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_await_approval_skips_the_pause_when_no_pending_draft_exists(
    db_session: AsyncSession,
) -> None:
    from app.agent.nodes.await_approval import await_approval
    from app.agent.schemas import CaseContext

    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await _insert_bare_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    try:
        state = {
            "message_id": uuid.uuid4(),
            "case_context": CaseContext(
                case_id=uuid.UUID(case_id), landlord_id=uuid.UUID(landlord_id)
            ),
            "reasoning_log": [],
        }
        # Called directly (no compiled graph, no interrupt() context) --
        # if this incorrectly called interrupt() it would raise a
        # LangGraph-internal error here; instead it must return cleanly.
        result = await await_approval(state)  # type: ignore[arg-type]

        assert result.get("reasoning_log")
        assert "couldn't find a reply" in result["reasoning_log"][-1]

        case_row = (
            (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert case_row["status"] == "open"  # mark_awaiting_approval never ran either
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Concurrency — genuine concurrent asyncio tasks, not just sequential calls.
# Proves app/agent/graph.py's per-case pg_advisory_xact_lock actually
# serializes resume_case_thread against itself and against a concurrent
# run_graph re-run (safety review MERGE-BLOCKING, this issue's own review
# round).
# ---------------------------------------------------------------------------


class _DelayedFakeMessages(_FakeMessages):
    """Same fake Anthropic client, but each ``create()`` call sleeps first
    (an injected delay, widening the window a concurrent task can observe
    the lock as held) and signals *started_event* the first time it's
    called -- lets a test wait until it KNOWS the lock-holding call is
    underway before starting a competing concurrent task, rather than
    guessing with a bare sleep."""

    def __init__(
        self, *, responses: list[Any], delay_seconds: float, started_event: asyncio.Event
    ) -> None:
        super().__init__(responses=responses)
        self._delay_seconds = delay_seconds
        self._started_event = started_event

    async def create(self, **kwargs: Any) -> Any:
        if not self._started_event.is_set():
            self._started_event.set()
        await asyncio.sleep(self._delay_seconds)
        return await super().create(**kwargs)


async def _insert_bare_case(
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


@pytest.mark.integration
async def test_concurrent_double_resume_exactly_one_proceeds(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two genuinely concurrent ``resume_case_thread`` calls for the SAME
    case + draft_id + resume_value -- the per-case advisory lock must
    serialize them so exactly one actually resumes the thread; the other
    must observe (after waiting for the lock) that there is no longer a
    live interrupt to resume."""
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

        results = await asyncio.gather(
            resume_case_thread(
                case_id=uuid.UUID(case["id"]),
                draft_id=draft_id,
                resume_value={"action": "approved"},
            ),
            resume_case_thread(
                case_id=uuid.UUID(case["id"]),
                draft_id=draft_id,
                resume_value={"action": "approved"},
            ),
            return_exceptions=True,
        )

        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(successes) == 1, results
        assert len(failures) == 1, results
        assert isinstance(failures[0], CaseNotAwaitingApprovalError), failures[0]

        # The thread genuinely only resumed once -- fully completed, no
        # interrupt left over.
        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ()
        assert snapshot.interrupts == ()
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_concurrent_resume_racing_new_inbound_rerun_staleness_wins(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landlord's approve tap (``resume_case_thread``, referencing the
    OLD draft) races a tenant's new message (``run_graph``, which stales
    that OLD draft and produces a fresh one) -- under the per-case lock,
    staleness must win regardless of which one was "logically" initiated
    first: the resume's own staleness check happens INSIDE the lock,
    immediately before it would resume anything, so it always sees the
    fully-committed truth, never a torn/interleaved read."""
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

        started_event = asyncio.Event()
        delayed_fake = _DelayedFakeMessages(
            responses=[
                _intent_response(),
                _severity_response(),
                _draft_response_message(body="I hear you, sending someone out shortly."),
            ],
            delay_seconds=0.4,
            started_event=started_event,
        )
        _patch_client(monkeypatch, delayed_fake)

        # run_graph acquires the per-case lock BEFORE its first (delayed)
        # LLM call -- waiting for started_event guarantees the lock is
        # already held by the time we launch the competing resume below.
        run_graph_task = asyncio.create_task(run_graph(uuid.UUID(second_message_id)))
        await asyncio.wait_for(started_event.wait(), timeout=5)

        resume_task = asyncio.create_task(
            resume_case_thread(
                case_id=uuid.UUID(case["id"]),
                draft_id=first_draft_id,
                resume_value={"action": "approved"},
            )
        )

        with pytest.raises(DraftStaleError) as exc_info:
            await resume_task

        await run_graph_task  # let the re-run finish cleanly

        fresh_draft_id = await _pending_draft_id(db_session, case_id=case["id"])
        assert fresh_draft_id != first_draft_id
        assert exc_info.value.fresh_draft_id == fresh_draft_id
        assert exc_info.value.draft_id == first_draft_id

        # The thread ends up correctly paused on the FRESH draft -- the
        # rejected resume attempt left nothing corrupted.
        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ("await_approval",)
        assert len(snapshot.interrupts) == 1
        assert snapshot.interrupts[0].value["draft_id"] == str(fresh_draft_id)
    finally:
        await _cleanup(db_session, landlord_id)
