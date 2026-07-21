"""Integration tests for the push_tokens.revoked_at + push_outbox
migration (revision 0012, #210 M3).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0012 implements the schema-v1.md v1.13 amendments: a new nullable
``push_tokens.revoked_at`` column, and a new ``push_outbox`` table
(durable push-delivery queue) with RLS + ``app_role`` grants matching
migration 0005's pattern — this is the first migration since 0005 to add
a genuinely new table.

These tests verify (helpers below duplicate ``tests/test_migrations_0011.
py``'s patterns rather than importing them, matching that module's own
stated self-contained convention):

1. ``push_tokens.revoked_at`` exists, nullable, defaults ``NULL``.
2. ``push_outbox`` exists with the right columns/CHECKs/indexes/FKs.
3. ``ON DELETE CASCADE`` actually cascades — from ``landlords`` and from
   ``push_tokens`` — exercised via real INSERT/DELETE, not just catalog
   inspection.
4. CHECK constraints (``kind``, ``status``) reject out-of-vocabulary
   values.
5. RLS is enabled on ``push_outbox`` with exactly one ``app_role`` policy
   (the migration-0005-pattern check this migration itself claims to
   reproduce).
6. Downgrade to 0011 drops ``push_outbox`` and ``push_tokens.revoked_at``;
   re-upgrade to head restores both (full round-trip).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0012.py -m integration -v
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
    """Apply migrations exactly once per test session (ends at head/0012)."""
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


async def _insert_push_token(conn: AsyncConnection, landlord_id: str) -> str:
    push_token_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO push_tokens (id, landlord_id, token, platform) "
            "VALUES (:id, :landlord_id, :token, 'ios')"
        ),
        {"id": push_token_id, "landlord_id": landlord_id, "token": f"token-{uuid.uuid4()}"},
    )
    return push_token_id


async def _insert_push_outbox(conn: AsyncConnection, landlord_id: str, device_token_id: str) -> str:
    push_outbox_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO push_outbox (id, landlord_id, device_token_id, kind) "
            "VALUES (:id, :landlord_id, :device_token_id, 'draft_awaiting_approval')"
        ),
        {"id": push_outbox_id, "landlord_id": landlord_id, "device_token_id": device_token_id},
    )
    return push_outbox_id


# ---------------------------------------------------------------------------
# 1. push_tokens.revoked_at — exists, nullable, defaults NULL
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_push_tokens_revoked_at_column_exists_nullable_and_defaults_null(
    conn: AsyncConnection,
) -> None:
    landlord_id = await _insert_landlord(conn)
    push_token_id = await _insert_push_token(conn, landlord_id)

    revoked_at = (
        await conn.execute(
            text("SELECT revoked_at FROM push_tokens WHERE id = :id"), {"id": push_token_id}
        )
    ).scalar_one()
    assert revoked_at is None

    # nullable + no default that would reject an explicit value either --
    # the sweep sets this directly via UPDATE.
    await conn.execute(
        text("UPDATE push_tokens SET revoked_at = now() WHERE id = :id"), {"id": push_token_id}
    )
    revoked_at_after = (
        await conn.execute(
            text("SELECT revoked_at FROM push_tokens WHERE id = :id"), {"id": push_token_id}
        )
    ).scalar_one()
    assert revoked_at_after is not None


# ---------------------------------------------------------------------------
# 2. push_outbox — table/column/index existence
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_push_outbox_table_and_indexes_exist(conn: AsyncConnection) -> None:
    columns = (
        (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'push_outbox'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(columns) == {
        "id",
        "landlord_id",
        "device_token_id",
        "kind",
        "payload",
        "status",
        "attempt",
        "next_attempt_at",
        "created_at",
        "updated_at",
    }

    indexes = (
        (
            await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'push_outbox'")
            )
        )
        .scalars()
        .all()
    )
    assert set(indexes) == {
        "push_outbox_pkey",
        "idx_push_outbox_sweep",
        "idx_push_outbox_landlord",
        "idx_push_outbox_device",
    }


@pytest.mark.integration
async def test_push_outbox_defaults(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    push_token_id = await _insert_push_token(conn, landlord_id)
    push_outbox_id = await _insert_push_outbox(conn, landlord_id, push_token_id)

    row = (
        (
            await conn.execute(
                text(
                    "SELECT status, attempt, next_attempt_at, payload FROM push_outbox "
                    "WHERE id = :id"
                ),
                {"id": push_outbox_id},
            )
        )
        .mappings()
        .one()
    )
    assert row["status"] == "pending"
    assert row["attempt"] == 0
    assert row["next_attempt_at"] is None
    assert row["payload"] == {}


# ---------------------------------------------------------------------------
# 3. ON DELETE CASCADE — from landlords, and from push_tokens
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_push_outbox_cascades_on_landlord_delete(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    push_token_id = await _insert_push_token(conn, landlord_id)
    await _insert_push_outbox(conn, landlord_id, push_token_id)

    await conn.execute(text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id})

    count = (
        await conn.execute(
            text("SELECT count(*) FROM push_outbox WHERE landlord_id = :id"), {"id": landlord_id}
        )
    ).scalar_one()
    assert count == 0


@pytest.mark.integration
async def test_push_outbox_cascades_on_push_token_delete(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    push_token_id = await _insert_push_token(conn, landlord_id)
    push_outbox_id = await _insert_push_outbox(conn, landlord_id, push_token_id)

    await conn.execute(text("DELETE FROM push_tokens WHERE id = :id"), {"id": push_token_id})

    count = (
        await conn.execute(
            text("SELECT count(*) FROM push_outbox WHERE id = :id"), {"id": push_outbox_id}
        )
    ).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# 4. CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_push_outbox_kind_check_rejects_out_of_vocabulary(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    push_token_id = await _insert_push_token(conn, landlord_id)
    with pytest.raises(IntegrityError):
        await conn.execute(
            text(
                "INSERT INTO push_outbox (landlord_id, device_token_id, kind) "
                "VALUES (:landlord_id, :device_token_id, 'not_a_real_kind')"
            ),
            {"landlord_id": landlord_id, "device_token_id": push_token_id},
        )


@pytest.mark.integration
async def test_push_outbox_status_check_rejects_out_of_vocabulary(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    push_token_id = await _insert_push_token(conn, landlord_id)
    with pytest.raises(IntegrityError):
        await conn.execute(
            text(
                "INSERT INTO push_outbox (landlord_id, device_token_id, kind, status) "
                "VALUES (:landlord_id, :device_token_id, 'draft_awaiting_approval', 'bogus')"
            ),
            {"landlord_id": landlord_id, "device_token_id": push_token_id},
        )


# ---------------------------------------------------------------------------
# 5. RLS — enabled, exactly one app_role policy (migration 0005's pattern)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_push_outbox_rls_enabled_with_exactly_one_policy(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        rowsecurity = (
            await connection.execute(
                text(
                    "SELECT relrowsecurity FROM pg_class "
                    "WHERE relname = 'push_outbox' AND relnamespace = 'public'::regnamespace"
                )
            )
        ).scalar_one()
        assert rowsecurity is True

        policies = (
            (
                await connection.execute(
                    text("SELECT polname FROM pg_policy WHERE polrelid = 'push_outbox'::regclass")
                )
            )
            .scalars()
            .all()
        )
        assert policies == ["push_outbox_isolation"]


@pytest.mark.integration
async def test_app_role_granted_on_push_outbox(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        privileges = (
            (
                await connection.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = 'push_outbox' AND grantee = 'app_role'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert set(privileges) == {"SELECT", "INSERT", "UPDATE", "DELETE"}


# ---------------------------------------------------------------------------
# 6. Downgrade to 0011 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0011_drops_push_outbox_and_revoked_at(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0011"))

    async with db.connect() as connection:
        table_exists = (
            await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_class "
                    "WHERE relname = 'push_outbox' AND relnamespace = 'public'::regnamespace)"
                )
            )
        ).scalar_one()
        assert table_exists is False

        columns = (
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'push_tokens'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "revoked_at" not in columns


@pytest.mark.integration
async def test_reupgrade_restores_push_outbox_and_revoked_at(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        table_exists = (
            await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_class "
                    "WHERE relname = 'push_outbox' AND relnamespace = 'public'::regnamespace)"
                )
            )
        ).scalar_one()
        assert table_exists is True

        columns = (
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'push_tokens'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "revoked_at" in columns
