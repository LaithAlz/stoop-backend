"""Integration tests for Properties CRUD (#54).

Marker: ``integration`` — requires docker-compose Postgres.

Harness: direct handler-function calls (same pattern as
``tests/test_require_landlord.py``: "prefer testing the dependency
directly ... FastAPI's Depends(...) wiring is just a type-hint annotation").
Route-level wiring (every ``/v1/`` route actually depends on
``require_landlord``) is already proven generically by
``tests/test_rls_isolation_matrix.py::test_every_v1_route_except_
allowlist_requires_landlord_scoping`` — no need to duplicate that here via
JWT/HTTP round-trips.

Cross-tenant isolation here is enforced at the APPLICATION level (explicit
``landlord_id = :landlord_id`` predicates) rather than genuine Postgres RLS:
locally ``APP_DATABASE_URL`` is unset, so ``get_session`` falls back to the
admin (superuser) engine, and Postgres superusers always bypass RLS
regardless of the GUC (see ``app/db/session.py``'s module docstring). Real
RLS *enforcement* is proven separately by
``tests/test_rls_isolation_matrix.py`` (``SET LOCAL ROLE app_role``). This
file proves the same cross-tenant boundary the other way: at the query
level every one of these endpoints actually uses.
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
from app.pagination import decode_cursor
from app.routers.properties import (
    PropertyCreateRequest,
    PropertyUpdateRequest,
    create_property,
    delete_property,
    get_property,
    list_properties,
    update_property,
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
    await session.rollback()  # clear any aborted transaction from a caught IntegrityError
    params = {"lid": landlord_id}
    await session.execute(
        text(
            "DELETE FROM message_status_events WHERE message_id IN "
            "(SELECT id FROM messages WHERE landlord_id = :lid)"
        ),
        params,
    )
    for table in ("audit_log", "drafts", "messages", "cases", "tenants", "properties"):
        await session.execute(text(f"DELETE FROM {table} WHERE landlord_id = :lid"), params)  # noqa: S608
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


@pytest.mark.integration
async def test_create_and_get_property_roundtrip(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created = await create_property(
            PropertyCreateRequest(
                label="41 Palmerston",
                address_line1="41 Palmerston Ave",
                city="Toronto",
                house_rules="No smoking indoors.",
            ),
            (landlord, session),
        )
        assert created.label == "41 Palmerston"
        assert created.province == "ON"  # DB default applied via COALESCE
        assert created.twilio_number is None  # #53 provisioning is out of scope
        assert created.open_case_count == 0
        assert created.quiet_hours == {"start": "21:00", "end": "08:00"}

        fetched = await get_property(created.id, (landlord, session))
        assert fetched.id == created.id
        assert fetched.house_rules == "No smoking indoors."
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_get_property_not_found_returns_404(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await get_property(uuid.uuid4(), (landlord, session))
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "property_not_found"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_cross_tenant_property_access_returns_404(session: AsyncSession) -> None:
    """Landlord A's property must be invisible/unmodifiable/undeletable to landlord B."""
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    landlord_a = Landlord(id=uuid.UUID(landlord_a_id))
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="A's place", address_line1="1 A St", city="Toronto"),
            (landlord_a, session),
        )

        with pytest.raises(AppError) as get_exc:
            await get_property(prop.id, (landlord_b, session))
        assert get_exc.value.status_code == 404
        assert get_exc.value.code == "property_not_found"

        with pytest.raises(AppError) as update_exc:
            await update_property(
                prop.id, PropertyUpdateRequest(label="hijacked"), (landlord_b, session)
            )
        assert update_exc.value.status_code == 404
        assert update_exc.value.code == "property_not_found"

        with pytest.raises(AppError) as delete_exc:
            await delete_property(prop.id, (landlord_b, session))
        assert delete_exc.value.status_code == 404
        assert delete_exc.value.code == "property_not_found"

        # Landlord A can still see it, unaffected by B's failed attempts.
        still_there = await get_property(prop.id, (landlord_a, session))
        assert still_there.label == "A's place"

        # It must not show up in landlord B's own list.
        b_list = await list_properties((landlord_b, session))
        assert prop.id not in {p.id for p in b_list.items}
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)


