"""Integration tests for the LangGraph checkpoint schema migration
(revision 0007).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0007 implements schema-v1.md's v1.4 amendments (#24): a dedicated
``langgraph`` schema, granted to no one (``PUBLIC``, ``app_role``, and —
guarded, live-Supabase-only — ``anon``/``authenticated`` all get an
explicit ``REVOKE ALL``), with no tables created by the migration itself
(``AsyncPostgresSaver.setup()`` creates those later, at application
startup — see ``tests/test_checkpointer.py`` for that round trip).

These tests verify:
1. The schema exists after ``upgrade head``.
2. ``app_role`` has no ``USAGE``/``CREATE`` privilege on it.
3. Guarded: if ``anon``/``authenticated`` exist (live Supabase only —
   never true locally/CI), they have no privilege either.
4. Downgrade to 0006 drops the schema; re-upgrade to head restores it
   (full round-trip).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0007.py -m integration -v
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# ---------------------------------------------------------------------------
# Helpers — duplicated (not imported) from tests/test_migrations_0006.py, to
# keep this module self-contained (established convention in this repo).
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
    """Per-test async engine; depends on ``_migrate_once`` for DB state."""
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


# ---------------------------------------------------------------------------
# 1. Schema existence
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_langgraph_schema_exists_after_upgrade_head(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT nspname FROM pg_namespace WHERE nspname = 'langgraph'")
        )
        assert result.one_or_none() is not None, "langgraph schema must exist after upgrade head"


# ---------------------------------------------------------------------------
# 2. app_role / PUBLIC have no privilege on the schema
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_role_has_no_privilege_on_langgraph_schema(db: AsyncEngine) -> None:
    """The checkpoint schema is RLS-free/admin-only BY CONSTRUCTION —
    app_role (the request-path role, #22) must hold neither USAGE nor
    CREATE on it."""
    async with db.connect() as connection:
        usage = (
            await connection.execute(
                text("SELECT has_schema_privilege('app_role', 'langgraph', 'USAGE')")
            )
        ).scalar_one()
        create = (
            await connection.execute(
                text("SELECT has_schema_privilege('app_role', 'langgraph', 'CREATE')")
            )
        ).scalar_one()

    assert usage is False, "app_role must not have USAGE on the langgraph schema"
    assert create is False, "app_role must not have CREATE on the langgraph schema"


@pytest.mark.integration
async def test_public_pseudo_role_has_no_privilege_on_langgraph_schema(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        usage = (
            await connection.execute(
                text("SELECT has_schema_privilege('public', 'langgraph', 'USAGE')")
            )
        ).scalar_one()

    assert usage is False, "the PUBLIC pseudo-role must not have USAGE on the langgraph schema"


# ---------------------------------------------------------------------------
# 3. Guarded: anon/authenticated (live Supabase only) have no privilege either
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_anon_authenticated_have_no_privilege_if_present(db: AsyncEngine) -> None:
    """anon/authenticated (PostgREST roles) exist only on live Supabase —
    never locally/CI. Guarded so this test is a genuine no-op (not a
    false-pass) in either environment."""
    async with db.connect() as connection:
        existing_roles = (
            (
                await connection.execute(
                    text("SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")
                )
            )
            .scalars()
            .all()
        )
        for role in existing_roles:
            usage = (
                await connection.execute(
                    text("SELECT has_schema_privilege(:role, 'langgraph', 'USAGE')"),
                    {"role": role},
                )
            ).scalar_one()
            assert usage is False, f"{role} must not have USAGE on the langgraph schema"


# ---------------------------------------------------------------------------
# 4. Downgrade to 0006 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0006_drops_schema(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0006"))

    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT nspname FROM pg_namespace WHERE nspname = 'langgraph'")
        )
        assert result.one_or_none() is None, "langgraph schema should be gone after downgrade"


@pytest.mark.integration
async def test_reupgrade_restores_schema(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT nspname FROM pg_namespace WHERE nspname = 'langgraph'")
        )
        assert result.one_or_none() is not None, "langgraph schema should exist again at head"
