"""Integration tests for the landlord_id_for_auth_user SECURITY DEFINER
identity-lookup migration (0019, #194).

Marker: ``integration`` -- requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0019 implements #194: a single ``SECURITY DEFINER`` function,
``public.landlord_id_for_auth_user(uuid) RETURNS uuid``, that replaces the
two-session admin-lookup ``app/deps.py::require_landlord`` used to open on
every authenticated request. See the migration module docstring
(``migrations/versions/0019_landlord_id_for_auth_user.py``) for the full
design rationale (the pinned ``search_path`` privilege-escalation guard,
the "no dedicated owner role" precedent from migration 0004, and the
mutation-testing evidence for the search_path pin -- NOT reproduced as an
automated test here, since it requires temporarily mutating and restoring
committed schema objects mid-test; see that docstring for the live
transcript instead).

These tests verify, against the local docker-compose Postgres:

1. The function exists: ``SECURITY DEFINER``, ``STRICT``, ``STABLE``,
   pinned ``search_path``, owned by the migrating role, ``PUBLIC EXECUTE``
   revoked, ``app_role EXECUTE`` granted.
2. ADVERSARIAL TENANCY PROOF (#194's own "Prove it" requirement), all
   called as genuine ``app_role`` (``SET LOCAL ROLE app_role``, the
   migration-0005 test convention), with ``app.current_landlord_id``
   deliberately left UNSET the entire time:
   a. A real landlord's own ``auth_user_id`` -> that landlord's own id.
   b. A DIFFERENT real landlord's ``auth_user_id``, called in the SAME
      transaction/session right after (a) -- returns THAT landlord's own
      id, never the first landlord's id and never anything else. There is
      no argument that makes this function return an id other than the
      one that genuinely maps to the supplied ``auth_user_id``.
   c. A nonexistent ``auth_user_id`` -> ``NULL``.
   d. ``NULL`` itself -> ``NULL`` (the ``STRICT`` short-circuit).
   e. A soft-deleted landlord's ``auth_user_id`` -> ``NULL`` (excluded by
      the ``deleted_at IS NULL`` filter, same as before this migration).
   f. The GUC is never read or written by any of the above -- confirmed
      unset before AND after every call in this list.
3. A role with no ``EXECUTE`` grant at all gets ``permission denied`` --
   proves the ``PUBLIC`` revoke is real, not just present in catalog
   metadata.
4. Downgrade removes the function; re-upgrade restores it (full
   round-trip) -- runs last per the mutation-order convention documented
   in test_migrations_0003.py / test_migrations_core.py.

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0019.py -m integration -v
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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from tests import migration_harness

# ---------------------------------------------------------------------------
# Helpers -- duplicated (not imported), matching every other
# tests/test_migrations_*.py module's established self-contained convention.
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


# ---------------------------------------------------------------------------
# Session-scoped synchronous setup (avoids pytest-asyncio scope-mismatch).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0019).

    Delegates to ``tests.migration_harness.migrate_from_base_to_head`` --
    see that module's docstring for why (issue #281).
    """
    migration_harness.migrate_from_base_to_head(_alembic, _get_db_url())
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
    """Per-test connection wrapped in a transaction that is always rolled
    back -- nothing here is ever committed, so no test leaves rows behind.
    """
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            yield connection
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# Row-builder helpers
# ---------------------------------------------------------------------------


async def _seed_landlord(
    conn: AsyncConnection, *, auth_user_id: str, soft_deleted: bool = False
) -> str:
    landlord_id = str(uuid.uuid4())
    await conn.execute(
        text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_user_id, :email)"),
        {
            "id": landlord_id,
            "auth_user_id": auth_user_id,
            "email": f"{landlord_id}@example.com",
        },
    )
    if soft_deleted:
        await conn.execute(
            text("UPDATE landlords SET deleted_at = now() WHERE id = :id"),
            {"id": landlord_id},
        )
    return landlord_id


async def _call_lookup(conn: AsyncConnection, auth_user_id: str | None) -> str | None:
    result = await conn.execute(
        text("SELECT landlord_id_for_auth_user(:auth_user_id) AS id"),
        {"auth_user_id": auth_user_id},
    )
    value = result.scalar_one_or_none()
    return str(value) if value is not None else None


async def _guc_value(conn: AsyncConnection) -> str | None:
    result = await conn.execute(text("SELECT current_setting('app.current_landlord_id', true)"))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# 1. Object existence -- SECURITY DEFINER, STRICT, STABLE, pinned
#    search_path, ownership, EXECUTE grants
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_function_is_security_definer_strict_stable_with_pinned_search_path(
    db: AsyncEngine,
) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT prosecdef, proisstrict, provolatile, proconfig FROM pg_proc "
                "WHERE proname = 'landlord_id_for_auth_user'"
            )
        )
        row = result.one()

    is_security_definer, is_strict, volatility, config = row
    if isinstance(volatility, bytes):
        volatility = volatility.decode()
    assert is_security_definer is True, "must be SECURITY DEFINER"
    assert is_strict is True, "must be STRICT (RETURNS NULL ON NULL INPUT)"
    assert volatility == "s", "must be STABLE"
    assert config is not None and any("search_path=public, pg_temp" in c for c in config), (
        "search_path must be pinned to exactly 'public, pg_temp'"
    )


