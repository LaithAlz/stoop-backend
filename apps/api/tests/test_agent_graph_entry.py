"""Integration tests for app.agent.graph_entry — the #40→#30-series
background-task seam.

Marker: ``integration`` — writes to the real ``audit_log`` table via the
admin engine. Self-contained per the project convention.
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
from app.agent.graph_entry import enqueue_classification

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


async def _insert_landlord(session: AsyncSession) -> str:
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"),
        {"id": landlord_id, "auth_id": str(uuid.uuid4()), "email": f"{landlord_id}@example.com"},
    )
    await session.commit()
    return landlord_id


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(
        text("DELETE FROM audit_log WHERE landlord_id = :lid"), {"lid": landlord_id}
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
