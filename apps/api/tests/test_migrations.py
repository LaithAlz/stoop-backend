"""Integration tests for the landlords migration (revision 0001).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

These tests verify:
1. The landlords table exists after ``upgrade head``.
2. All columns from schema-v1.md are present with the right types.
3. CHECK constraints on price_cohort, subscription_tier, subscription_status.
4. UNIQUE constraints on auth_user_id and stripe_customer_id.
5. Default values are applied automatically.
6. Downgrade cleanly removes the table.
7. Re-upgrade restores the table (full round-trip).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations.py -m integration -v
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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
    """Run an alembic sub-command synchronously via subprocess.

    Pass each argument separately, e.g.::

        _alembic("downgrade", "base")
        _alembic("upgrade", "head")
    """
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
    """Apply migrations exactly once per test session.

    Synchronous + session-scoped avoids the pytest-asyncio ScopeMismatch
    error that occurs when module-scoped async fixtures conflict with the
    function-scoped event loop (``asyncio_default_fixture_loop_scope=function``).
    """
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_landlords_table_exists(db: AsyncEngine) -> None:
    """landlords table must exist in public schema after migration."""
    async with db.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'landlords'
                """
            )
        )
        rows = result.fetchall()
    assert len(rows) == 1, "landlords table not found in public schema"


