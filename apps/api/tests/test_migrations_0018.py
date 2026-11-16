"""Integration tests for the ack_token_backup expression-index migration
(revision 0018, #289).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0018 implements the schema-v1.md v1.24 amendment: a single
partial UNIQUE expression index, ``uq_notifications_ack_token_backup``,
over ``(payload ->> 'ack_token_backup')`` — no new column or table, the
sibling of migration 0010's ``uq_notifications_ack_token``. See that
migration's own module docstring for the full rationale.

These tests verify:
1. The index exists after ``upgrade head``, is UNIQUE, and is partial
   (``WHERE payload ->> 'ack_token_backup' IS NOT NULL``).
2. Two rows with NO ``ack_token_backup`` key never collide (NULL-safe —
   the same pattern every other partial unique index in this schema
   already uses).
3. Two rows with the SAME ``ack_token_backup`` value DO collide (a real
   ``IntegrityError``) — the uniqueness guarantee is real, not just
   syntactically present.
4. A row's ``ack_token`` and ``ack_token_backup`` are indexed
   independently — the SAME string value in both keys on the SAME row
   does not raise (they are two different expressions, two different
   indexes).
5. Downgrade to 0017 drops the index; re-upgrade to head restores it
   (full round-trip) — and, unlike migration 0009, downgrading succeeds
   even with a live row that HAS an ``ack_token_backup`` set (no CHECK
   being narrowed here, so there is no fail-closed hazard to prove).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0018.py -m integration -v
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

from tests import migration_harness


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
    """Apply migrations exactly once per test session (ends at head/0018).

    Delegates to ``tests.migration_harness.migrate_from_base_to_head``,
    same rationale as ``test_migrations_0010.py``'s own fixture."""
    migration_harness.migrate_from_base_to_head(_alembic, _get_db_url())
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


# ---------------------------------------------------------------------------
# 1. Index existence, uniqueness, partiality
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_index_exists_unique_and_partial(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_ack_token_backup'"
            )
        )
        row = result.one_or_none()

    assert row is not None, "uq_notifications_ack_token_backup must exist after upgrade head"
    indexdef = row[0]
    assert "UNIQUE" in indexdef
    assert "WHERE" in indexdef  # partial
    assert "ack_token_backup" in indexdef


# ---------------------------------------------------------------------------
# 2. NULL-safe — rows with no ack_token_backup never collide
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_emergency_call_rows_without_ack_token_backup_never_collide(
    conn: AsyncConnection,
) -> None:
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
# 3. Real uniqueness — two rows with the SAME ack_token_backup collide
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_duplicate_ack_token_backup_raises_integrity_error(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    token = uuid.uuid4().hex
    payload_1 = json.dumps({"message_id": str(uuid.uuid4()), "ack_token_backup": token})
    payload_2 = json.dumps({"message_id": str(uuid.uuid4()), "ack_token_backup": token})

    await conn.execute(
        _INSERT_EMERGENCY_CALL_SQL, {"landlord_id": landlord_id, "payload": payload_1}
    )

    with pytest.raises(IntegrityError):
        await conn.execute(
            _INSERT_EMERGENCY_CALL_SQL, {"landlord_id": landlord_id, "payload": payload_2}
        )


# ---------------------------------------------------------------------------
# 4. ack_token and ack_token_backup are indexed independently — the SAME
# string value in both keys on one row is legal (two different expression
# indexes, never cross-checked against each other).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_ack_token_and_ack_token_backup_are_independent_indexes(
    conn: AsyncConnection,
) -> None:
    landlord_id = await _insert_landlord(conn)
    shared_value = uuid.uuid4().hex
    payload = json.dumps(
        {
            "message_id": str(uuid.uuid4()),
            "ack_token": shared_value,
            "ack_token_backup": shared_value,
        }
    )
    # Must NOT raise: uq_notifications_ack_token and
    # uq_notifications_ack_token_backup are two independent expression
    # indexes, each scoped to its own jsonb key — the same literal value
    # appearing under both keys on the same row never collides with itself.
    await conn.execute(_INSERT_EMERGENCY_CALL_SQL, {"landlord_id": landlord_id, "payload": payload})

    row = (
        (
            await conn.execute(
                text(
                    "SELECT payload ->> 'ack_token' AS landlord_token, "
                    "payload ->> 'ack_token_backup' AS backup_token FROM notifications "
                    "WHERE landlord_id = :lid"
                ),
                {"lid": landlord_id},
            )
        )
        .mappings()
        .one()
    )
    assert row["landlord_token"] == shared_value  # noqa: S105 -- test fixture value
    assert row["backup_token"] == shared_value  # noqa: S105 -- test fixture value


# ---------------------------------------------------------------------------
# 5. Downgrade / re-upgrade round trip — MUST run last (mutates schema
# state for the remainder of the session), and unlike migration 0009,
# succeeds even with a live ack_token_backup'd row present.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0017_drops_index_reupgrade_restores_it(db: AsyncEngine) -> None:
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
                    {
                        "message_id": str(uuid.uuid4()),
                        "ack_token_backup": "still-here-after-downgrade",
                    }
                ),
            },
        )
        await connection.commit()

    _alembic("downgrade", "0017")

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_ack_token_backup'"
            )
        )
        assert result.one_or_none() is None, "index must be gone after downgrade to 0017"

        # The payload data itself survives the downgrade untouched -- only
        # the index is gone, never the underlying row/data.
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT payload ->> 'ack_token_backup' AS token FROM notifications "
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
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_ack_token_backup'"
            )
        )
        assert result.one_or_none() is not None, "index must be restored after re-upgrade to head"
