"""Behavioral Row-Level Security tests — proves enforcement, not just catalog
state (migration 0005, #22).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

WHY THIS FILE EXISTS SEPARATELY FROM ``test_migrations_0005.py``
------------------------------------------------------------------------
``docker-compose``'s local Postgres runs as a bootstrap SUPERUSER
(``stoop``). Superusers ALWAYS bypass Row-Level Security. So every query
run as ``stoop`` sees every row no matter what the policies say; catalog
assertions (RLS enabled, policies exist, grants exact) are the most
``test_migrations_0005.py`` can honestly prove. To prove enforcement, a
test must run its queries as a session that is genuinely subject to
``app_role``'s policies — not the migrating/admin role.

HOW THESE TESTS BECOME "``app_role``" — ``SET LOCAL ROLE``, not a real
LOGIN (#22 safety review item 5 — reworked from an earlier password-based
design)
------------------------------------------------------------------------
``app_role`` is created ``NOLOGIN`` (migration 0005) — nothing can
authenticate as it via a normal client connection, by design. Every test
below instead runs, on the existing superuser (``stoop``) connection,
inside its own transaction:

    SET LOCAL ROLE app_role;
    ... (rest of the transaction runs AS app_role for every permission/RLS
         check — current_user is now app_role) ...
    -- transaction ends (always via rollback here) — ROLE automatically
    -- reverts, exactly like any other `SET LOCAL` setting.

Per PostgreSQL's own role-switching rules, a superuser may ``SET ROLE`` to
ANY role regardless of membership (``GRANT app_role TO stoop`` is never
needed, and never run — self-granting membership is exactly the
"terminates the connection" hazard documented in the migration's
docstring, on live Supabase). ``SET LOCAL`` (rather than session-level
``SET ROLE`` + a manual ``RESET ROLE``) means the role switch is scoped to
the current transaction and reverts automatically at COMMIT/ROLLBACK —
identical in spirit to how ``require_landlord`` sets
``app.current_landlord_id`` via ``set_config(..., true)`` (see
``app/deps.py``). There is no separate cleanup step to forget, and no
window where a pooled connection could leak the switched role to another
test.

ROOT CAUSE OF A TRANSIENT FAILURE SEEN UNDER THE OLD DESIGN (#22 safety
review item 11 — do not re-introduce the password pattern)
------------------------------------------------------------------------
An earlier revision of this file used ``ALTER ROLE app_role LOGIN
PASSWORD '...'`` + a second physical connection authenticated as
``app_role``, then ``ALTER ROLE app_role NOLOGIN PASSWORD NULL`` to
revert. That version was observed to leave ``app_role`` with
``rolcanlogin = true`` in ``test_reupgrade_restores_0005_state``
(``tests/test_migrations_0005.py``) when a test run was interrupted mid
-window (LOGIN granted, revert never reached) — a real, if narrow, gap:
the revert lived in a ``finally`` block, but ANYTHING that kills the test
process between the grant and the revert (a hard interrupt, an OOM kill,
a fixture teardown ordering surprise) skips ``finally`` too. This
``SET LOCAL ROLE`` design eliminates that failure class categorically:
there is no persistent role-level state change to leak in the first
place — ``SET LOCAL`` is bound to a transaction on a connection this test
already owns and rolls back either way. Migration 0005's own defensive
``ALTER ROLE app_role NOLOGIN`` right after its existence-guarded
``CREATE ROLE`` (#22 safety review item 10) is the belt-and-braces version
of the same concern, guarding against a stale LOGIN-enabled ``app_role``
surviving a re-migration regardless of which test design created it.

A KNOWN, ACCEPTED LIMITATION OF THIS TECHNIQUE
------------------------------------------------------------------------
``SET ROLE``/``SET LOCAL ROLE`` bypassing the membership check is a
SUPERUSER-only privilege. ``stoop`` (local docker-compose) is confirmed
superuser (``rolsuper = true``). On live Supabase, ``postgres`` is NOT a
superuser (it has ``rolbypassrls = true`` instead, a different, narrower
attribute — see the migration's "LIVE ROLE FACTS") and holds no
membership in ``app_role``, so this exact technique would NOT work there
(and granting that membership is the dangerous, connection-terminating
self-grant this whole design avoids). This file's ``SET LOCAL ROLE``
approach is therefore a LOCAL/CI-only testing convenience — the
production path for actually connecting as ``app_role`` remains the
password-based operator step documented in ``app/db/session.py``'s module
docstring, never exercised here.

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_rls_isolation.py -m integration -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# ---------------------------------------------------------------------------
# Helpers — duplicated (not imported) from tests/test_migrations_0005.py, to
# keep this module self-contained (same convention as the sibling migration
# test modules).
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


# ``SET LOCAL`` — scoped to the current transaction, reverts automatically
# at COMMIT/ROLLBACK (see module docstring). Only a superuser (``stoop``
# locally) can switch to a NOLOGIN role like this without prior membership.
_SET_ROLE_APP_ROLE_SQL = text("SET LOCAL ROLE app_role")


def _set_landlord_guc_sql() -> object:
    return text("SELECT set_config('app.current_landlord_id', :landlord_id, true)")


# ---------------------------------------------------------------------------
# Session-scoped setup
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0005)."""
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    # Leave schema in place; CI drops the DB container after the run.


