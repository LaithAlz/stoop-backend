"""Exhaustive cross-tenant RLS isolation matrix — the M2 gate (#23).

``tests/test_rls_isolation.py`` (#22) proves the RLS enforcement
*mechanism* once per scoping SHAPE (direct ``landlord_id``, ``id``-keyed,
and both ``EXISTS``-join tables). This file proves the same enforcement
EXHAUSTIVELY, for every one of the 14 tables in
``docs/03-engineering/schema-v1.md`` (all but ``alembic_version`` —
``push_outbox`` added #210 M3, migration 0012, the first migration since
0005 to add a genuinely new table), across the full operations matrix
(SELECT / UPDATE / DELETE / INSERT), plus the structural gates a senior
review would ask for on top of that:

1. **The full per-table operations matrix** (this file's main body) —
   generated PROGRAMMATICALLY from ``TABLE_DESCRIPTORS`` below, not
   hand-duplicated per table. Adding a 15th table to a future migration
   without adding a matching ``TableDescriptor`` here fails
   ``test_descriptor_table_set_matches_public_schema_catalog`` (reads the
   live catalog, not a hardcoded list) — "a failing policy is impossible
   to merge past" (issue #23 AC) enforced by machine, not by memory.

2. **Catalog completeness gate** — every table in ``public`` (except
   ``alembic_version``) must have ``rowsecurity`` enabled AND exactly one
   policy (``test_every_catalog_table_has_rls_enabled_and_exactly_one_
   policy``). This reads ``pg_class``/``pg_policy`` directly, so a future
   migration that adds a table and forgets ``ENABLE ROW LEVEL SECURITY``
   goes red in CI automatically — no list to remember to update.
   ``tests/test_migrations_0005.py``'s ``test_rls_enabled_not_forced_on_
   every_table`` / ``test_exactly_one_policy_per_table`` proved this
   against a hardcoded ``_ALL_RLS_TABLES`` list for migration 0005 itself;
   this file's version is the future-proof generalisation.

3. **The "endpoint forgets ``require_landlord``" scenario** (senior-review
   gap) — ``test_every_v1_route_except_allowlist_requires_landlord_
   scoping`` walks ``app.routes``' dependency trees and asserts every
   route whose path starts with ``/v1/`` uses ``require_landlord``, except
   an explicit, documented allowlist. Red-fails the moment a future issue
   (e.g. #53+) adds a landlord-scoped endpoint that reaches for
   ``require_user``/``get_session`` directly instead of
   ``require_landlord``. See that test's docstring for the allowlist
   itself and the growth process.

4. **Checkpoint-table isolation** (issue #23 AC) — LangGraph's checkpoint
   tables (``AsyncPostgresSaver.setup()``, #24) do not exist yet.
   ``test_no_tables_outside_descriptor_set_exist_in_public_schema``
   implements the check the issue asks for in the only way possible
   before #24 lands: assert NO table outside ``TABLE_DESCRIPTORS`` exists
   in ``public``. When checkpoint tables DO land, this test forces a
   deliberate decision at that moment — either give them a
   ``TableDescriptor`` + an RLS policy like every other table, or move them
   to a dedicated non-``public`` schema reached only via the admin engine
   (the design already documented in schema-v1.md's v1.2 amendments point
   5 and migration 0005's module docstring point 5) — never silently
   present in ``public`` with no policy and no descriptor.

5. **CI trigger criterion** ("runs on every PR touching migrations or
   deps.py", issue #23 AC): this file's tests are ``integration``/``unit``
   marked and collected by the DEFAULT ``pytest -m "not eval"`` job that
   already runs on every PR (``.github/workflows/ci.yml`` — a Postgres
   service is already provisioned for that job). Being an ordinary member
   of the default test suite means this AC is satisfied by inclusion, not
   by any extra conditional CI wiring: every PR, whether or not it touches
   migrations or ``deps.py``, already runs the full matrix. No
   path-filtered workflow trigger is needed or added.

Conventions carried over from #22 (``tests/test_rls_isolation.py``) —
read that file's module docstring for the full rationale:
- ``SET LOCAL ROLE app_role`` inside a transaction that is ALWAYS rolled
  back — never a real ``LOGIN``, never left switched after the test ends.
- Seed data is inserted and committed by the superuser (``stoop``) in a
  SEPARATE transaction/connection so a later ``app_role``-switched
  transaction can see it; cleanup afterwards is also plain superuser
  ``DELETE`` (bypassing grants entirely, since ``stoop`` is a bootstrap
  superuser, not ``app_role``) — exactly the existing ``seed`` fixture's
  own teardown pattern in ``test_rls_isolation.py``. This file never
  issues ``UPDATE``/``DELETE`` against ``messages``/``audit_log``/
  ``message_status_events`` AS ``app_role`` for any reason other than
  proving those statements are rejected (``permission denied``) — the
  one and only place this file's own assertions expect such a statement
  to even be attempted.
- Helpers are duplicated here rather than imported from
  ``test_rls_isolation.py``/``test_migrations_0005.py``, matching the
  established "each integration test module is self-contained" convention
  in this repo.

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_rls_isolation_matrix.py -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Literal

import pytest
import pytest_asyncio
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# ---------------------------------------------------------------------------
# Helpers — duplicated (not imported) from tests/test_rls_isolation.py /
# tests/test_migrations_0005.py; see module docstring.
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


_SET_ROLE_APP_ROLE_SQL = text("SET LOCAL ROLE app_role")


def _set_landlord_guc_sql() -> object:
    return text("SELECT set_config('app.current_landlord_id', :landlord_id, true)")


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations exactly once per test session (ends at head/0005)."""
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    # Leave schema in place; CI drops the DB container after the run.


