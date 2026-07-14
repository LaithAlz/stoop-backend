"""Integration tests for ``POST /v1/properties/{id}/trust/revoke`` (#60).

Marker: ``integration`` — requires a running Postgres instance + ``alembic
upgrade head``. Harness mirrors ``tests/test_properties_router.py``: direct
handler-function calls with a synthetic ``(Landlord, AsyncSession)`` tuple
(no HTTP round-trip needed — RLS route-scoping is already proven generically
by ``tests/test_rls_isolation_matrix.py``).
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

from app.deps import Landlord
from app.errors import AppError
from app.routers.trust import RevokeTrustRequest, revoke_trust
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
    tables = ("audit_log", "trust_metrics", "drafts", "messages", "cases", "tenants", "properties")
    for table in tables:
        await session.execute(text(f"DELETE FROM {table} WHERE landlord_id = :lid"), params)  # noqa: S608
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


async def _trust_row(session: AsyncSession, *, property_id: str, severity: str = "routine") -> dict:
    row = (
        (
            await session.execute(
                text(
                    "SELECT autonomy_unlocked, revoked_at, consecutive_clean FROM trust_metrics "
                    "WHERE property_id = :pid AND severity = :severity"
                ),
                {"pid": property_id, "severity": severity},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def _audit_rows(session: AsyncSession, *, landlord_id: str, action: str) -> list[dict]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT actor, payload FROM audit_log "
                    "WHERE landlord_id = :lid AND action = :action ORDER BY id"
                ),
                {"lid": landlord_id, "action": action},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@pytest.mark.integration
async def test_revoke_property_scope_flips_unlocked_row_and_audits(
    session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    await factories.insert_trust_metrics(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        autonomy_unlocked=True,
        consecutive_clean=15,
    )
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        response = await revoke_trust(
            uuid.UUID(property_id), (landlord, session), RevokeTrustRequest(scope="property")
        )
        assert response.scope == "property"
        assert response.revoked_count == 1

        row = await _trust_row(session, property_id=property_id)
        assert row["autonomy_unlocked"] is False
        assert row["revoked_at"] is not None
        assert row["consecutive_clean"] == 0  # re-graduation starts over

        audits = await _audit_rows(session, landlord_id=landlord_id, action="trust_revoked")
        assert len(audits) == 1
        assert audits[0]["actor"] == "landlord"
        assert audits[0]["payload"]["scope"] == "property"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_revoke_is_idempotent_zero_count_still_200_and_audited(
    session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    # never unlocked at all.
    await factories.insert_trust_metrics(
        session, landlord_id=landlord_id, property_id=property_id, autonomy_unlocked=False
    )
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        response = await revoke_trust(uuid.UUID(property_id), (landlord, session), None)
        assert response.revoked_count == 0

        # Calling again is still a clean 200, never an error.
        response_again = await revoke_trust(uuid.UUID(property_id), (landlord, session), None)
        assert response_again.revoked_count == 0

        audits = await _audit_rows(session, landlord_id=landlord_id, action="trust_revoked")
        assert len(audits) == 2  # one per call -- each landlord action is real, always recorded
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_revoke_global_scope_flips_every_unlocked_row(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_a = await factories.insert_property(session, landlord_id)
    property_b = await factories.insert_property(session, landlord_id)
    await factories.insert_trust_metrics(
        session, landlord_id=landlord_id, property_id=property_a, autonomy_unlocked=True
    )
    await factories.insert_trust_metrics(
        session, landlord_id=landlord_id, property_id=property_b, autonomy_unlocked=True
    )
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        response = await revoke_trust(
            uuid.UUID(property_a), (landlord, session), RevokeTrustRequest(scope="global")
        )
        assert response.scope == "global"
        assert response.revoked_count == 2

        row_a = await _trust_row(session, property_id=property_a)
        row_b = await _trust_row(session, property_id=property_b)
        assert row_a["autonomy_unlocked"] is False
        assert row_b["autonomy_unlocked"] is False
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_revoke_cross_tenant_property_404(session: AsyncSession) -> None:
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    property_a = await factories.insert_property(session, landlord_a_id)
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await revoke_trust(uuid.UUID(property_a), (landlord_b, session), None)
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "property_not_found"
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)


@pytest.mark.integration
async def test_revoke_missing_property_404(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await revoke_trust(uuid.uuid4(), (landlord, session), None)
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "property_not_found"
    finally:
        await _cleanup(session, landlord_id)
