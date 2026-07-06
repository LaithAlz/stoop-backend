"""Integration tests for ``app/agent/graph.py`` (#34) — the wired
StateGraph + Postgres checkpointer.

Marker: ``integration`` — requires a running Postgres instance (docker
-compose) + ``alembic upgrade head``. The Anthropic SDK itself is ALWAYS
mocked (``app.integrations.anthropic.get_client`` monkeypatched) — no real
API calls anywhere in this suite. Seeding helpers come from
``tests/factories.py`` (senior review: shared factories, not re-duplicated
in every new test module); the fake-Anthropic-client machinery and
``_cleanup``/``_case_thread_id`` stay local (not part of that extraction).

Run with:
    export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop
    uv run pytest tests/test_agent_graph.py -m integration -v

Proves the #34 acceptance criteria end to end:
1. One inbound message, run through the full graph, produces: the
   classified + drafted ``audit_log`` rows, a ``pending`` draft row, and
   checkpoint rows in the ``langgraph`` schema.
2. Resume-from-checkpoint works after a simulated process restart
   (close + reopen the checkpointer pool, re-instantiate the compiled
   graph, read the SAME thread's state back).
3. ``classification_failed`` (double garbage response) routes to
   ``degraded_mode`` instead of ``draft_response`` — a ``needs_eyes``
   notification + ``degraded_mode`` audit row are written, never a silent
   dead end.
4. ``draft_guard_failed`` ALSO routes to ``degraded_mode`` — in addition
   to (not instead of) the draft ``draft_response`` already inserted.
5. An LLM-classified EMERGENCY severity (a Tier-0 miss the model itself
   caught) ALSO routes to ``degraded_mode`` — in addition to the draft —
   never silently queued as an ordinary approval-only draft (spec review
   CRITICAL).
6. ``identify_case`` never re-queries open cases — it consumes
   ``state["open_cases"]`` from ``load_context`` (G3).
7. ``reasoning_log`` accumulates without duplication under the chosen
   no-reducer convention.
8. The unknown-sender path checkpoints under a per-message thread id
   (``checkpointer.py``'s documented exception), never a case thread.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
import pytest_asyncio
from anthropic.types import ToolUseBlock
from langgraph.graph import END, StateGraph
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.agent.checkpointer as cp_mod
import app.db.session as db_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.agent.graph import compile_case_graph, run_graph
from app.agent.prompts.v2 import PROMPT_VERSION
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
    must run before ``get_checkpointer()`` is ever used. The global autouse
    ``_reset_checkpointer_pool`` fixture (``tests/conftest.py``) drops the
    pool reference before every test, so each test here re-opens its own.
    """
    await setup_checkpointer()
    yield
    await close_checkpointer()


# ---------------------------------------------------------------------------
# Local-only helpers (NOT part of the tests/factories.py extraction: cleanup
# and thread-id lookup are specific to this module's own assertions).
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


async def _case_thread_id(session: AsyncSession, *, landlord_id: str) -> str:
    row = (
        (
            await session.execute(
                text("SELECT langgraph_thread_id FROM cases WHERE landlord_id = :lid"),
                {"lid": landlord_id},
            )
        )
        .mappings()
        .one()
    )
    return str(row["langgraph_thread_id"])


# ---------------------------------------------------------------------------
# Fake Anthropic client — generic over tool_input, shared across the three
# LLM call sites the graph exercises (classify_intent / classify_severity /
# draft_response) exactly like each node's own dedicated test module.
# ---------------------------------------------------------------------------


def _fake_message(*, tool_input: dict[str, Any], tool_name: str = "tool") -> SimpleNamespace:
    block = ToolUseBlock(id="toolu_test", input=tool_input, name=tool_name, type="tool_use")
    usage = SimpleNamespace(input_tokens=150, output_tokens=40)
    return SimpleNamespace(content=[block], usage=usage, model="claude-sonnet-5")


