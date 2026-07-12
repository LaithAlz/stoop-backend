"""Integration tests for Tenants CRUD (#54).

Marker: ``integration``. Same direct-handler-call harness as
``tests/test_properties_router.py`` — see that file's module docstring for
the full rationale (route-wiring proven generically by
``test_rls_isolation_matrix.py``; cross-tenant proof is application-level
here since local sessions run as a superuser that bypasses RLS).
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
from app.routers.tenants import (
    TenantCreateRequest,
    TenantUpdateRequest,
    create_tenant,
    delete_tenant,
    list_tenants,
    update_tenant,
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
    for table in ("audit_log", "drafts", "messages", "cases", "tenants", "properties"):
        await session.execute(text(f"DELETE FROM {table} WHERE landlord_id = :lid"), params)  # noqa: S608
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


@pytest.mark.integration
async def test_create_list_update_delete_tenant_roundtrip(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created = await create_tenant(
            uuid.UUID(property_id),
            TenantCreateRequest(phone="+14165550001", name="Maria", unit="2"),
            (landlord, session),
        )
        assert created.name == "Maria"
        assert created.active is True

        listing = await list_tenants(uuid.UUID(property_id), (landlord, session))
        assert [t.id for t in listing.items] == [created.id]

        updated = await update_tenant(
            created.id, TenantUpdateRequest(unit="3"), (landlord, session)
        )
        assert updated.unit == "3"
        assert updated.name == "Maria"  # untouched fields survive a partial PATCH

        deleted = await delete_tenant(created.id, (landlord, session))
        assert deleted.active is False

        # Idempotent: deleting again is a no-op, not an error.
        deleted_again = await delete_tenant(created.id, (landlord, session))
        assert deleted_again.active is False

        # Soft-deleted tenants still appear in the list (no deleted_at column
        # to filter by — schema-v1.md only has `active`).
        listing_after = await list_tenants(uuid.UUID(property_id), (landlord, session))
        assert listing_after.items[0].active is False
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_list_tenants_property_not_found_returns_404(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await list_tenants(uuid.uuid4(), (landlord, session))
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "property_not_found"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_cross_tenant_tenant_access_returns_404(session: AsyncSession) -> None:
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    property_a_id = await factories.insert_property(session, landlord_a_id)
    landlord_a = Landlord(id=uuid.UUID(landlord_a_id))
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    try:
        tenant = await create_tenant(
            uuid.UUID(property_a_id),
            TenantCreateRequest(phone="+14165550002", name="A's tenant"),
            (landlord_a, session),
        )

        # B cannot list A's property's tenants.
        with pytest.raises(AppError) as list_exc:
            await list_tenants(uuid.UUID(property_a_id), (landlord_b, session))
        assert list_exc.value.status_code == 404
        assert list_exc.value.code == "property_not_found"

        # B cannot create a tenant under A's property.
        with pytest.raises(AppError) as create_exc:
            await create_tenant(
                uuid.UUID(property_a_id),
                TenantCreateRequest(phone="+14165550003", name="Sneaky"),
                (landlord_b, session),
            )
        assert create_exc.value.status_code == 404
        assert create_exc.value.code == "property_not_found"

        # B cannot update or delete A's tenant.
        with pytest.raises(AppError) as update_exc:
            await update_tenant(
                tenant.id, TenantUpdateRequest(name="hijacked"), (landlord_b, session)
            )
        assert update_exc.value.status_code == 404
        assert update_exc.value.code == "tenant_not_found"

        with pytest.raises(AppError) as delete_exc:
            await delete_tenant(tenant.id, (landlord_b, session))
        assert delete_exc.value.status_code == 404
        assert delete_exc.value.code == "tenant_not_found"

        # A's tenant is unaffected.
        still_there = await list_tenants(uuid.UUID(property_a_id), (landlord_a, session))
        assert still_there.items[0].name == "A's tenant"
        assert still_there.items[0].active is True
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)


@pytest.mark.integration
async def test_vulnerable_occupant_validation(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created = await create_tenant(
            uuid.UUID(property_id),
            TenantCreateRequest(phone="+14165550004", vulnerable_occupant="infant"),
            (landlord, session),
        )
        assert created.vulnerable_occupant == "infant"

        with pytest.raises(ValidationError):
            TenantCreateRequest(phone="+14165550005", vulnerable_occupant="not_a_real_value")
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_update_phone_explicit_null_rejected(session: AsyncSession) -> None:
    """``phone`` is NOT NULL in schema-v1.md (senior review on PR #195, B3)."""
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created = await create_tenant(
            uuid.UUID(property_id), TenantCreateRequest(phone="+14165550006"), (landlord, session)
        )
        with pytest.raises(AppError) as exc_info:
            await update_tenant(created.id, TenantUpdateRequest(phone=None), (landlord, session))
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "invalid_field"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_duplicate_phone_on_create_returns_409(session: AsyncSession) -> None:
    """``UNIQUE (property_id, phone)`` (senior review on PR #195, A1)."""
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await create_tenant(
            uuid.UUID(property_id),
            TenantCreateRequest(phone="+14165550007", name="First"),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await create_tenant(
                uuid.UUID(property_id),
                TenantCreateRequest(phone="+14165550007", name="Duplicate"),
                (landlord, session),
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "duplicate_phone"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_duplicate_phone_on_update_returns_409(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await create_tenant(
            uuid.UUID(property_id),
            TenantCreateRequest(phone="+14165550008", name="First"),
            (landlord, session),
        )
        second = await create_tenant(
            uuid.UUID(property_id),
            TenantCreateRequest(phone="+14165550009", name="Second"),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await update_tenant(
                second.id, TenantUpdateRequest(phone="+14165550008"), (landlord, session)
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "duplicate_phone"
    finally:
        await _cleanup(session, landlord_id)