@pytest_asyncio.fixture
async def db(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    """Per-test admin (superuser) engine — see ``test_rls_isolation.py``'s
    module docstring for why ``SET LOCAL ROLE app_role`` on this connection
    is how these tests become genuinely subject to RLS."""
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


# ---------------------------------------------------------------------------
# Seed — one row per landlord (A, B) in every one of the 14 tables.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MatrixSeed:
    landlord_a: str
    landlord_b: str
    property_a: str
    property_b: str
    vendor_a: str
    vendor_b: str
    tenant_a: str
    tenant_b: str
    case_a: str
    case_b: str
    message_a: str
    message_b: str
    draft_a: str
    draft_b: str
    trust_metric_a: str
    trust_metric_b: str
    audit_log_a: int
    audit_log_b: int
    notification_a: str
    notification_b: str
    push_token_a: str
    push_token_b: str
    push_outbox_a: str
    push_outbox_b: str
    message_status_event_a: int
    message_status_event_b: int
    # message_cases has no id of its own; identified by its (unique, in this
    # seed) case_id — same value as case_a/case_b above, kept as separate
    # fields for descriptor clarity.
    message_cases_case_a: str
    message_cases_case_b: str


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


async def _insert_vendor(conn: AsyncConnection, landlord_id: str) -> str:
    vendor_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO vendors (id, landlord_id, name, trade, phone) "
            "VALUES (:id, :landlord_id, 'Test Vendor', 'plumbing', :phone)"
        ),
        {
            "id": vendor_id,
            "landlord_id": landlord_id,
            "phone": f"+1416{uuid.uuid4().int % 10_000_000:07d}",
        },
    )
    return vendor_id


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


async def _insert_message_cases(conn: AsyncConnection, message_id: str, case_id: str) -> None:
    await conn.execute(
        text("INSERT INTO message_cases (message_id, case_id) VALUES (:m, :c)"),
        {"m": message_id, "c": case_id},
    )


async def _insert_message_status_event(conn: AsyncConnection, message_id: str) -> int:
    result = await conn.execute(
        text(
            "INSERT INTO message_status_events (message_id, status) "
            "VALUES (:m, 'queued') RETURNING id"
        ),
        {"m": message_id},
    )
    return int(result.scalar_one())


async def _insert_draft(conn: AsyncConnection, landlord_id: str, case_id: str) -> str:
    draft_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO drafts (id, landlord_id, case_id, recipient, body, prompt_version) "
            "VALUES (:id, :landlord_id, :case_id, 'tenant', 'test draft body', 'v1')"
        ),
        {"id": draft_id, "landlord_id": landlord_id, "case_id": case_id},
    )
    return draft_id


async def _insert_trust_metric(conn: AsyncConnection, landlord_id: str, property_id: str) -> str:
    trust_metric_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO trust_metrics (id, landlord_id, property_id, severity) "
            "VALUES (:id, :landlord_id, :property_id, 'routine')"
        ),
        {"id": trust_metric_id, "landlord_id": landlord_id, "property_id": property_id},
    )
    return trust_metric_id


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


async def _insert_notification(conn: AsyncConnection, landlord_id: str) -> str:
    notification_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO notifications (id, landlord_id, type, channel) "
            "VALUES (:id, :landlord_id, 'needs_eyes', 'push')"
        ),
        {"id": notification_id, "landlord_id": landlord_id},
    )
    return notification_id


async def _insert_push_token(conn: AsyncConnection, landlord_id: str) -> str:
    push_token_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO push_tokens (id, landlord_id, token, platform) "
            "VALUES (:id, :landlord_id, :token, 'web')"
        ),
        {"id": push_token_id, "landlord_id": landlord_id, "token": f"token-{uuid.uuid4()}"},
    )
    return push_token_id


async def _insert_push_outbox(conn: AsyncConnection, landlord_id: str, device_token_id: str) -> str:
    push_outbox_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO push_outbox (id, landlord_id, device_token_id, kind) "
            "VALUES (:id, :landlord_id, :device_token_id, 'draft_awaiting_approval')"
        ),
        {"id": push_outbox_id, "landlord_id": landlord_id, "device_token_id": device_token_id},
    )
    return push_outbox_id


