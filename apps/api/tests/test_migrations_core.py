"""Integration tests for the core-schema migration (revision 0002).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0002 creates the eleven tables listed in schema-v1.md beyond
``landlords``: properties, vendors, tenants, cases, messages, message_cases,
drafts, trust_metrics, audit_log, notifications, push_tokens.

These tests verify (per docs/03-engineering/schema-v1.md, the canonical
source — see also ``tests/test_migrations.py`` for the revision-0001
equivalent, whose ``_alembic``/``_migrate_once`` harness this module
mirrors):

1. All 11 tables exist after ``upgrade head``; downgrade to 0001 removes
   them but leaves ``landlords``; re-upgrade restores them.
2. Load-bearing constraint behavior, exercised via real INSERT/DELETE
   statements (not just catalog inspection):
   - ``uq_drafts_one_pending`` — one pending draft per case, ever.
   - ``cases.severity`` CHECK — lowercase accepted, uppercase rejected
     (guards the ``Severity.db_value`` lowercasing convention).
   - UNIQUE constraints: trust_metrics(property_id, severity),
     tenants(property_id, phone), vendors(landlord_id, phone),
     cases.langgraph_thread_id, messages.twilio_sid.
   - audit_log.actor / audit_log.action CHECKs reject out-of-vocabulary
     values; audit_log has no FK on landlord_id/case_id (rows survive
     deletes of the referenced rows, intentionally).
   - FK ON DELETE RESTRICT (landlords -> properties) and push_tokens'
     ON DELETE CASCADE (landlords -> push_tokens).
3. The deferred-REVOKE append-only gate (rule #2) is still documented in
   the migration source for both ``messages`` and ``audit_log`` — so it
   can't silently disappear in a refactor before #22 closes it. Actual
   REVOKE enforcement is NOT tested here: the app role doesn't exist in
   local Postgres (documented deferral, HANDOFF option 1).

Every test that touches data uses its own connection wrapped in an
explicit transaction that is always rolled back at teardown (see the
``conn`` fixture) — nothing is ever committed, so nothing needs deleting,
and no test ever issues UPDATE/DELETE against the append-only
``messages``/``audit_log`` tables (INSERT is exercised via ``execute``
inside that same doomed-to-rollback transaction).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_core.py -m integration -v
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# ---------------------------------------------------------------------------
# Helpers — mirrors tests/test_migrations.py (duplicated, not imported, to
# keep this module self-contained and avoid cross-module fixture-identity
# ambiguity in pytest's session-scoped caching).
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "migrations" / "versions" / "0002_core_schema.py"
)

_CORE_TABLES: list[str] = [
    "properties",
    "vendors",
    "tenants",
    "cases",
    "messages",
    "message_cases",
    "drafts",
    "trust_metrics",
    "audit_log",
    "notifications",
    "push_tokens",
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
    """Apply migrations exactly once per test session."""
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

    Every constraint test runs INSERT/DELETE statements inside this
    transaction and never commits, so no cleanup step is needed and no test
    ever leaves rows behind for other tests (or other test modules sharing
    this Postgres instance) to trip over.
    """
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            yield connection
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# Row-builder helpers (FK chain: landlord -> property -> tenant/vendor -> case)
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


async def _insert_tenant(
    conn: AsyncConnection, landlord_id: str, property_id: str, *, phone: str | None = None
) -> str:
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
            "phone": phone or f"+1416555{uuid.uuid4().int % 10000:04d}",
        },
    )
    return tenant_id


async def _insert_vendor(
    conn: AsyncConnection, landlord_id: str, *, phone: str | None = None
) -> str:
    vendor_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO vendors (id, landlord_id, name, trade, phone) "
            "VALUES (:id, :landlord_id, 'Test Vendor', 'plumbing', :phone)"
        ),
        {
            "id": vendor_id,
            "landlord_id": landlord_id,
            "phone": phone or f"+1416555{uuid.uuid4().int % 10000:04d}",
        },
    )
    return vendor_id


async def _insert_case(
    conn: AsyncConnection,
    landlord_id: str,
    property_id: str,
    tenant_id: str,
    *,
    severity: str | None = None,
    thread_id: str | None = None,
) -> str:
    case_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO cases (id, landlord_id, property_id, tenant_id, severity, "
            "langgraph_thread_id) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, :severity, :thread_id)"
        ),
        {
            "id": case_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "severity": severity,
            "thread_id": thread_id or f"thread-{uuid.uuid4()}",
        },
    )
    return case_id


async def _insert_draft(
    conn: AsyncConnection, landlord_id: str, case_id: str, *, status: str = "pending"
) -> str:
    draft_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO drafts (id, landlord_id, case_id, recipient, body, "
            "prompt_version, status) "
            "VALUES (:id, :landlord_id, :case_id, 'tenant', 'test body', 'v1', :status)"
        ),
        {"id": draft_id, "landlord_id": landlord_id, "case_id": case_id, "status": status},
    )
    return draft_id


