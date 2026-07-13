"""Integration tests for the ack_token expression-index migration
(revision 0010, #108 safety review finding 8).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0010 implements the schema-v1.md v1.9 amendment: a single
partial UNIQUE expression index, ``uq_notifications_ack_token``, over
``(payload ->> 'ack_token')`` — no new column or table. See that
migration's own module docstring for the full rationale.

These tests verify:
1. The index exists after ``upgrade head``, is UNIQUE, and is partial
   (``WHERE payload ->> 'ack_token' IS NOT NULL``).
2. Two rows with NO ``ack_token`` key never collide (NULL-safe — the same
   pattern every other partial unique index in this schema already uses).
3. Two rows with the SAME ``ack_token`` value DO collide (a real
   `IntegrityError`) — the uniqueness guarantee is real, not just
   syntactically present.
4. Downgrade to 0009 drops the index; re-upgrade to head restores it
   (full round-trip) — and, unlike migration 0009, downgrading succeeds
   even with a live row that HAS an ``ack_token`` set (no CHECK being
   narrowed here, so there is no fail-closed hazard to prove).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0010.py -m integration -v
"""

from __future__ import annotations

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


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0010)."""
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


_INSERT_EMERGENCY_CALL_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'emergency_call', 'voice', 'pending', CAST(:payload AS jsonb))
    """
)

_INSERT_DRAFT_READY_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'draft_ready', 'push', 'pending', '{}'::jsonb)
    """
)


# ---------------------------------------------------------------------------
# 1. Index existence, uniqueness, partiality
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_index_exists_unique_and_partial(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'notifications' AND indexname = 'uq_notifications_ack_token'"
            )
        )
        row = result.one_or_none()

    assert row is not None, "uq_notifications_ack_token must exist after upgrade head"
    indexdef = row[0]
    assert "UNIQUE" in indexdef
    assert "WHERE" in indexdef  # partial
    assert "ack_token" in indexdef


# ---------------------------------------------------------------------------
# 2. NULL-safe — rows with no ack_token never collide
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_rows_without_ack_token_never_collide(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    for _ in range(3):
        await conn.execute(_INSERT_DRAFT_READY_SQL, {"landlord_id": landlord_id})

    count = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid "
                "AND type = 'draft_ready'"
            ),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 3


@pytest.mark.integration
async def test_emergency_call_rows_without_ack_token_never_collide(conn: AsyncConnection) -> None:
    """A row with no ``ack_token`` key at all (this test's own bare INSERT,
    standing in for e.g. a legacy/edge row from before the webhook's own
    INSERT started writing one at creation time — safety review,
    2026-07-12, finding N1) — multiple such rows for DIFFERENT messages
    must never collide on the ack_token index (NULL-safe partial unique
    index, see this migration's own docstring)."""
    landlord_id = await _insert_landlord(conn)
    for _ in range(3):
        payload = json.dumps({"message_id": str(uuid.uuid4()), "categories": ["fire"]})
        await conn.execute(
            _INSERT_EMERGENCY_CALL_SQL, {"landlord_id": landlord_id, "payload": payload}
        )

    count = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid "
                "AND type = 'emergency_call'"
            ),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 3


# ---------------------------------------------------------------------------
# 3. Real uniqueness — two rows with the SAME ack_token collide
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_duplicate_ack_token_raises_integrity_error(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    token = uuid.uuid4().hex
    payload_1 = json.dumps({"message_id": str(uuid.uuid4()), "ack_token": token})
    payload_2 = json.dumps({"message_id": str(uuid.uuid4()), "ack_token": token})

    await conn.execute(
        _INSERT_EMERGENCY_CALL_SQL, {"landlord_id": landlord_id, "payload": payload_1}
    )

    with pytest.raises(IntegrityError):
        await conn.execute(
            _INSERT_EMERGENCY_CALL_SQL, {"landlord_id": landlord_id, "payload": payload_2}
        )


# ---------------------------------------------------------------------------
# 4. Downgrade / re-upgrade round trip — MUST run last (mutates schema
# state for the remainder of the session), and unlike migration 0009,
# succeeds even with a live ack_token'd row present.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0009_drops_index_reupgrade_restores_it(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        landlord_id = str(uuid.uuid4())
        await connection.execute(
            text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"),
            {
                "id": landlord_id,
                "auth_id": str(uuid.uuid4()),
                "email": f"{landlord_id}@example.com",
            },
        )
        await connection.execute(
            _INSERT_EMERGENCY_CALL_SQL,
            {
                "landlord_id": landlord_id,
                "payload": json.dumps(
                    {"message_id": str(uuid.uuid4()), "ack_token": "still-here-after-downgrade"}
                ),
            },
        )
        await connection.commit()

    _alembic("downgrade", "0009")

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'notifications' AND indexname = 'uq_notifications_ack_token'"
            )
        )
        assert result.one_or_none() is None, "index must be gone after downgrade to 0009"

        # The payload data itself survives the downgrade untouched -- only
        # the index is gone, never the underlying row/data.
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT payload ->> 'ack_token' AS token FROM notifications "
                        "WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["token"] == "still-here-after-downgrade"  # noqa: S105 -- a test fixture token

    _alembic("upgrade", "head")

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'notifications' AND indexname = 'uq_notifications_ack_token'"
            )
        )
        assert result.one_or_none() is not None, "index must be restored after re-upgrade to head"
