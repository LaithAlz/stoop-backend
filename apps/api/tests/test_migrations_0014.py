"""Integration tests for the drafts.approved_via + messages landlord-null
CHECK migration (revision 0014, #122 approve-by-SMS).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0014 implements the schema-v1.md v1.16 amendment (renumbered from
v1.15/migration 0013 at rebase time — #203 item 2's properties address
-dedupe amendment claimed v1.15/migration 0013 first, merged as PR #227;
this migration now chains after it as `down_revision = "0013"`):

1. A new nullable ``drafts.approved_via text CHECK (approved_via IN
   ('dashboard', 'sms'))`` column.
2. A new ``messages_landlord_party_null_check`` CHECK constraint
   enforcing that ``party = 'landlord'`` rows carry ``tenant_id``/
   ``vendor_id`` NULL.

These tests verify (helpers duplicate ``tests/test_migrations_0012.py``'s
patterns rather than importing them, matching that module's own stated
self-contained convention):

1. ``drafts.approved_via`` exists, nullable, defaults ``NULL``.
2. The ``approved_via`` CHECK rejects an out-of-vocabulary value.
3. The new ``messages`` CHECK accepts a valid landlord row (tenant_id/
   vendor_id both NULL) and rejects one that isn't.
4. The new ``messages`` CHECK is a no-op for ``party = 'tenant'``/
   ``'vendor'`` rows (tenant_id/vendor_id may be non-NULL there).
5. Downgrade to 0013 drops both; re-upgrade to head restores both (full
   round-trip) — 0013 (properties address-dedupe) itself stays applied
   throughout, since this migration's own down_revision is 0013, not 0012.

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0014.py -m integration -v
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
    """Apply migrations exactly once per test session (ends at head/0014)."""
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


async def _insert_landlord(conn: AsyncConnection, *, phone: str | None = None) -> str:
    landlord_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO landlords (id, auth_user_id, email, phone) "
            "VALUES (:id, :auth_id, :email, :phone)"
        ),
        {
            "id": landlord_id,
            "auth_id": str(uuid.uuid4()),
            "email": f"{landlord_id}@example.com",
            "phone": phone,
        },
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
    conn: AsyncConnection, landlord_id: str, property_id: str, tenant_id: str
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


async def _insert_draft(conn: AsyncConnection, landlord_id: str, case_id: str) -> str:
    draft_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO drafts (id, landlord_id, case_id, recipient, body, prompt_version) "
            "VALUES (:id, :landlord_id, :case_id, 'tenant', 'hello', 'v1')"
        ),
        {"id": draft_id, "landlord_id": landlord_id, "case_id": case_id},
    )
    return draft_id


# ---------------------------------------------------------------------------
# 1. drafts.approved_via — exists, nullable, defaults NULL
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_drafts_approved_via_column_exists_nullable_and_defaults_null(
    conn: AsyncConnection,
) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)
    case_id = await _insert_case(conn, landlord_id, property_id, tenant_id)
    draft_id = await _insert_draft(conn, landlord_id, case_id)

    approved_via = (
        await conn.execute(text("SELECT approved_via FROM drafts WHERE id = :id"), {"id": draft_id})
    ).scalar_one()
    assert approved_via is None

    await conn.execute(
        text("UPDATE drafts SET approved_via = 'sms' WHERE id = :id"), {"id": draft_id}
    )
    approved_via_after = (
        await conn.execute(text("SELECT approved_via FROM drafts WHERE id = :id"), {"id": draft_id})
    ).scalar_one()
    assert approved_via_after == "sms"


@pytest.mark.integration
async def test_drafts_approved_via_check_rejects_out_of_vocabulary(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)
    case_id = await _insert_case(conn, landlord_id, property_id, tenant_id)
    draft_id = await _insert_draft(conn, landlord_id, case_id)

    with pytest.raises(IntegrityError):
        await conn.execute(
            text("UPDATE drafts SET approved_via = 'carrier_pigeon' WHERE id = :id"),
            {"id": draft_id},
        )


# ---------------------------------------------------------------------------
# 2. messages_landlord_party_null_check
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_messages_landlord_row_with_null_tenant_and_vendor_is_accepted(
    conn: AsyncConnection,
) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)

    # Should not raise.
    await conn.execute(
        text(
            "INSERT INTO messages (landlord_id, property_id, tenant_id, vendor_id, "
            "direction, party, body) "
            "VALUES (:landlord_id, :property_id, NULL, NULL, 'inbound', 'landlord', '1')"
        ),
        {"landlord_id": landlord_id, "property_id": property_id},
    )


@pytest.mark.integration
async def test_messages_landlord_row_with_non_null_tenant_id_is_rejected(
    conn: AsyncConnection,
) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)

    with pytest.raises(IntegrityError):
        await conn.execute(
            text(
                "INSERT INTO messages (landlord_id, property_id, tenant_id, "
                "direction, party, body) "
                "VALUES (:landlord_id, :property_id, :tenant_id, 'inbound', 'landlord', '1')"
            ),
            {"landlord_id": landlord_id, "property_id": property_id, "tenant_id": tenant_id},
        )


@pytest.mark.integration
async def test_messages_tenant_row_with_non_null_tenant_id_is_unaffected(
    conn: AsyncConnection,
) -> None:
    """The new CHECK is a no-op for party != 'landlord' rows -- the ordinary
    tenant-message shape (tenant_id set) must still insert cleanly."""
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)

    # Should not raise.
    await conn.execute(
        text(
            "INSERT INTO messages (landlord_id, property_id, tenant_id, "
            "direction, party, body) "
            "VALUES (:landlord_id, :property_id, :tenant_id, 'inbound', 'tenant', 'hi')"
        ),
        {"landlord_id": landlord_id, "property_id": property_id, "tenant_id": tenant_id},
    )


# ---------------------------------------------------------------------------
# 3. Round-trip -- downgrades only to 0013 (properties address-dedupe stays
# applied); re-upgrade restores this migration's own column/constraint.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0013_drops_approved_via_and_messages_check(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0013"))

    async with db.connect() as connection:
        columns = (
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'drafts'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "approved_via" not in columns

        constraint_exists = (
            await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'messages_landlord_party_null_check')"
                )
            )
        ).scalar_one()
        assert constraint_exists is False

        # 0013 (properties address-dedupe) must still be applied -- this
        # migration's down_revision is 0013, not 0012, so downgrading one
        # step must not also drop the dedupe index.
        index_exists = (
            await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                    "WHERE tablename = 'properties' "
                    "AND indexname = 'uq_properties_landlord_address_dedupe')"
                )
            )
        ).scalar_one()
        assert index_exists is True


@pytest.mark.integration
async def test_reupgrade_restores_approved_via_and_messages_check(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        columns = (
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'drafts'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "approved_via" in columns

        constraint_exists = (
            await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'messages_landlord_party_null_check')"
                )
            )
        ).scalar_one()
        assert constraint_exists is True
