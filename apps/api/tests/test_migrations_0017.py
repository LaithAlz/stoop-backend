"""Integration tests for the phone-canonicalization data migration
(revision 0017, #232/#260).

Marker: ``integration`` — requires a running Postgres instance.
Use ``docker compose up -d`` at the repo root before running locally.

Revision 0017 implements the schema-v1.md v1.21 amendments: every
existing ``landlords.phone``/``properties.twilio_number``/``properties.
backup_contact->>'phone'``/``tenants.phone``/``vendors.phone`` value that
``app.phone.to_e164`` can canonicalize is rewritten to its canonical E.164
form; a value it cannot canonicalize is left completely untouched (never
nulled, never dropped). This migration issues no DDL at all — every
column already existed with the right type; only application-level
enforcement is new (this same PR's write-path changes).

Self-contained per the project's migration-test convention (helpers
duplicated, not imported from ``tests/factories.py`` — see
``tests/test_migrations_0015.py``'s own module docstring for the same
rationale: migration tests need precise control over schema state at
specific down-revisions).

Run with:
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_migrations_0017.py -m integration -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", _DB_URL_DEFAULT)
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


def _alembic(*args: str) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env={**os.environ, "DATABASE_URL": _db_url()},
    )
    if result.returncode != 0:
        cmd = " ".join(args)
        raise RuntimeError(
            f"alembic {cmd!r} failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )


@pytest.mark.integration
async def test_upgrade_canonicalizes_existing_rows_and_preserves_uncanonicalizable_ones() -> None:
    """Downgrade to 0016 (structurally a no-op — 0017 has no DDL), seed
    dirty rows directly via raw SQL (bypassing this PR's own write-time
    validation entirely, exactly like a pre-#260 row would have been
    written), re-upgrade to head (re-running 0017's backfill), and assert:
    canonicalizable values are rewritten to their canonical E.164 form; a
    genuinely uncanonicalizable value is left byte-identical to what was
    seeded, never nulled and never dropped."""
    engine = create_async_engine(_db_url())
    landlord_id = str(uuid.uuid4())
    property_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    vendor_id = str(uuid.uuid4())
    try:
        _alembic("upgrade", "head")
        _alembic("downgrade", "0016")

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO landlords (id, auth_user_id, email, phone) "
                    "VALUES (:id, :auth_id, :email, :phone)"
                ),
                {
                    "id": landlord_id,
                    "auth_id": str(uuid.uuid4()),
                    "email": f"{landlord_id}@example.com",
                    "phone": "(416) 555-0134",  # drifted, canonicalizable
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO properties (id, landlord_id, label, address_line1, city, "
                    "twilio_number, backup_contact) "
                    "VALUES (:id, :landlord_id, 'Test Property', :addr, 'Toronto', "
                    ":twilio_number, CAST(:backup_contact AS jsonb))"
                ),
                {
                    "id": property_id,
                    "landlord_id": landlord_id,
                    "addr": f"{property_id} Test St",
                    "twilio_number": "1-416-555-0199",  # drifted, canonicalizable
                    "backup_contact": '{"name": "Backup", "phone": "416.555.0177"}',
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, landlord_id, property_id, phone) "
                    "VALUES (:id, :landlord_id, :property_id, :phone)"
                ),
                {
                    "id": tenant_id,
                    "landlord_id": landlord_id,
                    "property_id": property_id,
                    "phone": "n/a",  # UNCANONICALIZABLE — must survive untouched
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO vendors (id, landlord_id, name, trade, phone) "
                    "VALUES (:id, :landlord_id, 'Test Vendor', 'plumbing', :phone)"
                ),
                {
                    "id": vendor_id,
                    "landlord_id": landlord_id,
                    "phone": "4165550188",  # drifted (no +1), canonicalizable
                },
            )

        _alembic("upgrade", "head")

        async with engine.connect() as conn:
            landlord_row = (
                (
                    await conn.execute(
                        text("SELECT phone FROM landlords WHERE id = :id"), {"id": landlord_id}
                    )
                )
                .mappings()
                .one()
            )
            property_row = (
                (
                    await conn.execute(
                        text("SELECT twilio_number, backup_contact FROM properties WHERE id = :id"),
                        {"id": property_id},
                    )
                )
                .mappings()
                .one()
            )
            tenant_row = (
                (
                    await conn.execute(
                        text("SELECT phone FROM tenants WHERE id = :id"), {"id": tenant_id}
                    )
                )
                .mappings()
                .one()
            )
            vendor_row = (
                (
                    await conn.execute(
                        text("SELECT phone FROM vendors WHERE id = :id"), {"id": vendor_id}
                    )
                )
                .mappings()
                .one()
            )

        assert landlord_row["phone"] == "+14165550134"
        assert property_row["twilio_number"] == "+14165550199"
        assert property_row["backup_contact"]["phone"] == "+14165550177"
        assert property_row["backup_contact"]["name"] == "Backup"  # untouched sibling key
        assert tenant_row["phone"] == "n/a"  # UNCHANGED — never nulled, never dropped
        assert vendor_row["phone"] == "+14165550188"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
            await conn.execute(text("DELETE FROM vendors WHERE id = :id"), {"id": vendor_id})
            await conn.execute(text("DELETE FROM properties WHERE id = :id"), {"id": property_id})
            await conn.execute(text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id})
        await engine.dispose()


@pytest.mark.integration
async def test_upgrade_is_idempotent_on_already_canonical_values() -> None:
    """Running the backfill a second time (downgrade to 0016, then
    re-upgrade) must never touch a value that is already canonical."""
    engine = create_async_engine(_db_url())
    landlord_id = str(uuid.uuid4())
    try:
        _alembic("upgrade", "head")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO landlords (id, auth_user_id, email, phone) "
                    "VALUES (:id, :auth_id, :email, :phone)"
                ),
                {
                    "id": landlord_id,
                    "auth_id": str(uuid.uuid4()),
                    "email": f"{landlord_id}@example.com",
                    "phone": "+14165550199",  # already canonical
                },
            )

        _alembic("downgrade", "0016")
        _alembic("upgrade", "head")

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT phone FROM landlords WHERE id = :id"), {"id": landlord_id}
                    )
                )
                .mappings()
                .one()
            )
        assert row["phone"] == "+14165550199"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM landlords WHERE id = :id"), {"id": landlord_id})
        await engine.dispose()


@pytest.mark.integration
async def test_downgrade_to_0016_is_noop_and_reupgrade_restores_head() -> None:
    """Round-trip (``apps/api/CLAUDE.md``: "down/up must round-trip"):
    this migration issues no DDL, so ``downgrade()`` is a deliberate
    no-op — verified structurally (every column it touches is still
    present and queryable immediately after downgrade) and behaviorally
    (re-upgrading succeeds and lands back on revision 0017)."""
    engine = create_async_engine(_db_url())
    try:
        _alembic("upgrade", "head")
        _alembic("downgrade", "0016")

        # No DDL to reverse — every column this migration touches is
        # still there (0017 never added/dropped anything).
        async with engine.connect() as conn:
            await conn.execute(text("SELECT phone FROM landlords LIMIT 1"))
            await conn.execute(text("SELECT twilio_number, backup_contact FROM properties LIMIT 1"))
            await conn.execute(text("SELECT phone FROM tenants LIMIT 1"))
            await conn.execute(text("SELECT phone FROM vendors LIMIT 1"))

        _alembic("upgrade", "head")

        async with engine.connect() as conn:
            version = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        assert version == "0017"
    finally:
        await engine.dispose()
