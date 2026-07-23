"""Tests for ``app/agent/approve_by_sms.py`` (#122) — the token parser and
the two-phase resolve helpers. The full dispatch (approve/reject/undo/
stale/disambiguation/property-scoping) is exercised end-to-end via
``POST /webhooks/twilio/sms`` in ``tests/test_webhooks_twilio_approve_by_sms.py``;
this module focuses on ``parse_command`` (pure) and the resolve-phase
correlation helpers in isolation.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent import approve_by_sms, landlord_sms
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


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(text("DELETE FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM notifications WHERE landlord_id = :lid"), {"lid": landlord_id}
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


# ---------------------------------------------------------------------------
# parse_command -- pure, no DB. api-contracts.md's exact token vocabulary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("1", approve_by_sms.COMMAND_APPROVE),
        (" 1 ", approve_by_sms.COMMAND_APPROVE),
        ("2", approve_by_sms.COMMAND_REJECT),
        (" 2\n", approve_by_sms.COMMAND_REJECT),
        ("undo", approve_by_sms.COMMAND_UNDO),
        ("UNDO", approve_by_sms.COMMAND_UNDO),
        ("Undo", approve_by_sms.COMMAND_UNDO),
        (" undo ", approve_by_sms.COMMAND_UNDO),
        ("", None),
        ("11", None),
        ("yes", None),
        ("1.", None),
        ("can you call me instead?", None),
        ("undone", None),
    ],
)
def test_parse_command(body: str, expected: str | None) -> None:
    assert approve_by_sms.parse_command(body) == expected


# ---------------------------------------------------------------------------
# resolve_reply / resolve_reply_for_recovered_case -- correlation-only.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_reply_unrecognized_token_never_correlates(db_session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)

    try:
        parsed = await approve_by_sms.resolve_reply(
            db_session,
            landlord_id=uuid.UUID(landlord_id),
            property_id=uuid.UUID(property_id),
            body="thanks!",
        )
        assert parsed.command is None
        assert parsed.case_id is None
        assert parsed.draft_id is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_resolve_reply_recognized_token_no_ready_notice_falls_back(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)

    try:
        parsed = await approve_by_sms.resolve_reply(
            db_session,
            landlord_id=uuid.UUID(landlord_id),
            property_id=uuid.UUID(property_id),
            body="1",
        )
        assert parsed.command == approve_by_sms.COMMAND_APPROVE
        assert parsed.case_id is None  # nothing to correlate against
        assert parsed.draft_id is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_resolve_reply_correlates_to_most_recent_ready_notice(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)
    await landlord_sms.enqueue_landlord_sms(
        db_session,
        landlord_id=uuid.UUID(landlord_id),
        case_id=uuid.UUID(case_id),
        draft_id=uuid.UUID(draft_id),
        kind=landlord_sms.KIND_READY,
        body="Draft ready...",
    )
    await db_session.commit()

    try:
        parsed = await approve_by_sms.resolve_reply(
            db_session,
            landlord_id=uuid.UUID(landlord_id),
            property_id=uuid.UUID(property_id),
            body="1",
        )
        assert parsed.command == approve_by_sms.COMMAND_APPROVE
        assert str(parsed.case_id) == case_id
        assert str(parsed.draft_id) == draft_id
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_resolve_reply_for_recovered_case_reuses_the_stored_case_id(
    db_session: AsyncSession,
) -> None:
    """The redelivery-path helper never re-resolves `most_recent_ready_
    draft` from scratch -- it re-derives ONLY the draft_id, scoped to the
    case_id the FIRST delivery already durably stored."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)
    await landlord_sms.enqueue_landlord_sms(
        db_session,
        landlord_id=uuid.UUID(landlord_id),
        case_id=uuid.UUID(case_id),
        draft_id=uuid.UUID(draft_id),
        kind=landlord_sms.KIND_READY,
        body="Draft ready...",
    )
    await db_session.commit()

    try:
        parsed = await approve_by_sms.resolve_reply_for_recovered_case(
            db_session, case_id=uuid.UUID(case_id), body="1"
        )
        assert parsed.command == approve_by_sms.COMMAND_APPROVE
        assert str(parsed.case_id) == case_id
        assert str(parsed.draft_id) == draft_id

        # A tenant-party recovered row (case_id=None) never correlates.
        parsed_tenant = await approve_by_sms.resolve_reply_for_recovered_case(
            db_session, case_id=None, body="1"
        )
        assert parsed_tenant.command == approve_by_sms.COMMAND_APPROVE
        assert parsed_tenant.case_id is None
        assert parsed_tenant.draft_id is None
    finally:
        await _cleanup(db_session, landlord_id)