@pytest_asyncio.fixture
async def matrix_seed(db: AsyncEngine) -> AsyncGenerator[_MatrixSeed, None]:
    """Two landlords, each with one row in every one of the 14 tables,
    committed (not rolled back) so later, separate role-switched
    transactions can see them — same pattern as ``test_rls_isolation.py``'s
    ``seed`` fixture, extended to cover every table."""
    async with db.connect() as connection:
        trans = await connection.begin()
        landlord_a = await _insert_landlord(connection)
        landlord_b = await _insert_landlord(connection)
        property_a = await _insert_property(connection, landlord_a)
        property_b = await _insert_property(connection, landlord_b)
        vendor_a = await _insert_vendor(connection, landlord_a)
        vendor_b = await _insert_vendor(connection, landlord_b)
        tenant_a = await _insert_tenant(connection, landlord_a, property_a)
        tenant_b = await _insert_tenant(connection, landlord_b, property_b)
        case_a = await _insert_case(connection, landlord_a, property_a, tenant_a)
        case_b = await _insert_case(connection, landlord_b, property_b, tenant_b)
        message_a = await _insert_message(connection, landlord_a, property_a, tenant_a, case_a)
        message_b = await _insert_message(connection, landlord_b, property_b, tenant_b, case_b)
        await _insert_message_cases(connection, message_a, case_a)
        await _insert_message_cases(connection, message_b, case_b)
        event_a = await _insert_message_status_event(connection, message_a)
        event_b = await _insert_message_status_event(connection, message_b)
        draft_a = await _insert_draft(connection, landlord_a, case_a)
        draft_b = await _insert_draft(connection, landlord_b, case_b)
        trust_metric_a = await _insert_trust_metric(connection, landlord_a, property_a)
        trust_metric_b = await _insert_trust_metric(connection, landlord_b, property_b)
        audit_log_a = await _insert_audit_log(connection, landlord_a)
        audit_log_b = await _insert_audit_log(connection, landlord_b)
        notification_a = await _insert_notification(connection, landlord_a)
        notification_b = await _insert_notification(connection, landlord_b)
        push_token_a = await _insert_push_token(connection, landlord_a)
        push_token_b = await _insert_push_token(connection, landlord_b)
        push_outbox_a = await _insert_push_outbox(connection, landlord_a, push_token_a)
        push_outbox_b = await _insert_push_outbox(connection, landlord_b, push_token_b)
        await trans.commit()

    seeded = _MatrixSeed(
        landlord_a=landlord_a,
        landlord_b=landlord_b,
        property_a=property_a,
        property_b=property_b,
        vendor_a=vendor_a,
        vendor_b=vendor_b,
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        case_a=case_a,
        case_b=case_b,
        message_a=message_a,
        message_b=message_b,
        draft_a=draft_a,
        draft_b=draft_b,
        trust_metric_a=trust_metric_a,
        trust_metric_b=trust_metric_b,
        audit_log_a=audit_log_a,
        audit_log_b=audit_log_b,
        notification_a=notification_a,
        notification_b=notification_b,
        push_token_a=push_token_a,
        push_token_b=push_token_b,
        push_outbox_a=push_outbox_a,
        push_outbox_b=push_outbox_b,
        message_status_event_a=event_a,
        message_status_event_b=event_b,
        message_cases_case_a=case_a,
        message_cases_case_b=case_b,
    )
    try:
        yield seeded
    finally:
        # Superuser DELETE cleanup, in FK-dependency-safe (children-first)
        # order. This is the SAME convention test_rls_isolation.py's own
        # ``seed`` fixture teardown uses for messages/audit_log — plain
        # superuser DELETE, never app_role, never application code; the
        # append-only REVOKE (rule #2) only applies to app_role. Nothing
        # here is ever attempted as app_role.
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
                    text("DELETE FROM drafts WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM notifications WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM messages WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM cases WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM trust_metrics WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM tenants WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM push_outbox WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM push_tokens WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM vendors WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM audit_log WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM properties WHERE landlord_id = :id"), {"id": landlord_id}
                )
                await connection.execute(
                    text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id}
                )
            await trans.commit()


# ---------------------------------------------------------------------------
# Table descriptors — the programmatic matrix generator.
#
# Every one of the 14 schema-v1.md tables (all but alembic_version) gets
# exactly one ``TableDescriptor``. ``TABLE_DESCRIPTORS`` is asserted equal
# to the live public-schema catalog below — a 15th table added to a future
# migration without a matching descriptor here fails that assertion, which
# is exactly the enforcement issue #23 asks for.
# ---------------------------------------------------------------------------

_ScopingShape = Literal["direct_landlord_id", "id_keyed", "exists_join"]


