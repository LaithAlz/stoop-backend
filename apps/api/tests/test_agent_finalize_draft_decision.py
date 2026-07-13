"""Integration tests for #44/#45 — ``finalize_approval``/``finalize_rejection``
(``app/agent/nodes/finalize_draft_decision.py``) and the
``resolve_draft_decision`` entry point (``app/agent/graph.py``).

Marker: ``integration`` — requires a running Postgres instance + ``alembic
upgrade head``. Mirrors ``tests/test_agent_shadow_interrupt.py``'s harness
exactly (same fake-Anthropic-client machinery, same ``_cleanup``/
``_case_row``/``_pending_draft_id`` helpers) since this module picks up
right where that one leaves off: resuming a paused ``await_approval``
interrupt with a REAL action this time, instead of an opaque placeholder.

Two entry surfaces exercised throughout:
- ``resolve_draft_decision`` — the ONE public #44/#45 seam
  ``routers/drafts.py`` calls.
- Seeding a case directly at a non-``awaiting_approval`` status (via
  ``tests/factories.py``'s ``insert_case``/``insert_draft``) to exercise
  the "degraded-path draft, never paused" fallback WITHOUT needing to
  reproduce the full EMERGENCY/``draft_guard_failed`` graph route — the
  fallback's own logic only cares that ``cases.status != 'awaiting_approval'``
  while a ``'pending'`` draft exists, which is exactly what
  ``draft_response``'s degraded-mode exit ALSO produces (see
  ``app/agent/graph.py``'s module docstring "The #44-pinned open design
  question, decided").
"""

from __future__ import annotations

import asyncio
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
from langgraph.graph import END
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.agent.graph import (
    DraftStaleError,
    _route_after_await_approval,
    compile_case_graph,
    resolve_draft_decision,
    resume_case_thread,
    run_graph,
)
from app.agent.nodes.await_approval import await_approval
from app.agent.nodes.finalize_draft_decision import (
    ACTION_APPROVE,
    ACTION_EDIT_AND_SEND,
    ACTION_REJECT,
)
from app.agent.schemas import CaseContext
from app.integrations import anthropic as anthropic_mod
from tests import factories
from tests.test_agent_shadow_interrupt import _insert_bare_case

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
# Local helpers — mirrors tests/test_agent_shadow_interrupt.py's own section.
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


async def _case_row(session: AsyncSession, *, case_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text("SELECT id, status, langgraph_thread_id FROM cases WHERE id = :cid"),
                {"cid": case_id},
            )
        )
        .mappings()
        .one()
    )
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "langgraph_thread_id": str(row["langgraph_thread_id"]),
    }


async def _draft_row(session: AsyncSession, *, draft_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    "SELECT id, status, scheduled_send_at, edited, final_body, body "
                    "FROM drafts WHERE id = :did"
                ),
                {"did": draft_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


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
    *, body: str = "I'll take a look today.", severity: str = "URGENT"
) -> _FakeMessages:
    return _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "intent": "maintenance",
                    "is_new_issue": True,
                    "summary": "Heat is out",
                },
                tool_name="classify_intent",
            ),
            _fake_message(
                tool_input={
                    "severity": severity,
                    "rules_fired": ["No heat, mild weather"],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["Tenant reports no heat."],
                },
                tool_name="classify_severity",
            ),
            _fake_message(
                tool_input={"body": body, "refusal_templates_used": []}, tool_name="draft_message"
            ),
        ]
    )


async def _seed_paused_case(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str = "I'll take a look today.",
) -> tuple[str, str, str]:
    """Runs the real graph to a genuine paused ``await_approval`` interrupt.
    Returns ``(landlord_id, case_id, draft_id)``."""
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
    _patch_client(monkeypatch, _happy_path_fake_messages(body=body))
    await run_graph(uuid.UUID(message_id))
    case = await _case_row(db_session, case_id=(await _find_case_id(db_session, landlord_id)))
    draft_id = str(await _pending_draft_id(db_session, case_id=case["id"]))
    return landlord_id, case["id"], draft_id


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


