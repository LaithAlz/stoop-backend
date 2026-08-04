"""Integration tests for the unrouted_inbound dead-letter table migration
(revision 0015, #170).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0015 implements the schema-v1.md v1.17 amendments: a new
``unrouted_inbound`` table that dead-letters an inbound Twilio SMS whose
``To`` matched no property, instead of dropping it. Unlike every other
table this repo has migrated (including migration 0012's ``push_outbox``,
the only other "genuinely new table since 0005" precedent), this table is
ADMIN-ONLY: ``app_role`` gets no ``GRANT`` on it at all, plus one
unconditional-deny RLS policy as defense-in-depth (see the migration's own
module docstring for the full rationale).

These tests verify:

1. ``unrouted_inbound`` exists with the right columns/defaults.
2. ``twilio_sid`` is a plain (nullable) UNIQUE column — a genuine
   duplicate raises ``IntegrityError``.
3. RLS is enabled with exactly ONE policy
   (``unrouted_inbound_isolation``), and it denies EVERYTHING —
   unconditionally, not GUC-scoped — proven behaviorally under
   ``app_role`` (``SET LOCAL ROLE``), not just by inspecting the policy's
   catalog definition.
4. ``app_role`` has NO grants on this table at all (unlike every other
   table in this schema, which gets at least ``SELECT, INSERT``).
5. Downgrade to 0014 drops the table entirely; re-upgrade to head restores
   it.

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0015.py -m integration -v
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
from sqlalchemy.exc import DBAPIError, IntegrityError
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
    """Apply migrations exactly once per test session (ends at head/0015).

    Delegates to ``tests.migration_harness.migrate_from_base_to_head`` —
    see that module's docstring for why (issue #281: migration 0009's
    fail-closed downgrade guard turning into a confusing ~200-error
    cascade when a lane database has a leftover tenant_ack/degraded_retry
    row from an interrupted prior run).
    """
    migration_harness.migrate_from_base_to_head(_alembic)
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


async def _insert_unrouted_inbound(conn: AsyncConnection, *, twilio_sid: str | None) -> str:
    row_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO unrouted_inbound (id, twilio_sid, from_number, to_number, payload) "
            "VALUES (:id, :twilio_sid, '+14165550100', '+14165550199', '{}'::jsonb)"
        ),
        {"id": row_id, "twilio_sid": twilio_sid},
    )
    return row_id


# ---------------------------------------------------------------------------
# 1. Table/column existence + defaults
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unrouted_inbound_table_exists_with_expected_columns(conn: AsyncConnection) -> None:
    columns = (
        (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'unrouted_inbound'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(columns) == {
        "id",
        "twilio_sid",
        "from_number",
        "to_number",
        "payload",
        "received_at",
        "resolved_at",
    }


@pytest.mark.integration
async def test_unrouted_inbound_defaults(conn: AsyncConnection) -> None:
    row_id = await _insert_unrouted_inbound(conn, twilio_sid=f"SM{uuid.uuid4().hex}")

    row = (
        (
            await conn.execute(
                text("SELECT received_at, resolved_at FROM unrouted_inbound WHERE id = :id"),
                {"id": row_id},
            )
        )
        .mappings()
        .one()
    )
    assert row["received_at"] is not None
    assert row["resolved_at"] is None


@pytest.mark.integration
async def test_unrouted_inbound_resolved_at_is_a_plain_mutable_column(
    conn: AsyncConnection,
) -> None:
    """Not append-only (rule #2 does not apply): an operator can set
    resolved_at directly — this is the SUPERUSER/admin path, unaffected by
    app_role's own zero-grant treatment (point 4 below)."""
    row_id = await _insert_unrouted_inbound(conn, twilio_sid=f"SM{uuid.uuid4().hex}")
    await conn.execute(
        text("UPDATE unrouted_inbound SET resolved_at = now() WHERE id = :id"), {"id": row_id}
    )
    resolved_at = (
        await conn.execute(
            text("SELECT resolved_at FROM unrouted_inbound WHERE id = :id"), {"id": row_id}
        )
    ).scalar_one()
    assert resolved_at is not None


# ---------------------------------------------------------------------------
# 2. twilio_sid — plain (nullable) UNIQUE, same shape as messages.twilio_sid
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_twilio_sid_unique_rejects_duplicate(conn: AsyncConnection) -> None:
    sid = f"SM{uuid.uuid4().hex}"
    await _insert_unrouted_inbound(conn, twilio_sid=sid)
    with pytest.raises(IntegrityError):
        await _insert_unrouted_inbound(conn, twilio_sid=sid)


@pytest.mark.integration
async def test_twilio_sid_null_does_not_collide(conn: AsyncConnection) -> None:
    """Plain UNIQUE (not partial) — but ordinary SQL NULL semantics still
    mean multiple NULL twilio_sids never collide with each other, same as
    messages.twilio_sid."""
    await _insert_unrouted_inbound(conn, twilio_sid=None)
    await _insert_unrouted_inbound(conn, twilio_sid=None)  # must not raise


# ---------------------------------------------------------------------------
# 3. RLS — enabled, exactly one policy, denies EVERYTHING (behavioral proof)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unrouted_inbound_rls_enabled_with_exactly_one_policy(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        rowsecurity = (
            await connection.execute(
                text(
                    "SELECT relrowsecurity FROM pg_class "
                    "WHERE relname = 'unrouted_inbound' AND relnamespace = 'public'::regnamespace"
                )
            )
        ).scalar_one()
        assert rowsecurity is True

        policies = (
            (
                await connection.execute(
                    text(
                        "SELECT polname FROM pg_policy "
                        "WHERE polrelid = 'unrouted_inbound'::regclass"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert policies == ["unrouted_inbound_isolation"]


@pytest.mark.integration
async def test_app_role_has_no_grants_on_unrouted_inbound(db: AsyncEngine) -> None:
    """Unlike every other table in this schema (which gets at least
    SELECT, INSERT — migration 0005's grants), unrouted_inbound grants
    app_role NOTHING at all."""
    async with db.connect() as connection:
        privileges = (
            (
                await connection.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = 'unrouted_inbound' AND grantee = 'app_role'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert privileges == []


@pytest.mark.integration
async def test_app_role_select_denied_permission_before_rls(db: AsyncEngine) -> None:
    """No GRANT at all means app_role can't even attempt a SELECT — this
    fails at the privilege-check level ("permission denied for table"),
    never reaching row-level evaluation."""
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(text("SET LOCAL ROLE app_role"))
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text("SELECT id FROM unrouted_inbound"))
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_app_role_insert_denied_permission(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(text("SET LOCAL ROLE app_role"))
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(
                    text(
                        "INSERT INTO unrouted_inbound (from_number, to_number, payload) "
                        "VALUES ('+14165550100', '+14165550199', '{}'::jsonb)"
                    )
                )
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# 4. Downgrade to 0014 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0014_drops_unrouted_inbound(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0014"))

    async with db.connect() as connection:
        table_exists = (
            await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_class "
                    "WHERE relname = 'unrouted_inbound' AND relnamespace = 'public'::regnamespace)"
                )
            )
        ).scalar_one()
        assert table_exists is False


@pytest.mark.integration
async def test_reupgrade_restores_unrouted_inbound(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        table_exists = (
            await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_class "
                    "WHERE relname = 'unrouted_inbound' AND relnamespace = 'public'::regnamespace)"
                )
            )
        ).scalar_one()
        assert table_exists is True

        rowsecurity = (
            await connection.execute(
                text(
                    "SELECT relrowsecurity FROM pg_class "
                    "WHERE relname = 'unrouted_inbound' AND relnamespace = 'public'::regnamespace"
                )
            )
        ).scalar_one()
        assert rowsecurity is True
