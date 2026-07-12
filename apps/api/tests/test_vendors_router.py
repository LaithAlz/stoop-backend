"""Integration tests for Vendors CRUD (#54).

Marker: ``integration``. Same direct-handler-call harness as
``tests/test_properties_router.py`` — see that file's module docstring.
"""

from __future__ import annotations

import base64
import json
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
@pytest.mark.parametrize("field", ["name", "trade", "phone", "active"])
async def test_update_not_nullable_field_explicit_null_rejected(
    session: AsyncSession, field: str
) -> None:
    """Every NOT NULL patchable column (senior review on PR #195, B3)."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created = await create_vendor(
            VendorCreateRequest(name="NN test", trade="general", phone="+14165551999"),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await update_vendor(
                created.id, VendorUpdateRequest(**{field: None}), (landlord, session)
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "invalid_field"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_duplicate_phone_on_create_returns_409(session: AsyncSession) -> None:
    """``UNIQUE (landlord_id, phone)`` (senior review on PR #195, A1)."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await create_vendor(
            VendorCreateRequest(name="First", trade="general", phone="+14165552100"),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await create_vendor(
                VendorCreateRequest(name="Duplicate", trade="hvac", phone="+14165552100"),
                (landlord, session),
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "duplicate_phone"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_duplicate_phone_on_update_returns_409(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await create_vendor(
            VendorCreateRequest(name="First", trade="general", phone="+14165552200"),
            (landlord, session),
        )
        second = await create_vendor(
            VendorCreateRequest(name="Second", trade="general", phone="+14165552201"),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await update_vendor(
                second.id, VendorUpdateRequest(phone="+14165552200"), (landlord, session)
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "duplicate_phone"
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
async def test_list_vendors_invalid_cursor_returns_400(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await list_vendors((landlord, session), cursor="not-a-valid-cursor!!")
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "invalid_cursor"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("cursor_id", "cursor_k"),
    [
        # plain garbage id (original B2)
        ("not-a-uuid", "2026-07-04T12:00:00+00:00"),
        # extreme-offset datetimes: fromisoformat accepts, UTC conversion
        # under/overflows inside asyncpg without the astimezone(UTC)
        # normalization in decode_cursor
        ("0e37df36-f698-11e6-8dd4-cb9ced3df976", "0001-01-01T00:00:00+23:59"),
        ("0e37df36-f698-11e6-8dd4-cb9ced3df976", "9999-12-31T23:59:59-23:59"),
    ],
)
async def test_list_vendors_crafted_cursor_returns_400(
    session: AsyncSession, cursor_id: str, cursor_k: str
) -> None:
    """A cursor that is well-formed base64+JSON but carries a payload the
    DB bind layer would reject must 400 ``invalid_cursor`` — never a raw
    500 (senior review on PR #195, B2 + the normalization residual:
    ``uuid.UUID`` accepts non-canonical forms, ``fromisoformat`` accepts
    extreme offsets; ``decode_cursor`` must NORMALIZE, not shape-check).
    Hand-crafts cursors rather than going through ``encode_cursor`` (which
    only ever produces valid ones).
    """
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    crafted = base64.urlsafe_b64encode(
        json.dumps({"k": cursor_k, "id": cursor_id}).encode("utf-8")
    ).decode("ascii")
    try:
        with pytest.raises(AppError) as exc_info:
            await list_vendors((landlord, session), cursor=crafted)
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "invalid_cursor"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "cursor_id",
    [
        # non-canonical uuid forms uuid.UUID ACCEPTS but asyncpg's bind
        # encoder would reject as raw strings — decode_cursor NORMALIZES
        # them to canonical form, so the query succeeds harmlessly (empty
        # page for an unknown id). The invariant: never a raw 500 (senior
        # re-review on PR #195, B2 residual).
        "urn:uuid:0e37df36-f698-11e6-8dd4-cb9ced3df976",
        "{0e37df36-f698-11e6-8dd4-cb9ced3df976}",
    ],
)
async def test_list_vendors_noncanonical_uuid_cursor_is_normalized_never_500(
    session: AsyncSession, cursor_id: str
) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    crafted = base64.urlsafe_b64encode(
        json.dumps({"k": "2026-07-04T12:00:00+00:00", "id": cursor_id}).encode("utf-8")
    ).decode("ascii")
    try:
        # Must not raise anything — normalization makes the bind value
        # canonical; an unknown id just yields an empty/ordinary page.
        response = await list_vendors((landlord, session), cursor=crafted)
        assert response.items == []
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
