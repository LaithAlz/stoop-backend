"""Integration tests for the ``require_landlord`` dependency (#22; two-session
fix per the #54/#55/#57 spec review — see ``app/deps.py``'s module docstring
"Two-session rationale").

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
   default this dependency is the only thing that ever changes.
5. **The regression pin for the two-session fix**: ``require_landlord``
   resolves the landlord and sets the GUC correctly even when the CALLER's
   session is genuinely subject to RLS (``SET LOCAL ROLE app_role``, the
   same technique ``tests/test_rls_isolation.py`` uses — see that module's
   docstring for why a superuser can ``SET ROLE`` without prior
   membership). Before the fix, this exact scenario 403'd
   ``account_deleted`` for a real, live, committed landlord row, every
   time, because the ``landlords`` lookup ran on the SAME app_role-scoped
   transaction the GUC hadn't been set on yet — ``landlords``' RLS policy
   is ``id = current_setting('app.current_landlord_id', true)::uuid``, and
   an unset GUC reads back as SQL ``NULL``, matching zero rows. Manually
   verified red under the pre-fix code (via ``git stash``) and green under
   the fix — see this PR's report for the transcript.

Cross-loop pool hazard note: tests below that use ``require_landlord`` now
touch the module-level ADMIN engine (``app.db.session.engine``, via
``get_admin_session`` — the two-session fix) IN ADDITION to each test's own
``db_engine`` fixture. ``asyncio_default_fixture_loop_scope = "function"``
means every test gets its own event loop; a connection pooled by the
module-level singleton engine during one test is bound to that test's
(now-closed) loop, and reusing it from a later test's new loop raises
``RuntimeError: got Future ... attached to a different loop`` (the exact
failure class ``tests/test_me.py``'s ``dispose_app_engine`` fixture already
guards against). ``_dispose_admin_engine`` below is that same guard,
reproduced here for the same reason.

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

import app.db.session as db_session_mod
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


@pytest_asyncio.fixture(autouse=True)
async def _dispose_admin_engine() -> AsyncGenerator[None, None]:
    """Dispose the module-level admin engine before/after each test — see
    module docstring's "Cross-loop pool hazard note"."""
    await db_session_mod.engine.dispose()
    yield
    await db_session_mod.engine.dispose()


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


async def _insert_landlord_committed(
    db_engine: AsyncEngine,
    *,
    auth_user_id: str,
    soft_deleted: bool = False,
) -> str:
    """Insert a landlords row on its OWN connection and COMMIT it.

    Committing (rather than the old rollback-only pattern) is now required:
    the two-session fix means ``require_landlord``'s lookup runs on a
    SEPARATE admin session/connection from the caller's own ``session``
    fixture — an uncommitted write on one connection is invisible to any
    other connection, by ordinary transaction-isolation semantics, so the
    row must be durably committed for that lookup to ever see it (exactly
    like a real request would: the landlord row was provisioned and
    committed in some earlier request entirely). Callers MUST clean up via
    ``_delete_landlord`` in a ``finally`` block.
    """
    landlord_id = str(uuid.uuid4())
    deleted_at = datetime.now(UTC) if soft_deleted else None
    async with db_engine.connect() as connection:
        trans = await connection.begin()
        await connection.execute(
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
        await trans.commit()
    return landlord_id


async def _delete_landlord(db_engine: AsyncEngine, landlord_id: str) -> None:
    async with db_engine.connect() as connection:
        trans = await connection.begin()
        await connection.execute(text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id})
        await trans.commit()


def _auth_user(auth_user_id: str) -> AuthUser:
    return AuthUser(user_id=uuid.UUID(auth_user_id), email="test@example.com", full_name="Test")


