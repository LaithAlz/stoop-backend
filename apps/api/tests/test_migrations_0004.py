"""Integration tests for the auth.users lifecycle-trigger migration (0004).

Marker: ``integration`` -- requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0004 implements #15 (docs/03-engineering/issue-specs/
015-auth-user-lifecycle.md): a ``SECURITY DEFINER`` function set + triggers
on ``auth.users`` that sync sign-up / email-change / delete into
``landlords``. See the migration module docstring
(``migrations/versions/0004_auth_users_lifecycle_trigger.py``) for the full
design rationale (email-edge-case normalization consistent with #161, the
"OWNERSHIP MODEL" section explaining why there is no dedicated owner role,
the guarded local ``auth`` schema shim).

These tests verify, against the local docker-compose Postgres (which has no
real Supabase ``auth`` schema, so migration 0004 creates the guarded shim):

1. The shim ``auth`` schema/table, the three functions (SECURITY DEFINER,
   pinned search_path, owned by the migrating role, PUBLIC EXECUTE
   revoked), and the four triggers all exist as designed after
   ``upgrade head``.
2. INSERT into auth.users with a real email -> a landlords row appears with
   the correct auth_user_id/email/full_name.
3. Idempotency: a landlords row that already exists for an auth_user_id
   (e.g. seeded by the lazy ``GET /v1/me`` upsert) is updated, not
   duplicated, when the matching auth.users INSERT trigger fires; and two
   *different*-value email updates fired back-to-back apply cleanly, with
   no duplicate row (an earlier version of this test fired the SAME value
   twice, which is dead weight under the WHEN guard below -- see #5).
4. INSERT with a blank/whitespace email -> no landlords row (the #161-style
   edge case; landlords.email stays NOT NULL).
5. UPDATE OF email -> propagates to the matching landlords row; a same
   -value UPDATE (no actual change) never fires the trigger at all (the
   ``WHEN (NEW.email IS DISTINCT FROM OLD.email)`` guard) -- proven via a
   divergence lever (a manually-set sentinel value on ``landlords.email``
   survives untouched only if the trigger is genuinely suppressed; if it
   fired, it would clobber the sentinel back), NOT via an ``updated_at``
   timestamp comparison: within one transaction, ``now()`` is
   ``transaction_timestamp()``, frozen for the whole transaction, so that
   comparison is a tautology (verified empirically -- see the test's own
   docstring for the mutation-testing evidence).
6. UPDATE OF email to '' -> landlords.email is left unchanged (documented
   choice, never overwrite with blank).
7. DELETE FROM auth.users -> landlords.deleted_at is set, row still present
   (never a hard delete).
8. UPDATE auth.users SET deleted_at = now() (GoTrue's alternate soft-delete
   path) -> landlords.deleted_at is set the same way.
9. A landlord "created by /v1/me" (seeded directly, simulating the lazy
   upsert) then its auth user deleted -> soft-deleted.
10. The EXCEPTION WHEN OTHERS guard (never block sign-up/update/delete) is
    present in the migration source for all three functions.
11. Behavioral proof of that guard: a temporary BEFORE INSERT trigger on
    ``public.landlords`` that always RAISEs forces
    ``handle_auth_user_created()`` into its exception path at runtime; the
    auth.users INSERT still SUCCEEDS (sign-up is never blocked) and no
    landlords row is created (the forced failure was swallowed, not
    silently successful) -- not just a source-grep, an actual forced
    failure at runtime. (An earlier version of this test used ``REVOKE
    INSERT ... FROM landlord_sync_role`` as the failure lever; that role no
    longer exists -- see migration docstring "OWNERSHIP MODEL" -- so this
    uses a same-table trigger instead.)
12. Downgrade to 0003 removes the triggers/functions/shim; re-upgrade to
    head restores everything (full round-trip) -- runs last per the
    mutation-order convention documented in test_migrations_0003.py /
    test_migrations_core.py.

A privilege-ordering proof test existed in an earlier revision of this
file (exercising a genuine non-superuser role via ``SET ROLE`` to prove
two ACL rules an ``ALTER FUNCTION ... OWNER TO`` ownership-transfer step
depended on). That step no longer exists in the migration -- the live
Supabase dry-run proved the whole ownership-transfer design impossible
on the platform (see migration docstring "OWNERSHIP MODEL") -- so that
test was removed rather than kept as a hollow shell: with no ownership
transfer, "the creating role owns what it just created" is true by
construction and untestable-as-a-risk, and the ALTER-FUNCTION-OWNER-TO
ACL rules it proved no longer apply to anything this migration does.

Every test that touches data uses its own connection wrapped in an explicit
transaction that is always rolled back at teardown (the ``conn`` fixture)
EXCEPT the round-trip test at the bottom, which must actually mutate schema
state and therefore runs last.

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0004.py -m integration -v
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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# ---------------------------------------------------------------------------
# Helpers -- duplicated (not imported) from tests/test_migrations_0003.py, to
# keep this module self-contained (same convention as that module).
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "0004_auth_users_lifecycle_trigger.py"
)


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
    """Apply migrations exactly once per test session (ends at head/0004)."""
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

    Nothing here is ever committed, so no test leaves rows behind in either
    ``auth.users`` or ``landlords``.
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


async def _insert_auth_user(
    conn: AsyncConnection,
    *,
    user_id: str | None = None,
    email: str | None = "user@example.com",
    full_name: str | None = "Test User",
) -> str:
    user_id = user_id or str(uuid.uuid4())
    meta = {"full_name": full_name} if full_name is not None else {}
    await conn.execute(
        text(
            "INSERT INTO auth.users (id, email, raw_user_meta_data) "
            "VALUES (:id, :email, CAST(:meta AS jsonb))"
        ),
        {"id": user_id, "email": email, "meta": _to_jsonb(meta)},
    )
    return user_id


def _to_jsonb(d: dict[str, str]) -> str:
    import json

    return json.dumps(d)


async def _seed_landlord(conn: AsyncConnection, auth_user_id: str, *, email: str) -> None:
    """Directly insert a landlords row -- simulates the lazy GET /v1/me upsert
    (#11) having already provisioned the row before the auth trigger fires."""
    await conn.execute(
        text("INSERT INTO landlords (auth_user_id, email) VALUES (:auth_user_id, :email)"),
        {"auth_user_id": auth_user_id, "email": email},
    )


async def _get_landlord(conn: AsyncConnection, auth_user_id: str) -> dict[str, object] | None:
    result = await conn.execute(
        text(
            "SELECT auth_user_id, email, full_name, deleted_at FROM landlords "
            "WHERE auth_user_id = :auth_user_id"
        ),
        {"auth_user_id": auth_user_id},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def _count_landlords(conn: AsyncConnection, auth_user_id: str) -> int:
    result = await conn.execute(
        text("SELECT count(*) FROM landlords WHERE auth_user_id = :auth_user_id"),
        {"auth_user_id": auth_user_id},
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# 1. Object existence -- shim schema/table, functions, triggers, ownership
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_shim_auth_schema_and_users_table_exist(db: AsyncEngine) -> None:
    """The guarded local shim must create `auth.users` with the minimal
    column subset the triggers touch."""
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'auth' AND table_name = 'users'"
            )
        )
        columns = {row[0]: row[1] for row in result.fetchall()}

    assert columns == {
        "id": "uuid",
        "email": "text",
        "raw_user_meta_data": "jsonb",
        "deleted_at": "timestamp with time zone",
    }


@pytest.mark.integration
async def test_shim_schema_carries_marker_comment(db: AsyncEngine) -> None:
    """The shim schema must carry the `stoop-local-shim` marker comment so
    downgrade() can tell it apart from a real Supabase `auth` schema."""
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace "
                "WHERE nspname = 'auth'"
            )
        )
        comment = result.scalar_one()

    assert comment is not None
    assert comment.startswith("stoop-local-shim (migration 0004)")