@pytest.mark.integration
async def test_landlords_columns(db: AsyncEngine) -> None:
    """All schema-v1.md columns must be present with correct types."""
    expected: dict[str, str] = {
        "id": "uuid",
        "auth_user_id": "uuid",
        "email": "text",
        "full_name": "text",
        "phone": "text",
        "timezone": "text",
        "voice_profile": "jsonb",
        "price_cohort": "text",
        "subscription_tier": "text",
        "subscription_status": "text",
        "stripe_customer_id": "text",
        "deleted_at": "timestamp with time zone",
        "created_at": "timestamp with time zone",
        "updated_at": "timestamp with time zone",
    }

    async with db.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'landlords'
                ORDER BY ordinal_position
                """
            )
        )
        actual = {row[0]: row[1] for row in result.fetchall()}

    for col, dtype in expected.items():
        assert col in actual, f"Column {col!r} missing from landlords"
        assert actual[col] == dtype, f"Column {col!r}: expected {dtype!r}, got {actual[col]!r}"

    extra = set(actual) - set(expected)
    assert not extra, f"Unexpected extra columns in landlords: {extra}"


@pytest.mark.integration
async def test_check_price_cohort(db: AsyncEngine) -> None:
    """price_cohort CHECK must reject values outside ('early_access','standard')."""
    async with db.connect() as conn:
        with pytest.raises(Exception, match="check"):
            await conn.execute(
                text(
                    "INSERT INTO landlords (auth_user_id, email, price_cohort, "
                    "subscription_tier, subscription_status) "
                    "VALUES (:uid, 'pc@example.com', 'invalid_cohort', 'free', 'none')"
                ),
                {"uid": str(uuid.uuid4())},
            )
            await conn.commit()


@pytest.mark.integration
async def test_check_subscription_tier(db: AsyncEngine) -> None:
    """subscription_tier CHECK must reject values outside ('free','full','desk')."""
    async with db.connect() as conn:
        with pytest.raises(Exception, match="check"):
            await conn.execute(
                text(
                    "INSERT INTO landlords (auth_user_id, email, price_cohort, "
                    "subscription_tier, subscription_status) "
                    "VALUES (:uid, 'st@example.com', 'early_access', 'premium', 'none')"
                ),
                {"uid": str(uuid.uuid4())},
            )
            await conn.commit()


@pytest.mark.integration
async def test_check_subscription_status(db: AsyncEngine) -> None:
    """subscription_status CHECK must reject values outside valid set."""
    async with db.connect() as conn:
        with pytest.raises(Exception, match="check"):
            await conn.execute(
                text(
                    "INSERT INTO landlords (auth_user_id, email, price_cohort, "
                    "subscription_tier, subscription_status) "
                    "VALUES (:uid, 'ss@example.com', 'early_access', 'free', 'expired')"
                ),
                {"uid": str(uuid.uuid4())},
            )
            await conn.commit()


@pytest.mark.integration
async def test_unique_auth_user_id(db: AsyncEngine) -> None:
    """auth_user_id UNIQUE must prevent duplicate inserts."""
    uid = str(uuid.uuid4())

    async with db.connect() as conn:
        await conn.execute(
            text("INSERT INTO landlords (auth_user_id, email) VALUES (:uid, 'first@example.com')"),
            {"uid": uid},
        )
        await conn.commit()

    async with db.connect() as conn:
        with pytest.raises(Exception, match="unique|duplicate"):
            await conn.execute(
                text(
                    "INSERT INTO landlords (auth_user_id, email) "
                    "VALUES (:uid, 'second@example.com')"
                ),
                {"uid": uid},
            )
            await conn.commit()

    # Cleanup — landlords is NOT append-only (only messages/audit_log are).
    async with db.connect() as conn:
        await conn.execute(
            text("DELETE FROM landlords WHERE auth_user_id = :uid"),
            {"uid": uid},
        )
        await conn.commit()


@pytest.mark.integration
async def test_unique_stripe_customer_id(db: AsyncEngine) -> None:
    """stripe_customer_id UNIQUE must prevent duplicate inserts."""
    stripe_id = f"cus_test_{uuid.uuid4().hex[:8]}"

    async with db.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO landlords (auth_user_id, email, stripe_customer_id) "
                "VALUES (:uid, 'stripe1@example.com', :sid)"
            ),
            {"uid": str(uuid.uuid4()), "sid": stripe_id},
        )
        await conn.commit()

    async with db.connect() as conn:
        with pytest.raises(Exception, match="unique|duplicate"):
            await conn.execute(
                text(
                    "INSERT INTO landlords (auth_user_id, email, stripe_customer_id) "
                    "VALUES (:uid, 'stripe2@example.com', :sid)"
                ),
                {"uid": str(uuid.uuid4()), "sid": stripe_id},
            )
            await conn.commit()

    # Cleanup.
    async with db.connect() as conn:
        await conn.execute(
            text("DELETE FROM landlords WHERE stripe_customer_id = :sid"),
            {"sid": stripe_id},
        )
        await conn.commit()


@pytest.mark.integration
async def test_defaults_applied(db: AsyncEngine) -> None:
    """Columns with DEFAULT values must be populated automatically."""
    uid = str(uuid.uuid4())

    async with db.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO landlords (auth_user_id, email) VALUES (:uid, 'defaults@example.com')"
            ),
            {"uid": uid},
        )
        await conn.commit()

        result = await conn.execute(
            text(
                "SELECT timezone, price_cohort, subscription_tier, "
                "subscription_status, created_at, updated_at "
                "FROM landlords WHERE auth_user_id = :uid"
            ),
            {"uid": uid},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == "America/Toronto", "timezone default wrong"
    assert row[1] == "early_access", "price_cohort default wrong"
    assert row[2] == "free", "subscription_tier default wrong"
    assert row[3] == "none", "subscription_status default wrong"
    assert row[4] is not None, "created_at should be populated by default"
    assert row[5] is not None, "updated_at should be populated by default"

    # Cleanup.
    async with db.connect() as conn:
        await conn.execute(
            text("DELETE FROM landlords WHERE auth_user_id = :uid"),
            {"uid": uid},
        )
        await conn.commit()


@pytest.mark.integration
async def test_downgrade_removes_table(db: AsyncEngine) -> None:
    """Downgrade to base must remove the landlords table."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _alembic("downgrade", "base"))

    async with db.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'landlords'
                """
            )
        )
        rows = result.fetchall()

    assert len(rows) == 0, "landlords table should be gone after downgrade"

    # Re-upgrade so subsequent tests still have the table.
    await loop.run_in_executor(None, lambda: _alembic("upgrade", "head"))


@pytest.mark.integration
async def test_reupgrade_round_trip(db: AsyncEngine) -> None:
    """After downgrade+re-upgrade the landlords table must exist again."""
    async with db.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'landlords'
                """
            )
        )
        rows = result.fetchall()

    assert len(rows) == 1, "landlords table must exist after re-upgrade"