# ---------------------------------------------------------------------------
# 1. Soft-deleted landlord -> 403 account_deleted
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_soft_deleted_landlord_returns_403_account_deleted(
    db_engine: AsyncEngine, session: AsyncSession
) -> None:
    auth_user_id = str(uuid.uuid4())
    landlord_id = await _insert_landlord_committed(
        db_engine, auth_user_id=auth_user_id, soft_deleted=True
    )
    try:
        with pytest.raises(AppError) as exc_info:
            await require_landlord(_auth_user(auth_user_id), session)

        assert exc_info.value.status_code == 403
        assert exc_info.value.code == "account_deleted"
    finally:
        await _delete_landlord(db_engine, landlord_id)


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
async def test_happy_path_returns_landlord_and_sets_guc(
    db_engine: AsyncEngine, session: AsyncSession
) -> None:
    auth_user_id = str(uuid.uuid4())
    landlord_id = await _insert_landlord_committed(db_engine, auth_user_id=auth_user_id)
    try:
        landlord, returned_session = await require_landlord(_auth_user(auth_user_id), session)

        assert isinstance(landlord, Landlord)
        assert str(landlord.id) == landlord_id
        assert returned_session is session

        guc_value = (
            await session.execute(text("SELECT current_setting('app.current_landlord_id', true)"))
        ).scalar_one()
        assert guc_value == landlord_id
    finally:
        await _delete_landlord(db_engine, landlord_id)


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


# ---------------------------------------------------------------------------
# 5. THE REGRESSION PIN — require_landlord under REAL RLS enforcement
# (SET LOCAL ROLE app_role, the tests/test_rls_isolation.py technique).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_require_landlord_resolves_under_real_rls_enforcement(
    db_engine: AsyncEngine,
) -> None:
    """The authoritative proof for the two-session fix (app/deps.py).

    Seeds a committed landlord row on an ordinary (superuser) connection,
    then calls ``require_landlord`` with a caller ``session`` that is
    GENUINELY ``app_role``-scoped (``SET LOCAL ROLE app_role`` on its own
    connection/transaction, with the GUC deliberately still UNSET at call
    time — exactly the state a fresh request-path session is in). Before
    the fix, ``require_landlord``'s ``landlords`` lookup ran on this SAME
    app_role-scoped, GUC-unset transaction and was rejected by RLS (zero
    rows, since ``id = current_setting(..., true)::uuid`` can never match
    while the GUC is NULL) — this test would have 403'd account_deleted for
    a real landlord, exactly the production bug found in spec review.
    After the fix, the lookup runs on a separate ADMIN session and always
    succeeds; the GUC is then set on THIS app_role-scoped session/
    connection, verified by reading it back on the SAME connection.

    Manually confirmed red against the pre-fix single-session code (via a
    local ``git stash`` of the ``app/deps.py`` fix) and green against the
    fix — see this PR's report for the transcript; this test is what stays
    in the suite permanently.
    """
    auth_user_id = str(uuid.uuid4())
    landlord_id = str(uuid.uuid4())

    # Seed + commit on an ordinary (superuser) connection — durably visible
    # to any later transaction, including require_landlord's own internal
    # admin-session lookup.
    async with db_engine.connect() as seed_connection:
        trans = await seed_connection.begin()
        await seed_connection.execute(
            text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth, :email)"),
            {"id": landlord_id, "auth": auth_user_id, "email": f"{landlord_id}@example.com"},
        )
        await trans.commit()

    try:
        async with db_engine.connect() as connection:
            trans = await connection.begin()
            try:
                # From here on, current_user is app_role for the rest of
                # this transaction — genuinely subject to the landlords
                # RLS policy, not the local superuser bypass.
                await connection.execute(text("SET LOCAL ROLE app_role"))

                # Wrap this already-app_role-scoped connection in a plain
                # AsyncSession — exactly what get_session hands a real
                # handler, just constructed manually here instead of via
                # the FastAPI dependency graph.
                app_role_session = AsyncSession(bind=connection)

                # Precondition: the GUC is genuinely unset at call time —
                # this is the exact chicken-and-egg state the bug required.
                guc_before = (
                    await connection.execute(
                        text("SELECT current_setting('app.current_landlord_id', true)")
                    )
                ).scalar_one()
                assert guc_before is None

                landlord, returned_session = await require_landlord(
                    _auth_user(auth_user_id), app_role_session
                )

                assert str(landlord.id) == landlord_id
                assert returned_session is app_role_session

                guc_after = (
                    await connection.execute(
                        text("SELECT current_setting('app.current_landlord_id', true)")
                    )
                ).scalar_one()
                assert guc_after == landlord_id
            finally:
                await trans.rollback()
    finally:
        await _delete_landlord(db_engine, landlord_id)
