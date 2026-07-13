"""Integration tests for the number_release notification-type migration
(revision 0011, #53).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0011 implements the schema-v1.md v1.10 amendments: one new
``notifications.type`` CHECK value (``number_release``) plus its own
partial unique dedupe index (``uq_notifications_number_release_dedupe``,
keyed on ``payload ->> 'twilio_sid'`` rather than ``message_id`` — this
type has no ``message_id`` at all). Same shape as migrations 0006/0009's
own dedupe indexes.

These tests verify (helpers below duplicate
``tests/test_migrations_0009.py``'s patterns rather than importing them,
matching that module's own stated self-contained convention):

1. The index exists after ``upgrade head``, is UNIQUE, and is partial.
2. The widened CHECK accepts ``number_release`` and still rejects an
   out-of-vocabulary value.
3. ``ON CONFLICT`` inference against the new index behaves exactly as
   ``app/property_provisioning.py::schedule_number_release`` relies on:
   first insert for a given ``twilio_sid`` succeeds, a second attempt for
   the SAME ``twilio_sid`` conflicts and returns no row.
4. Downgrade FAILS CLOSED (raises, rolls back, stays at head) when a live
   ``number_release`` row exists.
5. Downgrade to 0010 drops the index and restores the narrower CHECK;
   re-upgrade to head restores everything (full round-trip).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0011.py -m integration -v
"""

from __future__ import annotations

import asyncio
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


_INSERT_NUMBER_RELEASE_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'number_release', 'push', 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'twilio_sid')) WHERE type = 'number_release'
    DO NOTHING
    RETURNING id
    """
)


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0011)."""
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


# ---------------------------------------------------------------------------
# 1. Index existence, uniqueness, partiality
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_number_release_index_exists_unique_and_partial(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_number_release_dedupe'"
            )
        )
        row = result.one_or_none()

    assert row is not None, "uq_notifications_number_release_dedupe must exist after upgrade head"
    indexdef = row[0]
    assert "UNIQUE" in indexdef
    assert "WHERE" in indexdef
    assert "number_release" in indexdef
    assert "twilio_sid" in indexdef


# ---------------------------------------------------------------------------
# 2. Widened CHECK — accepts number_release, still rejects a bogus one
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_accepts_number_release(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    await conn.execute(
        text(
            "INSERT INTO notifications (landlord_id, type, channel) "
            "VALUES (:landlord_id, 'number_release', 'push')"
        ),
        {"landlord_id": landlord_id},
    )
    count = (
        await conn.execute(
            text("SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid"),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.integration
async def test_check_rejects_out_of_vocabulary_type(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    with pytest.raises(IntegrityError):
        await conn.execute(
            text(
                "INSERT INTO notifications (landlord_id, type, channel) "
                "VALUES (:landlord_id, 'not_a_real_type', 'push')"
            ),
            {"landlord_id": landlord_id},
        )


# ---------------------------------------------------------------------------
# 3. ON CONFLICT inference, keyed on twilio_sid
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_number_release_on_conflict_inference(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    payload = json.dumps({"twilio_sid": f"PN{uuid.uuid4().hex}"})
    params = {"landlord_id": landlord_id, "payload": payload}

    row1 = (await conn.execute(_INSERT_NUMBER_RELEASE_SQL, params)).mappings().one_or_none()
    assert row1 is not None

    row2 = (await conn.execute(_INSERT_NUMBER_RELEASE_SQL, params)).mappings().one_or_none()
    assert row2 is None

    count = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid "
                "AND type = 'number_release'"
            ),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# 4. Downgrade FAILS CLOSED when a live number_release row exists.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_fails_closed_when_number_release_row_exists(db: AsyncEngine) -> None:
    landlord_id: str | None = None
    async with db.connect() as setup_conn:
        trans = await setup_conn.begin()
        landlord_id = await _insert_landlord(setup_conn)
        await setup_conn.execute(
            text(
                "INSERT INTO notifications (landlord_id, type, channel, payload) "
                "VALUES (:landlord_id, 'number_release', 'push', CAST(:payload AS jsonb))"
            ),
            {
                "landlord_id": landlord_id,
                "payload": json.dumps({"twilio_sid": f"PN{uuid.uuid4().hex}"}),
            },
        )
        await trans.commit()

    try:
        loop = asyncio.get_running_loop()
        with pytest.raises(RuntimeError, match="notifications_type_check"):
            await loop.run_in_executor(None, lambda: _alembic("downgrade", "0010"))

        async with db.connect() as verify_conn:
            version = (
                await verify_conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            assert version == "0011"
    finally:
        async with db.connect() as cleanup_conn:
            trans = await cleanup_conn.begin()
            await cleanup_conn.execute(
                text("DELETE FROM notifications WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
            await cleanup_conn.execute(
                text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id}
            )
            await trans.commit()


# ---------------------------------------------------------------------------
# 5. Downgrade to 0010 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0010_drops_index_and_narrows_check(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0010"))

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_number_release_dedupe'"
            )
        )
        assert not result.fetchall(), "index should be dropped after downgrade to 0010"

        constraint_def = (
            await connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'notifications_type_check'"
                )
            )
        ).scalar_one()
        assert "number_release" not in constraint_def


@pytest.mark.integration
async def test_reupgrade_restores_index_and_widened_check(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_number_release_dedupe'"
            )
        )
        assert {row[0] for row in result.fetchall()} == {"uq_notifications_number_release_dedupe"}

        constraint_def = (
            await connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'notifications_type_check'"
                )
            )
        ).scalar_one()
        assert "number_release" in constraint_def
