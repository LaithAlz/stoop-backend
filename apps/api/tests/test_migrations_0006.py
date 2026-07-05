"""Integration tests for the notifications dedupe-index migration
(revision 0006).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0006 implements the schema-v1.md v1.3 amendments (consolidated
safety review, #40/#152, item 1 — BLOCKING): a partial unique expression
index, ``uq_notifications_message_dedupe``, on
``notifications ((payload ->> 'message_id'), type)`` for
``type IN ('emergency_call', 'needs_eyes')``. This closes a cross-process
concurrency hole in the Twilio webhook's idempotent artifact creation: an
application-level ``WHERE NOT EXISTS`` check-then-insert is NOT safe
across processes/connections (two genuinely concurrent redeliveries can
each pass the existence check before either commits), reproduced 3/3
during the consolidated safety review with genuinely overlapping
transactions.

These tests verify (per docs/03-engineering/schema-v1.md, the canonical
source; helpers below duplicate ``tests/test_migrations_0003.py``'s
patterns rather than importing them, to keep this module self-contained):

1. The index exists after ``upgrade head``, is UNIQUE, and is partial
   (has a predicate).
2. NULL semantics: rows with no ``message_id`` key in ``payload`` (so
   ``payload ->> 'message_id'`` is SQL NULL) never collide with each
   other, regardless of how many are inserted.
3. Partial scope: notification types OUTSIDE
   ``('emergency_call', 'needs_eyes')`` are entirely unaffected and may
   repeat freely with the same ``message_id``.
4. ``ON CONFLICT`` inference against the index behaves exactly as the
   webhook handler relies on: first insert for a given
   ``(message_id, type)`` succeeds and returns a row; a second attempt
   for the SAME pair conflicts and returns none.
5. **The concurrency proof itself** (the reviewer's repro shape): two
   GENUINELY overlapping transactions, synchronized with an
   ``asyncio.Barrier`` so both reach their ``INSERT`` at effectively the
   same instant on two independent connections, each attempt to insert
   for the SAME ``(message_id, type)`` — exactly one must win; Postgres's
   own unique-index enforcement (not application logic) guarantees this
   regardless of interleaving.
6. Downgrade to 0005 drops the index; re-upgrade to head restores it
   (full round-trip).

Every test that touches data uses its own connection wrapped in an
explicit transaction that is always rolled back at teardown (the ``conn``
fixture) EXCEPT the concurrency test (which must actually COMMIT so two
independent connections can observe each other's conflict, and cleans up
explicitly) and the round-trip test at the bottom (which mutates schema
state and therefore runs last).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0006.py -m integration -v
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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# ---------------------------------------------------------------------------
# Helpers — duplicated (not imported) from tests/test_migrations_0003.py, to
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


# The exact INSERT the webhook handler uses (app/routers/webhooks/twilio.py
# _INSERT_EMERGENCY_NOTIFICATION_SQL / _INSERT_NEEDS_EYES_SQL) -- duplicated
# here (not imported) so this migration-level test is self-contained and
# independent of the app module's internals.
_INSERT_NOTIFICATION_ON_CONFLICT_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, :type, :channel, 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'message_id'), type) WHERE type IN ('emergency_call', 'needs_eyes')
    DO NOTHING
    RETURNING id
    """
)


# ---------------------------------------------------------------------------
# Session-scoped synchronous setup (avoids pytest-asyncio scope-mismatch).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0006)."""
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    # Leave schema in place; CI drops the DB container after the run.


@pytest_asyncio.fixture
async def db(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    """Per-test async engine; depends on ``_migrate_once`` for DB state."""
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def conn(db: AsyncEngine) -> AsyncGenerator[AsyncConnection, None]:
    """Per-test connection wrapped in a transaction that is always rolled back."""
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
async def test_index_exists_unique_and_partial(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_message_dedupe'"
            )
        )
        row = result.one_or_none()

    assert row is not None, "uq_notifications_message_dedupe must exist after upgrade head"
    indexdef = row[0]
    assert "UNIQUE" in indexdef
    assert "WHERE" in indexdef  # partial
    assert "message_id" in indexdef
    assert "type" in indexdef


# ---------------------------------------------------------------------------
# 2. NULL semantics — rows with no message_id key never collide
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_null_message_id_rows_never_collide(conn: AsyncConnection) -> None:
    """A payload with no `message_id` key extracts SQL NULL via `->>` --
    ordinary SQL NULL semantics mean a Postgres unique index never treats
    two NULLs as equal, so any number of such rows coexist without
    colliding."""
    landlord_id = await _insert_landlord(conn)

    for _ in range(3):
        result = await conn.execute(
            _INSERT_NOTIFICATION_ON_CONFLICT_SQL,
            {
                "landlord_id": landlord_id,
                "type": "emergency_call",
                "channel": "voice",
                "payload": json.dumps({"note": "no message_id key here"}),
            },
        )
        row = result.mappings().one_or_none()
        assert row is not None, "a payload with no message_id must never conflict"

    count = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM notifications "
                "WHERE landlord_id = :lid AND type = 'emergency_call'"
            ),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 3


# ---------------------------------------------------------------------------
# 3. Partial scope — other notification types are unaffected
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_other_notification_types_not_deduped(conn: AsyncConnection) -> None:
    """The partial index only covers emergency_call/needs_eyes -- e.g.
    draft_ready rows with the SAME message_id may repeat freely (no
    ON CONFLICT inference applies to them at all)."""
    landlord_id = await _insert_landlord(conn)
    message_id = str(uuid.uuid4())
    payload = json.dumps({"message_id": message_id})

    for _ in range(2):
        await conn.execute(
            text(
                "INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload) "
                "VALUES (:landlord_id, NULL, 'draft_ready', 'push', 'pending', "
                "CAST(:payload AS jsonb))"
            ),
            {"landlord_id": landlord_id, "payload": payload},
        )

    count = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM notifications "
                "WHERE landlord_id = :lid AND type = 'draft_ready'"
            ),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 2