@dataclass(frozen=True)
class TableDescriptor:
    """One row's worth of test data + SQL shapes for a single table.

    ``row_a_id``/``row_b_id`` extract the value used to identify each
    landlord's seeded row (``id`` for most tables; ``case_id`` for
    ``message_cases``, which has no single-column id of its own).
    ``update_sql``/``delete_sql`` target ``:row_id`` against that same
    column, with a harmless self-assigning ``SET`` clause for UPDATE (the
    isolation proof is that the WHERE clause matches zero rows once RLS
    hides landlord B's row — what the SET clause assigns is irrelevant).
    ``insert_mismatch_sql``/``insert_mismatch_params`` construct an INSERT
    whose owning landlord (or, for ``landlords`` itself, its own ``id``)
    is landlord B's while every OTHER foreign key in the row is also
    consistently landlord B's own data — isolating the failure to the
    ONE thing under test (the row's own landlord scoping vs. the
    session's GUC), never an incidental FK/logical mismatch.
    """

    name: str
    scoping: _ScopingShape
    append_only: bool
    id_column: str
    row_a_id: Callable[[_MatrixSeed], str | int]
    row_b_id: Callable[[_MatrixSeed], str | int]
    update_sql: str
    delete_sql: str
    insert_mismatch_sql: str
    insert_mismatch_params: Callable[[_MatrixSeed], dict[str, object]]


