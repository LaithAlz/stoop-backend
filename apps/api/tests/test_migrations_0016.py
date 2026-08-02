"""Migration 0016 (#184 item 4) — `uq_audit_message_received_dedupe`.

Pins: the index exists with the exact expression + predicate the
`graph_entry.py` ON CONFLICT inference depends on; two GENUINELY
CONCURRENT message_received inserts collapse to one row (the house
Recipe-2 race shape, mirroring tests/test_migrations_0006.py); the
downgrade drops the index cleanly. MUTANT KILLED by the race test:
reverting graph_entry's INSERT to the old WHERE NOT EXISTS form (or
dropping the index) lets both concurrent inserts land — two rows.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agent.graph_entry import _INSERT_RECEIVED_IF_NOT_EXISTS_SQL

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", _DB_URL_DEFAULT)
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


@pytest.mark.integration
async def test_index_exists_with_exact_expression_and_predicate() -> None:
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'uq_audit_message_received_dedupe'"
                    )
                )
            ).scalar_one_or_none()
        assert row is not None, "migration 0016's index is missing"
        assert "UNIQUE" in row
        assert "payload ->> 'message_id'" in row.replace("(", "").replace(")", "") or (
            "payload ->> 'message_id'::text" in row
        )
        assert "message_received" in row
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_concurrent_message_received_inserts_exactly_one_row() -> None:
    """Recipe-2 race — GENUINELY concurrent (#184 safety review B4: the
    first draft gathered two inserts on ONE lazily-connecting engine,
    which serializes — even the old WHERE NOT EXISTS form passed). Now:
    two SEPARATE engines, both pre-warmed, an asyncio.Barrier holding both
    transactions open at the pre-INSERT line so conflict arbitration
    happens with both uncommitted. The INDEX (not application logic) wins.
    MUTANT KILLED: reverting graph_entry's INSERT to WHERE NOT EXISTS
    yields two rows under this barrier."""
    engine_a = create_async_engine(_db_url())
    engine_b = create_async_engine(_db_url())
    landlord_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    barrier = asyncio.Barrier(2)
    try:
        async with engine_a.begin() as conn:
            await conn.execute(
                text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth, :email)"),
                {"id": landlord_id, "auth": str(uuid.uuid4()), "email": "race@test.local"},
            )
        for eng in (engine_a, engine_b):
            async with eng.connect() as conn:
                await conn.execute(text("SELECT 1"))

        async def _insert(eng: object) -> None:
            async with eng.begin() as conn:  # type: ignore[attr-defined]
                await barrier.wait()  # both txns open, neither committed
                await conn.execute(
                    _INSERT_RECEIVED_IF_NOT_EXISTS_SQL,
                    {"landlord_id": landlord_id, "message_id": message_id},
                )

        await asyncio.gather(_insert(engine_a), _insert(engine_b))

        async with engine_a.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM audit_log "
                        "WHERE action = 'message_received' "
                        "AND payload ->> 'message_id' = :mid"
                    ),
                    {"mid": message_id},
                )
            ).scalar_one()
        assert count == 1
    finally:
        async with engine_a.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM audit_log WHERE payload ->> 'message_id' = :mid "
                    "AND action = 'message_received'"
                ),
                {"mid": message_id},
            )
            await conn.execute(text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id})
        await engine_a.dispose()
        await engine_b.dispose()


@pytest.mark.integration
async def test_downgrade_to_0015_drops_index_and_reupgrade_restores() -> None:
    """Round-trip (the 0006 precedent this migration claims): downgrade
    drops `uq_audit_message_received_dedupe`, re-upgrade restores it.
    Serialized via subprocess alembic, same as the sibling suites."""
    import subprocess
    import sys

    def _alembic(*args: str) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", *args],
            capture_output=True,
            text=True,
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            env={**os.environ, "DATABASE_URL": _db_url()},
        )
        if result.returncode != 0:
            raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stderr}")

    engine = create_async_engine(_db_url())

    async def _index_exists() -> bool:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT 1 FROM pg_indexes "
                        "WHERE indexname = 'uq_audit_message_received_dedupe'"
                    )
                )
            ).scalar_one_or_none() is not None

    try:
        assert await _index_exists()  # head state
        _alembic("downgrade", "0015")
        assert not await _index_exists()
        _alembic("upgrade", "head")
        assert await _index_exists()
    finally:
        await engine.dispose()