class _FakeMessages:
    def __init__(self, *, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
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
        tool_input={
            "intent": "maintenance",
            "is_new_issue": True,
            "summary": "Heat is out",
        },
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


# ---------------------------------------------------------------------------
# 1. Happy path: one message through the whole pipeline
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_run_graph_happy_path_produces_draft_audit_and_checkpoints(
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

    fake_messages = _FakeMessages(
        responses=[_intent_response(), _severity_response(), _draft_response_message()]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        final_state = await run_graph(uuid.UUID(message_id))

        assert final_state.get("classification_failed") is not True
        assert final_state.get("draft_guard_failed") is not True
        assert final_state.get("draft") is not None

        # No duplicated reasoning_log lines under the no-reducer convention
        # (module docstring "reasoning_log accumulation").
        reasoning_log = final_state["reasoning_log"]
        assert len(reasoning_log) == len(set(reasoning_log)), reasoning_log

        draft_row = (
            (
                await db_session.execute(
                    text("SELECT status, prompt_version FROM drafts WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert draft_row["status"] == "pending"
        assert draft_row["prompt_version"] == PROMPT_VERSION

        audit_actions = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE landlord_id = :lid ORDER BY id"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        actions = [row["action"] for row in audit_actions]
        assert actions.count("case_opened") == 1
        assert actions.count("classified") == 2  # intent + severity
        assert actions.count("drafted") == 1
        assert "degraded_mode" not in actions

        thread_id = await _case_thread_id(db_session, landlord_id=landlord_id)
        checkpoint_count = (
            await db_session.execute(
                text("SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :tid"),
                {"tid": thread_id},
            )
        ).scalar_one()
        assert checkpoint_count > 0
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2. Resume-from-checkpoint after a simulated process restart
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_run_graph_resume_from_checkpoint_after_restart(
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
        body="fridge stopped working overnight",
    )

    fake_messages = _FakeMessages(
        responses=[_intent_response(), _severity_response(), _draft_response_message()]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        await run_graph(uuid.UUID(message_id))
        thread_id = await _case_thread_id(db_session, landlord_id=landlord_id)

        # Simulate a process restart: close the checkpointer's pool, forget
        # the module-global reference, then bring it back up exactly like
        # app/main.py's lifespan does at startup.
        await close_checkpointer()
        assert cp_mod._pool is None  # noqa: SLF001
        await setup_checkpointer()

        # A brand-new compiled graph object, same thread_id -- proves the
        # checkpoint is durable Postgres state, not in-process memory.
        fresh_case_graph = compile_case_graph()
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await fresh_case_graph.aget_state(config)

        assert snapshot.values.get("draft") is not None
        assert snapshot.values.get("classification_failed") is not True
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 3. classification_failed routes to degraded_mode (G1) -- double garbage
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_run_graph_classification_failed_routes_to_degraded_mode(
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
        body="not sure what's going on but something seems off",
    )

    fake_messages = _FakeMessages(
        responses=[
            _intent_response(),
            # Both severity attempts return schema-invalid garbage.
            _fake_message(tool_input={"severity": "NOT_A_REAL_SEVERITY"}),
            _fake_message(tool_input={"severity": "ALSO_BAD"}),
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        final_state = await run_graph(uuid.UUID(message_id))

        assert final_state.get("classification_failed") is True
        assert final_state.get("draft") is None  # draft_response never ran

        # NO SILENT DEAD END -- a durable needs_eyes notification + audit row.
        notif_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT type, status, payload FROM notifications WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif_row["type"] == "needs_eyes"
        assert notif_row["payload"]["reasons"] == ["classification_failed"]

        audit_actions = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE landlord_id = :lid ORDER BY id"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        actions = [row["action"] for row in audit_actions]
        assert "degraded_mode" in actions
        assert "drafted" not in actions

        # No draft row was ever inserted for this case.
        draft_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert draft_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 4. draft_guard_failed ALSO routes to degraded_mode (in addition to the
#    draft that draft_response already inserted with the safe fallback)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_run_graph_draft_guard_failed_routes_to_degraded_mode(
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
            # Both draft attempts violate the dollar/compensation hard guard.
            _draft_response_message(body=unsafe_body),
            _draft_response_message(body=unsafe_body),
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        final_state = await run_graph(uuid.UUID(message_id))

        assert final_state.get("draft_guard_failed") is True
        assert final_state.get("draft") is not None  # the safe fallback WAS inserted

        draft_row = (
            (
                await db_session.execute(
                    text("SELECT status FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id}
                )
            )
            .mappings()
            .one()
        )
        assert draft_row["status"] == "pending"

        notif_row = (
            (
                await db_session.execute(
                    text("SELECT type, payload FROM notifications WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif_row["type"] == "needs_eyes"
        assert notif_row["payload"]["reasons"] == ["draft_guard_failed"]

        audit_actions = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE landlord_id = :lid ORDER BY id"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        actions = [row["action"] for row in audit_actions]
        assert "drafted" in actions  # the fallback draft is still recorded
        assert "degraded_mode" in actions
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 5. An LLM-classified EMERGENCY (Tier-0 miss the model catches) ALSO
#    routes to degraded_mode -- spec review CRITICAL: an earlier revision
#    let this fall through to an ordinary approval-queued draft with NO
#    notification at all, silently.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_run_graph_llm_emergency_routes_to_degraded_mode_with_a_draft(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    # Deliberately NOT a Tier-0 HARD-hit phrase -- prefilter defaults to
    # hard_hit=False (factories.insert_message's default) so this is
    # exactly the "Tier-0 missed it, the model caught it" scenario.
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="my elderly mother lives here alone and hasn't been able to reach anyone for days",
    )

    fake_messages = _FakeMessages(
        responses=[
            _intent_response(),
            _severity_response(severity="EMERGENCY"),
            _draft_response_message(),
        ]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        final_state = await run_graph(uuid.UUID(message_id))

        assert final_state["severity"].severity.value == "EMERGENCY"
        assert final_state.get("draft") is not None  # still drafted -- not skipped
        assert final_state.get("draft_guard_failed") is not True

        # The notification is the gate: never silent for an EMERGENCY.
        notif_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT type, status, payload FROM notifications WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif_row["type"] == "needs_eyes"
        assert notif_row["status"] == "pending"
        assert notif_row["payload"]["reasons"] == ["severity_emergency"]

        draft_row = (
            (
                await db_session.execute(
                    text("SELECT status FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id}
                )
            )
            .mappings()
            .one()
        )
        assert draft_row["status"] == "pending"  # a draft plus needs_eyes beats needs_eyes alone

        audit_actions = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE landlord_id = :lid ORDER BY id"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        actions = [row["action"] for row in audit_actions]
        assert "drafted" in actions
        assert "degraded_mode" in actions
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 6. identify_case never re-queries open cases -- exercised implicitly by
#    the happy-path run above (a second message on the SAME tenant must
#    attach to the case load_context found, using state alone).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_run_graph_second_message_attaches_via_state_open_cases(
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

    fake_messages = _FakeMessages(
        responses=[_intent_response(), _severity_response(), _draft_response_message()]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        await run_graph(uuid.UUID(first_message_id))

        case_count_after_first = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert case_count_after_first == 1

        second_message_id = await factories.insert_message(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="still no heat, it's getting cold",
        )
        fake_messages_2 = _FakeMessages(
            responses=[_intent_response(), _severity_response(), _draft_response_message()]
        )
        _patch_client(monkeypatch, fake_messages_2)

        await run_graph(uuid.UUID(second_message_id))

        # Still exactly one case -- the second message attached to the
        # existing open case rather than opening a new one.
        case_count_after_second = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert case_count_after_second == 1

        # Exactly one pending draft (the second stales the first -- the
        # existing stale-then-insert path, now reached through the graph).
        pending_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid AND status = 'pending'"),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert pending_count == 1

        stale_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid AND status = 'stale'"),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert stale_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 7. Unknown sender -- checkpointer.py's documented per-message thread
#    fallback (spec MINOR).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_run_graph_unknown_sender_uses_message_scoped_thread(
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

    fake_messages = _FakeMessages(
        responses=[_intent_response(), _severity_response(severity="ROUTINE")]
    )
    _patch_client(monkeypatch, fake_messages)

    try:
        final_state = await run_graph(uuid.UUID(message_id))

        # draft_response returns early (no case_id) -- no draft, no crash.
        assert final_state.get("draft") is None

        # No case was ever created for an unresolved sender.
        case_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert case_count == 0

        # Checkpoints exist under the documented per-message fallback
        # thread (app/agent/checkpointer.py's "Documented exception"),
        # never a case thread (there is none).
        expected_thread_id = f"message:{message_id}"
        checkpoint_count = (
            await db_session.execute(
                text("SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :tid"),
                {"tid": expected_thread_id},
            )
        ).scalar_one()
        assert checkpoint_count > 0
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 8. reasoning_log accumulation, no-reducer convention -- direct unit-level
#    regression test (no DB), pinning the design decision itself.
# ---------------------------------------------------------------------------


class _TwoNodeState(TypedDict, total=False):
    reasoning_log: list[str]


def _node_a(state: _TwoNodeState) -> dict[str, list[str]]:
    log = list(state.get("reasoning_log") or [])
    log.append("node a ran")
    return {"reasoning_log": log}


def _node_b(state: _TwoNodeState) -> dict[str, list[str]]:
    log = list(state.get("reasoning_log") or [])
    log.append("node b ran")
    return {"reasoning_log": log}


@pytest.mark.unit
async def test_reasoning_log_no_reducer_convention_does_not_duplicate() -> None:
    """Pins the design decision documented in ``app/agent/graph.py``'s
    module docstring: plain TypedDict last-write-wins semantics (no
    ``Annotated[list[str], operator.add]`` reducer) plus every node
    returning its FULL accumulated log is safe for a LINEAR chain and
    produces each line exactly once."""
    graph: StateGraph[_TwoNodeState, None, _TwoNodeState, _TwoNodeState] = StateGraph(_TwoNodeState)
    graph.add_node("a", _node_a)
    graph.add_node("b", _node_b)
    graph.set_entry_point("a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    compiled = graph.compile()

    result = await compiled.ainvoke({"reasoning_log": []})  # type: ignore[attr-defined]

    assert result["reasoning_log"] == ["node a ran", "node b ran"]