TABLE_DESCRIPTORS: list[TableDescriptor] = [
    TableDescriptor(
        name="landlords",
        scoping="id_keyed",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.landlord_a,
        row_b_id=lambda s: s.landlord_b,
        update_sql="UPDATE landlords SET full_name = full_name WHERE id = :row_id",
        delete_sql="DELETE FROM landlords WHERE id = :row_id",
        # Special case: landlords is id-keyed, not landlord_id-keyed, and a
        # brand-new row's id is gen_random_uuid()'d — it can never equal the
        # GUC set before that id exists. This is the generalised version of
        # the dedicated test_admin_path_provisions_new_landlord_but_app_
        # role_cannot proof, folded into the matrix.
        insert_mismatch_sql=(
            "INSERT INTO landlords (auth_user_id, email) VALUES (:auth_user_id, :email)"
        ),
        insert_mismatch_params=lambda _s: {
            "auth_user_id": str(uuid.uuid4()),
            "email": f"{uuid.uuid4()}@example.com",
        },
    ),
    TableDescriptor(
        name="properties",
        scoping="direct_landlord_id",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.property_a,
        row_b_id=lambda s: s.property_b,
        update_sql="UPDATE properties SET label = label WHERE id = :row_id",
        delete_sql="DELETE FROM properties WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO properties (landlord_id, label, address_line1, city) "
            "VALUES (:landlord_id, 'Sneaky', '1 Nowhere', 'Toronto')"
        ),
        insert_mismatch_params=lambda s: {"landlord_id": s.landlord_b},
    ),
    TableDescriptor(
        name="vendors",
        scoping="direct_landlord_id",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.vendor_a,
        row_b_id=lambda s: s.vendor_b,
        update_sql="UPDATE vendors SET name = name WHERE id = :row_id",
        delete_sql="DELETE FROM vendors WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO vendors (landlord_id, name, trade, phone) "
            "VALUES (:landlord_id, 'Sneaky Vendor', 'plumbing', :phone)"
        ),
        insert_mismatch_params=lambda s: {
            "landlord_id": s.landlord_b,
            "phone": f"+1416{uuid.uuid4().int % 10_000_000:07d}",
        },
    ),
    TableDescriptor(
        name="tenants",
        scoping="direct_landlord_id",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.tenant_a,
        row_b_id=lambda s: s.tenant_b,
        update_sql="UPDATE tenants SET name = name WHERE id = :row_id",
        delete_sql="DELETE FROM tenants WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO tenants (landlord_id, property_id, phone) "
            "VALUES (:landlord_id, :property_id, :phone)"
        ),
        insert_mismatch_params=lambda s: {
            "landlord_id": s.landlord_b,
            "property_id": s.property_b,
            "phone": f"+1416{uuid.uuid4().int % 10_000_000:07d}",
        },
    ),
    TableDescriptor(
        name="cases",
        scoping="direct_landlord_id",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.case_a,
        row_b_id=lambda s: s.case_b,
        update_sql="UPDATE cases SET title = title WHERE id = :row_id",
        delete_sql="DELETE FROM cases WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO cases (landlord_id, property_id, tenant_id, langgraph_thread_id) "
            "VALUES (:landlord_id, :property_id, :tenant_id, :thread_id)"
        ),
        insert_mismatch_params=lambda s: {
            "landlord_id": s.landlord_b,
            "property_id": s.property_b,
            "tenant_id": s.tenant_b,
            "thread_id": f"thread-{uuid.uuid4()}",
        },
    ),
    TableDescriptor(
        name="messages",
        scoping="direct_landlord_id",
        append_only=True,
        id_column="id",
        row_a_id=lambda s: s.message_a,
        row_b_id=lambda s: s.message_b,
        update_sql="UPDATE messages SET body = 'edited' WHERE id = :row_id",
        delete_sql="DELETE FROM messages WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO messages (landlord_id, property_id, tenant_id, case_id, "
            "direction, party, body) "
            "VALUES (:landlord_id, :property_id, :tenant_id, :case_id, "
            "'inbound', 'tenant', 'sneaky')"
        ),
        insert_mismatch_params=lambda s: {
            "landlord_id": s.landlord_b,
            "property_id": s.property_b,
            "tenant_id": s.tenant_b,
            "case_id": s.case_b,
        },
    ),
    TableDescriptor(
        name="message_cases",
        scoping="exists_join",
        append_only=False,
        id_column="case_id",
        row_a_id=lambda s: s.message_cases_case_a,
        row_b_id=lambda s: s.message_cases_case_b,
        update_sql="UPDATE message_cases SET case_id = case_id WHERE case_id = :row_id",
        delete_sql="DELETE FROM message_cases WHERE case_id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO message_cases (message_id, case_id) VALUES (:message_id, :case_id)"
        ),
        insert_mismatch_params=lambda s: {"message_id": s.message_b, "case_id": s.case_b},
    ),
    TableDescriptor(
        name="message_status_events",
        scoping="exists_join",
        append_only=True,
        id_column="id",
        row_a_id=lambda s: s.message_status_event_a,
        row_b_id=lambda s: s.message_status_event_b,
        update_sql="UPDATE message_status_events SET status = 'failed' WHERE id = :row_id",
        delete_sql="DELETE FROM message_status_events WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO message_status_events (message_id, status) VALUES (:message_id, 'queued')"
        ),
        insert_mismatch_params=lambda s: {"message_id": s.message_b},
    ),
    TableDescriptor(
        name="drafts",
        scoping="direct_landlord_id",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.draft_a,
        row_b_id=lambda s: s.draft_b,
        update_sql="UPDATE drafts SET body = body WHERE id = :row_id",
        delete_sql="DELETE FROM drafts WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO drafts (landlord_id, case_id, recipient, body, prompt_version) "
            "VALUES (:landlord_id, :case_id, 'tenant', 'sneaky', 'v1')"
        ),
        insert_mismatch_params=lambda s: {"landlord_id": s.landlord_b, "case_id": s.case_b},
    ),
    TableDescriptor(
        name="trust_metrics",
        scoping="direct_landlord_id",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.trust_metric_a,
        row_b_id=lambda s: s.trust_metric_b,
        update_sql="UPDATE trust_metrics SET clean_approvals = clean_approvals WHERE id = :row_id",
        delete_sql="DELETE FROM trust_metrics WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO trust_metrics (landlord_id, property_id, severity) "
            "VALUES (:landlord_id, :property_id, 'urgent')"
        ),
        insert_mismatch_params=lambda s: {"landlord_id": s.landlord_b, "property_id": s.property_b},
    ),
    TableDescriptor(
        name="audit_log",
        scoping="direct_landlord_id",
        append_only=True,
        id_column="id",
        row_a_id=lambda s: s.audit_log_a,
        row_b_id=lambda s: s.audit_log_b,
        update_sql="UPDATE audit_log SET payload = '{}'::jsonb WHERE id = :row_id",
        delete_sql="DELETE FROM audit_log WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO audit_log (landlord_id, actor, action) "
            "VALUES (:landlord_id, 'system', 'message_received')"
        ),
        insert_mismatch_params=lambda s: {"landlord_id": s.landlord_b},
    ),
    TableDescriptor(
        name="notifications",
        scoping="direct_landlord_id",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.notification_a,
        row_b_id=lambda s: s.notification_b,
        update_sql="UPDATE notifications SET status = status WHERE id = :row_id",
        delete_sql="DELETE FROM notifications WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO notifications (landlord_id, type, channel) "
            "VALUES (:landlord_id, 'needs_eyes', 'push')"
        ),
        insert_mismatch_params=lambda s: {"landlord_id": s.landlord_b},
    ),
    TableDescriptor(
        name="push_tokens",
        scoping="direct_landlord_id",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.push_token_a,
        row_b_id=lambda s: s.push_token_b,
        update_sql="UPDATE push_tokens SET last_seen_at = last_seen_at WHERE id = :row_id",
        delete_sql="DELETE FROM push_tokens WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO push_tokens (landlord_id, token, platform) "
            "VALUES (:landlord_id, :token, 'web')"
        ),
        insert_mismatch_params=lambda s: {
            "landlord_id": s.landlord_b,
            "token": f"token-{uuid.uuid4()}",
        },
    ),
    TableDescriptor(
        name="push_outbox",
        scoping="direct_landlord_id",
        append_only=False,
        id_column="id",
        row_a_id=lambda s: s.push_outbox_a,
        row_b_id=lambda s: s.push_outbox_b,
        update_sql="UPDATE push_outbox SET attempt = attempt WHERE id = :row_id",
        delete_sql="DELETE FROM push_outbox WHERE id = :row_id",
        insert_mismatch_sql=(
            "INSERT INTO push_outbox (landlord_id, device_token_id, kind) "
            "VALUES (:landlord_id, :device_token_id, 'draft_awaiting_approval')"
        ),
        insert_mismatch_params=lambda s: {
            "landlord_id": s.landlord_b,
            "device_token_id": s.push_token_b,
        },
    ),
]

