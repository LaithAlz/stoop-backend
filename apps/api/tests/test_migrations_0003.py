"""Integration tests for the v1.1-amendments migration (revision 0003).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0003 implements the schema-v1.md v1.1 amendments (#151):

1. New append-only table ``message_status_events`` — Twilio delivery-status
   callbacks append here instead of ever touching ``messages``.
2. ``messages.party`` CHECK relaxed from ``('tenant','vendor')`` to
   ``('tenant','vendor','landlord')`` for approve-by-SMS (#122).
3. ``messages.twilio_status`` dropped (deprecated v1.1, no writer existed).

These tests verify (per docs/03-engineering/schema-v1.md, the canonical
source; helpers below duplicate the ``tests/test_migrations_core.py``
patterns rather than importing them, to keep this module self-contained):

1. ``message_status_events`` exists after ``upgrade head``; INSERT succeeds
   for every vocabulary status; an out-of-vocabulary status is rejected by
   the CHECK; a random (non-existent) ``message_id`` is rejected by the FK.
2. ``messages.party`` now accepts ``'landlord'`` and still rejects
   out-of-vocabulary values.
3. ``messages.twilio_status`` is gone (information_schema check).
4. The deferred-REVOKE append-only gate (rule #2) is documented in the 0003
   migration source for ``message_status_events``, same anchor-window
   pattern as ``test_append_only_revoke_gate_documented`` in
   ``test_migrations_core.py``.
5. Downgrade to 0002 restores ``twilio_status`` + the narrower ``party``
   CHECK and drops ``message_status_events``; re-upgrade to head restores
   the 0003 state (full round-trip).

Every test that touches data uses its own connection wrapped in an
explicit transaction that is always rolled back at teardown (the ``conn``
fixture) EXCEPT the round-trip test at the bottom, which must actually
mutate schema state (downgrade/upgrade) and therefore runs last, per the
same mutation-order caveat ``test_migrations_core.py`` documents for its
own downgrade/re-upgrade pair.

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0003.py -m integration -v
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# ---------------------------------------------------------------------------
# Helpers — duplicated (not imported) from tests/test_migrations_core.py, to
# keep this module self-contained (see module docstring).
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "0003_message_status_events.py"
)

_STATUS_VOCAB: list[str] = [
    "accepted",
    "queued",
    "sending",
    "sent",
    "delivered",
    "undelivered",
    "failed",
]


def _get_db_url() -> str:
    """Resolve and normalise the database URL."""
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop",
    )
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


def _alembic(*args: str) -> None:
    """Run an alembic sub-command synchronously via subprocess."""
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


def _window_after(content: str, anchor: str, size: int = 2000) -> str:
    """Return ``size`` characters of ``content`` starting at ``anchor``."""
    idx = content.index(anchor)
    return content[idx : idx + size]


# ---------------------------------------------------------------------------
# Session-scoped synchronous setup (avoids pytest-asyncio scope-mismatch).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0003)."""
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
    """Per-test connection wrapped in a transaction that is always rolled back.

    Nothing here is ever committed, so no test leaves rows behind and no
    test ever issues UPDATE/DELETE against the append-only
    ``messages``/``message_status_events`` tables.
    """
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            yield connection
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# Row-builder helpers (FK chain: landlord -> property -> message)
# ---------------------------------------------------------------------------


async def _insert_landlord(conn: AsyncConnection) -> str:
    landlord_id = str(uuid.uuid4())
    await conn.execute(
        text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"),
        {"id": landlord_id, "auth_id": str(uuid.uuid4()), "email": f"{landlord_id}@example.com"},
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


async def _insert_message(
    conn: AsyncConnection,
    landlord_id: str,
    property_id: str,
    *,
    party: str = "tenant",
    twilio_sid: str | None = None,
) -> str:
    message_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO messages (id, landlord_id, property_id, direction, party, "
            "body, twilio_sid) "
            "VALUES (:id, :landlord_id, :property_id, 'inbound', :party, "
            "'test message body', :sid)"
        ),
        {
            "id": message_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "party": party,
            "sid": twilio_sid,
        },
    )
    return message_id


async def _insert_status_event(
    conn: AsyncConnection, message_id: str, *, status: str = "queued"
) -> None:
    await conn.execute(
        text(
            "INSERT INTO message_status_events (message_id, status) VALUES (:message_id, :status)"
        ),
        {"message_id": message_id, "status": status},
    )


async def _make_message(conn: AsyncConnection) -> str:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    return await _insert_message(conn, landlord_id, property_id)


# ---------------------------------------------------------------------------
# 1. message_status_events — existence, vocabulary, CHECK, FK
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_message_status_events_table_exists(db: AsyncEngine) -> None:
    """message_status_events must exist in public schema after upgrade head."""
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'message_status_events'"
            )
        )
        rows = result.fetchall()
    assert len(rows) == 1, "message_status_events table not found in public schema"