@pytest_asyncio.fixture
async def db(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    """Per-test admin (superuser) engine — used both to seed/clean up data
    AND, via ``SET LOCAL ROLE app_role`` inside a transaction, to run
    queries genuinely subject to app_role's RLS policies and grants (see
    module docstring)."""
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


# ---------------------------------------------------------------------------
# Seed fixture — two landlords, each with a property/tenant/case/message,
# committed (not the usual rollback-only pattern) so a query in a SEPARATE
# transaction can see them; cleaned up explicitly after.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seed:
    landlord_a: str
    landlord_b: str
    property_a: str
    property_b: str
    tenant_a: str
    tenant_b: str
    case_a: str
    case_b: str
    message_a: str
    message_b: str


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


async def _insert_tenant(conn: AsyncConnection, landlord_id: str, property_id: str) -> str:
    tenant_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO tenants (id, landlord_id, property_id, phone) "
            "VALUES (:id, :landlord_id, :property_id, :phone)"
        ),
        {
            "id": tenant_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "phone": f"+1416{uuid.uuid4().int % 10_000_000:07d}",
        },
    )
    return tenant_id


async def _insert_case(
    conn: AsyncConnection, landlord_id: str, property_id: str, tenant_id: str
) -> str:
    case_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO cases (id, landlord_id, property_id, tenant_id, langgraph_thread_id) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, :thread_id)"
        ),
        {
            "id": case_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "thread_id": f"thread-{uuid.uuid4()}",
        },
    )
    return case_id


async def _insert_message(
    conn: AsyncConnection,
    landlord_id: str,
    property_id: str,
    tenant_id: str,
    case_id: str,
) -> str:
    message_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO messages "
            "(id, landlord_id, property_id, tenant_id, case_id, direction, party, body) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, :case_id, "
            "'inbound', 'tenant', 'test message body')"
        ),
        {
            "id": message_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "case_id": case_id,
        },
    )
    return message_id


async def _insert_audit_log(conn: AsyncConnection, landlord_id: str) -> int:
    result = await conn.execute(
        text(
            "INSERT INTO audit_log (landlord_id, actor, action) "
            "VALUES (:landlord_id, 'system', 'message_received') "
            "RETURNING id"
        ),
        {"landlord_id": landlord_id},
    )
    return int(result.scalar_one())