_DESCRIPTOR_IDS = [d.name for d in TABLE_DESCRIPTORS]


@pytest.mark.unit
def test_table_descriptors_cover_exactly_fourteen_tables() -> None:
    """Cheap, DB-free sanity pin: schema-v1.md lists 14 tables besides
    ``alembic_version`` (the original 13 enumerated by the doc's v1.2
    amendments block and migration 0005's module docstring, plus
    ``push_outbox`` — added by the v1.13 amendments block / migration
    0012, #210 M3)."""
    assert len(TABLE_DESCRIPTORS) == 14
    assert len({d.name for d in TABLE_DESCRIPTORS}) == 14, "descriptor names must be unique"


# ---------------------------------------------------------------------------
# 1. Catalog completeness gate — descriptor set vs. the live catalog.
# ---------------------------------------------------------------------------


async def _public_tables_except_alembic_version(db: AsyncEngine) -> set[str]:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relname FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                "AND relname <> 'alembic_version'"
            )
        )
        return {row[0] for row in result.fetchall()}


@pytest.mark.integration
async def test_descriptor_table_set_matches_public_schema_catalog(db: AsyncEngine) -> None:
    """``TABLE_DESCRIPTORS`` must exactly match every real table in
    ``public`` (except ``alembic_version``) — read from the live catalog,
    not a hardcoded list. A migration that adds a 15th table without a
    matching descriptor here, or a descriptor for a table that no longer
    exists, fails this test."""
    catalog_tables = await _public_tables_except_alembic_version(db)
    descriptor_tables = set(_DESCRIPTOR_IDS)

    assert descriptor_tables == catalog_tables, (
        "TABLE_DESCRIPTORS is out of sync with the public schema catalog: "
        f"descriptors missing from catalog = {descriptor_tables - catalog_tables}, "
        f"catalog tables missing a descriptor = {catalog_tables - descriptor_tables}. "
        "Add/remove a TableDescriptor (scoping shape + seed rows + SQL "
        "shapes) in tests/test_rls_isolation_matrix.py to match — see the "
        "module docstring's 'catalog completeness gate'."
    )


@pytest.mark.integration
async def test_every_catalog_table_has_rls_enabled_and_exactly_one_policy(
    db: AsyncEngine,
) -> None:
    """Every table in ``public`` (except ``alembic_version``) — read from
    the catalog, not a hardcoded list — must have ``rowsecurity`` enabled
    AND exactly one policy. A future migration that adds a table and
    forgets ``ENABLE ROW LEVEL SECURITY`` (or adds a second, conflicting
    policy) goes red here automatically."""
    async with db.connect() as connection:
        table_rows = await connection.execute(
            text(
                "SELECT relname, relrowsecurity FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                "AND relname <> 'alembic_version'"
            )
        )
        tables = dict(table_rows.fetchall())

        policy_rows = await connection.execute(
            text("SELECT polrelid::regclass::text, polname FROM pg_policy")
        )
        policies_by_table: dict[str, list[str]] = {}
        for table, policy in policy_rows.fetchall():
            policies_by_table.setdefault(table, []).append(policy)

    assert tables, "expected at least one table in the public schema"

    for table, rls_enabled in tables.items():
        assert rls_enabled is True, f"{table}: RLS (rowsecurity) must be enabled"
        policies = policies_by_table.get(table, [])
        assert len(policies) == 1, f"{table}: expected exactly one policy, got {policies}"


# ---------------------------------------------------------------------------
# 2. Checkpoint-table isolation (#23 AC) — #24 hasn't landed; enforce that
# no table exists outside the descriptor set at all.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_tables_outside_descriptor_set_exist_in_public_schema(db: AsyncEngine) -> None:
    """Checkpoint-table isolation, verified the only way possible before
    #24 (LangGraph's ``AsyncPostgresSaver.setup()`` checkpoint tables)
    lands: assert no table exists in ``public`` outside
    ``TABLE_DESCRIPTORS``.

    When checkpoint tables DO land, this goes red and forces a deliberate
    choice at that moment (schema-v1.md v1.2 amendments point 5; migration
    0005 module docstring point 5): either (a) give them a
    ``TableDescriptor`` here + a real RLS policy in a migration, exactly
    like every other table, or (b) put them in a dedicated schema reached
    only via the admin engine, never ``app_role`` and never ``public`` —
    a documented exclusion, not a silent gap. Either way, this test is
    what makes the decision non-optional instead of an oversight.
    """
    catalog_tables = await _public_tables_except_alembic_version(db)
    descriptor_tables = set(_DESCRIPTOR_IDS)
    extra = catalog_tables - descriptor_tables

    assert not extra, (
        f"unexpected table(s) in public schema with no RLS descriptor: {sorted(extra)}. "
        "See test_no_tables_outside_descriptor_set_exist_in_public_schema's "
        "docstring for the two acceptable outcomes (descriptor + policy, or "
        "a documented separate-schema exclusion)."
    )


