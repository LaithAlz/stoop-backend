"""Integration tests for Devices registration (#210 M3,
``app/routers/devices.py``).

Marker: ``integration``. Same direct-handler-call harness as
``tests/test_vendors_router.py`` — see that file's module docstring.
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
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.deps import Landlord
from app.errors import AppError
from app.routers.devices import DeviceRegisterRequest, register_device, unregister_device
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


def _fresh_token() -> str:
    return f"ExponentPushToken[{uuid.uuid4()}]"


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.rollback()
    await session.execute(
        text("DELETE FROM push_outbox WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM push_tokens WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


# ---------------------------------------------------------------------------
# POST /v1/devices — register / upsert
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_register_device_creates_row(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    token = _fresh_token()
    try:
        response = await register_device(
            DeviceRegisterRequest(token=token, platform="ios"), (landlord, session)
        )
        assert response.platform == "ios"

        row = (
            (
                await session.execute(
                    text(
                        "SELECT landlord_id, platform, revoked_at FROM push_tokens WHERE id = :id"
                    ),
                    {"id": str(response.id)},
                )
            )
            .mappings()
            .one()
        )
        assert str(row["landlord_id"]) == landlord_id
        assert row["platform"] == "ios"
        assert row["revoked_at"] is None
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_register_device_same_landlord_reregistration_is_idempotent(
    session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    token = _fresh_token()
    try:
        first = await register_device(
            DeviceRegisterRequest(token=token, platform="ios"), (landlord, session)
        )
        second = await register_device(
            DeviceRegisterRequest(token=token, platform="ios"), (landlord, session)
        )
        assert first.id == second.id

        count = (
            await session.execute(
                text("SELECT count(*) FROM push_tokens WHERE token = :token"), {"token": token}
            )
        ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_register_device_moves_token_between_landlords_on_reregistration(
    session: AsyncSession,
) -> None:
    """The shared-device/sign-out-sign-in flow: landlord B registers the
    SAME token landlord A previously registered — the row moves to B, A's
    old registration is simply gone (schema-v1.md's v1.13 amendments,
    "token ownership model")."""
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    landlord_a = Landlord(id=uuid.UUID(landlord_a_id))
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    token = _fresh_token()
    try:
        first = await register_device(
            DeviceRegisterRequest(token=token, platform="ios"), (landlord_a, session)
        )
        second = await register_device(
            DeviceRegisterRequest(token=token, platform="android"), (landlord_b, session)
        )
        assert first.id == second.id  # same row, moved

        row = (
            (
                await session.execute(
                    text("SELECT landlord_id, platform FROM push_tokens WHERE id = :id"),
                    {"id": str(second.id)},
                )
            )
            .mappings()
            .one()
        )
        assert str(row["landlord_id"]) == landlord_b_id
        assert row["platform"] == "android"

        count = (
            await session.execute(
                text("SELECT count(*) FROM push_tokens WHERE token = :token"), {"token": token}
            )
        ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)


@pytest.mark.integration
async def test_register_device_clears_revoked_at_on_reregistration(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    token = _fresh_token()
    try:
        await session.execute(
            text(
                "INSERT INTO push_tokens (landlord_id, token, platform, revoked_at) "
                "VALUES (:landlord_id, :token, 'ios', now())"
            ),
            {"landlord_id": landlord_id, "token": token},
        )
        await session.commit()

        response = await register_device(
            DeviceRegisterRequest(token=token, platform="ios"), (landlord, session)
        )

        revoked_at = (
            await session.execute(
                text("SELECT revoked_at FROM push_tokens WHERE id = :id"), {"id": str(response.id)}
            )
        ).scalar_one()
        assert revoked_at is None
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.unit
def test_register_device_rejects_empty_token() -> None:
    with pytest.raises(ValidationError):
        DeviceRegisterRequest(token="", platform="ios")


@pytest.mark.unit
def test_register_device_rejects_whitespace_only_token() -> None:
    with pytest.raises(ValidationError):
        DeviceRegisterRequest(token="   ", platform="ios")  # noqa: S106 -- test fixture, not a secret


@pytest.mark.unit
def test_register_device_rejects_invalid_platform() -> None:
    with pytest.raises(ValidationError):
        DeviceRegisterRequest(token=_fresh_token(), platform="web")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DELETE /v1/devices/{id} — unregister
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unregister_device_deletes_row(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        push_token_id = await factories.insert_push_token(session, landlord_id=landlord_id)

        response = await unregister_device(uuid.UUID(push_token_id), (landlord, session))
        assert response.status == "deleted"

        count = (
            await session.execute(
                text("SELECT count(*) FROM push_tokens WHERE id = :id"), {"id": push_token_id}
            )
        ).scalar_one()
        assert count == 0
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_unregister_device_not_found_returns_404(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc:
            await unregister_device(uuid.uuid4(), (landlord, session))
        assert exc.value.status_code == 404
        assert exc.value.code == "device_not_found"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_unregister_device_repeat_call_returns_404_not_idempotent_200(
    session: AsyncSession,
) -> None:
    """Genuine hard delete — unlike ``POST /v1/cases/{id}/resolve``'s
    idempotent-200, a repeat delete of an already-gone row 404s exactly
    like any other missing id (api-contracts.md's Devices section)."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        push_token_id = await factories.insert_push_token(session, landlord_id=landlord_id)
        device_id = uuid.UUID(push_token_id)

        await unregister_device(device_id, (landlord, session))

        with pytest.raises(AppError) as exc:
            await unregister_device(device_id, (landlord, session))
        assert exc.value.status_code == 404
        assert exc.value.code == "device_not_found"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_cross_tenant_unregister_returns_404_and_leaves_row_untouched(
    session: AsyncSession,
) -> None:
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    try:
        push_token_id = await factories.insert_push_token(session, landlord_id=landlord_a_id)

        with pytest.raises(AppError) as exc:
            await unregister_device(uuid.UUID(push_token_id), (landlord_b, session))
        assert exc.value.status_code == 404
        assert exc.value.code == "device_not_found"

        count = (
            await session.execute(
                text("SELECT count(*) FROM push_tokens WHERE id = :id"), {"id": push_token_id}
            )
        ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)