@pytest_asyncio.fixture
async def seed(db: AsyncEngine) -> AsyncGenerator[_Seed, None]:
    async with db.connect() as connection:
        trans = await connection.begin()
        landlord_a = await _insert_landlord(connection)
        landlord_b = await _insert_landlord(connection)
        property_a = await _insert_property(connection, landlord_a)
        property_b = await _insert_property(connection, landlord_b)
        tenant_a = await _insert_tenant(connection, landlord_a, property_a)
        tenant_b = await _insert_tenant(connection, landlord_b, property_b)
        case_a = await _insert_case(connection, landlord_a, property_a, tenant_a)
        case_b = await _insert_case(connection, landlord_b, property_b, tenant_b)
        message_a = await _insert_message(connection, landlord_a, property_a, tenant_a, case_a)
        message_b = await _insert_message(connection, landlord_b, property_b, tenant_b, case_b)
        await trans.commit()

    seeded = _Seed(
        landlord_a=landlord_a,
        landlord_b=landlord_b,
        property_a=property_a,
        property_b=property_b,
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        case_a=case_a,
        case_b=case_b,
        message_a=message_a,
        message_b=message_b,
    )
    try:
        yield seeded
    finally:
        async with db.connect() as connection:
            trans = await connection.begin()
            for landlord_id in (landlord_a, landlord_b):
                await connection.execute(
                    text(
                        "DELETE FROM message_status_events WHERE message_id IN "
                        "(SELECT id FROM messages WHERE landlord_id = :id)"
                    ),
                    {"id": landlord_id},
                )
                await connection.execute(
                    text(
                        "DELETE FROM message_cases WHERE case_id IN "
                        "(SELECT id FROM cases WHERE landlord_id = :id)"
                    ),
                    {"id": landlord_id},
                )
                await connection.execute(
                    text("DELETE FROM audit_log WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM messages WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM cases WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM tenants WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM properties WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id}
                )
            await trans.commit()


# ---------------------------------------------------------------------------
# 1. With the GUC set to landlord A: only A's rows are visible, across
# every table shape (direct landlord_id, id-keyed, and both EXISTS-joins).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_role_sees_only_matching_landlord_direct_tables(
    db: AsyncEngine, seed: _Seed
) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})

            for table, id_a, id_b in [
                ("properties", seed.property_a, seed.property_b),
                ("tenants", seed.tenant_a, seed.tenant_b),
                ("cases", seed.case_a, seed.case_b),
                ("messages", seed.message_a, seed.message_b),
            ]:
                visible = (
                    (
                        await connection.execute(
                            text(f"SELECT id FROM {table} WHERE id IN (:a, :b)"),  # noqa: S608
                            {"a": id_a, "b": id_b},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert [str(v) for v in visible] == [id_a], (
                    f"{table}: expected only landlord A's row visible, got {visible}"
                )
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_app_role_sees_only_matching_landlords_row(db: AsyncEngine, seed: _Seed) -> None:
    """landlords itself is keyed on `id`, not `landlord_id` — separate proof."""
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})

            visible = (
                (
                    await connection.execute(
                        text("SELECT id FROM landlords WHERE id IN (:a, :b)"),
                        {"a": seed.landlord_a, "b": seed.landlord_b},
                    )
                )
                .scalars()
                .all()
            )
            assert [str(v) for v in visible] == [seed.landlord_a]
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_app_role_sees_only_matching_message_cases(db: AsyncEngine, seed: _Seed) -> None:
    """message_cases has no landlord_id — EXISTS join through cases.

    Seeded via a plain admin transaction (no role switch), same as the
    message_status_events test below — simpler and avoids any RLS
    interaction on the write side, which isn't what this test is about
    (the WITH CHECK / mismatched-INSERT behavior is covered separately by
    ``test_app_role_insert_mismatched_landlord_id_rejected``). The ``seed``
    fixture's own teardown already deletes message_cases rows scoped to
    both seeded landlords, so no extra cleanup is needed here.
    """
    async with db.connect() as connection:
        trans = await connection.begin()
        await connection.execute(
            text("INSERT INTO message_cases (message_id, case_id) VALUES (:m, :c)"),
            {"m": seed.message_a, "c": seed.case_a},
        )
        await connection.execute(
            text("INSERT INTO message_cases (message_id, case_id) VALUES (:m, :c)"),
            {"m": seed.message_b, "c": seed.case_b},
        )
        await trans.commit()

    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})
            visible = (
                (
                    await connection.execute(
                        text("SELECT case_id FROM message_cases WHERE case_id IN (:a, :b)"),
                        {"a": seed.case_a, "b": seed.case_b},
                    )
                )
                .scalars()
                .all()
            )
            assert [str(v) for v in visible] == [seed.case_a]
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_app_role_sees_only_matching_message_status_events(
    db: AsyncEngine, seed: _Seed
) -> None:
    """message_status_events has no landlord_id — EXISTS join through messages."""
    event_a = str(uuid.uuid4())
    event_b = str(uuid.uuid4())
    async with db.connect() as connection:
        trans = await connection.begin()
        await connection.execute(
            text(
                "INSERT INTO message_status_events (message_id, status, payload) "
                "VALUES (:m, 'queued', jsonb_build_object('marker', CAST(:marker AS text)))"
            ),
            {"m": seed.message_a, "marker": event_a},
        )
        await connection.execute(
            text(
                "INSERT INTO message_status_events (message_id, status, payload) "
                "VALUES (:m, 'queued', jsonb_build_object('marker', CAST(:marker AS text)))"
            ),
            {"m": seed.message_b, "marker": event_b},
        )
        await trans.commit()

    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})
            markers = (
                (
                    await connection.execute(
                        text(
                            "SELECT payload ->> 'marker' FROM message_status_events "
                            "WHERE payload ->> 'marker' IN (:a, :b)"
                        ),
                        {"a": event_a, "b": event_b},
                    )
                )
                .scalars()
                .all()
            )
            assert markers == [event_a]
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# 2. Without the GUC set: zero rows visible, zero rows writable.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_role_sees_zero_rows_without_guc_set(db: AsyncEngine, seed: _Seed) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            # Deliberately no set_config call on this connection/transaction.
            for table, id_a, id_b in [
                ("properties", seed.property_a, seed.property_b),
                ("tenants", seed.tenant_a, seed.tenant_b),
                ("cases", seed.case_a, seed.case_b),
                ("messages", seed.message_a, seed.message_b),
                ("landlords", seed.landlord_a, seed.landlord_b),
            ]:
                visible = (
                    (
                        await connection.execute(
                            text(f"SELECT id FROM {table} WHERE id IN (:a, :b)"),  # noqa: S608
                            {"a": id_a, "b": id_b},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert visible == [], f"{table}: expected zero rows with no GUC set, got {visible}"
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# 3. WITH CHECK rejects a mismatched landlord_id on INSERT.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_role_insert_mismatched_landlord_id_rejected(
    db: AsyncEngine, seed: _Seed
) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})

            with pytest.raises(DBAPIError, match="row-level security|row level security"):
                await connection.execute(
                    text(
                        "INSERT INTO properties (landlord_id, label, address_line1, city) "
                        "VALUES (:landlord_id, 'Sneaky', '1 Nowhere', 'Toronto')"
                    ),
                    # Mismatched on purpose: GUC says A, this row claims B.
                    {"landlord_id": seed.landlord_b},
                )
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# 4. UPDATE/DELETE on append-only tables as app_role: permission denied
# (rule #2). Covers all three append-only tables (#22 safety review item
# 9 — messages was already covered; audit_log/message_status_events added).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_role_cannot_update_messages(db: AsyncEngine, seed: _Seed) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})

            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(
                    text("UPDATE messages SET body = 'edited' WHERE id = :id"),
                    {"id": seed.message_a},
                )
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_app_role_cannot_delete_messages(db: AsyncEngine, seed: _Seed) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})

            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(
                    text("DELETE FROM messages WHERE id = :id"),
                    {"id": seed.message_a},
                )
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_app_role_cannot_update_audit_log(db: AsyncEngine, seed: _Seed) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            audit_id = await _insert_audit_log(connection, seed.landlord_a)

            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})

            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(
                    text("UPDATE audit_log SET payload = '{}'::jsonb WHERE id = :id"),
                    {"id": audit_id},
                )
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_app_role_cannot_delete_audit_log(db: AsyncEngine, seed: _Seed) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            audit_id = await _insert_audit_log(connection, seed.landlord_a)

            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})

            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(
                    text("DELETE FROM audit_log WHERE id = :id"),
                    {"id": audit_id},
                )
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_app_role_cannot_update_message_status_events(db: AsyncEngine, seed: _Seed) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            result = await connection.execute(
                text(
                    "INSERT INTO message_status_events (message_id, status) "
                    "VALUES (:m, 'queued') RETURNING id"
                ),
                {"m": seed.message_a},
            )
            event_id = result.scalar_one()

            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})

            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(
                    text("UPDATE message_status_events SET status = 'failed' WHERE id = :id"),
                    {"id": event_id},
                )
        finally:
            await trans.rollback()