# ---------------------------------------------------------------------------
# 3. The operations matrix — SELECT / UPDATE / DELETE / INSERT, generated
# from TABLE_DESCRIPTORS. 14 tables x 4 operations = 56 parametrized cases.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("descriptor", TABLE_DESCRIPTORS, ids=_DESCRIPTOR_IDS)
async def test_select_matrix_landlord_a_sees_only_own_row(
    db: AsyncEngine, matrix_seed: _MatrixSeed, descriptor: TableDescriptor
) -> None:
    """SELECT, every table: landlord A's session sees A's row and not B's."""
    row_a = descriptor.row_a_id(matrix_seed)
    row_b = descriptor.row_b_id(matrix_seed)

    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(
                _set_landlord_guc_sql(), {"landlord_id": matrix_seed.landlord_a}
            )
            visible = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {descriptor.id_column} FROM {descriptor.name} "  # noqa: S608
                            f"WHERE {descriptor.id_column} IN (:a, :b)"
                        ),
                        {"a": row_a, "b": row_b},
                    )
                )
                .scalars()
                .all()
            )
            assert [str(v) for v in visible] == [str(row_a)], (
                f"{descriptor.name}: expected only landlord A's row visible, got {visible}"
            )
        finally:
            await trans.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("descriptor", TABLE_DESCRIPTORS, ids=_DESCRIPTOR_IDS)
async def test_update_matrix_landlord_a_cannot_touch_landlord_b_row(
    db: AsyncEngine, matrix_seed: _MatrixSeed, descriptor: TableDescriptor
) -> None:
    """UPDATE of landlord B's row, every table, under landlord A's GUC:
    append-only tables reject outright (permission denied, rule #2);
    every other table's UPDATE matches zero rows (RLS hides B's row from
    the WHERE clause entirely — not an error)."""
    row_b = descriptor.row_b_id(matrix_seed)

    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(
                _set_landlord_guc_sql(), {"landlord_id": matrix_seed.landlord_a}
            )
            if descriptor.append_only:
                with pytest.raises(DBAPIError, match="permission denied"):
                    await connection.execute(text(descriptor.update_sql), {"row_id": row_b})
            else:
                result = await connection.execute(text(descriptor.update_sql), {"row_id": row_b})
                assert result.rowcount == 0, (
                    f"{descriptor.name}: UPDATE of landlord B's row should affect 0 rows "
                    f"under landlord A's GUC, affected {result.rowcount}"
                )
        finally:
            await trans.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("descriptor", TABLE_DESCRIPTORS, ids=_DESCRIPTOR_IDS)
async def test_delete_matrix_landlord_a_cannot_touch_landlord_b_row(
    db: AsyncEngine, matrix_seed: _MatrixSeed, descriptor: TableDescriptor
) -> None:
    """DELETE of landlord B's row, every table, under landlord A's GUC —
    same shape as the UPDATE matrix above."""
    row_b = descriptor.row_b_id(matrix_seed)

    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(
                _set_landlord_guc_sql(), {"landlord_id": matrix_seed.landlord_a}
            )
            if descriptor.append_only:
                with pytest.raises(DBAPIError, match="permission denied"):
                    await connection.execute(text(descriptor.delete_sql), {"row_id": row_b})
            else:
                result = await connection.execute(text(descriptor.delete_sql), {"row_id": row_b})
                assert result.rowcount == 0, (
                    f"{descriptor.name}: DELETE of landlord B's row should affect 0 rows "
                    f"under landlord A's GUC, affected {result.rowcount}"
                )
        finally:
            await trans.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("descriptor", TABLE_DESCRIPTORS, ids=_DESCRIPTOR_IDS)
