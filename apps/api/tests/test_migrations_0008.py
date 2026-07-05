"""Integration tests for the `cases.pending_resolved_at` migration
(revision 0008, #110).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0008 implements the schema-v1.md v1.5 amendments: a nullable
`cases.pending_resolved_at timestamptz` column — the durable, timer-shaped
marker for the tenant-confirmed-resolution 48h auto-apply window (see that
doc block and ``app/agent/case_lifecycle.py`` for the full design).

These tests verify:
1. The column exists after ``upgrade head``, is nullable, has no default.
2. Every pre-existing row (created before this migration touched anything)
   reads back ``NULL`` — no backfill was needed or performed.
3. The application can freely set/clear it (ordinary UPDATE, no CHECK
   constraint blocking any timestamp value).
4. Downgrade to 0007 drops the column; re-upgrade to head restores it
   (full round-trip).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0008.py -m integration -v
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# ---------------------------------------------------------------------------
# Helpers — duplicated (not imported) from other test_migrations_*.py modules.
# ---------------------------------------------------------------------------


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
    """Apply migrations exactly once per test session (ends at head/0008)."""
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    # Leave schema in place; CI drops the DB container after the run.


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


async def _insert_property(conn: AsyncConnection, landlord_id: str) -> str:
    property_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO properties (id, landlord_id, label, address_line1, city) "
            "VALUES (:id, :landlord_id, 'Test Property', '123 Test St', 'Toronto')"
        ),
        {"id": property_id, "landlord_id": landlord_id},
    )
    return property_id


async def _insert_tenant(conn: AsyncConnection, landlord_id: str, property_id: str) -> str:
    tenant_id = str(uuid.uuid4())
    phone = f"+1416{uuid.uuid4().int % 10_000_000:07d}"
    await conn.execute(
        text(
            "INSERT INTO tenants (id, landlord_id, property_id, phone) "
            "VALUES (:id, :landlord_id, :property_id, :phone)"
        ),
        {"id": tenant_id, "landlord_id": landlord_id, "property_id": property_id, "phone": phone},
    )
    return tenant_id


async def _insert_case(
    conn: AsyncConnection, *, landlord_id: str, property_id: str, tenant_id: str
) -> str:
    case_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO cases (id, landlord_id, property_id, tenant_id, langgraph_thread_id) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, :thread_id)"
        ),
        {
            "id": case_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "thread_id": str(uuid.uuid4()),
        },
    )
    return case_id


# ---------------------------------------------------------------------------
# 1. Column existence, nullability, no default
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_column_exists_nullable_no_default(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT is_nullable, column_default, data_type "
                "FROM information_schema.columns "
                "WHERE table_name = 'cases' AND column_name = 'pending_resolved_at'"
            )
        )
        row = result.one_or_none()

    assert row is not None, "cases.pending_resolved_at must exist after upgrade head"
    is_nullable, column_default, data_type = row
    assert is_nullable == "YES"
    assert column_default is None
    assert data_type == "timestamp with time zone"


# ---------------------------------------------------------------------------
# 2. Existing rows read back NULL — no backfill
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_existing_case_reads_back_null(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)
    case_id = await _insert_case(
        conn, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    value = (
        await conn.execute(
            text("SELECT pending_resolved_at FROM cases WHERE id = :cid"), {"cid": case_id}
        )
    ).scalar_one()
    assert value is None


# ---------------------------------------------------------------------------
# 3. Application can freely set/clear it
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_column_can_be_set_and_cleared(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)
    case_id = await _insert_case(
        conn, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )

    deadline = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    await conn.execute(
        text("UPDATE cases SET pending_resolved_at = :deadline WHERE id = :cid"),
        {"deadline": deadline, "cid": case_id},
    )
    value = (
        await conn.execute(
            text("SELECT pending_resolved_at FROM cases WHERE id = :cid"), {"cid": case_id}
        )
    ).scalar_one()
    assert value == deadline

    await conn.execute(
        text("UPDATE cases SET pending_resolved_at = NULL WHERE id = :cid"), {"cid": case_id}
    )
    cleared = (
        await conn.execute(
            text("SELECT pending_resolved_at FROM cases WHERE id = :cid"), {"cid": case_id}
        )
    ).scalar_one()
    assert cleared is None


# ---------------------------------------------------------------------------
# 4. Downgrade to 0007 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0007_drops_column(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0007"))

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'cases' AND column_name = 'pending_resolved_at'"
            )
        )
        assert not result.fetchall(), "column should be dropped after downgrade to 0007"


@pytest.mark.integration
async def test_reupgrade_restores_column(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'cases' AND column_name = 'pending_resolved_at'"
            )
        )
        assert result.fetchall(), "column should exist again after re-upgrade to head"