@pytest.mark.integration
async def test_app_role_cannot_delete_message_status_events(db: AsyncEngine, seed: _Seed) -> None:
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            result = await connection.execute(
                text(
                    "INSERT INTO message_status_events (message_id, status) "
                    "VALUES (:m, 'queued') RETURNING id"
                ),
                {"m": seed.message_a},
            )
            event_id = result.scalar_one()

            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": seed.landlord_a})

            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(
                    text("DELETE FROM message_status_events WHERE id = :id"),
                    {"id": event_id},
                )
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# 5. Regression pin: /v1/me's provisioning upsert survives RLS via the
# admin path (#22 safety review item 1, BLOCKING; item 6, the regression
# test explicitly requested for it).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_admin_path_provisions_new_landlord_but_app_role_cannot(db: AsyncEngine) -> None:
    """The exact upsert ``GET /v1/me`` issues (``routers/me.py``, via
    ``get_admin_session``) must succeed for a BRAND-NEW ``auth_user_id``
    with NO GUC set at all — proving the provisioning front door survives
    migration 0005 landing RLS on ``landlords``. The SAME statement
    attempted under ``app_role`` (``SET LOCAL ROLE``, see module
    docstring) is rejected both with no GUC set and with the GUC set to an
    unrelated landlord — proving isolation is still fully intact and this
    is not an accidental RLS bypass for everyone, just the deliberate
    admin/service path.

    This is the direct regression pin for the empirically-reproduced
    blocker: under ``app_role``, a freshly ``gen_random_uuid()``'d
    ``landlords.id`` can never equal a GUC value that would have to be set
    BEFORE that id exists — provisioning is structurally incompatible
    with RLS-scoped sessions, which is exactly why ``routers/me.py`` uses
    ``get_admin_session`` instead.
    """
    upsert_sql = text(
        "INSERT INTO landlords (auth_user_id, email, full_name) "
        "VALUES (:auth_user_id, :email, :full_name) "
        "ON CONFLICT (auth_user_id) DO UPDATE "
        "SET email = EXCLUDED.email, "
        "full_name = COALESCE(EXCLUDED.full_name, landlords.full_name), "
        "updated_at = now() "
        "RETURNING id"
    )

    # 1. Admin path (no SET ROLE at all — exactly what get_admin_session
    # gives routers/me.py): succeeds for a brand-new auth_user_id, no GUC.
    admin_auth_user_id = str(uuid.uuid4())
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            result = await connection.execute(
                upsert_sql,
                {
                    "auth_user_id": admin_auth_user_id,
                    "email": f"{admin_auth_user_id}@example.com",
                    "full_name": "New Landlord",
                },
            )
            landlord_id = result.scalar_one()
            assert landlord_id is not None
        finally:
            await trans.rollback()

    # 2. Same statement, a different brand-new auth_user_id, as app_role,
    # NO GUC set: rejected by the landlords WITH CHECK policy.
    app_role_auth_user_id = str(uuid.uuid4())
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            with pytest.raises(DBAPIError, match="row-level security|row level security"):
                await connection.execute(
                    upsert_sql,
                    {
                        "auth_user_id": app_role_auth_user_id,
                        "email": f"{app_role_auth_user_id}@example.com",
                        "full_name": "Nope",
                    },
                )
        finally:
            await trans.rollback()

    # 3. Same, but with the GUC set to an unrelated landlord id: still
    # rejected — a freshly gen_random_uuid()'d id can never equal a GUC
    # value set before that id exists.
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(_set_landlord_guc_sql(), {"landlord_id": str(uuid.uuid4())})
            with pytest.raises(DBAPIError, match="row-level security|row level security"):
                await connection.execute(
                    upsert_sql,
                    {
                        "auth_user_id": app_role_auth_user_id,
                        "email": f"{app_role_auth_user_id}@example.com",
                        "full_name": "Still nope",
                    },
                )
        finally:
            await trans.rollback()