async def test_insert_matrix_mismatched_landlord_rejected(
    db: AsyncEngine, matrix_seed: _MatrixSeed, descriptor: TableDescriptor
) -> None:
    """INSERT with landlord B's owning key while the GUC says landlord A,
    every table: rejected by WITH CHECK regardless of append-only status
    (append-only tables still get INSERT — rule #2 only revokes UPDATE/
    DELETE — so WITH CHECK still applies to their INSERTs)."""
    params = descriptor.insert_mismatch_params(matrix_seed)

    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            await connection.execute(_SET_ROLE_APP_ROLE_SQL)
            await connection.execute(
                _set_landlord_guc_sql(), {"landlord_id": matrix_seed.landlord_a}
            )
            with pytest.raises(DBAPIError, match="row-level security|row level security"):
                await connection.execute(text(descriptor.insert_mismatch_sql), params)
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# 4. The "endpoint forgets require_landlord" scenario.
#
# Structure: every route whose path starts with "/v1/" must include
# require_landlord somewhere in its dependency tree, EXCEPT an explicit,
# documented allowlist of deliberately-exempt routes.
#
# ALLOWLIST GROWTH PROCESS — read this before adding an entry:
#   1. A new /v1/ endpoint is landlord-scoped by default. Use
#      `Depends(require_landlord)` (see app/deps.py's usage example) and
#      this test passes with no changes.
#   2. If an endpoint is genuinely pre-identity/admin-by-design (like
#      GET /v1/me's provisioning upsert) or a deliberately temporary,
#      non-tenant-scoped debug/manual-verification endpoint (like
#      GET /v1/auth-test), add its exact path to
#      _LANDLORD_SCOPING_ALLOWLIST below, WITH a comment explaining why,
#      in the same PR that adds the endpoint — never as a drive-by fix to
#      an unrelated failure.
#   3. When an allowlisted endpoint is removed (e.g. auth-test once #11
#      fully supersedes it), remove its entry in the SAME PR —
#      test_landlord_scoping_allowlist_entries_are_live_routes below
#      fails loudly if the allowlist ever points at a route that no
#      longer exists, forcing that cleanup instead of letting the
#      allowlist silently rot.
#   4. Health checks (/healthz, /readyz) and future webhook endpoints
#      (Twilio/Stripe, #40+) need NO entry at all: they are not under
#      `/v1/` (health has no prefix; webhooks are expected to land under
#      their own non-`/v1/` prefix per apps/api/CLAUDE.md's router layout)
#      and so never match this test's filter in the first place. If a
#      webhook or health endpoint is ever moved under `/v1/`, it must
#      either use require_landlord or be added here deliberately, per
#      step 2.
# ---------------------------------------------------------------------------

_LANDLORD_SCOPED_PREFIX = "/v1/"

_LANDLORD_SCOPING_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Provisioning path — the first request for a new auth user has no
        # `landlords` row yet, so there is no landlord to scope by; this
        # endpoint lazily CREATES that row (app/routers/me.py's module
        # docstring, "Session: get_admin_session ... #22 safety review,
        # BLOCKING item"). Deliberately require_user, not require_landlord.
        "/v1/me",
        # Temporary manual-JWT-verification endpoint for engineers testing
        # the JWKS pipeline against real Supabase tokens
        # (app/routers/auth_test.py) — predates GET /v1/me (#11) and is
        # marked for removal in a follow-up PR; not landlord-scoped by
        # design (it only echoes the verified identity back to its own
        # caller). Remove this allowlist entry in the same PR that removes
        # the router.
        "/v1/auth-test",
    }
)


def _collect_dependency_callables(dependant: Dependant) -> set[object]:
    """Recursively collect every ``.call`` callable in a route's dependant
    tree (top-level parameters AND nested sub-dependencies), so that
    ``require_landlord`` is found even when it's a dependency of a
    dependency."""
    calls: set[object] = {dependant.call}
    for sub in dependant.dependencies:
        calls |= _collect_dependency_callables(sub)
    return calls


@pytest.mark.unit
def test_every_v1_route_except_allowlist_requires_landlord_scoping() -> None:
    """Every route whose path starts with ``/v1/`` must have
    ``require_landlord`` somewhere in its dependency tree, except the
    explicit, documented ``_LANDLORD_SCOPING_ALLOWLIST`` above.

    Red-fails the moment a future issue (#53 onward) adds a
    landlord-scoped endpoint that reaches for `require_user`/`get_session`
    directly instead of `require_landlord` — exactly the "endpoint forgets
    require_landlord" senior-review gap this test closes by machine.
    """
    from app.deps import require_landlord
    from app.main import app as fastapi_app

    offending: list[str] = []
    for route in fastapi_app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith(_LANDLORD_SCOPED_PREFIX):
            continue
        if route.path in _LANDLORD_SCOPING_ALLOWLIST:
            continue

        calls = _collect_dependency_callables(route.dependant)
        if require_landlord not in calls:
            offending.append(route.path)

    assert not offending, (
        f"route(s) under {_LANDLORD_SCOPED_PREFIX!r} missing require_landlord "
        f"and not on the allowlist: {offending}. Either add "
        "Depends(require_landlord) to the endpoint, or — if it is genuinely "
        "pre-identity/admin-by-design — add it to "
        "_LANDLORD_SCOPING_ALLOWLIST with a comment explaining why (see "
        "that constant's docstring for the growth process)."
    )


@pytest.mark.unit
def test_landlord_scoping_allowlist_entries_are_live_routes() -> None:
    """The allowlist must never point at a route that no longer exists —
    otherwise a removed endpoint (e.g. auth-test once #11 fully supersedes
    it) leaves a stale entry that silently narrows future coverage instead
    of forcing the deliberate cleanup step 3 (see the allowlist's growth
    process docstring above) asks for."""
    from app.main import app as fastapi_app

    live_paths = {route.path for route in fastapi_app.routes if isinstance(route, APIRoute)}
    stale = _LANDLORD_SCOPING_ALLOWLIST - live_paths
    assert not stale, (
        f"_LANDLORD_SCOPING_ALLOWLIST references route(s) that no longer "
        f"exist: {sorted(stale)} — remove the stale entry/entries."
    )
