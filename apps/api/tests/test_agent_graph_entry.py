"""Integration tests for app.agent.graph_entry — the #40→#34 background
-task seam.

Marker: ``integration`` — writes to the real ``audit_log`` table via the
admin engine. Seeding helpers (landlord/property/tenant/message) come from
``tests/factories.py`` (senior review: shared factories, not re-duplicated
in every new test module); ``_cleanup`` and the fake-Anthropic-client
machinery stay local. The Anthropic SDK itself is ALWAYS mocked
(``app.integrations.anthropic.get_client`` monkeypatched) for tests that
exercise the real graph invocation — no real API calls anywhere in this
suite.
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

import app.agent.graph_entry as graph_entry_mod
import app.db.session as db_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
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
    must run before the graph's checkpointer is ever used — see that
    module's docstring."""
    await setup_checkpointer()
    yield
    await close_checkpointer()


# ---------------------------------------------------------------------------
# Local-only helpers (NOT part of the tests/factories.py extraction)
# ---------------------------------------------------------------------------


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


def _full_success_fake_messages() -> _FakeMessages:
    return _FakeMessages(
        responses=[
            _fake_message(
                tool_input={"intent": "maintenance", "is_new_issue": True, "summary": "No heat"},
                tool_name="classify_intent",
            ),
            _fake_message(
                tool_input={
                    "severity": "URGENT",
                    "rules_fired": ["No heat"],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["Tenant reports no heat."],
                },
                tool_name="classify_severity",
            ),
            _fake_message(
                tool_input={"body": "Thanks, I'll look into it.", "refusal_templates_used": []},
                tool_name="draft_message",
            ),
        ]
    )


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


@pytest.mark.integration
async def test_enqueue_classification_appends_message_received_once(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    message_id = uuid.uuid4()

    try:
        await enqueue_classification(message_id, uuid.UUID(landlord_id))

        rows = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor, action FROM audit_log "
                        "WHERE landlord_id = :lid AND payload ->> 'message_id' = :mid"
                    ),
                    {"lid": landlord_id, "mid": str(message_id)},
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["actor"] == "system"
        assert rows[0]["action"] == "message_received"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_enqueue_classification_is_idempotent_no_double_log(
    db_session: AsyncSession,
) -> None:
    """``message_received`` itself never duplicates across repeated calls
    for the same (still-incomplete) message -- see module docstring
    "Idempotent INSERT, single statement". Note this does NOT mean the
    graph itself is only attempted once: with no ``messages`` row at all
    (this test's ``message_id`` is never persisted), neither call ever
    reaches a completion marker, so ``run_graph`` genuinely IS retried on
    the second call too (see ``test_enqueue_classification_retries_after_a
    _failed_attempt`` for a dedicated test of that behavior) -- it just
    fails again for the same reason, caught the same way."""
    landlord_id = await factories.insert_landlord(db_session)
    message_id = uuid.uuid4()

    try:
        await enqueue_classification(message_id, uuid.UUID(landlord_id))
        await enqueue_classification(message_id, uuid.UUID(landlord_id))  # called again

        count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND payload ->> 'message_id' = :mid"
                ),
                {"lid": landlord_id, "mid": str(message_id)},
            )
        ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_enqueue_classification_never_raises_for_nonexistent_landlord(
    db_session: AsyncSession,
) -> None:
    """``audit_log.landlord_id`` has no FK constraint (migration 0002 —
    audit rows must survive deletion of the row they reference), so this
    insert actually succeeds even for a landlord id that was never
    created. Pins the "never raises outward" contract regardless: a
    ``BackgroundTasks`` callback has no caller left to handle an
    exception -- including the Sentry capture and last-resort
    ``needs_eyes`` attempt, whose OWN INSERT fails its FK (no real
    landlord row) and must be swallowed too."""
    message_id = uuid.uuid4()
    bogus_landlord_id = uuid.uuid4()

    try:
        await enqueue_classification(message_id, bogus_landlord_id)
    finally:
        await db_session.execute(
            text("DELETE FROM audit_log WHERE landlord_id = :lid"),
            {"lid": str(bogus_landlord_id)},
        )
        await db_session.commit()