@pytest.mark.integration
async def test_list_properties_cursor_pagination_newest_first(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created_ids = []
        for i in range(3):
            p = await create_property(
                PropertyCreateRequest(
                    label=f"Property {i}", address_line1=f"{i} Test St", city="Toronto"
                ),
                (landlord, session),
            )
            created_ids.append(p.id)
            # Postgres `now()` is transaction-start-time, not statement-time —
            # committing between creates (as separate real requests would,
            # each with its own get_session transaction) gives each property
            # a genuinely distinct created_at to sort by.
            await session.commit()

        page1 = await list_properties((landlord, session), limit=2)
        assert len(page1.items) == 2
        assert page1.next_cursor is not None
        # Newest-first: the most recently created property comes first.
        assert page1.items[0].id == created_ids[-1]

        page2 = await list_properties((landlord, session), limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 1
        assert page2.next_cursor is None
        assert page2.items[0].id == created_ids[0]

        all_ids = {p.id for p in page1.items} | {p.id for p in page2.items}
        assert all_ids == set(created_ids)
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_list_properties_invalid_cursor_returns_400(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await list_properties((landlord, session), cursor="not-a-valid-cursor!!")
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "invalid_cursor"
    finally:
        await _cleanup(session, landlord_id)


def test_cursor_round_trips() -> None:
    from datetime import UTC, datetime

    from app.pagination import encode_cursor

    now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)
    row_id = str(uuid.uuid4())
    cursor = encode_cursor(now, row_id)
    decoded_at, decoded_id = decode_cursor(cursor)
    assert decoded_at == now
    assert decoded_id == row_id


@pytest.mark.integration
async def test_update_house_rules_writes_audit_log_once(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(
                label="Audit test",
                address_line1="1 Audit St",
                city="Toronto",
                house_rules="original rules",
            ),
            (landlord, session),
        )

        updated = await update_property(
            prop.id, PropertyUpdateRequest(house_rules="new rules"), (landlord, session)
        )
        assert updated.house_rules == "new rules"

        rows = (
            (
                await session.execute(
                    text(
                        "SELECT action, payload FROM audit_log "
                        "WHERE landlord_id = :lid AND action = 'settings_changed'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["payload"]["field"] == "house_rules"
        assert rows[0]["payload"]["property_id"] == str(prop.id)

        # Re-patching with the SAME value must not write a second audit row.
        await update_property(
            prop.id, PropertyUpdateRequest(house_rules="new rules"), (landlord, session)
        )
        rows_after = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'settings_changed'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert rows_after == 1
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_update_quiet_hours_explicit_null_rejected(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="QH test", address_line1="1 QH St", city="Toronto"),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await update_property(
                prop.id, PropertyUpdateRequest(quiet_hours=None), (landlord, session)
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "invalid_field"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
@pytest.mark.parametrize("field", ["label", "address_line1", "city", "province"])
async def test_update_not_nullable_text_field_explicit_null_rejected(
    session: AsyncSession, field: str
) -> None:
    """Every NOT NULL patchable column, not just quiet_hours/heating_season
    (senior review on PR #195, B3) — an explicit JSON null must 422, never
    reach the DB as a raw IntegrityError/500."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="NN test", address_line1="1 NN St", city="Toronto"),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await update_property(
                prop.id, PropertyUpdateRequest(**{field: None}), (landlord, session)
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "invalid_field"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_delete_blocked_by_open_case_returns_409(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="Del test", address_line1="1 Del St", city="Toronto"),
            (landlord, session),
        )
        tenant_id = await factories.insert_tenant(session, landlord_id, str(prop.id))
        await factories.insert_case(
            session, landlord_id=landlord_id, property_id=str(prop.id), tenant_id=tenant_id
        )

        with pytest.raises(AppError) as exc_info:
            await delete_property(prop.id, (landlord, session))
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "has_open_cases"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_delete_blocked_by_dependents_returns_409(session: AsyncSession) -> None:
    """No open case, but a tenant row still FK-references the property —
    ``ON DELETE RESTRICT`` fires; must surface as a clean 409, not a 500."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="FK test", address_line1="1 FK St", city="Toronto"),
            (landlord, session),
        )
        await factories.insert_tenant(session, landlord_id, str(prop.id))

        with pytest.raises(AppError) as exc_info:
            await delete_property(prop.id, (landlord, session))
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "has_dependents"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_delete_property_success(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="Deletable", address_line1="1 Gone St", city="Toronto"),
            (landlord, session),
        )
        await delete_property(prop.id, (landlord, session))

        with pytest.raises(AppError) as exc_info:
            await get_property(prop.id, (landlord, session))
        assert exc_info.value.code == "property_not_found"
    finally:
        await _cleanup(session, landlord_id)
