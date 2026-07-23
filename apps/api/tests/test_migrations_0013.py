"""Integration tests for the properties address-dedupe UNIQUE index
(revision 0013, #203 item 2).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0013 implements schema-v1.md's v1.15 amendment: a landlord
-scoped, normalized-address UNIQUE index on ``properties`` —
``(landlord_id, lower(trim(address_line1)), lower(trim(city)),
lower(trim(province)))`` — mirroring ``app/routers/properties.py``'s own
``_DUPLICATE_PROPERTY_SQL`` pre-check normalization EXACTLY. Closes the
DB-level half of the pre-check's TOCTOU race (two genuinely concurrent
creates for the same address could both pass the application-level
pre-check before either committed).

These tests verify (helpers below duplicate ``tests/test_migrations_0011.
py``'s patterns rather than importing them, matching that module's own
stated self-contained convention):

1. The index exists after ``upgrade head``, is UNIQUE, and its definition
   matches the expected columns/expression.
2. Same landlord + same normalized address (case/whitespace-insensitive)
   collides — ``IntegrityError``.
3. Same landlord, DIFFERENT address (any of the three components) — no
   collision.
4. DIFFERENT landlord, same address — no collision (the index is
   landlord-scoped, matching the multi-tenant dedupe semantics).
5. ``postal_code`` is excluded from the uniqueness — two rows with the
   same address but different (or absent) postal codes still collide,
   matching ``_DUPLICATE_PROPERTY_SQL``'s own documented exclusion.
6. Round-trip: downgrade to 0012 drops the index (and a previously
   -blocked duplicate insert now succeeds); re-upgrade restores it. Also
   proves the "fails closed" hazard the module docstring documents:
   re-running ``CREATE UNIQUE INDEX`` while a live duplicate exists raises
   and leaves the schema at the prior revision, exactly like the CHECK
   -narrowing hazards in migrations 0009/0011.

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0013.py -m integration -v
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine


def _get_db_url() -> str:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop",
    )
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
    """Apply migrations exactly once per test session (ends at head — 0013
    when this file was written)."""
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield


@pytest_asyncio.fixture
async def db(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def conn(db: AsyncEngine) -> AsyncGenerator[AsyncConnection, None]:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            yield connection
        finally:
            await trans.rollback()


async def _insert_landlord(conn: AsyncConnection) -> str:
    landlord_id = str(uuid.uuid4())
    await conn.execute(
        text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"),
        {"id": landlord_id, "auth_id": str(uuid.uuid4()), "email": f"{landlord_id}@example.com"},
    )
    return landlord_id


_INSERT_PROPERTY_SQL = text(
    """
    INSERT INTO properties (landlord_id, label, address_line1, city, province, postal_code)
    VALUES (:landlord_id, :label, :address_line1, :city, :province, :postal_code)
    """
)


async def _insert_property(
    conn: AsyncConnection,
    landlord_id: str,
    *,
    label: str = "Test",
    address_line1: str = "41 Palmerston Ave",
    city: str = "Toronto",
    province: str = "ON",
    postal_code: str | None = None,
) -> None:
    await conn.execute(
        _INSERT_PROPERTY_SQL,
        {
            "landlord_id": landlord_id,
            "label": label,
            "address_line1": address_line1,
            "city": city,
            "province": province,
            "postal_code": postal_code,
        },
    )


# ---------------------------------------------------------------------------
# 1. Index existence, uniqueness, expression
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_index_exists_unique_with_expected_expression(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'properties' "
                "AND indexname = 'uq_properties_landlord_address_dedupe'"
            )
        )
        row = result.one_or_none()

    assert row is not None, "uq_properties_landlord_address_dedupe must exist after upgrade head"
    indexdef = row[0]
    assert "UNIQUE" in indexdef
    assert "landlord_id" in indexdef
    assert "lower(" in indexdef
    assert "trim(" in indexdef.lower() or "btrim(" in indexdef.lower()
    assert "address_line1" in indexdef
    assert "city" in indexdef
    assert "province" in indexdef
    # No WHERE clause -- properties has no soft-delete column to exclude
    # (see the migration's own "NOT ACTUALLY PARTIAL" docstring note).
    assert "WHERE" not in indexdef


# ---------------------------------------------------------------------------
# 2. Same landlord + same normalized address collides
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_same_landlord_normalized_duplicate_address_collides(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    await _insert_property(
        conn, landlord_id, address_line1="41 Palmerston Ave", city="Toronto", province="ON"
    )

    with pytest.raises(IntegrityError):
        await _insert_property(
            conn,
            landlord_id,
            label="Retry",
            address_line1="  41 palmerston ave  ",
            city="TORONTO",
            province="on",
        )


# ---------------------------------------------------------------------------
# 3. Same landlord, different address component -- no collision
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("address_line1", "city", "province"),
    [
        ("99 Different St", "Toronto", "ON"),  # different address_line1
        ("41 Palmerston Ave", "Ottawa", "ON"),  # different city
        ("41 Palmerston Ave", "Toronto", "AB"),  # different province
    ],
)
async def test_same_landlord_different_address_component_allowed(
    conn: AsyncConnection, address_line1: str, city: str, province: str
) -> None:
    landlord_id = await _insert_landlord(conn)
    await _insert_property(
        conn, landlord_id, address_line1="41 Palmerston Ave", city="Toronto", province="ON"
    )
    # No IntegrityError -- a genuinely different address for the same
    # landlord must always be allowed.
    await _insert_property(
        conn, landlord_id, label="Second", address_line1=address_line1, city=city, province=province
    )

    count = (
        await conn.execute(
            text("SELECT COUNT(*) FROM properties WHERE landlord_id = :lid"), {"lid": landlord_id}
        )
    ).scalar_one()
    assert count == 2


# ---------------------------------------------------------------------------
# 4. Different landlord, same address -- no collision (landlord-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_different_landlord_same_address_allowed(conn: AsyncConnection) -> None:
    landlord_a = await _insert_landlord(conn)
    landlord_b = await _insert_landlord(conn)

    await _insert_property(
        conn, landlord_a, address_line1="41 Palmerston Ave", city="Toronto", province="ON"
    )
    # Same normalized address, different landlord -- must succeed.
    await _insert_property(
        conn, landlord_b, address_line1="41 Palmerston Ave", city="Toronto", province="ON"
    )

    count = (
        await conn.execute(
            text("SELECT COUNT(*) FROM properties WHERE landlord_id IN (:a, :b)"),
            {"a": landlord_a, "b": landlord_b},
        )
    ).scalar_one()
    assert count == 2


# ---------------------------------------------------------------------------
# 5. postal_code excluded from the uniqueness
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_postal_code_excluded_from_uniqueness(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    await _insert_property(
        conn,
        landlord_id,
        address_line1="41 Palmerston Ave",
        city="Toronto",
        province="ON",
        postal_code="M6G 2P4",
    )

    with pytest.raises(IntegrityError):
        # Same address, DIFFERENT (and this time absent) postal_code --
        # must still collide, matching _DUPLICATE_PROPERTY_SQL's own
        # documented exclusion of postal_code from the comparison.
        await _insert_property(
            conn,
            landlord_id,
            label="Retry, no postal code",
            address_line1="41 Palmerston Ave",
            city="Toronto",
            province="ON",
            postal_code=None,
        )


# ---------------------------------------------------------------------------
# 6. Round-trip -- MUST run last: it mutates schema state for the
# remainder of the session, and exercises the "fails closed" hazard too.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_drops_index_then_reupgrade_fails_closed_on_live_duplicate(
    db: AsyncEngine,
) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0012"))

    landlord_id: str | None = None
    try:
        async with db.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'properties' "
                    "AND indexname = 'uq_properties_landlord_address_dedupe'"
                )
            )
            assert not result.fetchall(), "index should be dropped after downgrade to 0012"

        # With the index gone, a duplicate normalized address for the same
        # landlord must now succeed -- proving the constraint really is
        # what was blocking it above.
        async with db.connect() as setup_conn:
            trans = await setup_conn.begin()
            landlord_id = await _insert_landlord(setup_conn)
            await _insert_property(
                setup_conn, landlord_id, address_line1="1 Hazard St", city="Toronto", province="ON"
            )
            await _insert_property(
                setup_conn,
                landlord_id,
                label="Duplicate (allowed without the index)",
                address_line1="1 hazard st",
                city="TORONTO",
                province="on",
            )
            await trans.commit()

        async with db.connect() as before_conn:
            version_before = (
                await before_conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()

        # Re-upgrading now FAILS CLOSED: CREATE UNIQUE INDEX validates
        # against the live duplicate row pair above.
        with pytest.raises(RuntimeError, match="uq_properties_landlord_address_dedupe"):
            await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

        async with db.connect() as verify_conn:
            version_after = (
                await verify_conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            assert version_after == version_before
    finally:
        if landlord_id is not None:
            async with db.connect() as cleanup_conn:
                trans = await cleanup_conn.begin()
                await cleanup_conn.execute(
                    text("DELETE FROM properties WHERE landlord_id = :lid"), {"lid": landlord_id}
                )
                await cleanup_conn.execute(
                    text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id}
                )
                await trans.commit()

    # Duplicate resolved -- re-upgrade now succeeds, restoring the index
    # for any test module collected after this one in the same session.
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as final_conn:
        result = await final_conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'properties' "
                "AND indexname = 'uq_properties_landlord_address_dedupe'"
            )
        )
        assert {row[0] for row in result.fetchall()} == {"uq_properties_landlord_address_dedupe"}