@pytest.mark.integration
async def test_trigger_functions_exist_security_definer_with_pinned_search_path(
    db: AsyncEngine,
) -> None:
    """All three trigger functions must be SECURITY DEFINER with search_path
    pinned to `public, pg_temp` (privilege-escalation guard)."""
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT proname, prosecdef, proconfig FROM pg_proc "
                "WHERE proname IN ("
                "'handle_auth_user_created', "
                "'handle_auth_user_email_updated', "
                "'handle_auth_user_soft_delete'"
                ")"
            )
        )
        rows = {row[0]: (row[1], row[2]) for row in result.fetchall()}

    assert set(rows) == {
        "handle_auth_user_created",
        "handle_auth_user_email_updated",
        "handle_auth_user_soft_delete",
    }
    for name, (is_security_definer, config) in rows.items():
        assert is_security_definer is True, f"{name} must be SECURITY DEFINER"
        assert config is not None and any("search_path=public" in c for c in config), (
            f"{name} must pin search_path"
        )


@pytest.mark.integration
async def test_triggers_registered_on_auth_users(db: AsyncEngine) -> None:
    """All four triggers must be attached to auth.users."""
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'auth.users'::regclass AND NOT tgisinternal"
            )
        )
        names = {row[0] for row in result.fetchall()}

    assert names == {
        "on_auth_user_created",
        "on_auth_user_email_updated",
        "on_auth_user_deleted",
        "on_auth_user_deleted_at_updated",
    }


