"""Integration tests for the app_role + RLS migration (revision 0005).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0005 implements #22 (docs/03-engineering/schema-v1.md's v1.2
amendments): a dedicated ``app_role`` Postgres role, grants (ordinary
tables get full CRUD, append-only tables get SELECT/INSERT only — the
rule-#2 REVOKE finally lands), and ``ENABLE ROW LEVEL SECURITY`` (no
``FORCE`` — see the migration module docstring "WHY NO FORCE", #22 safety
review item 2) + one policy per table on every table in schema-v1.md
except ``alembic_version``. See the migration module docstring
(``migrations/versions/0005_app_role_and_rls.py``) for the full design
rationale, including why ``message_status_events`` gets RLS too even
though the GitHub issue's acceptance criteria don't name it.

These tests verify catalog-level state only (object existence, RLS
flags, policy names, exact grant sets) — actual enforcement (does a
session connected AS ``app_role`` really only see one landlord's rows) is
proven separately in ``tests/test_rls_isolation.py``, because the
migrating/admin role here (``stoop`` locally, ``postgres``/``service_role``
on live Supabase) is never subject to RLS in the first place — deliberately
so, since it backs pre-identity service paths (``GET /v1/me``'s
provisioning upsert, the 0004 auth trigger, future webhook ingestion,
#40) that structurally cannot be GUC-scoped. Catalog assertions are the
right level for *this* file.

Also in this file: the writer-grep guard test promised by the issue-#22
DoD comment thread — grep ``app/`` for any UPDATE/DELETE SQL text
targeting an append-only table, and separately assert the migration source
actually carries the REVOKE. Migration 0002/0003 pinned the "deferred gate"
comment via ``test_append_only_revoke_gate_documented``; this is the
promised follow-up that closes the loop by machine, not by memory. A
sibling guard (#22 safety review item 7a) greps ``app/`` for anything
OTHER than ``deps.py`` setting ``app.current_landlord_id``. A third guard
(#22 safety review item 12) machine-enforces an allowlist of exactly which
files may reference ``get_admin_session`` (the RLS-bypassing session
dependency) — today just its definition and its sole caller, ``GET
/v1/me``.

Every test that touches data uses its own connection wrapped in an
explicit transaction that is always rolled back at teardown (the ``conn``
fixture) EXCEPT the round-trip test at the bottom, which must actually
mutate schema state (downgrade/upgrade) and therefore runs last, per the
same mutation-order convention documented in test_migrations_0003.py /
test_migrations_0004.py / test_migrations_core.py.

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0005.py -m integration -v
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# ---------------------------------------------------------------------------
# Helpers — duplicated (not imported) from tests/test_migrations_0004.py, to
# keep this module self-contained (same convention as that module).
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "migrations" / "versions" / "0005_app_role_and_rls.py"
)

_APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Every table in schema-v1.md except alembic_version.
_ALL_RLS_TABLES: list[str] = [
    "landlords",
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
    "message_status_events",
]

# Ordinary tables: full CRUD for app_role.
_ORDINARY_TABLES: list[str] = [
    "landlords",
    "properties",
    "vendors",
    "tenants",
    "cases",
    "message_cases",
    "drafts",
    "trust_metrics",
    "notifications",
    "push_tokens",
]

# Append-only tables: SELECT/INSERT only for app_role (rule #2).
_APPEND_ONLY_TABLES: list[str] = ["messages", "audit_log", "message_status_events"]


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
    """Apply migrations exactly once per test session (ends at head/0005)."""
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
    """Per-test connection wrapped in a transaction that is always rolled back."""
    async with db.connect() as connection:
        trans = await connection.begin()
        try:
            yield connection
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# 1. app_role — exists, NOLOGIN
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_role_exists_nologin(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = 'app_role'")
        )
        row = result.one_or_none()

    assert row is not None, "app_role must exist after upgrade head"
    assert row[1] is False, "app_role must be NOLOGIN"


# ---------------------------------------------------------------------------
# 2. RLS enabled (NOT forced) on every table
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_rls_enabled_not_forced_on_every_table(db: AsyncEngine) -> None:
    """ENABLE ROW LEVEL SECURITY on every table, deliberately WITHOUT FORCE
    (#22 safety review item 2 — see migration module docstring "WHY NO
    FORCE"). FORCE would bind the table OWNER (the migrating/admin role)
    to RLS too, which would break the deliberately-unscoped admin-engine
    service paths (GET /v1/me provisioning, the 0004 auth trigger, future
    webhook ingestion). app_role — never the owner of anything — is fully
    subject to RLS the moment ENABLE runs, FORCE or not; see
    tests/test_rls_isolation.py for the behavioral proof."""
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
            )
        )
        rows = {row[0]: (row[1], row[2]) for row in result.fetchall()}

    for table in _ALL_RLS_TABLES:
        assert table in rows, f"{table} missing from public schema"
        enabled, forced = rows[table]
        assert enabled is True, f"{table} must have RLS enabled"
        assert forced is False, f"{table} must NOT have RLS forced (see module docstring)"

    # alembic_version is deliberately excluded.
    assert rows["alembic_version"] == (False, False)


# ---------------------------------------------------------------------------
# 3. Exactly one policy per table, named <table>_isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_exactly_one_policy_per_table(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text("SELECT polrelid::regclass::text, polname FROM pg_policy")
        )
        rows = result.fetchall()

    by_table: dict[str, list[str]] = {}
    for table, policy in rows:
        by_table.setdefault(table, []).append(policy)

    assert set(by_table) == set(_ALL_RLS_TABLES), (
        f"policy coverage mismatch: {set(by_table)} != {set(_ALL_RLS_TABLES)}"
    )
    for table, policies in by_table.items():
        assert policies == [f"{table}_isolation"], (
            f"{table} should have exactly one policy named {table}_isolation, got {policies}"
        )


# ---------------------------------------------------------------------------
# 4. Grants — exactly as specified
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_role_has_full_crud_on_ordinary_tables(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'app_role'"
            )
        )
        rows = result.fetchall()

    by_table: dict[str, set[str]] = {}
    for table, privilege in rows:
        by_table.setdefault(table, set()).add(privilege)

    for table in _ORDINARY_TABLES:
        assert by_table.get(table) == {"SELECT", "INSERT", "UPDATE", "DELETE"}, (
            f"{table}: expected exactly SELECT/INSERT/UPDATE/DELETE for app_role, "
            f"got {by_table.get(table)}"
        )


@pytest.mark.integration
async def test_app_role_has_select_insert_only_on_append_only_tables(db: AsyncEngine) -> None:
    """The rule-#2 proof at the grant level: append-only tables get SELECT
    and INSERT for app_role, and NOTHING else — no UPDATE, no DELETE."""
    async with db.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'app_role'"
            )
        )
        rows = result.fetchall()

    by_table: dict[str, set[str]] = {}
    for table, privilege in rows:
        by_table.setdefault(table, set()).add(privilege)

    for table in _APPEND_ONLY_TABLES:
        privileges = by_table.get(table, set())
        assert privileges == {"SELECT", "INSERT"}, (
            f"{table}: expected exactly SELECT/INSERT for app_role (append-only, rule #2), "
            f"got {privileges}"
        )
        assert "UPDATE" not in privileges
        assert "DELETE" not in privileges


@pytest.mark.integration
async def test_app_role_has_schema_usage(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        can_use = (
            await connection.execute(
                text("SELECT has_schema_privilege('app_role', 'public', 'USAGE')")
            )
        ).scalar_one()
    assert can_use is True


@pytest.mark.integration
async def test_app_role_has_sequence_usage_for_identity_columns(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        audit_seq = (
            await connection.execute(
                text("SELECT has_sequence_privilege('app_role', 'audit_log_id_seq', 'USAGE')")
            )
        ).scalar_one()
        events_seq = (
            await connection.execute(
                text(
                    "SELECT has_sequence_privilege("
                    "'app_role', 'message_status_events_id_seq', 'USAGE')"
                )
            )
        ).scalar_one()

    assert audit_seq is True
    assert events_seq is True


# ---------------------------------------------------------------------------
# 5. Writer-grep guard (issue #22 DoD comment) — closes rule #2 by machine
# ---------------------------------------------------------------------------

# Tightened (#22 safety review item 7b) to also catch schema-qualified
# (``public.messages``) writers — ``\s+`` between the verb and the table
# name already spans newlines regardless of any per-line flag, so
# newline-broken statements ("DELETE\n  FROM\n  messages") were already
# covered; ``re.DOTALL`` is added anyway for explicitness since this
# pattern is applied to whole-file, multi-line content.
_APPEND_ONLY_WRITE_PATTERN = re.compile(
    r"\b(UPDATE|DELETE\s+FROM)\s+(?:public\.)?(messages|audit_log|message_status_events)\b",
    re.IGNORECASE | re.DOTALL,
)

# Only app/deps.py's require_landlord may set this GUC (#22 safety review
# item 7a) — matches both the set_config() call it uses and a bare
# SET/SET LOCAL form, in case a future writer reaches for that instead.
_GUC_SETTER_PATTERN = re.compile(
    r"(set_config\(\s*['\"]app\.current_landlord_id|SET(?:\s+LOCAL)?\s+app\.current_landlord_id\b)",
    re.IGNORECASE | re.DOTALL,
)


@pytest.mark.unit
def test_no_writer_exists_for_append_only_tables_without_revoke() -> None:
    """Machine-checked version of the rule-#2 gate (DoD addendum on issue
    #22): grep ``app/`` for any UPDATE/DELETE SQL text targeting
    ``messages``, ``audit_log``, or ``message_status_events``, and fail the
    build if one exists. Separately assert the migration source actually
    carries the matching REVOKE (the DB-level proof lives in
    ``test_app_role_has_select_insert_only_on_append_only_tables`` above;
    this file-level assertion is what stops the REVOKE statement itself
    from silently disappearing in a future refactor).

    Migrations 0002/0003 pinned a "deferred gate" comment via
    ``test_append_only_revoke_gate_documented`` — that only proved a
    comment existed, not that a real writer was absent or that the REVOKE
    had actually landed. This is the promised follow-up: the rule-#2 gate
    now closes by machine, not by memory.

    Scope note: this greps for literal SQL text (the codebase is raw-SQL
    only today, no ORM — see apps/api/CLAUDE.md's target layout). If/when
    ORM models land, this pattern needs a matching update (e.g. grepping
    for ``session.delete(...)``/``update(...)`` targeting these models
    too) — not a gap in THIS test, a note for whoever adds the ORM layer.
    """
    offending: list[str] = []
    for path in _APP_DIR.rglob("*.py"):
        content = path.read_text()
        if _APPEND_ONLY_WRITE_PATTERN.search(content):
            offending.append(str(path.relative_to(_APP_DIR.parent)))

    assert not offending, (
        "found UPDATE/DELETE SQL text targeting an append-only table in "
        f"app/ (never-break rule #2): {offending}. messages/audit_log/"
        "message_status_events must never be written to except by INSERT."
    )

    migration_content = _MIGRATION_PATH.read_text()
    assert (
        "REVOKE UPDATE, DELETE ON messages, audit_log, message_status_events FROM app_role"
        in migration_content
    ), "migration 0005 must carry the append-only REVOKE for all three tables"


@pytest.mark.unit
@pytest.mark.parametrize(
    "snippet",
    [
        "UPDATE messages SET body = 'x'",
        "DELETE FROM messages WHERE id = :id",
        "UPDATE public.messages SET body = 'x'",
        "DELETE FROM public.audit_log WHERE id = :id",
        "UPDATE\n    message_status_events\nSET status = 'x'",
        "DELETE\n  FROM\n  messages",
    ],
)
def test_writer_grep_pattern_catches_every_shape(snippet: str) -> None:
    """The grep pattern used by ``test_no_writer_exists_for_append_only_
    tables_without_revoke`` must genuinely match every shape a real writer
    could take — plain, schema-qualified, and newline-broken — not just
    fail to false-positive on today's (violation-free) codebase, which
    would be a vacuous pass if the pattern were ever too narrow (#22 safety
    review item 7b).
    """
    assert _APPEND_ONLY_WRITE_PATTERN.search(snippet), f"pattern should catch: {snippet!r}"


@pytest.mark.unit
def test_current_landlord_id_guc_only_set_in_deps_py() -> None:
    """Only ``app/deps.py``'s ``require_landlord`` may set
    ``app.current_landlord_id`` (#22 safety review item 7a). Any OTHER
    module doing so would be a second, unreviewed place that could set the
    GUC to the wrong value, skip the soft-delete check, or otherwise
    bypass ``require_landlord``'s fail-closed contract entirely.
    """
    offending: list[str] = []
    for path in _APP_DIR.rglob("*.py"):
        if path.name == "deps.py":
            continue
        content = path.read_text()
        if _GUC_SETTER_PATTERN.search(content):
            offending.append(str(path.relative_to(_APP_DIR.parent)))

    assert not offending, (
        f"app.current_landlord_id set outside app/deps.py: {offending} — "
        "only require_landlord may ever set this GUC"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "snippet",
    [
        "set_config('app.current_landlord_id', landlord_id, true)",
        'set_config("app.current_landlord_id", landlord_id, true)',
        "SET app.current_landlord_id = 'x'",
        "SET LOCAL app.current_landlord_id = 'x'",
    ],
)
def test_guc_setter_pattern_catches_every_shape(snippet: str) -> None:
    """Positive-match validation for ``_GUC_SETTER_PATTERN`` — same
    rationale as ``test_writer_grep_pattern_catches_every_shape`` above."""
    assert _GUC_SETTER_PATTERN.search(snippet), f"pattern should catch: {snippet!r}"


# Files allowed to reference get_admin_session (#22 safety review item 12):
# its definition (app/db/session.py) and its callers, each a genuine
# pre-identity/service-path admin-engine use (see that module's docstring).
# EXTEND THIS DELIBERATELY, not by loosening the grep.
_ADMIN_SESSION_ALLOWLIST: frozenset[str] = frozenset(
    {
        "app/db/session.py",
        "app/routers/me.py",
        # #40: Twilio inbound webhooks (POST /webhooks/twilio/sms,
        # /webhooks/twilio/status) — no landlord JWT exists here to
        # resolve a `landlord_id` GUC from; persisting an inbound tenant
        # message MUST use the admin engine or an RLS mis-scope could
        # silently reject/misfile it (session.py's "#40, forward note").
        "app/routers/webhooks/twilio.py",
        # #40: the BackgroundTasks callback (app.agent.graph_entry.
        # enqueue_classification) scheduled by the SMS webhook runs AFTER
        # the request's own dependency stack (and its get_admin_session)
        # has already exited/closed — it must open its own admin session
        # for the same pre-identity reason the webhook router does.
        "app/agent/graph_entry.py",
        # #30/#110: the deterministic graph nodes run in the SAME
        # background/graph context as graph_entry.py above — no HTTP
        # request, no landlord JWT to resolve a `landlord_id` GUC from.
        # Each opens its own admin session, following graph_entry.py's
        # exact pattern (see each module's own docstring).
        "app/agent/nodes/identify_property.py",
        "app/agent/nodes/load_context.py",
        "app/agent/nodes/identify_case.py",
        # #31/#32/#33: same background/graph context, same pre-identity
        # rationale as the #30/#110 nodes above — classify_intent/
        # classify_severity/draft_response each open their own admin
        # session to read the message row and (severity/draft) write their
        # audit_log/drafts rows.
        "app/agent/nodes/classify_intent.py",
        "app/agent/nodes/classify_severity.py",
        "app/agent/nodes/draft_response.py",
        # #110: sweep_cases() (the time-driven case-lifecycle sweep) is a
        # future scheduled-job entrypoint with the same pre-identity
        # rationale — see its own docstring "The scheduler seam".
        "app/agent/case_lifecycle.py",
        # #34: the degraded-mode notification seam (G1) — same background
        # /graph context, no HTTP request/JWT, same pre-identity rationale
        # as every other node above.
        "app/agent/nodes/degraded_mode.py",
        # #34: app/agent/graph.py resolves a case's langgraph_thread_id via
        # its own small SELECT (the pre-routing/case-scoped graph split) —
        # same background/graph context as every node above.
        "app/agent/graph.py",
        # #43: the await_approval interrupt node flips the case to
        # awaiting_approval before the graph pauses — same background/graph
        # context, no HTTP request/JWT, same pre-identity rationale.
        "app/agent/nodes/await_approval.py",
        # #109: the degraded-mode re-classification sweep (a future
        # scheduled-job entrypoint, same pre-identity rationale as
        # case_lifecycle.sweep_cases above — see its own docstring "The
        # scheduler seam").
        "app/agent/degraded_mode_sweep.py",
    }
)


@pytest.mark.unit
def test_get_admin_session_referenced_only_by_allowlisted_files() -> None:
    """Machine-enforced admin-session allowlist (#22 safety review item
    12): ``get_admin_session`` bypasses RLS entirely, so every file that
    references it must be an intentional, reviewed choice — not an
    accidental import by some future landlord-scoped endpoint (#53+)
    reaching for the wrong session dependency instead of
    ``require_landlord``. Red-fails the instant a new file mentions
    ``get_admin_session`` without the allowlist above being updated to
    match, forcing that update to be a deliberate, visible diff.
    """
    referencing: set[str] = set()
    for path in _APP_DIR.rglob("*.py"):
        content = path.read_text()
        if "get_admin_session" in content:
            referencing.add(str(path.relative_to(_APP_DIR.parent)))

    assert referencing == set(_ADMIN_SESSION_ALLOWLIST), (
        f"files referencing get_admin_session changed: {referencing} != "
        f"{set(_ADMIN_SESSION_ALLOWLIST)} — update the allowlist deliberately "
        "if this is an intentional new caller (e.g. #40's webhook ingestion), "
        "with a comment explaining why"
    )


@pytest.mark.unit
def test_migration_source_pins_default_privileges_and_defensive_nologin() -> None:
    """Source-level pins for two guards that can't be verified against the
    LOCAL database, since ``anon``/``authenticated`` don't exist locally
    (their grants can't be queried) and a "stale LOGIN-enabled app_role"
    is a re-migration edge case, not a fresh-upgrade one (#22 safety review
    items 4 and 10)."""
    content = _MIGRATION_PATH.read_text()

    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon" in content
    assert (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM authenticated"
        in content
    )
    assert "ALTER ROLE app_role NOLOGIN" in content


# ---------------------------------------------------------------------------
# 6. Downgrade to 0004 / re-upgrade round-trip — MUST run last: it mutates
# schema state for the remainder of the session.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_downgrade_to_0004_removes_role_policies_and_grants(db: AsyncEngine) -> None:
    """Downgrading to 0004 must drop every policy, disable RLS on every
    table, and drop app_role entirely (nothing else in this single local
    database still depends on it after our own REVOKE)."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "0004"))

    async with db.connect() as connection:
        policy_count = (
            await connection.execute(text("SELECT count(*) FROM pg_policy"))
        ).scalar_one()
        assert policy_count == 0, "all policies should be gone after downgrade to 0004"

        rls_result = await connection.execute(
            text(
                "SELECT relname FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                "AND relrowsecurity"
            )
        )
        assert not rls_result.fetchall(), "no table should have RLS enabled"

        role_result = await connection.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = 'app_role'")
        )
        assert not role_result.fetchall(), "app_role should be dropped after downgrade to 0004"


@pytest.mark.integration
async def test_reupgrade_restores_0005_state(db: AsyncEngine) -> None:
    """After downgrade to 0004 + re-upgrade to head, 0005 state is restored:
    app_role exists again (NOLOGIN), RLS is enabled (not forced) again, and
    the grants are exactly as before."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))

    async with db.connect() as connection:
        role_result = await connection.execute(
            text("SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = 'app_role'")
        )
        row = role_result.one_or_none()
        assert row is not None
        assert row[1] is False

        policy_result = await connection.execute(text("SELECT count(*) FROM pg_policy"))
        assert policy_result.scalar_one() == len(_ALL_RLS_TABLES)

        rls_result = await connection.execute(
            text(
                "SELECT count(*) FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                "AND relrowsecurity AND NOT relforcerowsecurity"
            )
        )
        assert rls_result.scalar_one() == len(_ALL_RLS_TABLES)
