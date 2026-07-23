"""Integration tests for Properties CRUD (#54).

Marker: ``integration`` — requires docker-compose Postgres.

Harness: direct handler-function calls (same pattern as
``tests/test_require_landlord.py``: "prefer testing the dependency
directly ... FastAPI's Depends(...) wiring is just a type-hint annotation").
Route-level wiring (every ``/v1/`` route actually depends on
``require_landlord``) is already proven generically by
``tests/test_rls_isolation_matrix.py::test_every_v1_route_except_
allowlist_requires_landlord_scoping`` — no need to duplicate that here via
JWT/HTTP round-trips.

Cross-tenant isolation here is enforced at the APPLICATION level (explicit
``landlord_id = :landlord_id`` predicates) rather than genuine Postgres RLS:
locally ``APP_DATABASE_URL`` is unset, so ``get_session`` falls back to the
admin (superuser) engine, and Postgres superusers always bypass RLS
regardless of the GUC (see ``app/db/session.py``'s module docstring). Real
RLS *enforcement* is proven separately by
``tests/test_rls_isolation_matrix.py`` (``SET LOCAL ROLE app_role``). This
file proves the same cross-tenant boundary the other way: at the query
level every one of these endpoints actually uses.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app import property_provisioning
from app.config import settings
from app.deps import Landlord
from app.errors import AppError
from app.integrations import twilio_provision
from app.pagination import decode_cursor
from app.routers.properties import (
    PropertyCreateRequest,
    PropertyUpdateRequest,
    create_property,
    delete_property,
    get_property,
    list_properties,
    update_property,
)
from tests import factories

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"


class _FakeProvisioner:
    """Fake (#53) for router-level WIRING tests only — the full
    search/purchase/compensation MATRIX lives in
    ``tests/test_property_provisioning.py``. Always succeeds by default,
    giving every property a distinct, syntactically-plausible E.164
    number/SID pair; ``always_empty_search``/``fail_purchase`` let a
    handful of router-level tests exercise the NoNumbersAvailableError/
    ProvisioningFailedError -> HTTP-code mapping (spec MAJOR-2) without
    duplicating the full cascade/compensation matrix here."""

    def __init__(self) -> None:
        self.purchased: list[str] = []
        self.released: list[str] = []
        self.always_empty_search = False
        self.fail_purchase = False
        self.fixed_phone_number: str | None = None
        self._counter = 0

    async def search_available_numbers(
        self, *, area_code: str | None = None, region: str | None = None
    ) -> list[str]:
        if self.always_empty_search:
            return []
        if self.fixed_phone_number is not None:
            return [self.fixed_phone_number]
        self._counter += 1
        return [f"+1416555{self._counter:04d}"]

    async def purchase_number(self, *, phone_number: str) -> str:
        if self.fail_purchase:
            raise RuntimeError("fake purchase failure")
        sid = f"PN{uuid.uuid4().hex}"
        self.purchased.append(sid)
        return sid

    async def configure_webhooks(self, *, twilio_sid: str, sms_url: str, voice_url: str) -> None:
        return None

    async def associate_messaging_service(
        self, *, twilio_sid: str, messaging_service_sid: str
    ) -> None:
        return None

    async def release_number(self, *, twilio_sid: str) -> None:
        self.released.append(twilio_sid)


@pytest.fixture(autouse=True)
def _fake_twilio_provisioner(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_FakeProvisioner]:
    """Every test in this module that calls ``create_property`` now
    provisions (#53) — inject a fake so none of them need Twilio
    credentials or the network, and set ``public_base_url`` so the
    webhook-url config gate never fires here."""
    monkeypatch.setattr(settings, "public_base_url", "https://api.stoop.test")
    fake = _FakeProvisioner()
    twilio_provision.set_twilio_provisioner_for_tests(fake)
    yield fake
    twilio_provision.set_twilio_provisioner_for_tests(None)


def _get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", _DB_URL_DEFAULT)
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


def _alembic(*args: str) -> None:
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
    _alembic("upgrade", "head")
    yield


@pytest_asyncio.fixture
async def db_engine(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine) as sess:
        yield sess


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.rollback()  # clear any aborted transaction from a caught IntegrityError
    params = {"lid": landlord_id}
    await session.execute(
        text(
            "DELETE FROM message_status_events WHERE message_id IN "
            "(SELECT id FROM messages WHERE landlord_id = :lid)"
        ),
        params,
    )
    for table in (
        "audit_log",
        "drafts",
        "messages",
        "cases",
        "tenants",
        "properties",
        # #53: deprovisioning writes a `notifications` row keyed on
        # landlord_id (ON DELETE RESTRICT) — must be gone before the
        # landlords DELETE below or that statement itself raises.
        "notifications",
    ):
        await session.execute(text(f"DELETE FROM {table} WHERE landlord_id = :lid"), params)  # noqa: S608
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


@pytest.mark.integration
async def test_create_and_get_property_roundtrip(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created = await create_property(
            PropertyCreateRequest(
                label="41 Palmerston",
                address_line1="41 Palmerston Ave",
                city="Toronto",
                house_rules="No smoking indoors.",
            ),
            (landlord, session),
        )
        assert created.label == "41 Palmerston"
        assert created.province == "ON"  # DB default applied via COALESCE
        assert created.twilio_number is not None  # #53: now actually provisioned
        assert created.open_case_count == 0
        assert created.quiet_hours == {"start": "21:00", "end": "08:00"}

        fetched = await get_property(created.id, (landlord, session))
        assert fetched.id == created.id
        assert fetched.house_rules == "No smoking indoors."
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_get_property_not_found_returns_404(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await get_property(uuid.uuid4(), (landlord, session))
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "property_not_found"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_cross_tenant_property_access_returns_404(session: AsyncSession) -> None:
    """Landlord A's property must be invisible/unmodifiable/undeletable to landlord B."""
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    landlord_a = Landlord(id=uuid.UUID(landlord_a_id))
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="A's place", address_line1="1 A St", city="Toronto"),
            (landlord_a, session),
        )

        with pytest.raises(AppError) as get_exc:
            await get_property(prop.id, (landlord_b, session))
        assert get_exc.value.status_code == 404
        assert get_exc.value.code == "property_not_found"

        with pytest.raises(AppError) as update_exc:
            await update_property(
                prop.id, PropertyUpdateRequest(label="hijacked"), (landlord_b, session)
            )
        assert update_exc.value.status_code == 404
        assert update_exc.value.code == "property_not_found"

        with pytest.raises(AppError) as delete_exc:
            await delete_property(prop.id, (landlord_b, session), confirm=True)
        assert delete_exc.value.status_code == 404
        assert delete_exc.value.code == "property_not_found"

        # Landlord A can still see it, unaffected by B's failed attempts.
        still_there = await get_property(prop.id, (landlord_a, session))
        assert still_there.label == "A's place"

        # It must not show up in landlord B's own list.
        b_list = await list_properties((landlord_b, session))
        assert prop.id not in {p.id for p in b_list.items}
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)