@pytest.mark.integration
async def test_functions_owned_by_migrating_role_with_public_execute_revoked(
    db: AsyncEngine,
) -> None:
    """Ownership model (migration docstring "OWNERSHIP MODEL"): all three
    trigger functions are owned by whichever role ran the migration -- the
    connection's own session user (`stoop` locally, `postgres` on live
    Supabase) -- NOT a separately-provisioned role. An earlier
    `landlord_sync_role` design (dedicated NOLOGIN owner role) proved
    impossible on live Supabase: `ALTER FUNCTION ... OWNER TO
    landlord_sync_role` failed outright, the membership-guard this
    migration used was unsound on PG16+ (implicit ADMIN OPTION for a
    CREATEROLE creator), and the only fix for that -- self-granting
    membership -- terminates the connection on live Supabase (platform
    role protection). See the migration docstring for the full writeup and
    the accepted tradeoff.

    PUBLIC's default EXECUTE grant is still revoked on all three functions
    (defense-in-depth, unchanged by the redesign).
    """
    async with db.connect() as connection:
        session_user = (await connection.execute(text("SELECT current_user"))).scalar_one()

        owners = (
            (
                await connection.execute(
                    text(
                        "SELECT DISTINCT proowner::regrole::text FROM pg_proc "
                        "WHERE proname IN ("
                        "'handle_auth_user_created', "
                        "'handle_auth_user_email_updated', "
                        "'handle_auth_user_soft_delete'"
                        ")"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert owners == [session_user], (
            f"all three functions must be owned by the migrating role ({session_user!r}), "
            f"got {owners!r}"
        )

        for fn_name in [
            "handle_auth_user_created",
            "handle_auth_user_email_updated",
            "handle_auth_user_soft_delete",
        ]:
            can_execute = (
                await connection.execute(
                    text(
                        "SELECT has_function_privilege('public', oid, 'EXECUTE') "
                        "FROM pg_proc WHERE proname = :name"
                    ),
                    {"name": fn_name},
                )
            ).scalar_one()
            assert can_execute is False, f"PUBLIC must not retain EXECUTE on {fn_name}"


@pytest.mark.integration
async def test_exception_safety_documented_for_all_functions(db: AsyncEngine) -> None:
    """Every trigger function body must swallow errors (never block a
    sign-up/update/delete on auth.users) -- 015 spec gotcha."""
    content = _MIGRATION_PATH.read_text()

    for anchor in [
        "CREATE OR REPLACE FUNCTION public.handle_auth_user_created()",
        "CREATE OR REPLACE FUNCTION public.handle_auth_user_email_updated()",
        "CREATE OR REPLACE FUNCTION public.handle_auth_user_soft_delete()",
    ]:
        window = _window_after(content, anchor)
        assert "EXCEPTION" in window
        assert "WHEN OTHERS" in window


@pytest.mark.integration
async def test_insert_trigger_exception_path_never_blocks_signup(
    conn: AsyncConnection,
) -> None:
    """Behavioral proof of the exception-safety guard (the source-grep
    test above only proves the SQL text is present) -- issue #15's own
    top-listed risk: "a trigger failure on auth.users insert can block
    sign-up entirely."

    Forces `handle_auth_user_created()` into its EXCEPTION WHEN OTHERS
    path via a temporary `BEFORE INSERT` trigger on `public.landlords`
    that always RAISEs. (An earlier version of this test used `REVOKE
    INSERT ... FROM landlord_sync_role` as the failure lever; that role no
    longer exists -- see migration docstring "OWNERSHIP MODEL" -- the
    functions are now owned by the migrating role itself, which always
    has whatever privileges it needs on `landlords`, so there is no lower
    -privileged grant left to strip. A same-table trigger that
    unconditionally raises is the reliable lever instead.)

    Asserts:
    (a) the auth.users INSERT itself still SUCCEEDS (sign-up is never
        blocked -- the whole point of the EXCEPTION WHEN OTHERS guard);
    (b) no landlords row was created (the swallowed error means nothing
        was written, not a silently-successful write).

    The temporary function + trigger are created and dropped inside this
    test's transaction (the `conn` fixture, which always rolls back at
    teardown) -- the explicit `DROP`s in the `finally` block are a
    defensive backstop on top of that rollback, not a substitute for it.
    """
    suffix = uuid.uuid4().hex[:8]
    fn_name = f"raise_on_landlord_insert_{suffix}"
    trg_name = f"raise_on_landlord_insert_trg_{suffix}"
    user_id = str(uuid.uuid4())

    await conn.execute(
        text(
            f"CREATE FUNCTION public.{fn_name}() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'forced failure for "
            "test_insert_trigger_exception_path_never_blocks_signup'; "
            "END $$"
        )
    )
    await conn.execute(
        text(
            f"CREATE TRIGGER {trg_name} BEFORE INSERT ON public.landlords "
            f"FOR EACH ROW EXECUTE FUNCTION public.{fn_name}()"
        )
    )
    try:
        # Must NOT raise: handle_auth_user_created()'s own EXCEPTION WHEN
        # OTHERS must swallow the forced failure from the BEFORE INSERT
        # trigger above and let the auth.users INSERT succeed regardless.
        await _insert_auth_user(conn, user_id=user_id, email="blocked@example.com")

        assert await _count_landlords(conn, user_id) == 0, (
            "no landlords row should exist -- the forced BEFORE INSERT "
            "failure was swallowed by handle_auth_user_created()'s own "
            "exception handler, not silently succeeded"
        )
    finally:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {trg_name} ON public.landlords"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS public.{fn_name}()"))


# ---------------------------------------------------------------------------
# 2. AFTER INSERT -- provisioning + idempotency + blank-email edge case
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_insert_auth_user_creates_landlord(conn: AsyncConnection) -> None:
    """A real-email auth.users INSERT must create a matching landlords row
    with auth_user_id/email/full_name populated from raw_user_meta_data."""
    user_id = str(uuid.uuid4())
    await _insert_auth_user(conn, user_id=user_id, email="alice@example.com", full_name="Alice")

    row = await _get_landlord(conn, user_id)
    assert row is not None
    assert row["email"] == "alice@example.com"
    assert row["full_name"] == "Alice"
    assert row["deleted_at"] is None


@pytest.mark.integration
async def test_insert_is_idempotent_against_a_preexisting_landlord_row(
    conn: AsyncConnection,
) -> None:
    """If a landlords row already exists for this auth_user_id (e.g. seeded
    by the lazy GET /v1/me upsert), the INSERT trigger's ON CONFLICT DO
    UPDATE must update it in place -- never a duplicate-key error, never a
    second row."""
    user_id = str(uuid.uuid4())
    await _seed_landlord(conn, user_id, email="pre-seeded@example.com")

    await _insert_auth_user(conn, user_id=user_id, email="fresh@example.com", full_name="Bob")

    assert await _count_landlords(conn, user_id) == 1
    row = await _get_landlord(conn, user_id)
    assert row is not None
    assert row["email"] == "fresh@example.com"
    assert row["full_name"] == "Bob"


@pytest.mark.integration
async def test_repeated_different_value_email_updates_apply_cleanly(
    conn: AsyncConnection,
) -> None:
    """Firing the email-update trigger twice in a row, each with a
    genuinely different value, must apply cleanly each time -- no error,
    no duplicate landlords row, final state reflects the last update.

    (An earlier version of this test fired the SAME value twice. Under the
    `WHEN (NEW.email IS DISTINCT FROM OLD.email)` guard the trigger never
    fires at all for a same-value UPDATE, which made that version dead
    weight -- it passed whether or not the underlying UPDATE logic was
    even reachable. See
    `test_update_email_to_same_value_does_not_refire_trigger` for the
    guard's own behavior; this test instead exercises genuinely repeated
    *firing* of the trigger.)
    """
    user_id = str(uuid.uuid4())
    await _insert_auth_user(conn, user_id=user_id, email="stable@example.com")

    await conn.execute(
        text("UPDATE auth.users SET email = :email WHERE id = :id"),
        {"email": "first-change@example.com", "id": user_id},
    )
    await conn.execute(
        text("UPDATE auth.users SET email = :email WHERE id = :id"),
        {"email": "second-change@example.com", "id": user_id},
    )

    assert await _count_landlords(conn, user_id) == 1
    row = await _get_landlord(conn, user_id)
    assert row is not None
    assert row["email"] == "second-change@example.com"


@pytest.mark.integration
@pytest.mark.parametrize("blank_email", [None, "", "   "], ids=["null", "empty", "whitespace"])
async def test_insert_with_blank_email_creates_no_landlord_row(
    conn: AsyncConnection, blank_email: str | None
) -> None:
    """landlords.email is NOT NULL -- a phone-only auth user (blank/NULL
    email) must get no landlords row at all (consistent with #161's
    normalization; GET /v1/me's lazy upsert provisions it later)."""
    user_id = str(uuid.uuid4())
    await _insert_auth_user(conn, user_id=user_id, email=blank_email)

    assert await _count_landlords(conn, user_id) == 0


# ---------------------------------------------------------------------------
# 3. AFTER UPDATE OF email -- propagation + blank-email non-overwrite
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_update_email_propagates_to_landlord(conn: AsyncConnection) -> None:
    """A real-email UPDATE must propagate to the matching landlords row."""
    user_id = str(uuid.uuid4())
    await _insert_auth_user(conn, user_id=user_id, email="old@example.com")

    await conn.execute(
        text("UPDATE auth.users SET email = :email WHERE id = :id"),
        {"email": "new@example.com", "id": user_id},
    )

    row = await _get_landlord(conn, user_id)
    assert row is not None
    assert row["email"] == "new@example.com"


@pytest.mark.integration
async def test_update_email_to_same_value_does_not_refire_trigger(
    conn: AsyncConnection,
) -> None:
    """Setting auth.users.email to its CURRENT value must not fire the
    trigger at all -- the `WHEN (NEW.email IS DISTINCT FROM OLD.email)`
    guard (mirroring the deleted_at trigger's precise guard, added per
    safety-review advisory). `UPDATE OF email` alone (without a WHEN
    clause) would fire on any UPDATE that lists the email column, whether
    or not the value actually changed.

    Divergence-lever design (NOT a timestamp comparison -- see below for
    why): after inserting with email "stable@example.com", we directly
    `UPDATE landlords SET email = 'divergent@example.com'` -- bypassing
    the trigger entirely, so `landlords.email` and `auth.users.email` now
    deliberately disagree. We then fire a same-value UPDATE on
    `auth.users.email` (still "stable@example.com", i.e. no actual change
    from auth.users' point of view). If the WHEN guard correctly suppresses
    the trigger, the sentinel value survives untouched. If the guard were
    missing (or broken), the trigger WOULD fire and its `UPDATE
    public.landlords SET email = v_email ... WHERE auth_user_id = ...`
    would clobber the sentinel back to "stable@example.com" -- a real,
    directly observable behavioral difference either way.

    Why not compare `updated_at` before/after instead (as an earlier
    version of this test did): within a single Postgres transaction,
    `now()` is `transaction_timestamp()`, which is **frozen for the entire
    transaction** -- every call returns the exact same value, regardless of
    real wall-clock time elapsed (a `pg_sleep()` between calls does not
    change this). Since every statement in this test runs inside one
    transaction (the `conn` fixture, rolled back at teardown), comparing
    `landlords.updated_at` before and after the same-value UPDATE is a
    tautology: it is byte-identical whether or not the trigger fired,
    because `updated_at = now()` inside the trigger body would produce the
    same frozen timestamp as the original INSERT. This was verified
    empirically: with the WHEN guard temporarily removed, the old
    timestamp-based version of this test stayed green.
    """
    user_id = str(uuid.uuid4())
    await _insert_auth_user(conn, user_id=user_id, email="stable@example.com")

    await conn.execute(
        text(
            "UPDATE landlords SET email = 'divergent@example.com' "
            "WHERE auth_user_id = :auth_user_id"
        ),
        {"auth_user_id": user_id},
    )

    await conn.execute(
        text("UPDATE auth.users SET email = :email WHERE id = :id"),
        {"email": "stable@example.com", "id": user_id},
    )

    row = await _get_landlord(conn, user_id)
    assert row is not None
    assert row["email"] == "divergent@example.com", (
        "landlords.email was overwritten -- the WHEN (NEW.email IS "
        "DISTINCT FROM OLD.email) guard should have suppressed the "
        "trigger entirely for a same-value auth.users.email UPDATE; had "
        "it fired, it would have clobbered this sentinel value back to "
        "'stable@example.com'"
    )


@pytest.mark.integration
async def test_update_email_to_blank_leaves_stored_email_unchanged(conn: AsyncConnection) -> None:
    """Updating auth.users.email to '' must NOT overwrite the stored
    landlords.email -- documented choice, consistent with #161's regression
    test for the equivalent upsert-clobber bug."""
    user_id = str(uuid.uuid4())
    await _insert_auth_user(conn, user_id=user_id, email="keep-me@example.com")

    await conn.execute(
        text("UPDATE auth.users SET email = '' WHERE id = :id"),
        {"id": user_id},
    )

    row = await _get_landlord(conn, user_id)
    assert row is not None
    assert row["email"] == "keep-me@example.com"


@pytest.mark.integration
async def test_update_email_with_no_matching_landlord_is_a_noop(conn: AsyncConnection) -> None:
    """A phone-only auth user (no landlords row) who has their email column
    updated must not gain a landlords row from this trigger -- update-only,
    not an upsert (see migration docstring); the lazy GET /v1/me upsert
    covers this gap instead."""
    user_id = str(uuid.uuid4())
    await _insert_auth_user(conn, user_id=user_id, email=None)
    assert await _count_landlords(conn, user_id) == 0

    await conn.execute(
        text("UPDATE auth.users SET email = :email WHERE id = :id"),
        {"email": "late-add@example.com", "id": user_id},
    )

    assert await _count_landlords(conn, user_id) == 0


# ---------------------------------------------------------------------------
# 4. AFTER DELETE / AFTER UPDATE OF deleted_at -- soft-delete, never hard
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_delete_auth_user_soft_deletes_landlord(conn: AsyncConnection) -> None:
    """DELETE FROM auth.users must set landlords.deleted_at and leave the
    row present -- never a hard delete."""
    user_id = str(uuid.uuid4())
    await _insert_auth_user(conn, user_id=user_id, email="doomed@example.com")

    await conn.execute(text("DELETE FROM auth.users WHERE id = :id"), {"id": user_id})

    row = await _get_landlord(conn, user_id)
    assert row is not None, "landlords row must survive an auth.users delete"
    assert row["deleted_at"] is not None


@pytest.mark.integration
async def test_update_deleted_at_soft_deletes_landlord(conn: AsyncConnection) -> None:
    """Supabase/GoTrue sometimes soft-deletes auth.users in place (UPDATE
    deleted_at) instead of a hard DELETE -- this path must also propagate."""
    user_id = str(uuid.uuid4())
    await _insert_auth_user(conn, user_id=user_id, email="soft-doomed@example.com")

    await conn.execute(
        text("UPDATE auth.users SET deleted_at = now() WHERE id = :id"),
        {"id": user_id},
    )

    row = await _get_landlord(conn, user_id)
    assert row is not None
    assert row["deleted_at"] is not None


@pytest.mark.integration
async def test_landlord_from_lazy_upsert_then_auth_delete_is_soft_deleted(
    conn: AsyncConnection,
) -> None:
    """A landlord row provisioned out-of-band (simulating the lazy GET
    /v1/me upsert, #11) must still be found and soft-deleted when its
    auth.users row is later deleted -- the trigger matches purely on
    auth_user_id, regardless of how the landlords row was created."""
    user_id = str(uuid.uuid4())
    await _seed_landlord(conn, user_id, email="from-me-endpoint@example.com")
    # auth.users row created after the landlord (order-independent thanks to
    # the ON CONFLICT DO UPDATE in the INSERT trigger).
    await _insert_auth_user(conn, user_id=user_id, email="from-me-endpoint@example.com")
    assert await _count_landlords(conn, user_id) == 1

    await conn.execute(text("DELETE FROM auth.users WHERE id = :id"), {"id": user_id})

    row = await _get_landlord(conn, user_id)
    assert row is not None
    assert row["deleted_at"] is not None


# ---------------------------------------------------------------------------
# 5. Downgrade to 0003 / re-upgrade round-trip -- MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0003_removes_triggers_functions_and_shim(
    db: AsyncEngine,
) -> None:
    """Downgrading to 0003 must remove the triggers, the three functions,
    and the shim auth schema (it carries our marker). No role handling is
    involved -- the functions were never owned by anything but the
    migrating role."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0003"))

    async with db.connect() as connection:
        schema_result = await connection.execute(
            text("SELECT nspname FROM pg_namespace WHERE nspname = 'auth'")
        )
        assert not schema_result.fetchall(), "shim auth schema should be dropped"

        func_result = await connection.execute(
            text(
                "SELECT proname FROM pg_proc WHERE proname IN ("
                "'handle_auth_user_created', "
                "'handle_auth_user_email_updated', "
                "'handle_auth_user_soft_delete')"
            )
        )
        assert not func_result.fetchall(), "all three functions should be dropped"


@pytest.mark.integration
async def test_reupgrade_restores_0004_state(db: AsyncEngine) -> None:
    """After downgrade to 0003 + re-upgrade to head, 0004 state is restored:
    shim schema, functions, and triggers all exist again, and the
    end-to-end insert flow works."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        schema_result = await connection.execute(
            text("SELECT nspname FROM pg_namespace WHERE nspname = 'auth'")
        )
        assert schema_result.fetchall(), "shim auth schema should exist again"

        trigger_result = await connection.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'auth.users'::regclass AND NOT tgisinternal"
            )
        )
        assert len(trigger_result.fetchall()) == 4

    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            user_id = str(uuid.uuid4())
            await _insert_auth_user(connection, user_id=user_id, email="reupgrade@example.com")
            row = await _get_landlord(connection, user_id)
            assert row is not None
            assert row["email"] == "reupgrade@example.com"
        finally:
            await trans.rollback()