# ---------------------------------------------------------------------------
# 1. Normal path — approve.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_approve_schedules_send_and_writes_approved_audit_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id, case_id, draft_id = await _seed_paused_case(db_session, monkeypatch)
    try:
        before = datetime.now(UTC)
        await resolve_draft_decision(
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            resume_value={"action": ACTION_APPROVE},
        )

        draft = await _draft_row(db_session, draft_id=draft_id)
        assert draft["status"] == "approved"
        assert draft["edited"] is False
        assert draft["final_body"] is None
        assert draft["scheduled_send_at"] is not None
        assert (
            before + timedelta(seconds=4)
            <= draft["scheduled_send_at"]
            <= before + timedelta(seconds=6)
        )

        case = await _case_row(db_session, case_id=case_id)
        assert case["status"] == "awaiting_approval"  # unchanged by approve itself

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "approved" in actions

        # The thread fully resumed — no live interrupt left.
        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.next == ()
        assert snapshot.interrupts == ()
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2. Normal path — edit-and-send.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_edit_and_send_retains_original_and_records_edited_audit_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_body = "I'll take a look today."
    edited_body = "I'm heading over this afternoon to check the heat."
    landlord_id, case_id, draft_id = await _seed_paused_case(
        db_session, monkeypatch, body=original_body
    )
    try:
        await resolve_draft_decision(
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            resume_value={"action": ACTION_EDIT_AND_SEND, "body": edited_body},
        )

        draft = await _draft_row(db_session, draft_id=draft_id)
        assert draft["status"] == "approved"
        assert draft["edited"] is True
        assert draft["final_body"] == edited_body
        # "original + edit both retained" (#45 AC) -- the original body column
        # is NEVER touched by an edit.
        assert draft["body"] == original_body
        assert draft["scheduled_send_at"] is not None

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "edited" in actions
        assert "approved" not in actions
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 3. Normal path — reject.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reject_archives_draft_and_reopens_case(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id, case_id, draft_id = await _seed_paused_case(db_session, monkeypatch)
    try:
        await resolve_draft_decision(
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            resume_value={"action": ACTION_REJECT, "note": "not needed, tenant called"},
        )

        draft = await _draft_row(db_session, draft_id=draft_id)
        assert draft["status"] == "rejected"
        assert draft["scheduled_send_at"] is None

        case = await _case_row(db_session, case_id=case_id)
        assert case["status"] == "open"  # AC: "draft archived, case stays open"

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "rejected" in actions
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 4. Drain-sentinel-class / unrecognized resume values never send.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unrecognized_resume_action_routes_to_end_never_mutates_draft(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id, case_id, draft_id = await _seed_paused_case(db_session, monkeypatch)
    try:
        result = await resume_case_thread(
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            resume_value={"action": "some-unrecognized-sentinel"},
        )
        assert "__interrupt__" not in result  # resumed cleanly to END

        draft = await _draft_row(db_session, draft_id=draft_id)
        assert draft["status"] == "pending"  # untouched
        assert draft["scheduled_send_at"] is None

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "approved" not in actions
        assert "rejected" not in actions
        assert "edited" not in actions
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 5. The #44-pinned open design question — degraded-path drafts (never
#    paused) ARE approvable/rejectable via the SAME resolve_draft_decision
#    seam, via the sanctioned second (non-graph) path.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_degraded_path_pending_draft_is_approvable_via_fallback(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    # cases.status == 'open' -- exactly what the EMERGENCY/draft_guard_failed
    # degraded-mode exit leaves behind (never 'awaiting_approval').
    case_id = await factories.insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="open",
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)

    try:
        before = datetime.now(UTC)
        await resolve_draft_decision(
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            resume_value={"action": ACTION_APPROVE},
        )

        draft = await _draft_row(db_session, draft_id=draft_id)
        assert draft["status"] == "approved"
        assert draft["scheduled_send_at"] is not None
        assert before <= draft["scheduled_send_at"]

        case = await _case_row(db_session, case_id=case_id)
        # The fallback performs the just-in-time mark_awaiting_approval
        # equivalent -- consistent with the normal path's own end state.
        assert case["status"] == "awaiting_approval"

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "approved" in actions
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_degraded_path_pending_draft_is_rejectable_via_fallback(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="open",
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)

    try:
        await resolve_draft_decision(
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            resume_value={"action": ACTION_REJECT, "note": None},
        )

        draft = await _draft_row(db_session, draft_id=draft_id)
        assert draft["status"] == "rejected"

        case = await _case_row(db_session, case_id=case_id)
        assert case["status"] == "open"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_degraded_path_stale_draft_raises_draft_stale_error(db_session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="open",
    )
    stale_draft_id = await factories.insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, status="stale"
    )
    fresh_draft_id = await factories.insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id
    )

    try:
        with pytest.raises(DraftStaleError) as exc_info:
            await resolve_draft_decision(
                case_id=uuid.UUID(case_id),
                draft_id=uuid.UUID(stale_draft_id),
                resume_value={"action": ACTION_APPROVE},
            )
        assert str(exc_info.value.fresh_draft_id) == fresh_draft_id
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 6. Concurrency — genuinely concurrent resolve_draft_decision calls.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_double_approve_exactly_one_succeeds(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Now that approve has a REAL side effect (unlike the #43-era opaque
    placeholder), a genuine double-approve race manifests as the SECOND
    caller observing the draft is no longer pending — DraftStaleError,
    fresh_draft_id=None (there's no NEWER draft, the same one was simply
    already actioned by the winner) — reconciled to an idempotent 200 at
    the router layer (tests/test_drafts_router.py), never a double send."""
    landlord_id, case_id, draft_id = await _seed_paused_case(db_session, monkeypatch)
    try:
        results = await asyncio.gather(
            resolve_draft_decision(
                case_id=uuid.UUID(case_id),
                draft_id=uuid.UUID(draft_id),
                resume_value={"action": ACTION_APPROVE},
            ),
            resolve_draft_decision(
                case_id=uuid.UUID(case_id),
                draft_id=uuid.UUID(draft_id),
                resume_value={"action": ACTION_APPROVE},
            ),
            return_exceptions=True,
        )

        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(successes) == 1, results
        assert len(failures) == 1, results
        assert isinstance(failures[0], DraftStaleError), failures[0]
        assert failures[0].fresh_draft_id is None

        draft = await _draft_row(db_session, draft_id=draft_id)
        assert draft["status"] == "approved"  # exactly one write happened

        actions = await _audit_actions(db_session, case_id=case_id)
        assert actions.count("approved") == 1  # never double-recorded
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_concurrent_double_approve_on_degraded_path_exactly_one_succeeds(
    db_session: AsyncSession,
) -> None:
    """Same race, but for the non-graph fallback path — proves
    ``_finalize_never_paused_draft``'s own lock+staleness re-check
    serializes identically to the normal path's."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="open",
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)

    try:
        results = await asyncio.gather(
            resolve_draft_decision(
                case_id=uuid.UUID(case_id),
                draft_id=uuid.UUID(draft_id),
                resume_value={"action": ACTION_APPROVE},
            ),
            resolve_draft_decision(
                case_id=uuid.UUID(case_id),
                draft_id=uuid.UUID(draft_id),
                resume_value={"action": ACTION_APPROVE},
            ),
            return_exceptions=True,
        )

        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(successes) == 1, results
        assert len(failures) == 1, results
        assert isinstance(failures[0], DraftStaleError), failures[0]

        draft = await _draft_row(db_session, draft_id=draft_id)
        assert draft["status"] == "approved"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_concurrent_approve_racing_new_inbound_staleness_wins(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """approve-vs-new-inbound (conversation-model.md's own edge case),
    barrier-forced with a genuinely concurrent asyncio task — mirrors
    ``tests/test_agent_shadow_interrupt.py``'s own
    ``test_concurrent_resume_racing_new_inbound_rerun_staleness_wins``
    pattern, now exercised through ``resolve_draft_decision`` with a REAL
    approve action."""
    from tests.test_agent_shadow_interrupt import _DelayedFakeMessages

    landlord_id, case_id, first_draft_id = await _seed_paused_case(db_session, monkeypatch)
    try:
        second_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=(
                await db_session.execute(
                    text("SELECT property_id FROM cases WHERE id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()["property_id"],
            tenant_id=(
                await db_session.execute(
                    text("SELECT tenant_id FROM cases WHERE id = :cid"), {"cid": case_id}
                )
            )
            .mappings()
            .one()["tenant_id"],
            body="still no heat, it's getting cold in here",
        )

        started_event = asyncio.Event()
        delayed_fake = _DelayedFakeMessages(
            responses=[
                _fake_message(
                    tool_input={
                        "intent": "maintenance",
                        "is_new_issue": True,
                        "summary": "Heat is out",
                    },
                    tool_name="classify_intent",
                ),
                _fake_message(
                    tool_input={
                        "severity": "URGENT",
                        "rules_fired": ["No heat, mild weather"],
                        "modifier": None,
                        "refusal_flags": [],
                        "reasoning": ["Tenant reports no heat."],
                    },
                    tool_name="classify_severity",
                ),
                _fake_message(
                    tool_input={
                        "body": "I hear you, sending someone out shortly.",
                        "refusal_templates_used": [],
                    },
                    tool_name="draft_message",
                ),
            ],
            delay_seconds=0.4,
            started_event=started_event,
        )
        _patch_client(monkeypatch, delayed_fake)

        run_graph_task = asyncio.create_task(run_graph(uuid.UUID(second_message_id)))
        await asyncio.wait_for(started_event.wait(), timeout=5)

        approve_task = asyncio.create_task(
            resolve_draft_decision(
                case_id=uuid.UUID(case_id),
                draft_id=uuid.UUID(first_draft_id),
                resume_value={"action": ACTION_APPROVE},
            )
        )

        with pytest.raises(DraftStaleError) as exc_info:
            await approve_task

        await run_graph_task

        fresh_draft_id = await _pending_draft_id(db_session, case_id=case_id)
        assert str(fresh_draft_id) != first_draft_id
        assert str(exc_info.value.fresh_draft_id) == str(fresh_draft_id)

        first_draft = await _draft_row(db_session, draft_id=first_draft_id)
        assert first_draft["status"] == "stale"  # never approved -- staleness won
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 7. THE TRAP (safety review, MEDIUM) — a case parked at 'awaiting_approval'
#    by an EARLIER message, whose live interrupt was silently drained by a
#    LATER message's own degraded-mode exit. Must still be approvable
#    through the normal public seam, never a permanent 409 draft_stale
#    loop where fresh_draft_id == draft_id.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_trap_awaiting_approval_case_drained_interrupt_still_approvable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """msg1 pauses normally (case -> 'awaiting_approval', D1 pending, live
    interrupt). msg2 lands on the SAME case and classifies EMERGENCY --
    draft_response's OWN stale-then-insert still stales D1 and inserts a
    fresh D2 (unconditional), but ``_route_after_draft_response`` routes
    straight to ``degraded_mode -> END``, draining the live interrupt
    WITHOUT ``mark_awaiting_approval`` ever running again. Net effect:
    ``cases.status`` is STILL 'awaiting_approval' (msg1's own write), D2 is
    'pending', and the thread has NO live interrupt -- exactly
    ``CaseNotAwaitingApprovalError``'s cause 3. Approving D2 through the
    public seam must succeed, not loop on 409 draft_stale with
    fresh_draft_id == draft_id forever."""
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
    _patch_client(monkeypatch, _happy_path_fake_messages())
    try:
        await run_graph(uuid.UUID(first_message_id))
        case_id = await _find_case_id(db_session, landlord_id)
        case = await _case_row(db_session, case_id=case_id)
        assert case["status"] == "awaiting_approval"
        first_draft_id = await _pending_draft_id(db_session, case_id=case_id)

        second_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="actually there's smoke and I smell gas, please help",
        )
        _patch_client(monkeypatch, _happy_path_fake_messages(severity="EMERGENCY"))
        second_final_state = await run_graph(uuid.UUID(second_message_id))
        assert "__interrupt__" not in second_final_state  # drained -- degraded_mode -> END

        case_after = await _case_row(db_session, case_id=case_id)
        assert case_after["status"] == "awaiting_approval"  # STILL -- the trap

        second_draft_id = await _pending_draft_id(db_session, case_id=case_id)
        assert second_draft_id != first_draft_id

        first_draft = await _draft_row(db_session, draft_id=str(first_draft_id))
        assert first_draft["status"] == "stale"

        # The thread genuinely has no live interrupt.
        case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": case_after["langgraph_thread_id"]}}
        snapshot = await case_graph.aget_state(config)
        assert snapshot.interrupts == ()

        # Approving the fresh (D2) draft through the SAME public seam must
        # succeed -- not an infinite "fresh_draft_id == draft_id" 409 loop.
        await resolve_draft_decision(
            case_id=uuid.UUID(case_id),
            draft_id=second_draft_id,
            resume_value={"action": ACTION_APPROVE},
        )

        draft = await _draft_row(db_session, draft_id=str(second_draft_id))
        assert draft["status"] == "approved"
        assert draft["scheduled_send_at"] is not None

        actions = await _audit_actions(db_session, case_id=case_id)
        assert "approved" in actions
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_trap_awaiting_approval_case_drained_interrupt_still_rejectable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same trap, reject instead of approve -- also must fall through to
    the non-graph fallback rather than 409 forever."""
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
    _patch_client(monkeypatch, _happy_path_fake_messages())
    try:
        await run_graph(uuid.UUID(first_message_id))
        case_id = await _find_case_id(db_session, landlord_id)

        second_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="actually there's smoke and I smell gas, please help",
        )
        _patch_client(monkeypatch, _happy_path_fake_messages(severity="EMERGENCY"))
        await run_graph(uuid.UUID(second_message_id))

        case_after = await _case_row(db_session, case_id=case_id)
        assert case_after["status"] == "awaiting_approval"
        second_draft_id = await _pending_draft_id(db_session, case_id=case_id)

        await resolve_draft_decision(
            case_id=uuid.UUID(case_id),
            draft_id=second_draft_id,
            resume_value={"action": ACTION_REJECT, "note": None},
        )

        draft = await _draft_row(db_session, draft_id=str(second_draft_id))
        assert draft["status"] == "rejected"

        case_final = await _case_row(db_session, case_id=case_id)
        assert case_final["status"] == "open"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 8. Hardening (safety review, LOW/MEDIUM) — a stale `approval_resume` value
#    persisted in an earlier resume's checkpoint must never leak into a
#    LATER skip-the-pause pass on the same thread.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_await_approval_skip_branch_clears_stale_approval_resume(
    db_session: AsyncSession,
) -> None:
    """A thread that already resumed once carries `approval_resume`
    forward in its checkpoint (LangGraph's last-write-wins merge never
    clears a key nothing explicitly overwrites). If a LATER invocation on
    the SAME thread reaches `await_approval`'s skip-the-pause branch (no
    pending draft), it must not silently dispatch on that stale value.
    Exercises the node directly (same pattern as #43's own
    ``test_await_approval_skips_the_pause_when_no_pending_draft_exists``),
    with a stale `approval_resume` already present in the input state."""
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
            # Simulates a STALE value carried forward in the checkpoint
            # from an earlier, genuine resume on this same thread.
            "approval_resume": {"action": ACTION_APPROVE},
        }
        result = await await_approval(state)  # type: ignore[arg-type]

        # The skip branch (no pending draft) must explicitly clear it.
        assert result.get("approval_resume") is None

        # And the router must therefore never dispatch to a finalize node.
        merged_state = {**state, **result}
        assert _route_after_await_approval(merged_state) == END
    finally:
        await _cleanup(db_session, landlord_id)
