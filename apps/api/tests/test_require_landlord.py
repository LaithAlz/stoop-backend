"""Integration tests for the ``require_landlord`` dependency (#22).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Per issue #22's design ("prefer testing the dependency directly"), these
tests call ``app.deps.require_landlord`` as a plain async function —
FastAPI's ``Depends(...)`` wiring is just a type-hint annotation, not
something that needs an HTTP request/JWT round-trip to exercise. No new
internal test route was added (none was needed): a real ``AuthUser`` (the
same object ``require_user``/``verify_jwt`` would hand it in production)
and a real ``AsyncSession`` are enough to call it directly.

Covers:
1. Soft-deleted landlord (``deleted_at`` set) -> 403 ``account_deleted``.
2. No landlords row at all for this ``auth_user_id`` -> the same 403
   ``account_deleted`` (the two cases collapse deliberately — see the
   ``require_landlord`` docstring in ``app/deps.py``).
3. Happy path -> returns ``(Landlord, session)``, and
   ``current_setting('app.current_landlord_id', true)`` reads back the
   landlord's id on that SAME session (SET LOCAL semantics via
   ``set_config``).
4. A session that never went through ``require_landlord`` has the GUC
   unset (``current_setting(..., true)`` is NULL) — the fail-closed
   default this dependency is the only thing that ever changes. (Full RLS
   *enforcement* proof — a query genuinely returning zero rows — lives in
   ``tests/test_rls_isolation.py``, which connects as ``app_role``; this
   test is scoped to what ``require_landlord`` itself is responsible for.)

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_require_landlord.py -m integration -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.deps import Landlord, require_landlord
from app.errors import AppError
from app.integrations.supabase_auth import AuthUser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0005)."""
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    # Leave schema in place; CI drops the DB container after the run.


@pytest_asyncio.fixture
async def db_engine(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """A plain AsyncSession — stands in for the one ``get_session`` yields.

    Rolled back at teardown so no test leaves rows behind.
    """
    async with AsyncSession(db_engine) as sess:
        try:
            yield sess
        finally:
            await sess.rollback()


async def _insert_landlord(
    session: AsyncSession,
    *,
    auth_user_id: str,
    soft_deleted: bool = False,
) -> str:
    """Insert a landlords row and return its id. Does NOT commit — the
    caller's session is rolled back at teardown by the ``session`` fixture.

    ``deleted_at`` is always a bind parameter (a Python ``datetime`` or
    ``None``), never conditionally-built SQL text, to keep this a plain
    parameterized INSERT regardless of which case is being seeded.
    """
    landlord_id = str(uuid.uuid4())
    deleted_at = datetime.now(UTC) if soft_deleted else None
    await session.execute(
        text(
            "INSERT INTO landlords (id, auth_user_id, email, deleted_at) "
            "VALUES (:id, :auth_user_id, :email, :deleted_at)"
        ),
        {
            "id": landlord_id,
            "auth_user_id": auth_user_id,
            "email": f"{landlord_id}@example.com",
            "deleted_at": deleted_at,
        },
    )
    return landlord_id


def _auth_user(auth_user_id: str) -> AuthUser:
    return AuthUser(user_id=uuid.UUID(auth_user_id), email="test@example.com", full_name="Test")


# ---------------------------------------------------------------------------
# 1. Soft-deleted landlord -> 403 account_deleted
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_soft_deleted_landlord_returns_403_account_deleted(session: AsyncSession) -> None:
    auth_user_id = str(uuid.uuid4())
    await _insert_landlord(session, auth_user_id=auth_user_id, soft_deleted=True)

    with pytest.raises(AppError) as exc_info:
        await require_landlord(_auth_user(auth_user_id), session)

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "account_deleted"


# ---------------------------------------------------------------------------
# 2. No landlords row at all -> the same 403 account_deleted
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_missing_landlord_row_returns_403_account_deleted(session: AsyncSession) -> None:
    """No landlords row exists for this auth_user_id at all (never
    provisioned) -- collapses to the same account_deleted response as a
    soft-deleted row. See the require_landlord docstring for why."""
    auth_user_id = str(uuid.uuid4())

    with pytest.raises(AppError) as exc_info:
        await require_landlord(_auth_user(auth_user_id), session)

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "account_deleted"


# ---------------------------------------------------------------------------
# 3. Happy path -> (Landlord, session), GUC set on that session
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_happy_path_returns_landlord_and_sets_guc(session: AsyncSession) -> None:
    auth_user_id = str(uuid.uuid4())
    landlord_id = await _insert_landlord(session, auth_user_id=auth_user_id)

    landlord, returned_session = await require_landlord(_auth_user(auth_user_id), session)

    assert isinstance(landlord, Landlord)
    assert str(landlord.id) == landlord_id
    assert returned_session is session

    guc_value = (
        await session.execute(text("SELECT current_setting('app.current_landlord_id', true)"))
    ).scalar_one()
    assert guc_value == landlord_id


# ---------------------------------------------------------------------------
# 4. A session that never went through require_landlord has the GUC unset
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_session_without_require_landlord_has_guc_unset(session: AsyncSession) -> None:
    """Nothing else in the app sets ``app.current_landlord_id`` — a fresh
    session (never passed through ``require_landlord``) must read it back
    as NULL, the fail-closed default RLS keys off."""
    guc_value = (
        await session.execute(text("SELECT current_setting('app.current_landlord_id', true)"))
    ).scalar_one()
    assert guc_value is None
