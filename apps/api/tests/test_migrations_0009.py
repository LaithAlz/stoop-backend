"""Integration tests for the degraded-mode notification-types migration
(revision 0009, #109).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0009 implements the schema-v1.md v1.8 amendments: two new
``notifications.type`` CHECK values (``tenant_ack``, ``degraded_retry``)
plus their own partial unique dedupe indexes
(``uq_notifications_tenant_ack_dedupe``,
``uq_notifications_degraded_retry_dedupe``) — same shape as migration
0006's ``uq_notifications_message_dedupe``, one index per new type instead
of a shared one (see that doc block for why the two must not share a
dedupe slot with each other or with ``needs_eyes``).

These tests verify (helpers below duplicate
``tests/test_migrations_0006.py``'s patterns rather than importing them,
to keep this module self-contained, matching that module's own stated
convention):

1. Both indexes exist after ``upgrade head``, are UNIQUE, and are partial
   (each scoped to its own type).
2. The widened CHECK accepts both new values and still rejects an
   out-of-vocabulary one.
3. ``ON CONFLICT`` inference against each new index behaves exactly as
   ``app/agent/nodes/degraded_mode.py`` relies on: first insert for a
   given ``message_id`` succeeds, a second attempt for the SAME
   ``message_id``/type conflicts and returns no row.
4. The two new types never collide with each other OR with ``needs_eyes``
   for the SAME ``message_id`` (three independent dedupe slots).
5. Downgrade to 0008 drops both indexes and restores the narrower CHECK;
   re-upgrade to head restores everything (full round-trip).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0009.py -m integration -v
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

# ---------------------------------------------------------------------------
# Helpers — duplicated (not imported) from tests/test_migrations_0006.py, to
# keep this module self-contained (see module docstring).
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


# The exact ON CONFLICT shapes app/agent/nodes/degraded_mode.py uses --
# duplicated here (not imported) so this migration-level test is
# self-contained and independent of the app module's internals.
_INSERT_TENANT_ACK_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'tenant_ack', 'sms', 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'message_id')) WHERE type = 'tenant_ack'
    DO NOTHING
    RETURNING id
    """
)

_INSERT_DEGRADED_RETRY_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'degraded_retry', 'push', 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'message_id')) WHERE type = 'degraded_retry'
    DO NOTHING
    RETURNING id
    """
)

_INSERT_NEEDS_EYES_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'needs_eyes', 'push', 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'message_id'), type) WHERE type IN ('emergency_call', 'needs_eyes')
    DO NOTHING
    RETURNING id
    """
)


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0009)."""
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


# ---------------------------------------------------------------------------
# 1. Index existence, uniqueness, partiality
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_tenant_ack_index_exists_unique_and_partial(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_tenant_ack_dedupe'"
            )
        )
        row = result.one_or_none()

    assert row is not None, "uq_notifications_tenant_ack_dedupe must exist after upgrade head"
    indexdef = row[0]
    assert "UNIQUE" in indexdef
    assert "WHERE" in indexdef  # partial
    assert "tenant_ack" in indexdef


@pytest.mark.integration
async def test_degraded_retry_index_exists_unique_and_partial(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_degraded_retry_dedupe'"
            )
        )
        row = result.one_or_none()

    assert row is not None, "uq_notifications_degraded_retry_dedupe must exist after upgrade head"
    indexdef = row[0]
    assert "UNIQUE" in indexdef
    assert "WHERE" in indexdef
    assert "degraded_retry" in indexdef


# ---------------------------------------------------------------------------
# 2. Widened CHECK — accepts both new values, still rejects a bogus one
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_accepts_tenant_ack_and_degraded_retry(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    for notif_type, channel in (("tenant_ack", "sms"), ("degraded_retry", "push")):
        await conn.execute(
            text(
                "INSERT INTO notifications (landlord_id, type, channel) "
                "VALUES (:landlord_id, :type, :channel)"
            ),
            {"landlord_id": landlord_id, "type": notif_type, "channel": channel},
        )
    count = (
        await conn.execute(
            text("SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid"),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 2


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
# 3. ON CONFLICT inference — each new index, sequential proof
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_tenant_ack_on_conflict_inference(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    payload = json.dumps({"message_id": str(uuid.uuid4())})
    params = {"landlord_id": landlord_id, "payload": payload}

    row1 = (await conn.execute(_INSERT_TENANT_ACK_SQL, params)).mappings().one_or_none()
    assert row1 is not None

    row2 = (await conn.execute(_INSERT_TENANT_ACK_SQL, params)).mappings().one_or_none()
    assert row2 is None

    count = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid "
                "AND type = 'tenant_ack'"
            ),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.integration
async def test_degraded_retry_on_conflict_inference(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    payload = json.dumps({"message_id": str(uuid.uuid4())})
    params = {"landlord_id": landlord_id, "payload": payload}

    row1 = (await conn.execute(_INSERT_DEGRADED_RETRY_SQL, params)).mappings().one_or_none()
    assert row1 is not None

    row2 = (await conn.execute(_INSERT_DEGRADED_RETRY_SQL, params)).mappings().one_or_none()
    assert row2 is None

    count = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid "
                "AND type = 'degraded_retry'"
            ),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# 4. The two new types never collide with each other or with needs_eyes for
# the SAME message_id -- three independent dedupe slots.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_tenant_ack_degraded_retry_and_needs_eyes_do_not_collide(
    conn: AsyncConnection,
) -> None:
    landlord_id = await _insert_landlord(conn)
    payload = json.dumps({"message_id": str(uuid.uuid4())})
    params = {"landlord_id": landlord_id, "payload": payload}

    tenant_ack_row = (await conn.execute(_INSERT_TENANT_ACK_SQL, params)).mappings().one_or_none()
    retry_row = (await conn.execute(_INSERT_DEGRADED_RETRY_SQL, params)).mappings().one_or_none()
    needs_eyes_row = (await conn.execute(_INSERT_NEEDS_EYES_SQL, params)).mappings().one_or_none()

    assert tenant_ack_row is not None
    assert retry_row is not None
    assert needs_eyes_row is not None

    count = (
        await conn.execute(
            text("SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid"),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 3


# ---------------------------------------------------------------------------
# 5. Downgrade to 0008 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0008_drops_both_indexes_and_narrows_check(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0008"))

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname IN ("
                "  'uq_notifications_tenant_ack_dedupe', "
                "  'uq_notifications_degraded_retry_dedupe'"
                ")"
            )
        )
        assert not result.fetchall(), "both new indexes should be dropped after downgrade to 0008"

        constraint_def = (
            await connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'notifications_type_check'"
                )
            )
        ).scalar_one()
        assert "tenant_ack" not in constraint_def
        assert "degraded_retry" not in constraint_def


@pytest.mark.integration
async def test_reupgrade_restores_both_indexes_and_widened_check(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname IN ("
                "  'uq_notifications_tenant_ack_dedupe', "
                "  'uq_notifications_degraded_retry_dedupe'"
                ")"
            )
        )
        assert {row[0] for row in result.fetchall()} == {
            "uq_notifications_tenant_ack_dedupe",
            "uq_notifications_degraded_retry_dedupe",
        }

        constraint_def = (
            await connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'notifications_type_check'"
                )
            )
        ).scalar_one()
        assert "tenant_ack" in constraint_def
        assert "degraded_retry" in constraint_def