@pytest.mark.integration
async def test_list_properties_cursor_pagination_newest_first(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created_ids = []
        for i in range(3):
            p = await create_property(
                PropertyCreateRequest(
                    label=f"Property {i}", address_line1=f"{i} Test St", city="Toronto"
                ),
                (landlord, session),
            )
            created_ids.append(p.id)
            # Postgres `now()` is transaction-start-time, not statement-time —
            # committing between creates (as separate real requests would,
            # each with its own get_session transaction) gives each property
            # a genuinely distinct created_at to sort by.
            await session.commit()

        page1 = await list_properties((landlord, session), limit=2)
        assert len(page1.items) == 2
        assert page1.next_cursor is not None
        # Newest-first: the most recently created property comes first.
        assert page1.items[0].id == created_ids[-1]

        page2 = await list_properties((landlord, session), limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 1
        assert page2.next_cursor is None
        assert page2.items[0].id == created_ids[0]

        all_ids = {p.id for p in page1.items} | {p.id for p in page2.items}
        assert all_ids == set(created_ids)
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_list_properties_invalid_cursor_returns_400(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await list_properties((landlord, session), cursor="not-a-valid-cursor!!")
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "invalid_cursor"
    finally:
        await _cleanup(session, landlord_id)


def test_cursor_round_trips() -> None:
    from datetime import UTC, datetime

    from app.pagination import encode_cursor

    now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)
    row_id = str(uuid.uuid4())
    cursor = encode_cursor(now, row_id)
    decoded_at, decoded_id = decode_cursor(cursor)
    assert decoded_at == now
    assert decoded_id == row_id


@pytest.mark.integration
async def test_update_house_rules_writes_audit_log_once(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(
                label="Audit test",
                address_line1="1 Audit St",
                city="Toronto",
                house_rules="original rules",
            ),
            (landlord, session),
        )

        updated = await update_property(
            prop.id, PropertyUpdateRequest(house_rules="new rules"), (landlord, session)
        )
        assert updated.house_rules == "new rules"

        rows = (
            (
                await session.execute(
                    text(
                        "SELECT action, payload FROM audit_log "
                        "WHERE landlord_id = :lid AND action = 'settings_changed'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["payload"]["field"] == "house_rules"
        assert rows[0]["payload"]["property_id"] == str(prop.id)

        # Re-patching with the SAME value must not write a second audit row.
        await update_property(
            prop.id, PropertyUpdateRequest(house_rules="new rules"), (landlord, session)
        )
        rows_after = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'settings_changed'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert rows_after == 1
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_update_quiet_hours_explicit_null_rejected(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="QH test", address_line1="1 QH St", city="Toronto"),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await update_property(
                prop.id, PropertyUpdateRequest(quiet_hours=None), (landlord, session)
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "invalid_field"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
@pytest.mark.parametrize("field", ["label", "address_line1", "city", "province"])
async def test_update_not_nullable_text_field_explicit_null_rejected(
    session: AsyncSession, field: str
) -> None:
    """Every NOT NULL patchable column, not just quiet_hours/heating_season
    (senior review on PR #195, B3) — an explicit JSON null must 422, never
    reach the DB as a raw IntegrityError/500."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="NN test", address_line1="1 NN St", city="Toronto"),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await update_property(
                prop.id, PropertyUpdateRequest(**{field: None}), (landlord, session)
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "invalid_field"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_delete_blocked_by_open_case_returns_409(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="Del test", address_line1="1 Del St", city="Toronto"),
            (landlord, session),
        )
        tenant_id = await factories.insert_tenant(session, landlord_id, str(prop.id))
        await factories.insert_case(
            session, landlord_id=landlord_id, property_id=str(prop.id), tenant_id=tenant_id
        )

        with pytest.raises(AppError) as exc_info:
            await delete_property(prop.id, (landlord, session), confirm=True)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "has_open_cases"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_delete_blocked_by_dependents_returns_409(session: AsyncSession) -> None:
    """No open case, but a tenant row still FK-references the property —
    ``ON DELETE RESTRICT`` fires; must surface as a clean 409, not a 500."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="FK test", address_line1="1 FK St", city="Toronto"),
            (landlord, session),
        )
        await factories.insert_tenant(session, landlord_id, str(prop.id))

        with pytest.raises(AppError) as exc_info:
            await delete_property(prop.id, (landlord, session), confirm=True)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "has_dependents"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_delete_property_success(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="Deletable", address_line1="1 Gone St", city="Toronto"),
            (landlord, session),
        )
        await delete_property(prop.id, (landlord, session), confirm=True)

        with pytest.raises(AppError) as exc_info:
            await get_property(prop.id, (landlord, session))
        assert exc_info.value.code == "property_not_found"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_delete_without_confirm_returns_400(session: AsyncSession) -> None:
    """#53: deleting a property with a live phone number is irreversible for
    that number — an explicit ?confirm=true is required, checked before
    the open-cases/dependents business checks."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(
                label="Needs confirm", address_line1="1 Confirm St", city="Toronto"
            ),
            (landlord, session),
        )
        with pytest.raises(AppError) as exc_info:
            await delete_property(prop.id, (landlord, session))
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "confirmation_required"

        # Never actually deleted.
        still_there = await get_property(prop.id, (landlord, session))
        assert still_there.id == prop.id
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_create_property_provisions_twilio_number(
    session: AsyncSession, _fake_twilio_provisioner: _FakeProvisioner
) -> None:
    """#53: POST /v1/properties actually provisions — the fake purchased
    exactly one number and never released it (happy path)."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        created = await create_property(
            PropertyCreateRequest(
                label="Provisioned", address_line1="1 Provision St", city="Toronto"
            ),
            (landlord, session),
        )
        assert created.twilio_number is not None
        assert len(_fake_twilio_provisioner.purchased) == 1
        assert _fake_twilio_provisioner.released == []

        row = (
            (
                await session.execute(
                    text("SELECT twilio_sid FROM properties WHERE id = :id"),
                    {"id": str(created.id)},
                )
            )
            .mappings()
            .one()
        )
        assert row["twilio_sid"] == _fake_twilio_provisioner.purchased[0]
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_delete_schedules_number_release_with_grace_period(
    session: AsyncSession, _fake_twilio_provisioner: _FakeProvisioner
) -> None:
    """#53: deprovisioning does NOT call Twilio synchronously — it writes a
    durable, sweep-visible `number_release` row with a ~24h next_attempt_at,
    and never released the number (that's the sweeper's job)."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop = await create_property(
            PropertyCreateRequest(label="Grace period", address_line1="1 Grace St", city="Toronto"),
            (landlord, session),
        )
        twilio_sid = _fake_twilio_provisioner.purchased[0]

        await delete_property(prop.id, (landlord, session), confirm=True)
        assert _fake_twilio_provisioner.released == []  # not released synchronously

        row = (
            (
                await session.execute(
                    text(
                        "SELECT type, channel, status, payload, next_attempt_at, created_at "
                        "FROM notifications WHERE landlord_id = :lid AND type = 'number_release'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["channel"] == "push"
        assert row["status"] == "pending"
        assert row["payload"]["twilio_sid"] == twilio_sid
        assert row["payload"]["property_id"] == str(prop.id)
        assert row["payload"]["landlord_id"] == landlord_id
        delta = row["next_attempt_at"] - row["created_at"]
        assert 23 * 3600 < delta.total_seconds() < 25 * 3600
    finally:
        await _cleanup(session, landlord_id)


# ---------------------------------------------------------------------------
# H1 (safety review, 2026-07-13) — pre-Twilio-call money guards.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_property_cap_returns_409_before_any_purchase(
    session: AsyncSession,
    _fake_twilio_provisioner: _FakeProvisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "max_properties_per_landlord", 2)
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await create_property(
            PropertyCreateRequest(label="One", address_line1="1 Cap St", city="Toronto"),
            (landlord, session),
        )
        await create_property(
            PropertyCreateRequest(label="Two", address_line1="2 Cap St", city="Toronto"),
            (landlord, session),
        )
        purchased_before = len(_fake_twilio_provisioner.purchased)
        assert purchased_before == 2

        with pytest.raises(AppError) as exc_info:
            await create_property(
                PropertyCreateRequest(label="Three", address_line1="3 Cap St", city="Toronto"),
                (landlord, session),
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "property_limit_reached"
        # The cap check runs BEFORE any Twilio call -- no purchase attempted.
        assert len(_fake_twilio_provisioner.purchased) == purchased_before
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_create_property_duplicate_address_retry_buys_zero_numbers(
    session: AsyncSession, _fake_twilio_provisioner: _FakeProvisioner
) -> None:
    """H1: a client retrying the SAME create (e.g. after a timeout) must
    hit the dedupe check instead of buying a second number for what is,
    from the landlord's perspective, the same property."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await create_property(
            PropertyCreateRequest(
                label="Original", address_line1="41 Palmerston Ave", city="Toronto", province="ON"
            ),
            (landlord, session),
        )
        assert len(_fake_twilio_provisioner.purchased) == 1

        with pytest.raises(AppError) as exc_info:
            await create_property(
                # Different label + casing/whitespace -- SAME normalized address.
                PropertyCreateRequest(
                    label="Retry",
                    address_line1="  41 palmerston ave  ",
                    city="TORONTO",
                    province="on",
                ),
                (landlord, session),
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "duplicate_property"
        assert len(_fake_twilio_provisioner.purchased) == 1  # zero additional purchases
    finally:
        await _cleanup(session, landlord_id)


# ---------------------------------------------------------------------------
# Spec MAJOR-1 — DB-insert-failure compensation, end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_db_insert_failure_after_purchase_compensates_and_releases(
    session: AsyncSession, _fake_twilio_provisioner: _FakeProvisioner
) -> None:
    """Force the property INSERT to fail (a UNIQUE-constraint collision on
    twilio_number) after a successful fake purchase, and prove the full
    compensation contract: 502 provisioning_failed, the purchased SID gets
    released, and ZERO new properties rows persist."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        conflicting_number = "+14165559876"
        await factories.insert_property(session, landlord_id, twilio_number=conflicting_number)
        _fake_twilio_provisioner.fixed_phone_number = conflicting_number

        with pytest.raises(AppError) as exc_info:
            await create_property(
                PropertyCreateRequest(
                    label="Conflict test", address_line1="1 Conflict St", city="Toronto"
                ),
                (landlord, session),
            )
        assert exc_info.value.status_code == 502
        assert exc_info.value.code == "provisioning_failed"
        await session.rollback()  # clear the aborted transaction from the UNIQUE violation

        assert _fake_twilio_provisioner.purchased  # the purchase DID happen
        purchased_sid = _fake_twilio_provisioner.purchased[-1]
        assert _fake_twilio_provisioner.released == [purchased_sid]  # released as compensation

        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM properties WHERE landlord_id = :lid "
                    "AND label = 'Conflict test'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert count == 0  # zero new properties rows persisted
    finally:
        await _cleanup(session, landlord_id)


# ---------------------------------------------------------------------------
# Spec MAJOR-2 — each provisioning exception maps to its own contract code.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_property_public_base_url_unconfigured_returns_500(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "public_base_url", None)
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await create_property(
                PropertyCreateRequest(
                    label="No base url", address_line1="1 Unconfigured St", city="Toronto"
                ),
                (landlord, session),
            )
        assert exc_info.value.status_code == 500
        assert exc_info.value.code == "public_base_url_unconfigured"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_create_property_no_numbers_available_returns_503(
    session: AsyncSession, _fake_twilio_provisioner: _FakeProvisioner
) -> None:
    _fake_twilio_provisioner.always_empty_search = True
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await create_property(
                PropertyCreateRequest(
                    label="No numbers", address_line1="1 Empty St", city="Toronto"
                ),
                (landlord, session),
            )
        assert exc_info.value.status_code == 503
        assert exc_info.value.code == "no_numbers_available"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_create_property_purchase_failure_returns_502(
    session: AsyncSession, _fake_twilio_provisioner: _FakeProvisioner
) -> None:
    _fake_twilio_provisioner.fail_purchase = True
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await create_property(
                PropertyCreateRequest(
                    label="Purchase fails", address_line1="1 Fail St", city="Toronto"
                ),
                (landlord, session),
            )
        assert exc_info.value.status_code == 502
        assert exc_info.value.code == "provisioning_failed"
    finally:
        await _cleanup(session, landlord_id)


# ---------------------------------------------------------------------------
# #203 item 1 — the address-dedupe pre-check's TOCTOU race, closed at the DB
# level by migration 0013's uq_properties_landlord_address_dedupe.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_property_loses_concurrent_race_gets_409_and_releases_number(
    db_engine: AsyncEngine, _fake_twilio_provisioner: _FakeProvisioner
) -> None:
    """Two genuinely concurrent creates for the SAME normalized address:
    the pre-check SELECT alone can't stop this (neither request's own
    uncommitted insert is visible to the other's pre-check). Reproduced
    here with real DB-level blocking, not a hoped-for asyncio interleaving:
    session A holds an UNCOMMITTED insert for the address open, session B
    runs create_property normally as a real task and is proven BLOCKED
    (not merely fast) on the row lock the new unique index creates before A
    ever commits. Once A commits, B's blocked INSERT unblocks as a genuine
    IntegrityError; the router's widened compensation releases B's
    just-purchased number and returns a clean 409 duplicate_property --
    never an orphaned billed number."""
    async with AsyncSession(db_engine) as session_a, AsyncSession(db_engine) as session_b:
        landlord_id = await factories.insert_landlord(session_a)
        await session_a.commit()
        landlord = Landlord(id=uuid.UUID(landlord_id))

        try:
            # Session A: raw INSERT of the SAME normalized address, held
            # open (uncommitted) -- simulates "another concurrent request
            # already got past the same pre-check and is mid-flight."
            await session_a.execute(
                text(
                    "INSERT INTO properties (id, landlord_id, label, address_line1, city, "
                    "province, twilio_number, twilio_sid) VALUES "
                    "(:id, :landlord_id, 'Winner', '77 Race St', 'Toronto', 'ON', :tn, :ts)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "landlord_id": landlord_id,
                    "tn": "+19995550000",
                    "ts": "PNwinnersid00000000000000000000",
                },
            )

            # Session B: the normal create_property path, different
            # casing/whitespace -- same normalized key. Runs as a real task
            # so it can genuinely block on A's row lock.
            task_b = asyncio.create_task(
                create_property(
                    PropertyCreateRequest(
                        label="Loser",
                        address_line1="  77 race st  ",
                        city="TORONTO",
                        province="on",
                    ),
                    (landlord, session_b),
                )
            )

            # Give task_b's own pre-check + fake-Twilio purchase + INSERT a
            # moment to actually reach Postgres and block on A's row lock.
            await asyncio.sleep(0.5)
            assert not task_b.done(), "task_b should be blocked on the row lock, not finished yet"

            # Commit A -- unblocks B's INSERT, which now genuinely conflicts.
            await session_a.commit()

            with pytest.raises(AppError) as exc_info:
                await task_b
            assert exc_info.value.status_code == 409
            assert exc_info.value.code == "duplicate_property"

            # Exactly one property at this address for this landlord.
            count = (
                await session_a.execute(
                    text("SELECT COUNT(*) FROM properties WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            ).scalar_one()
            assert count == 1

            # B's purchase happened (it passed the pre-check, since A's
            # insert was uncommitted) but was released as compensation --
            # never an orphaned billed number.
            assert len(_fake_twilio_provisioner.purchased) == 1
            assert _fake_twilio_provisioner.released == [_fake_twilio_provisioner.purchased[0]]
        finally:
            await session_b.rollback()  # clear the aborted transaction from the IntegrityError
            await _cleanup(session_a, landlord_id)


# ---------------------------------------------------------------------------
# #203 item 2 — a require_landlord/get_session teardown-commit failure AFTER
# a successful purchase must page + release, never silently orphan.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_property_teardown_commit_failure_after_purchase_pages_and_releases(
    session: AsyncSession,
    _fake_twilio_provisioner: _FakeProvisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A require_landlord/get_session teardown-commit failure after a
    successful purchase used to orphan a billed number with zero signal
    (#204 senior review finding) -- create_property now commits explicitly
    INSIDE its own guarded try, so a commit-time failure hits the SAME
    compensation path (alert_purchased_but_unrecorded + release_number_
    best_effort) as any other post-purchase DB failure. Simulated here by
    making the FIRST session.commit() call (the new explicit one) raise;
    the session's real transaction is never actually committed at the
    Postgres level either, so nothing is left orphaned in the DB."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))

    real_commit = session.commit
    call_count = 0

    async def _failing_commit() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated teardown-commit failure")
        await real_commit()

    monkeypatch.setattr(session, "commit", _failing_commit)

    alerted_sids: list[str] = []
    monkeypatch.setattr(
        property_provisioning,
        "alert_purchased_but_unrecorded",
        lambda twilio_sid: alerted_sids.append(twilio_sid),
    )

    try:
        with pytest.raises(AppError) as exc_info:
            await create_property(
                PropertyCreateRequest(
                    label="Commit fails", address_line1="1 Commit Fail St", city="Toronto"
                ),
                (landlord, session),
            )
        assert exc_info.value.status_code == 502
        assert exc_info.value.code == "provisioning_failed"
        assert call_count == 1  # the failing commit was actually reached

        purchased_sid = _fake_twilio_provisioner.purchased[-1]
        assert alerted_sids == [purchased_sid]  # the loud, always-fires page
        assert _fake_twilio_provisioner.released == [purchased_sid]  # compensated, never orphaned

        # Restore the real commit before touching the session further --
        # discard the never-actually-committed INSERT.
        monkeypatch.setattr(session, "commit", real_commit)
        await session.rollback()
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM properties WHERE landlord_id = :lid "
                    "AND label = 'Commit fails'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert count == 0
    finally:
        monkeypatch.setattr(session, "commit", real_commit)
        await _cleanup(session, landlord_id)
