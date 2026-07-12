"""Integration tests for Vendors CRUD (#54).

Marker: ``integration``. Same direct-handler-call harness as
``tests/test_properties_router.py`` — see that file's module docstring.
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
from app.routers.vendors import (
    VendorCreateRequest,
    VendorUpdateRequest,
    create_vendor,
    delete_vendor,
    list_vendors,
    update_vendor,
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
    for table in ("audit_log", "drafts", "cases", "vendors", "tenants", "properties"):
        await session.execute(text(f"DELETE FROM {table} WHERE landlord_id = :lid"), params)  # noqa: S608
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


@pytest.mark.integration
async def test_create_list_update_delete_vendor_roundtrip(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created = await create_vendor(
            VendorCreateRequest(name="Ace Plumbing", trade="plumbing", phone="+14165551000"),
            (landlord, session),
        )
        assert created.trade == "plumbing"
        assert created.active is True

        listing = await list_vendors((landlord, session))
        assert [v.id for v in listing.items] == [created.id]

        updated = await update_vendor(
            created.id, VendorUpdateRequest(notes="no Sundays"), (landlord, session)
        )
        assert updated.notes == "no Sundays"
        assert updated.name == "Ace Plumbing"

        deleted = await delete_vendor(created.id, (landlord, session))
        assert deleted.active is False

        deleted_again = await delete_vendor(created.id, (landlord, session))
        assert deleted_again.active is False
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_list_vendors_cursor_pagination(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created_ids = []
        for i in range(3):
            v = await create_vendor(
                VendorCreateRequest(
                    name=f"Vendor {i}", trade="general", phone=f"+141655520{i:02d}"
                ),
                (landlord, session),
            )
            created_ids.append(v.id)
            await session.commit()

        page1 = await list_vendors((landlord, session), limit=2)
        assert len(page1.items) == 2
        assert page1.next_cursor is not None
        assert page1.items[0].id == created_ids[-1]

        page2 = await list_vendors((landlord, session), limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 1
        assert page2.next_cursor is None

        all_ids = {v.id for v in page1.items} | {v.id for v in page2.items}
        assert all_ids == set(created_ids)
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_cross_tenant_vendor_access_returns_404(session: AsyncSession) -> None:
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    landlord_a = Landlord(id=uuid.UUID(landlord_a_id))
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    try:
        vendor = await create_vendor(
            VendorCreateRequest(name="A's vendor", trade="hvac", phone="+14165553000"),
            (landlord_a, session),
        )

        with pytest.raises(AppError) as update_exc:
            await update_vendor(
                vendor.id, VendorUpdateRequest(name="hijacked"), (landlord_b, session)
            )
        assert update_exc.value.status_code == 404
        assert update_exc.value.code == "vendor_not_found"

        with pytest.raises(AppError) as delete_exc:
            await delete_vendor(vendor.id, (landlord_b, session))
        assert delete_exc.value.status_code == 404
        assert delete_exc.value.code == "vendor_not_found"

        b_list = await list_vendors((landlord_b, session))
        assert vendor.id not in {v.id for v in b_list.items}

        a_list = await list_vendors((landlord_a, session))
        assert a_list.items[0].name == "A's vendor"
        assert a_list.items[0].active is True
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)