async def _insert_message(
    conn: AsyncConnection, landlord_id: str, property_id: str, *, twilio_sid: str | None = None
) -> str:
    message_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO messages (id, landlord_id, property_id, direction, party, "
            "body, twilio_sid) "
            "VALUES (:id, :landlord_id, :property_id, 'inbound', 'tenant', "
            "'test message body', :sid)"
        ),
        {
            "id": message_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "sid": twilio_sid,
        },
    )
    return message_id


async def _insert_audit_log(
    conn: AsyncConnection,
    *,
    landlord_id: str | None = None,
    case_id: str | None = None,
    actor: str = "system",
    action: str = "message_received",
) -> None:
    await conn.execute(
        text(
            "INSERT INTO audit_log (landlord_id, case_id, actor, action) "
            "VALUES (:landlord_id, :case_id, :actor, :action)"
        ),
        {
            "landlord_id": landlord_id or str(uuid.uuid4()),
            "case_id": case_id,
            "actor": actor,
            "action": action,
        },
    )


@dataclass
class _CaseFixture:
    landlord_id: str
    property_id: str
    tenant_id: str
    case_id: str


async def _make_case(conn: AsyncConnection) -> _CaseFixture:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)
    case_id = await _insert_case(conn, landlord_id, property_id, tenant_id)
    return _CaseFixture(landlord_id, property_id, tenant_id, case_id)


# ---------------------------------------------------------------------------
# 1. Table existence + round-trip
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_all_core_tables_exist(db: AsyncEngine) -> None:
    """All 11 revision-0002 tables must exist in public schema after upgrade head."""
    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
        actual = {row[0] for row in result.fetchall()}

    missing = set(_CORE_TABLES) - actual
    assert not missing, f"Missing core-schema tables: {missing}"


# ---------------------------------------------------------------------------
# 2a. uq_drafts_one_pending — one pending draft per case, ever
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_two_pending_drafts_conflict(conn: AsyncConnection) -> None:
    """A second 'pending' draft on the same case must violate uq_drafts_one_pending."""
    fx = await _make_case(conn)
    await _insert_draft(conn, fx.landlord_id, fx.case_id, status="pending")

    with pytest.raises(IntegrityError, match="unique|duplicate"):
        await _insert_draft(conn, fx.landlord_id, fx.case_id, status="pending")


@pytest.mark.integration
async def test_pending_plus_stale_drafts_allowed(conn: AsyncConnection) -> None:
    """A 'pending' draft plus a 'stale' draft on the same case are both allowed."""
    fx = await _make_case(conn)
    await _insert_draft(conn, fx.landlord_id, fx.case_id, status="pending")
    await _insert_draft(conn, fx.landlord_id, fx.case_id, status="stale")

    result = await conn.execute(
        text("SELECT count(*) FROM drafts WHERE case_id = :case_id"),
        {"case_id": fx.case_id},
    )
    assert result.scalar() == 2


# ---------------------------------------------------------------------------
# 2b. cases.severity CHECK — lowercase accepted, uppercase rejected
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_case_severity_accepts_lowercase(conn: AsyncConnection) -> None:
    """severity CHECK must accept all three lowercase values."""
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)

    for severity in ("emergency", "urgent", "routine"):
        await _insert_case(conn, landlord_id, property_id, tenant_id, severity=severity)


@pytest.mark.integration
async def test_case_severity_rejects_uppercase(conn: AsyncConnection) -> None:
    """severity CHECK must reject uppercase (guards Severity.db_value lowercasing)."""
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)

    with pytest.raises(IntegrityError, match="check"):
        await _insert_case(conn, landlord_id, property_id, tenant_id, severity="EMERGENCY")


