"""Integration tests for app.agent.graph_entry — the #40→#34 background
-task seam.

Marker: ``integration`` — writes to the real ``audit_log`` table via the
admin engine. Self-contained per the project convention. The Anthropic SDK
itself is ALWAYS mocked (``app.integrations.anthropic.get_client``
monkeypatched) for tests that exercise the real graph invocation — no real
API calls anywhere in this suite.
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

import app.db.session as db_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.agent.graph_entry import enqueue_classification
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


@pytest_asyncio.fixture(autouse=True)
async def _checkpointer_lifecycle(_migrate_once: None) -> AsyncGenerator[None, None]:
    """Ordering contract (``app/agent/checkpointer.py``): ``setup_checkpointer()``
    must run before the graph's checkpointer is ever used — see that
    module's docstring."""
    await setup_checkpointer()
    yield
    await close_checkpointer()


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
            "phone": f"+1416{uuid.uuid4().int % 10_000_000:07d}",
        },
    )
    await session.commit()
    return tenant_id


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
    landlord_id = await _insert_landlord(db_session)
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
    landlord_id = await _insert_landlord(db_session)
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
    exception."""
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
    landlord_id = await _insert_landlord(db_session)
    property_id = await _insert_property(db_session, landlord_id)
    tenant_id = await _insert_tenant(db_session, landlord_id, property_id)
    message_id = await _insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat has been out since this morning",
    )

    fake_messages = _FakeMessages(
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
    _patch_client(monkeypatch, fake_messages)

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

        # Calling it again for the SAME message is a no-op -- the guard
        # skips re-running the graph entirely (see module docstring).
        await enqueue_classification(uuid.UUID(message_id), uuid.UUID(landlord_id))
        draft_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        ).scalar_one()
        assert draft_count == 1
    finally:
        await _cleanup(db_session, landlord_id)
