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
from alembic.config import Config
from alembic.script import ScriptDirectory
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
async def test_upgrade_skips_colliding_rows_without_raising_and_respects_uniqueness_scope() -> None:
    """Safety review, 2026-08-03, finding 2: two rows whose stored values
    canonicalize to the SAME target within the SAME uniqueness scope must
    be SKIPPED (never updated, never raise ``UniqueViolationError`` and
    abort the whole migration) — ``properties.twilio_number`` (globally
    unique) and ``tenants`` (``UNIQUE (property_id, phone)``, scoped) both
    exercise this. A cross-property tenant "collision" (same canonical
    value, DIFFERENT property_id scope) is not a real constraint conflict
    at all and must canonicalize normally, proving the scoping is not
    overly conservative."""
    engine = create_async_engine(_db_url())
    landlord_id = str(uuid.uuid4())
    property_a_id = str(uuid.uuid4())
    property_b_id = str(uuid.uuid4())
    property_c_id = str(uuid.uuid4())
    colliding_property_1 = str(uuid.uuid4())
    colliding_property_2 = str(uuid.uuid4())
    same_property_tenant_1 = str(uuid.uuid4())
    same_property_tenant_2 = str(uuid.uuid4())
    cross_property_tenant_1 = str(uuid.uuid4())
    cross_property_tenant_2 = str(uuid.uuid4())
    try:
        _alembic("upgrade", "head")
        _alembic("downgrade", "0016")

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"
                ),
                {
                    "id": landlord_id,
                    "auth_id": str(uuid.uuid4()),
                    "email": f"{landlord_id}@example.com",
                },
            )

            async def _insert_property(prop_id: str, *, twilio_number: str | None) -> None:
                await conn.execute(
                    text(
                        "INSERT INTO properties "
                        "(id, landlord_id, label, address_line1, city, twilio_number) "
                        "VALUES (:id, :landlord_id, 'Test Property', :addr, 'Toronto', "
                        ":twilio_number)"
                    ),
                    {
                        "id": prop_id,
                        "landlord_id": landlord_id,
                        "addr": f"{prop_id} Test St",
                        "twilio_number": twilio_number,
                    },
                )

            # Two properties whose twilio_number values canonicalize to
            # the SAME target — properties.twilio_number is globally
            # UNIQUE, so both must be skipped entirely.
            await _insert_property(colliding_property_1, twilio_number="1-416-555-0301")
            await _insert_property(colliding_property_2, twilio_number="+14165550301")
            # A property the tenants below attach to (its own number must
            # not collide with anything above).
            await _insert_property(property_a_id, twilio_number=None)
            await _insert_property(property_b_id, twilio_number=None)
            await _insert_property(property_c_id, twilio_number=None)

            async def _insert_tenant(tenant_id: str, prop_id: str, *, phone: str) -> None:
                await conn.execute(
                    text(
                        "INSERT INTO tenants (id, landlord_id, property_id, phone) "
                        "VALUES (:id, :landlord_id, :property_id, :phone)"
                    ),
                    {
                        "id": tenant_id,
                        "landlord_id": landlord_id,
                        "property_id": prop_id,
                        "phone": phone,
                    },
                )

            # Same property, same canonical target — a REAL collision
            # under tenants' UNIQUE (property_id, phone).
            await _insert_tenant(same_property_tenant_1, property_a_id, phone="1-416-555-0311")
            await _insert_tenant(same_property_tenant_2, property_a_id, phone="+14165550311")
            # DIFFERENT properties, same canonical target — NOT a real
            # collision (different property_id scope); both must
            # canonicalize normally.
            await _insert_tenant(cross_property_tenant_1, property_b_id, phone="1-416-555-0321")
            await _insert_tenant(cross_property_tenant_2, property_c_id, phone="+14165550321")

        # Must complete without raising (UniqueViolationError would abort
        # the whole migration and fail this alembic invocation).
        _alembic("upgrade", "head")

        async with engine.connect() as conn:
            prop1 = (
                (
                    await conn.execute(
                        text("SELECT twilio_number FROM properties WHERE id = :id"),
                        {"id": colliding_property_1},
                    )
                )
                .mappings()
                .one()
            )
            prop2 = (
                (
                    await conn.execute(
                        text("SELECT twilio_number FROM properties WHERE id = :id"),
                        {"id": colliding_property_2},
                    )
                )
                .mappings()
                .one()
            )
            same_1 = (
                (
                    await conn.execute(
                        text("SELECT phone FROM tenants WHERE id = :id"),
                        {"id": same_property_tenant_1},
                    )
                )
                .mappings()
                .one()
            )
            same_2 = (
                (
                    await conn.execute(
                        text("SELECT phone FROM tenants WHERE id = :id"),
                        {"id": same_property_tenant_2},
                    )
                )
                .mappings()
                .one()
            )
            cross_1 = (
                (
                    await conn.execute(
                        text("SELECT phone FROM tenants WHERE id = :id"),
                        {"id": cross_property_tenant_1},
                    )
                )
                .mappings()
                .one()
            )
            cross_2 = (
                (
                    await conn.execute(
                        text("SELECT phone FROM tenants WHERE id = :id"),
                        {"id": cross_property_tenant_2},
                    )
                )
                .mappings()
                .one()
            )

        # Global collision (properties.twilio_number): BOTH left untouched.
        assert prop1["twilio_number"] == "1-416-555-0301"
        assert prop2["twilio_number"] == "+14165550301"

        # Same-property collision (tenants, real UNIQUE conflict): BOTH
        # left untouched.
        assert same_1["phone"] == "1-416-555-0311"
        assert same_2["phone"] == "+14165550311"

        # Cross-property "collision" (different scope, no real conflict):
        # BOTH canonicalized normally.
        assert cross_1["phone"] == "+14165550321"
        assert cross_2["phone"] == "+14165550321"
    finally:
        async with engine.begin() as conn:
            for tenant_id in (
                same_property_tenant_1,
                same_property_tenant_2,
                cross_property_tenant_1,
                cross_property_tenant_2,
            ):
                await conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
            for prop_id in (
                colliding_property_1,
                colliding_property_2,
                property_a_id,
                property_b_id,
                property_c_id,
            ):
                await conn.execute(text("DELETE FROM properties WHERE id = :id"), {"id": prop_id})
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
        # NOT a hardcoded revision. This used to read `== "0017"`, which
        # made every future migration fail a test that has nothing to do
        # with it (both #289's 0018 and #194's 0019 tripped it on the same
        # day). What this round trip actually cares about is that
        # `upgrade head` lands on whatever the head IS, so it asks alembic
        # rather than pinning a string that goes stale by design.
        head = ScriptDirectory.from_config(
            Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
        ).get_current_head()
        assert version == head
    finally:
        await engine.dispose()