# ---------------------------------------------------------------------------
# 2c. UNIQUE constraints
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unique_trust_metrics_property_severity(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    insert_sql = text(
        "INSERT INTO trust_metrics (landlord_id, property_id, severity) "
        "VALUES (:landlord_id, :property_id, 'routine')"
    )
    params = {"landlord_id": landlord_id, "property_id": property_id}

    await conn.execute(insert_sql, params)

    with pytest.raises(IntegrityError, match="unique|duplicate"):
        await conn.execute(insert_sql, params)


@pytest.mark.integration
async def test_unique_tenants_property_phone(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    phone = "+14165550001"
    await _insert_tenant(conn, landlord_id, property_id, phone=phone)

    with pytest.raises(IntegrityError, match="unique|duplicate"):
        await _insert_tenant(conn, landlord_id, property_id, phone=phone)


@pytest.mark.integration
async def test_unique_vendors_landlord_phone(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    phone = "+14165550002"
    await _insert_vendor(conn, landlord_id, phone=phone)

    with pytest.raises(IntegrityError, match="unique|duplicate"):
        await _insert_vendor(conn, landlord_id, phone=phone)


@pytest.mark.integration
async def test_unique_cases_langgraph_thread_id(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    tenant_id = await _insert_tenant(conn, landlord_id, property_id)
    thread_id = f"thread-{uuid.uuid4()}"
    await _insert_case(conn, landlord_id, property_id, tenant_id, thread_id=thread_id)

    with pytest.raises(IntegrityError, match="unique|duplicate"):
        await _insert_case(conn, landlord_id, property_id, tenant_id, thread_id=thread_id)


@pytest.mark.integration
async def test_unique_messages_twilio_sid(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)
    sid = f"SM{uuid.uuid4().hex}"
    await _insert_message(conn, landlord_id, property_id, twilio_sid=sid)

    with pytest.raises(IntegrityError, match="unique|duplicate"):
        await _insert_message(conn, landlord_id, property_id, twilio_sid=sid)


# ---------------------------------------------------------------------------
# 2d. audit_log CHECKs + no FK (rows survive deletes, intentionally)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_audit_log_actor_check_rejects_invalid(conn: AsyncConnection) -> None:
    with pytest.raises(IntegrityError, match="check"):
        await _insert_audit_log(conn, actor="bogus_actor")


@pytest.mark.integration
async def test_audit_log_action_check_rejects_invalid(conn: AsyncConnection) -> None:
    with pytest.raises(IntegrityError, match="check"):
        await _insert_audit_log(conn, action="bogus_action")


@pytest.mark.integration
async def test_audit_log_no_fk_on_landlord_or_case(conn: AsyncConnection) -> None:
    """landlord_id/case_id are plain uuid with no FK — random, non-existent
    uuids must still succeed (so audit rows survive deletes upstream)."""
    await _insert_audit_log(conn, landlord_id=str(uuid.uuid4()), case_id=str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# 2e. FK ON DELETE RESTRICT / CASCADE
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_fk_restrict_landlord_delete_blocked_by_property(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    await _insert_property(conn, landlord_id)

    with pytest.raises(IntegrityError, match="foreign key|violates"):
        await conn.execute(text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id})


@pytest.mark.integration
async def test_push_tokens_cascade_on_landlord_delete(conn: AsyncConnection) -> None:
    landlord_id = await _insert_landlord(conn)
    await conn.execute(
        text(
            "INSERT INTO push_tokens (landlord_id, token, platform) "
            "VALUES (:landlord_id, :token, 'ios')"
        ),
        {"landlord_id": landlord_id, "token": f"tok-{uuid.uuid4()}"},
    )

    await conn.execute(text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id})

    result = await conn.execute(
        text("SELECT count(*) FROM push_tokens WHERE landlord_id = :id"),
        {"id": landlord_id},
    )
    assert result.scalar() == 0


# ---------------------------------------------------------------------------
# 3. Append-only gate documentation (rule #2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_append_only_revoke_gate_documented(conn: AsyncConnection) -> None:
    """messages/audit_log accept INSERT (the only sanctioned write path), and
    the migration source must still carry the deferred-REVOKE gate comment
    for BOTH tables — so it can't silently disappear in a refactor before
    #22 closes it. Actual REVOKE enforcement is NOT tested: the app role
    doesn't exist in local Postgres (documented deferral).
    """
    landlord_id = await _insert_landlord(conn)
    property_id = await _insert_property(conn, landlord_id)

    # (a) INSERT — the only sanctioned write path — must succeed for both.
    await _insert_message(conn, landlord_id, property_id)
    await _insert_audit_log(conn, landlord_id=landlord_id)

    # (b) the deferred-REVOKE gate must still be documented near both tables.
    content = _MIGRATION_PATH.read_text()

    messages_window = _window_after(content, "CREATE TABLE messages")
    assert "REVOKE" in messages_window
    assert "DEFERRED" in messages_window

    audit_log_window = _window_after(content, "CREATE TABLE audit_log")
    assert "REVOKE" in audit_log_window
    assert "DEFERRED" in audit_log_window


# ---------------------------------------------------------------------------
# 1b. Downgrade to 0001 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0001_removes_core_tables_keeps_landlords(db: AsyncEngine) -> None:
    """Downgrading to 0001 must remove all 11 core tables but keep landlords."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0001"))

    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
        remaining = {row[0] for row in result.fetchall()}

    still_present = remaining & set(_CORE_TABLES)
    assert not still_present, f"core tables should be gone after downgrade to 0001: {still_present}"
    assert "landlords" in remaining, "landlords must survive downgrade to 0001"


@pytest.mark.integration
async def test_reupgrade_restores_core_tables(db: AsyncEngine) -> None:
    """After downgrade to 0001 + re-upgrade to head, all 11 core tables exist again."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
        actual = {row[0] for row in result.fetchall()}

    missing = set(_CORE_TABLES) - actual
    assert not missing, f"Missing core-schema tables after re-upgrade: {missing}"