@pytest.mark.integration
async def test_function_owned_by_migrating_role_grants(db: AsyncEngine) -> None:
    """Owned by the migrating role (matching migration 0004's "OWNERSHIP
    MODEL" -- no dedicated owner role). PUBLIC EXECUTE revoked; app_role
    EXECUTE explicitly granted."""
    async with db.connect() as connection:
        session_user = (await connection.execute(text("SELECT current_user"))).scalar_one()

        owner = (
            await connection.execute(
                text(
                    "SELECT proowner::regrole::text FROM pg_proc "
                    "WHERE proname = 'landlord_id_for_auth_user'"
                )
            )
        ).scalar_one()
        assert owner == session_user

        public_can_exec = (
            await connection.execute(
                text(
                    "SELECT has_function_privilege('public', oid, 'EXECUTE') "
                    "FROM pg_proc WHERE proname = 'landlord_id_for_auth_user'"
                )
            )
        ).scalar_one()
        assert public_can_exec is False, "PUBLIC must not retain EXECUTE"

        app_role_can_exec = (
            await connection.execute(
                text(
                    "SELECT has_function_privilege('app_role', oid, 'EXECUTE') "
                    "FROM pg_proc WHERE proname = 'landlord_id_for_auth_user'"
                )
            )
        ).scalar_one()
        assert app_role_can_exec is True, "app_role must hold EXECUTE"


@pytest.mark.integration
async def test_role_without_execute_grant_is_denied(conn: AsyncConnection) -> None:
    """A role with no EXECUTE grant at all must be refused at the
    permission layer, not just missing from a catalog listing."""
    # No try/finally needed beyond the `conn` fixture's own: the expected
    # "permission denied" error aborts this transaction, and the fixture's
    # rollback (not a DROP ROLE) is what discards the temporary role --
    # no committed trace is ever created either way.
    probe_role = f"probe_role_{uuid.uuid4().hex[:8]}"
    await conn.execute(text(f"CREATE ROLE {probe_role} NOLOGIN"))
    await conn.execute(text(f"SET LOCAL ROLE {probe_role}"))
    with pytest.raises(Exception, match="permission denied"):
        await conn.execute(
            text("SELECT landlord_id_for_auth_user(:id)"),
            {"id": str(uuid.uuid4())},
        )


# ---------------------------------------------------------------------------
# 2. ADVERSARIAL TENANCY PROOF -- called as genuine app_role, GUC unset
#    throughout (#194's "Prove it" requirement)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_adversarial_tenancy_under_real_app_role_enforcement(
    db: AsyncEngine,
) -> None:
    """The authoritative adversarial proof for #194.

    Seeds two distinct, real landlords plus one soft-deleted landlord on an
    ordinary (superuser) connection, then -- on a SEPARATE connection that
    is genuinely ``app_role``-scoped (``SET LOCAL ROLE app_role``) with
    ``app.current_landlord_id`` deliberately left UNSET the entire time --
    calls ``landlord_id_for_auth_user`` with every adversarial argument
    #194 asks for: another landlord's real auth_user_id, a nonexistent
    one, NULL, and (implicitly, since the GUC is never set) "from a
    session with no app.current_landlord_id set".
    """
    landlord_a_auth = str(uuid.uuid4())
    landlord_b_auth = str(uuid.uuid4())
    soft_deleted_auth = str(uuid.uuid4())

    async with db.connect() as seed_connection:
        trans = await seed_connection.begin()
        landlord_a_id = await _seed_landlord(seed_connection, auth_user_id=landlord_a_auth)
        landlord_b_id = await _seed_landlord(seed_connection, auth_user_id=landlord_b_auth)
        soft_deleted_id = await _seed_landlord(
            seed_connection, auth_user_id=soft_deleted_auth, soft_deleted=True
        )
        await trans.commit()

    try:
        async with db.connect() as connection:
            trans = await connection.begin()
            try:
                await connection.execute(text("SET LOCAL ROLE app_role"))

                # Precondition: GUC genuinely unset.
                assert await _guc_value(connection) is None

                # (a) landlord A's own auth_user_id -> A's own id.
                result_a = await _call_lookup(connection, landlord_a_auth)
                assert result_a == landlord_a_id

                # (b) a DIFFERENT landlord's auth_user_id, same session,
                # right after -- B's own id, not A's, not anything else.
                result_b = await _call_lookup(connection, landlord_b_auth)
                assert result_b == landlord_b_id
                assert result_b != result_a

                # (c) nonexistent auth_user_id -> NULL.
                assert await _call_lookup(connection, str(uuid.uuid4())) is None

                # (d) NULL argument -> NULL.
                assert await _call_lookup(connection, None) is None

                # (e) soft-deleted landlord's auth_user_id -> NULL.
                result_soft = await _call_lookup(connection, soft_deleted_auth)
                assert result_soft is None
                assert soft_deleted_id is not None  # seeded, just never returned

                # (f) the GUC was never touched by any of the calls above.
                assert await _guc_value(connection) is None
            finally:
                await trans.rollback()
    finally:
        async with db.connect() as cleanup_connection:
            trans = await cleanup_connection.begin()
            await cleanup_connection.execute(
                text("DELETE FROM landlords WHERE id = ANY(:ids)"),
                {"ids": [landlord_a_id, landlord_b_id, soft_deleted_id]},
            )
            await trans.commit()


# ---------------------------------------------------------------------------
# 3. Downgrade / re-upgrade round-trip -- MUST run last: it mutates schema
# state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0017_removes_function(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0017"))

    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT proname FROM pg_proc WHERE proname = 'landlord_id_for_auth_user'")
        )
        assert not result.fetchall(), "function should be dropped"


@pytest.mark.integration
async def test_reupgrade_restores_function(db: AsyncEngine) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT proname FROM pg_proc WHERE proname = 'landlord_id_for_auth_user'")
        )
        assert result.fetchall(), "function should exist again after re-upgrade"

    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            auth_user_id = str(uuid.uuid4())
            landlord_id = await _seed_landlord(connection, auth_user_id=auth_user_id)
            assert await _call_lookup(connection, auth_user_id) == landlord_id
        finally:
            await trans.rollback()