@pytest.mark.integration
async def test_enqueue_classification_invokes_the_real_graph_end_to_end(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#34 — the webhook's background-task seam now runs the actual
    LangGraph pipeline (mocked Anthropic client), not just the
    ``message_received`` guard: a pending draft and the classified/drafted
    audit rows appear from a single ``enqueue_classification`` call."""
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

    _patch_client(monkeypatch, _full_success_fake_messages())

    try:
        await enqueue_classification(uuid.UUID(message_id), uuid.UUID(landlord_id))

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

        message_received_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid "
                    "AND action = 'message_received'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert message_received_count == 1

        # Calling it again for the SAME (now-completed) message is a no-op
        # -- the completion marker ('drafted') already exists, so the
        # guard skips re-running the graph entirely (module docstring
        # "Gating on COMPLETION, not RECEIPT").
        await enqueue_classification(uuid.UUID(message_id), uuid.UUID(landlord_id))
        draft_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert draft_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_enqueue_classification_retries_after_a_failed_attempt(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety review MEDIUM (#34 fix round): gating on COMPLETION, not
    RECEIPT. The FIRST attempt fails mid-graph (an empty fake-response
    queue raises inside ``classify_intent``, uncaught by that node's own
    narrower except clause, propagating all the way up) -- no completion
    marker is ever written. A SECOND call (simulating a Twilio
    redelivery after a crash) with a working fake client must actually
    RE-RUN the graph and succeed, rather than treating the earlier
    'message_received' row as "already handled"."""
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

    _patch_client(monkeypatch, _FakeMessages(responses=[]))  # forces an immediate failure

    try:
        await enqueue_classification(uuid.UUID(message_id), uuid.UUID(landlord_id))

        # No completion marker written -- the first attempt never got far.
        draft_count_after_first = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert draft_count_after_first == 0

        # Redelivery: a working client this time.
        _patch_client(monkeypatch, _full_success_fake_messages())
        await enqueue_classification(uuid.UUID(message_id), uuid.UUID(landlord_id))

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

        # Exactly one 'message_received' row despite two attempts -- the
        # observability row itself still never duplicates.
        message_received_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid "
                    "AND action = 'message_received'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert message_received_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_enqueue_classification_pages_sentry_and_writes_last_resort_needs_eyes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety review MEDIUM (#34 fix round): "Total-failure visibility".
    A ``run_graph`` failure must be paged (metadata only) AND leave a
    last-resort ``needs_eyes`` notification, so the message doesn't sit
    with NEITHER a draft NOR any notification."""
    landlord_id = await factories.insert_landlord(db_session)
    message_id = uuid.uuid4()  # no messages row -- run_graph fails deterministically

    captured: list[dict[str, Any]] = []

    def _fake_capture_message(message: str, **kwargs: Any) -> None:
        captured.append({"message": message, **kwargs})

    monkeypatch.setattr(graph_entry_mod.sentry_sdk, "capture_message", _fake_capture_message)

    try:
        await enqueue_classification(message_id, uuid.UUID(landlord_id))

        assert len(captured) == 1
        extras = captured[0]["extras"]
        assert extras["message_id"] == str(message_id)
        assert extras["landlord_id"] == landlord_id
        assert extras["exc_type"]  # some exception type name, never body/phone/JWT content
        # Metadata only -- rule #5.
        assert set(extras) == {"message_id", "landlord_id", "exc_type"}

        notif_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT type, status, case_id, payload FROM notifications "
                        "WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif_row["type"] == "needs_eyes"
        assert notif_row["status"] == "pending"
        assert notif_row["case_id"] is None
        assert notif_row["payload"]["message_id"] == str(message_id)
        assert notif_row["payload"]["reason"] == "run_graph_failed"
    finally:
        await _cleanup(db_session, landlord_id)
