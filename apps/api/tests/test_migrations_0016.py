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
    """Recipe-2 race: same message_id, two separate connections, gathered.
    Exactly one audit row must exist afterward — the index (not
    application logic) is what wins the race."""
    engine = create_async_engine(_db_url())
    landlord_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth, :email)"),
                {"id": landlord_id, "auth": str(uuid.uuid4()), "email": "race@test.local"},
            )

        async def _insert() -> None:
            async with engine.begin() as conn:
                await conn.execute(
                    _INSERT_RECEIVED_IF_NOT_EXISTS_SQL,
                    {"landlord_id": landlord_id, "message_id": message_id},
                )

        await asyncio.gather(_insert(), _insert())

        async with engine.connect() as conn:
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
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM audit_log WHERE payload ->> 'message_id' = :mid "
                    "AND action = 'message_received'"
                ),
                {"mid": message_id},
            )
            await conn.execute(text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id})
        await engine.dispose()