@pytest.mark.integration
async def test_message_status_events_accepts_every_vocab_status(conn: AsyncConnection) -> None:
    """INSERT must succeed for every status in the vocabulary."""
    message_id = await _make_message(conn)

    for status in _STATUS_VOCAB:
        await _insert_status_event(conn, message_id, status=status)

    result = await conn.execute(
        text("SELECT count(*) FROM message_status_events WHERE message_id = :message_id"),
        {"message_id": message_id},
    )
    assert result.scalar() == len(_STATUS_VOCAB)


@pytest.mark.integration
async def test_message_status_events_status_check_rejects_invalid(conn: AsyncConnection) -> None:
    """An out-of-vocabulary status must violate the CHECK constraint."""
    message_id = await _make_message(conn)

    with pytest.raises(IntegrityError, match="check"):
        await _insert_status_event(conn, message_id, status="bogus_status")


@pytest.mark.integration
async def test_message_status_events_fk_enforced(conn: AsyncConnection) -> None:
    """A random, non-existent message_id must violate the FK constraint."""
    with pytest.raises(IntegrityError, match="foreign key|violates"):
        await _insert_status_event(conn, str(uuid.uuid4()), status="queued")


# ---------------------------------------------------------------------------
# 2. messages.party — 'landlord' accepted, out-of-vocabulary still rejected
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_messages_party_accepts_landlord(conn: AsyncConnection) -> None:
    """party CHECK must accept 'landlord' (v1.1, approve-by-SMS #122)."""
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)

    await _insert_message(conn, landlord_id, property_id, party="landlord")


@pytest.mark.integration
async def test_messages_party_rejects_invalid(conn: AsyncConnection) -> None:
    """party CHECK must still reject values outside the vocabulary."""
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)

    with pytest.raises(IntegrityError, match="check"):
        await _insert_message(conn, landlord_id, property_id, party="bogus_party")


# ---------------------------------------------------------------------------
# 3. messages.twilio_status — column is gone
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_messages_twilio_status_column_gone(db: AsyncEngine) -> None:
    """twilio_status must no longer exist on messages (dropped, deprecated v1.1)."""
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'messages' "
                "AND column_name = 'twilio_status'"
            )
        )
        rows = result.fetchall()
    assert not rows, "messages.twilio_status should have been dropped in 0003"


# ---------------------------------------------------------------------------
# 4. Append-only gate documentation (rule #2) for message_status_events
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_append_only_revoke_gate_documented_for_status_events(
    conn: AsyncConnection,
) -> None:
    """message_status_events accepts INSERT (the only sanctioned write path),
    and the 0003 migration source must carry the deferred-REVOKE gate
    comment near its CREATE TABLE — so it can't silently disappear in a
    refactor before #22 closes it. Actual REVOKE enforcement is NOT tested:
    the app role doesn't exist in local Postgres (documented deferral).
    """
    message_id = await _make_message(conn)
    await _insert_status_event(conn, message_id, status="queued")

    content = _MIGRATION_PATH.read_text()
    window = _window_after(content, "CREATE TABLE message_status_events")
    assert "REVOKE" in window
    assert "DEFERRED" in window


# ---------------------------------------------------------------------------
# 5. Downgrade to 0002 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0002_reverses_v1_1_amendments(db: AsyncEngine) -> None:
    """Downgrading to 0002 must restore twilio_status + the narrower party
    CHECK, and drop message_status_events."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0002"))

    async with db.connect() as connection:
        # twilio_status column restored.
        col_result = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'messages' "
                "AND column_name = 'twilio_status'"
            )
        )
        assert col_result.fetchall(), "twilio_status should be restored after downgrade to 0002"

        # message_status_events dropped.
        table_result = await connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'message_status_events'"
            )
        )
        assert not table_result.fetchall(), (
            "message_status_events should be dropped after downgrade to 0002"
        )

    # Narrower party CHECK: 'landlord' must now be rejected.
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            landlord_id = await _insert_landlord(connection)
            property_id = await _insert_property(connection, landlord_id)
            with pytest.raises(IntegrityError, match="check"):
                await _insert_message(connection, landlord_id, property_id, party="landlord")
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_reupgrade_restores_0003_state(db: AsyncEngine) -> None:
    """After downgrade to 0002 + re-upgrade to head, 0003 state is restored:
    twilio_status gone again, message_status_events back, 'landlord' party
    accepted again."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        col_result = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'messages' "
                "AND column_name = 'twilio_status'"
            )
        )
        assert not col_result.fetchall(), "twilio_status should be gone again after re-upgrade"

        table_result = await connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'message_status_events'"
            )
        )
        assert table_result.fetchall(), (
            "message_status_events should exist again after re-upgrade to head"
        )

    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            landlord_id = await _insert_landlord(connection)
            property_id = await _insert_property(connection, landlord_id)
            await _insert_message(connection, landlord_id, property_id, party="landlord")
        finally:
            await trans.rollback()