# ---------------------------------------------------------------------------
# 4. ON CONFLICT inference — sequential proof (single connection)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_on_conflict_inference_sequential(conn: AsyncConnection) -> None:
    """The exact ON CONFLICT clause the webhook handler uses must be
    correctly inferred against this index: first insert for a
    (message_id, type) pair succeeds, a second attempt for the SAME pair
    conflicts and returns no row."""
    landlord_id = await _insert_landlord(conn)
    message_id = str(uuid.uuid4())
    payload = json.dumps({"message_id": message_id})
    params = {
        "landlord_id": landlord_id,
        "type": "emergency_call",
        "channel": "voice",
        "payload": payload,
    }

    r1 = await conn.execute(_INSERT_NOTIFICATION_ON_CONFLICT_SQL, params)
    row1 = r1.mappings().one_or_none()
    assert row1 is not None

    r2 = await conn.execute(_INSERT_NOTIFICATION_ON_CONFLICT_SQL, params)
    row2 = r2.mappings().one_or_none()
    assert row2 is None

    count = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM notifications "
                "WHERE landlord_id = :lid AND type = 'emergency_call'"
            ),
            {"lid": landlord_id},
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.integration
async def test_on_conflict_inference_different_types_do_not_collide(
    conn: AsyncConnection,
) -> None:
    """The dedupe key is (message_id, type) -- the SAME message_id with a
    DIFFERENT type (emergency_call vs needs_eyes) must not collide (the
    two are legitimately different artifacts for the same message in
    principle, and the index is defined on the pair, not message_id
    alone)."""
    landlord_id = await _insert_landlord(conn)
    message_id = str(uuid.uuid4())
    payload = json.dumps({"message_id": message_id})

    r1 = await conn.execute(
        _INSERT_NOTIFICATION_ON_CONFLICT_SQL,
        {
            "landlord_id": landlord_id,
            "type": "emergency_call",
            "channel": "voice",
            "payload": payload,
        },
    )
    r2 = await conn.execute(
        _INSERT_NOTIFICATION_ON_CONFLICT_SQL,
        {"landlord_id": landlord_id, "type": "needs_eyes", "channel": "push", "payload": payload},
    )

    assert r1.mappings().one_or_none() is not None
    assert r2.mappings().one_or_none() is not None


# ---------------------------------------------------------------------------
# 5. THE concurrency proof — two genuinely overlapping transactions
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_overlapping_inserts_exactly_one_wins(db: AsyncEngine) -> None:
    """Direct-SQL proof (the reviewer's repro shape): two GENUINELY
    overlapping transactions, each on its OWN independent connection,
    synchronized with an ``asyncio.Barrier`` so both reach their INSERT
    at effectively the same instant, each attempt to insert an
    emergency_call notification for the SAME message_id. Postgres's own
    unique-index enforcement (not application logic, which an earlier
    revision relied on and which a safety review proved unsafe here)
    guarantees exactly one wins, regardless of how the two transactions
    interleave.

    Cleans up explicitly (does not use the rollback-only ``conn``
    fixture): both attempts must actually COMMIT for one to genuinely
    conflict against the other's committed row.
    """
    landlord_id: str | None = None
    async with db.connect() as setup_conn:
        trans = await setup_conn.begin()
        landlord_id = await _insert_landlord(setup_conn)
        await trans.commit()

    message_id = str(uuid.uuid4())
    payload = json.dumps({"message_id": message_id})
    params = {
        "landlord_id": landlord_id,
        "type": "emergency_call",
        "channel": "voice",
        "payload": payload,
    }

    barrier = asyncio.Barrier(2)

    async def _attempt() -> str | None:
        async with db.connect() as connection:
            trans = await connection.begin()
            try:
                # Both coroutines wait here until BOTH have opened their
                # transaction -- guarantees genuine overlap, not accidental
                # serialization from one finishing before the other starts.
                await barrier.wait()
                result = await connection.execute(_INSERT_NOTIFICATION_ON_CONFLICT_SQL, params)
                row = result.mappings().one_or_none()
                await trans.commit()
                return str(row["id"]) if row is not None else None
            except Exception:
                await trans.rollback()
                raise

    try:
        results = await asyncio.gather(_attempt(), _attempt())

        winners = [r for r in results if r is not None]
        assert len(winners) == 1, f"expected exactly one winner, got {results}"

        async with db.connect() as verify_conn:
            count = (
                await verify_conn.execute(
                    text(
                        "SELECT COUNT(*) FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'emergency_call'"
                    ),
                    {"lid": landlord_id},
                )
            ).scalar_one()
            assert count == 1, (
                f"exactly one row should be persisted despite two concurrent attempts, got {count}"
            )
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
# 6. Downgrade to 0005 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0005_drops_index(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0005"))

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_message_dedupe'"
            )
        )
        assert not result.fetchall(), "index should be dropped after downgrade to 0005"


@pytest.mark.integration
async def test_reupgrade_restores_index(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'notifications' "
                "AND indexname = 'uq_notifications_message_dedupe'"
            )
        )
        assert result.fetchall(), "index should exist again after re-upgrade to head"
