"""Integration smoke test for the LangGraph checkpointer (#24).

Marker: ``integration`` — requires a running Postgres instance AND the
``DATABASE_URL`` env var exported (``app.config.settings`` reads it once at
import time; ``app/agent/checkpointer.py`` uses that same
``settings.database_url``, never its own re-read of the env var — see that
module's docstring). Matches the CI job's env-level ``DATABASE_URL``
(``.github/workflows/ci.yml``) and this repo's other integration test
modules' default local fallback.

Use ``docker compose up -d`` at the repo root, then:
    export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop
    uv run pytest tests/test_checkpointer.py -m integration -v

Verifies (#24 acceptance criteria):
1. ``setup_checkpointer()`` creates the library's four tables
   (``checkpoints``, ``checkpoint_blobs``, ``checkpoint_writes``,
   ``checkpoint_migrations``) inside the dedicated ``langgraph`` schema —
   never ``public``.
2. Idempotent: calling it twice does not raise.
3. A checkpoint written via the saver API can be read back byte-for-byte
   (same checkpoint id) through the SAME thread_id.
4. The ``public`` schema gains ZERO new tables — the #23 isolation gate
   (``tests/test_rls_isolation_matrix.py``'s
   ``test_no_tables_outside_descriptor_set_exist_in_public_schema``) must
   stay green.
5. ``app_role`` has no privilege on the ``langgraph`` schema even after
   the tables exist (migration-time REVOKEs are not merely a snapshot of
   an empty schema).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.agent.checkpointer import get_checkpointer, setup_checkpointer

# ---------------------------------------------------------------------------
# Helpers — duplicated (not imported) from other integration test modules,
# per this repo's established "self-contained test module" convention.
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


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0007)."""
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    # Leave schema in place; CI drops the DB container after the run.


@pytest_asyncio.fixture
async def db(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    """Per-test admin (superuser) engine for catalog assertions — a
    SEPARATE connection/driver from the checkpointer's own psycopg pool
    (see app/agent/checkpointer.py's module docstring for why they must be
    different)."""
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


# ---------------------------------------------------------------------------
# 1 & 2. Table creation, inside langgraph (never public), idempotent
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_setup_checkpointer_creates_tables_in_langgraph_schema(db: AsyncEngine) -> None:
    await setup_checkpointer()

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relname FROM pg_class "
                "WHERE relnamespace = 'langgraph'::regnamespace AND relkind = 'r'"
            )
        )
        tables = {row[0] for row in result.fetchall()}

    expected = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
    assert expected <= tables, f"expected {expected} to all exist in langgraph, got {tables}"


@pytest.mark.integration
async def test_setup_checkpointer_is_idempotent() -> None:
    await setup_checkpointer()
    await setup_checkpointer()  # must not raise the second time


# ---------------------------------------------------------------------------
# 3. Write + restore round trip via the saver API
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_checkpoint_write_and_restore_round_trip() -> None:
    await setup_checkpointer()
    saver = get_checkpointer()

    thread_id = f"test-thread-{uuid.uuid4()}"
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    metadata: dict[str, Any] = {}
    new_versions: dict[str, Any] = {}

    next_config = await saver.aput(config, checkpoint, metadata, new_versions)
    restored = await saver.aget_tuple(next_config)

    assert restored is not None, "a just-written checkpoint must be readable back"
    assert restored.checkpoint["id"] == checkpoint["id"]
    assert restored.config["configurable"]["thread_id"] == thread_id


# ---------------------------------------------------------------------------
# 4. The #23 gate stays green — public gains zero tables
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_checkpointer_setup_does_not_touch_public_schema(db: AsyncEngine) -> None:
    """tests/test_rls_isolation_matrix.py's
    test_no_tables_outside_descriptor_set_exist_in_public_schema depends on
    this staying true — checkpoint tables must NEVER leak into public."""
    await setup_checkpointer()

    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relname FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                "AND relname IN "
                "('checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'checkpoint_migrations')"
            )
        )
        assert not result.fetchall(), "checkpoint tables must never exist in the public schema"


# ---------------------------------------------------------------------------
# 5. app_role still has no privilege once the tables actually exist
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_role_has_no_privilege_after_tables_exist(db: AsyncEngine) -> None:
    await setup_checkpointer()

    async with db.connect() as connection:
        usage = (
            await connection.execute(
                text("SELECT has_schema_privilege('app_role', 'langgraph', 'USAGE')")
            )
        ).scalar_one()
        table_select = (
            await connection.execute(
                text("SELECT has_table_privilege('app_role', 'langgraph.checkpoints', 'SELECT')")
            )
        ).scalar_one()

    assert usage is False, "app_role must not have USAGE on langgraph even once tables exist"
    assert table_select is False, "app_role must not have SELECT on langgraph.checkpoints"
