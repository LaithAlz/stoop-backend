"""Integration tests for ``app/trust.py`` (#60) — the shared trust-ladder
helpers used by the auto-send eligibility check, the sender's graduation
write, and the landlord-facing revoke endpoint.

Marker: ``integration`` — requires a running Postgres instance + ``alembic
upgrade head``.
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

from app.trust import (
    GRADUATION_SEVERITY,
    is_routine_autonomy_unlocked,
    revoke_all_autonomy,
    revoke_property_autonomy,
)
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
async def session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine) as sess:
        yield sess


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.rollback()
    params = {"lid": landlord_id}
    for table in ("audit_log", "trust_metrics", "properties"):
        await session.execute(text(f"DELETE FROM {table} WHERE landlord_id = :lid"), params)  # noqa: S608
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


def test_graduation_severity_is_routine() -> None:
    assert GRADUATION_SEVERITY == "routine"


@pytest.mark.integration
async def test_is_routine_autonomy_unlocked_missing_row_is_false(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    try:
        unlocked = await is_routine_autonomy_unlocked(session, property_id=uuid.UUID(property_id))
        assert unlocked is False
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_is_routine_autonomy_unlocked_true_when_unlocked_and_not_revoked(
    session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    await factories.insert_trust_metrics(
        session, landlord_id=landlord_id, property_id=property_id, autonomy_unlocked=True
    )
    try:
        unlocked = await is_routine_autonomy_unlocked(session, property_id=uuid.UUID(property_id))
        assert unlocked is True
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_is_routine_autonomy_unlocked_false_when_revoked(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    import datetime as dt

    await factories.insert_trust_metrics(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        autonomy_unlocked=True,
        revoked_at=dt.datetime.now(dt.UTC),
    )
    try:
        unlocked = await is_routine_autonomy_unlocked(session, property_id=uuid.UUID(property_id))
        assert unlocked is False
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_is_routine_autonomy_unlocked_ignores_urgent_row_even_if_unlocked(
    session: AsyncSession,
) -> None:
    """Belt-and-braces (#60): a trust_metrics row with severity='urgent'
    manually forced autonomy_unlocked=true (should never happen via the
    real graduation write, which hardcodes 'routine' — but this proves the
    READ side never trusts a non-routine row either)."""
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    await factories.insert_trust_metrics(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        severity="urgent",
        autonomy_unlocked=True,
    )
    try:
        unlocked = await is_routine_autonomy_unlocked(session, property_id=uuid.UUID(property_id))
        assert unlocked is False
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_revoke_property_autonomy_resets_consecutive_clean(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    await factories.insert_trust_metrics(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        autonomy_unlocked=True,
        consecutive_clean=25,
    )
    try:
        count = await revoke_property_autonomy(
            session,
            landlord_id=uuid.UUID(landlord_id),
            property_id=uuid.UUID(property_id),
            actor="landlord",
            reason="test",
        )
        assert count == 1

        row = (
            (
                await session.execute(
                    text(
                        "SELECT consecutive_clean, autonomy_unlocked FROM trust_metrics "
                        "WHERE property_id = :pid AND severity = 'routine'"
                    ),
                    {"pid": property_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["consecutive_clean"] == 0
        assert row["autonomy_unlocked"] is False
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_revoke_all_autonomy_only_touches_unlocked_rows(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    # urgent row accumulating a streak but never unlocked (schema forbids
    # it from ever unlocking anyway) -- must be left completely untouched.
    await factories.insert_trust_metrics(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        severity="urgent",
        autonomy_unlocked=False,
        consecutive_clean=3,
    )
    await factories.insert_trust_metrics(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        severity="routine",
        autonomy_unlocked=True,
        consecutive_clean=12,
    )
    try:
        count = await revoke_all_autonomy(
            session, landlord_id=uuid.UUID(landlord_id), actor="system", reason="misclassification"
        )
        assert count == 1

        urgent_row = (
            (
                await session.execute(
                    text(
                        "SELECT consecutive_clean, revoked_at FROM trust_metrics "
                        "WHERE property_id = :pid AND severity = 'urgent'"
                    ),
                    {"pid": property_id},
                )
            )
            .mappings()
            .one()
        )
        assert urgent_row["consecutive_clean"] == 3  # untouched -- was never unlocked
        assert urgent_row["revoked_at"] is None
    finally:
        await _cleanup(session, landlord_id)
