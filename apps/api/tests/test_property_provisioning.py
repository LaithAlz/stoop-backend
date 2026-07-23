"""Tests for app/property_provisioning.py (#53) — provisioning
orchestration (search cascade, purchase, webhook config, best-effort A2P,
compensation-on-failure) and deprovisioning (grace-period scheduling +
sweep).

Provisioning-orchestration tests (``unit``) use a FAKE, non-HTTP
``TwilioProvisioner`` injected via ``set_twilio_provisioner_for_tests`` —
never touches the network. Twilio's actual REST shapes are covered
separately in ``tests/test_twilio_provision.py``.

Deprovisioning-scheduling/sweep tests (``integration``) need a real
Postgres (the `notifications` row + its dedupe index), same
docker-compose harness every other integration test module here uses.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
import app.property_provisioning as pp
from app.config import settings
from app.integrations import twilio_provision
from app.integrations.twilio_provision import TwilioNumberNotFoundError
from app.routers.webhooks.twilio import SMS_WEBHOOK_PATH, VOICE_WEBHOOK_PATH
from tests import factories

_PUBLIC_BASE_URL = "https://api.stoop.test"


# ---------------------------------------------------------------------------
# Fake TwilioProvisioner — records every call, configurable failure points.
# ---------------------------------------------------------------------------


@dataclass
class _FakeProvisioner:
    search_results: dict[str, list[str]] = field(default_factory=dict)
    fail_search: bool = False
    fail_purchase: bool = False
    fail_configure: bool = False
    fail_associate: bool = False
    fail_release: bool = False
    release_not_found: bool = False

    purchased: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    configured: list[tuple[str, str, str]] = field(default_factory=list)
    associated: list[tuple[str, str]] = field(default_factory=list)
    release_calls: int = 0

    async def search_available_numbers(
        self, *, area_code: str | None = None, region: str | None = None
    ) -> list[str]:
        if self.fail_search:
            raise RuntimeError("fake search failure")
        if area_code:
            return self.search_results.get(f"area:{area_code}", [])
        if region:
            return self.search_results.get(f"region:{region}", [])
        return self.search_results.get("any", [])

    async def purchase_number(self, *, phone_number: str) -> str:
        if self.fail_purchase:
            raise RuntimeError("fake purchase failure")
        sid = f"PN{len(self.purchased) + 1:04d}"
        self.purchased.append(sid)
        return sid

    async def configure_webhooks(self, *, twilio_sid: str, sms_url: str, voice_url: str) -> None:
        if self.fail_configure:
            raise RuntimeError("fake configure failure")
        self.configured.append((twilio_sid, sms_url, voice_url))

    async def associate_messaging_service(
        self, *, twilio_sid: str, messaging_service_sid: str
    ) -> None:
        if self.fail_associate:
            raise RuntimeError("fake associate failure")
        self.associated.append((twilio_sid, messaging_service_sid))

    async def release_number(self, *, twilio_sid: str) -> None:
        self.release_calls += 1
        if self.release_not_found:
            raise TwilioNumberNotFoundError(twilio_sid)
        if self.fail_release:
            raise RuntimeError("fake release failure")
        self.released.append(twilio_sid)


@pytest.fixture
def fake_provisioner() -> _FakeProvisioner:
    fake = _FakeProvisioner()
    twilio_provision.set_twilio_provisioner_for_tests(fake)
    return fake


@pytest.fixture(autouse=True)
def _configured_public_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    monkeypatch.setattr(settings, "twilio_messaging_service_sid", None)


# ---------------------------------------------------------------------------
# public_base_url config gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_public_base_url_unconfigured_raises_before_any_twilio_call(
    monkeypatch: pytest.MonkeyPatch, fake_provisioner: _FakeProvisioner
) -> None:
    monkeypatch.setattr(settings, "public_base_url", None)
    with pytest.raises(pp.PublicBaseUrlUnconfiguredError):
        await pp.provision_number(area_code=None, province="ON")
    assert fake_provisioner.purchased == []


# ---------------------------------------------------------------------------
# Search cascade
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_provision_number_uses_area_code_first(fake_provisioner: _FakeProvisioner) -> None:
    fake_provisioner.search_results = {"area:416": ["+14165551111"]}
    result = await pp.provision_number(area_code="416", province="ON")

    assert result.phone_number == "+14165551111"
    assert result.twilio_sid == fake_provisioner.purchased[0]
    assert fake_provisioner.configured == [
        (
            result.twilio_sid,
            f"{_PUBLIC_BASE_URL}{SMS_WEBHOOK_PATH}",
            f"{_PUBLIC_BASE_URL}{VOICE_WEBHOOK_PATH}",
        )
    ]
    assert result.a2p_status == "skipped_unconfigured"


@pytest.mark.unit
def test_webhook_paths_match_the_live_route_table() -> None:
    """Safety review finding L3: these constants must actually come from
    the registered FastAPI route table, not merely happen to equal the
    right literal — compare against the assembled app's own route list."""
    from app.main import app as fastapi_app

    registered = {
        route.path
        for route in fastapi_app.routes
        if hasattr(route, "path") and getattr(route, "path", "").startswith("/webhooks/twilio")
    }
    assert SMS_WEBHOOK_PATH in registered
    assert VOICE_WEBHOOK_PATH in registered
    assert SMS_WEBHOOK_PATH == "/webhooks/twilio/sms"
    assert VOICE_WEBHOOK_PATH == "/webhooks/twilio/voice"


@pytest.mark.unit
async def test_provision_number_falls_back_to_province_when_area_code_empty(
    fake_provisioner: _FakeProvisioner,
) -> None:
    fake_provisioner.search_results = {"area:416": [], "region:ON": ["+14165552222"]}
    result = await pp.provision_number(area_code="416", province="ON")
    assert result.phone_number == "+14165552222"


@pytest.mark.unit
async def test_provision_number_falls_back_to_any_when_area_code_and_province_empty(
    fake_provisioner: _FakeProvisioner,
) -> None:
    fake_provisioner.search_results = {"area:416": [], "region:ON": [], "any": ["+14165553333"]}
    result = await pp.provision_number(area_code="416", province="ON")
    assert result.phone_number == "+14165553333"


@pytest.mark.unit
async def test_provision_number_no_area_code_skips_straight_to_province(
    fake_provisioner: _FakeProvisioner,
) -> None:
    fake_provisioner.search_results = {"region:ON": ["+14165554444"]}
    result = await pp.provision_number(area_code=None, province="ON")
    assert result.phone_number == "+14165554444"


# ---------------------------------------------------------------------------
# No numbers available / genuine failures
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_provision_number_no_numbers_available_raises_and_purchases_nothing(
    fake_provisioner: _FakeProvisioner,
) -> None:
    fake_provisioner.search_results = {}
    with pytest.raises(pp.NoNumbersAvailableError):
        await pp.provision_number(area_code="416", province="ON")
    assert fake_provisioner.purchased == []


@pytest.mark.unit
async def test_provision_number_search_failure_raises_provisioning_failed(
    fake_provisioner: _FakeProvisioner,
) -> None:
    fake_provisioner.fail_search = True
    with pytest.raises(pp.ProvisioningFailedError) as exc_info:
        await pp.provision_number(area_code="416", province="ON")
    assert exc_info.value.stage == "search"
    assert fake_provisioner.purchased == []


@pytest.mark.unit
async def test_provision_number_purchase_failure_raises_without_release(
    fake_provisioner: _FakeProvisioner,
) -> None:
    fake_provisioner.search_results = {"area:416": ["+14165551111"]}
    fake_provisioner.fail_purchase = True
    with pytest.raises(pp.ProvisioningFailedError) as exc_info:
        await pp.provision_number(area_code="416", province="ON")
    assert exc_info.value.stage == "purchase"
    assert fake_provisioner.released == []  # nothing was ever purchased


@pytest.mark.unit
async def test_provision_number_configure_failure_releases_the_purchased_number(
    fake_provisioner: _FakeProvisioner,
) -> None:
    fake_provisioner.search_results = {"area:416": ["+14165551111"]}
    fake_provisioner.fail_configure = True
    with pytest.raises(pp.ProvisioningFailedError) as exc_info:
        await pp.provision_number(area_code="416", province="ON")
    assert exc_info.value.stage == "configure_webhooks"
    assert fake_provisioner.released == fake_provisioner.purchased  # compensated


@pytest.mark.unit
async def test_release_number_best_effort_never_raises_even_on_failure(
    fake_provisioner: _FakeProvisioner,
) -> None:
    fake_provisioner.fail_release = True
    await pp.release_number_best_effort("PN0001")  # must not raise
    assert fake_provisioner.released == []


@pytest.mark.unit
async def test_release_number_best_effort_guard_check_failure_fails_safe(
    fake_provisioner: _FakeProvisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2 guard infra failure (safety re-review advisory A1, #203): if the
    ownership SELECT itself raises, we cannot tell whether a live property
    still references the SID -- so the release is SKIPPED (never sever a
    possibly-live property's emergency line), the never-raises contract
    holds (a guard failure in ``provision_number``'s compensation path must
    not mask the original ``ProvisioningFailedError``), and ops is paged."""
    mock_capture = MagicMock()
    monkeypatch.setattr(pp.sentry_sdk, "capture_message", mock_capture)

    class _RaisingSession:
        async def execute(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("guard SELECT infra failure")

    async def _fake_admin_session() -> AsyncGenerator[_RaisingSession, None]:
        yield _RaisingSession()

    monkeypatch.setattr(pp, "get_admin_session", _fake_admin_session)

    await pp.release_number_best_effort("PN0001")  # must not raise

    # Fail-safe: ownership is unknown, so the release was never even attempted.
    assert fake_provisioner.release_calls == 0
    assert fake_provisioner.released == []
    # Ops paged with the distinct guard-failure message (not the "SKIPPED --
    # live property owns" nor the "release failed" page).
    mock_capture.assert_called_once()
    assert "guard could not run" in mock_capture.call_args.args[0]


# ---------------------------------------------------------------------------
# A2P association — configured / unconfigured / configured-but-failing
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_provision_number_a2p_unconfigured_skips_gracefully(
    fake_provisioner: _FakeProvisioner,
) -> None:
    fake_provisioner.search_results = {"area:416": ["+14165551111"]}
    result = await pp.provision_number(area_code="416", province="ON")
    assert result.a2p_status == "skipped_unconfigured"
    assert fake_provisioner.associated == []


@pytest.mark.unit
async def test_provision_number_a2p_configured_associates(
    monkeypatch: pytest.MonkeyPatch, fake_provisioner: _FakeProvisioner
) -> None:
    monkeypatch.setattr(settings, "twilio_messaging_service_sid", "MGxyz")
    fake_provisioner.search_results = {"area:416": ["+14165551111"]}
    result = await pp.provision_number(area_code="416", province="ON")
    assert result.a2p_status == "associated"
    assert fake_provisioner.associated == [(result.twilio_sid, "MGxyz")]


@pytest.mark.unit
async def test_provision_number_a2p_configured_but_failing_does_not_fail_provisioning(
    monkeypatch: pytest.MonkeyPatch, fake_provisioner: _FakeProvisioner
) -> None:
    monkeypatch.setattr(settings, "twilio_messaging_service_sid", "MGxyz")
    fake_provisioner.search_results = {"area:416": ["+14165551111"]}
    fake_provisioner.fail_associate = True

    result = await pp.provision_number(area_code="416", province="ON")

    assert result.a2p_status == "failed"
    assert result.twilio_sid == fake_provisioner.purchased[0]  # provisioning still succeeded
    assert fake_provisioner.released == []  # never compensated for an A2P failure


# ---------------------------------------------------------------------------
# Deprovisioning: schedule_number_release / sweep_pending_number_releases
# (integration — real Postgres for the notifications table + dedupe index)
# ---------------------------------------------------------------------------

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"


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
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine) as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def dispose_app_engine() -> AsyncGenerator[None, None]:
    """``sweep_pending_number_releases`` uses ``get_admin_session`` — the
    app's own module-level engine, separate from this file's ``db_engine``
    fixture. Same cross-event-loop hazard as
    ``tests/test_agent_emergency_chain.py``'s own fixture of this name."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.rollback()
    await session.execute(
        text("DELETE FROM notifications WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM properties WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


@pytest.mark.integration
async def test_schedule_number_release_writes_pending_row(db_session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165551111"
    )
    try:
        await pp.schedule_number_release(
            db_session, landlord_id=landlord_id, property_id=property_id, twilio_sid="PN0001"
        )
        await db_session.commit()

        row = (
            (
                await db_session.execute(
                    text(
                        "SELECT type, channel, status, payload, next_attempt_at, created_at "
                        "FROM notifications WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["type"] == "number_release"
        assert row["channel"] == "push"
        assert row["status"] == "pending"
        assert row["payload"]["twilio_sid"] == "PN0001"
        assert row["payload"]["property_id"] == property_id
        assert row["payload"]["landlord_id"] == landlord_id
        delta = (row["next_attempt_at"] - row["created_at"]).total_seconds()
        grace = pp.NUMBER_RELEASE_GRACE_PERIOD_SECONDS
        assert grace - 5 < delta < grace + 5
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_schedule_number_release_idempotent_on_same_sid(db_session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165551111"
    )
    try:
        await pp.schedule_number_release(
            db_session, landlord_id=landlord_id, property_id=property_id, twilio_sid="PN0001"
        )
        await pp.schedule_number_release(
            db_session, landlord_id=landlord_id, property_id=property_id, twilio_sid="PN0001"
        )
        await db_session.commit()

        count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'number_release'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_releases_due_row_and_marks_sent(
    db_session: AsyncSession, fake_provisioner: _FakeProvisioner
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165551111"
    )
    try:
        past = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.execute(
            text(
                "INSERT INTO notifications "
                "(landlord_id, type, channel, status, payload, next_attempt_at) "
                "VALUES (:lid, 'number_release', 'push', 'pending', "
                "CAST(:payload AS jsonb), :next_attempt_at)"
            ),
            {
                "lid": landlord_id,
                "payload": f'{{"twilio_sid": "PN0001", "property_id": "{property_id}"}}',
                "next_attempt_at": past,
            },
        )
        await db_session.commit()

        released = await pp.sweep_pending_number_releases()
        assert released == ["PN0001"]
        assert fake_provisioner.released == ["PN0001"]

        status = (
            await db_session.execute(
                text(
                    "SELECT status FROM notifications "
                    "WHERE landlord_id = :lid AND type = 'number_release'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert status == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_skips_not_yet_due_row(
    db_session: AsyncSession, fake_provisioner: _FakeProvisioner
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165551111"
    )
    try:
        future = datetime.now(UTC) + timedelta(hours=1)
        await db_session.execute(
            text(
                "INSERT INTO notifications "
                "(landlord_id, type, channel, status, payload, next_attempt_at) "
                "VALUES (:lid, 'number_release', 'push', 'pending', "
                "CAST(:payload AS jsonb), :next_attempt_at)"
            ),
            {
                "lid": landlord_id,
                "payload": f'{{"twilio_sid": "PN0002", "property_id": "{property_id}"}}',
                "next_attempt_at": future,
            },
        )
        await db_session.commit()

        released = await pp.sweep_pending_number_releases()
        assert released == []
        assert fake_provisioner.released == []
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_retries_on_failure_then_exhausts(
    db_session: AsyncSession, fake_provisioner: _FakeProvisioner
) -> None:
    fake_provisioner.fail_release = True
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165551111"
    )
    try:
        past = datetime.now(UTC) - timedelta(seconds=1)
        result = await db_session.execute(
            text(
                "INSERT INTO notifications "
                "(landlord_id, type, channel, status, payload, next_attempt_at, attempt) "
                "VALUES (:lid, 'number_release', 'push', 'pending', "
                "CAST(:payload AS jsonb), :next_attempt_at, :attempt) RETURNING id"
            ),
            {
                "lid": landlord_id,
                "payload": f'{{"twilio_sid": "PN0003", "property_id": "{property_id}"}}',
                "next_attempt_at": past,
                "attempt": pp._NUMBER_RELEASE_MAX_ATTEMPTS - 1,  # noqa: SLF001
            },
        )
        notification_id = result.scalar_one()
        await db_session.commit()

        released = await pp.sweep_pending_number_releases()
        assert released == []  # release itself failed

        row = (
            (
                await db_session.execute(
                    text("SELECT status, attempt FROM notifications WHERE id = :id"),
                    {"id": notification_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "exhausted"
        # The claim (M1/L1) always advances attempt BEFORE the release call,
        # regardless of whether this turns out to be the exhausting attempt.
        assert row["attempt"] == pp._NUMBER_RELEASE_MAX_ATTEMPTS  # noqa: SLF001
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_retries_before_exhausting(
    db_session: AsyncSession, fake_provisioner: _FakeProvisioner
) -> None:
    fake_provisioner.fail_release = True
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165551111"
    )
    try:
        past = datetime.now(UTC) - timedelta(seconds=1)
        result = await db_session.execute(
            text(
                "INSERT INTO notifications "
                "(landlord_id, type, channel, status, payload, next_attempt_at, attempt) "
                "VALUES (:lid, 'number_release', 'push', 'pending', "
                "CAST(:payload AS jsonb), :next_attempt_at, 0) RETURNING id"
            ),
            {
                "lid": landlord_id,
                "payload": f'{{"twilio_sid": "PN0004", "property_id": "{property_id}"}}',
                "next_attempt_at": past,
            },
        )
        notification_id = result.scalar_one()
        await db_session.commit()

        released = await pp.sweep_pending_number_releases()
        assert released == []

        row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, attempt, next_attempt_at FROM notifications WHERE id = :id"
                    ),
                    {"id": notification_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "pending"  # not yet exhausted
        assert row["attempt"] == 1
        assert row["next_attempt_at"] > past
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# M2 — a 404 from Twilio (already released) is treated as SUCCESS, never a
# retryable failure and never a Sentry page.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sweep_release_not_found_is_treated_as_success(
    db_session: AsyncSession,
    fake_provisioner: _FakeProvisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_provisioner.release_not_found = True
    mock_capture = MagicMock()
    monkeypatch.setattr(pp.sentry_sdk, "capture_message", mock_capture)

    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165551111"
    )
    try:
        past = datetime.now(UTC) - timedelta(seconds=1)
        result = await db_session.execute(
            text(
                "INSERT INTO notifications "
                "(landlord_id, type, channel, status, payload, next_attempt_at) "
                "VALUES (:lid, 'number_release', 'push', 'pending', "
                "CAST(:payload AS jsonb), :next_attempt_at) RETURNING id"
            ),
            {
                "lid": landlord_id,
                "payload": f'{{"twilio_sid": "PN0006", "property_id": "{property_id}"}}',
                "next_attempt_at": past,
            },
        )
        notification_id = result.scalar_one()
        await db_session.commit()

        released = await pp.sweep_pending_number_releases()
        assert released == ["PN0006"]  # a 404 counts as "released" for this goal
        assert fake_provisioner.release_calls == 1  # zero retries

        row = (
            (
                await db_session.execute(
                    text("SELECT status, attempt FROM notifications WHERE id = :id"),
                    {"id": notification_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "sent"
        mock_capture.assert_not_called()  # no Sentry page for an already-gone number
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# L2 — never release a SID a LIVE properties row still references.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sweep_blocks_release_when_a_live_property_owns_the_sid(
    db_session: AsyncSession,
    fake_provisioner: _FakeProvisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_capture = MagicMock()
    monkeypatch.setattr(pp.sentry_sdk, "capture_message", mock_capture)

    landlord_id = await factories.insert_landlord(db_session)
    # A DIFFERENT, still-live property currently owns this exact SID --
    # structurally shouldn't happen today, but the guard must fire anyway.
    # (_cleanup below deletes every property for this landlord regardless
    # of twilio_sid, so no separate teardown is needed for this row.)
    await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165559999", twilio_sid="PN0007"
    )
    try:
        past = datetime.now(UTC) - timedelta(seconds=1)
        result = await db_session.execute(
            text(
                "INSERT INTO notifications "
                "(landlord_id, type, channel, status, payload, next_attempt_at) "
                "VALUES (:lid, 'number_release', 'push', 'pending', "
                "CAST(:payload AS jsonb), :next_attempt_at) RETURNING id"
            ),
            {
                "lid": landlord_id,
                "payload": '{"twilio_sid": "PN0007", "property_id": "some-other-property"}',
                "next_attempt_at": past,
            },
        )
        notification_id = result.scalar_one()
        await db_session.commit()

        released = await pp.sweep_pending_number_releases()
        assert released == []
        assert fake_provisioner.release_calls == 0  # Twilio was never called

        row = (
            (
                await db_session.execute(
                    text("SELECT status FROM notifications WHERE id = :id"),
                    {"id": notification_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "exhausted"
        mock_capture.assert_called_once()
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# M1/L1 — claim-before-call: lost-claim race, loser does nothing.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_claim_sql_rejects_a_stale_attempt_value(db_session: AsyncSession) -> None:
    """Deterministic, mechanism-level proof of the CAS guard: a claim
    attempt using an ``old_attempt`` value that no longer matches the
    row's CURRENT attempt (simulating a reader whose SELECT happened
    before a concurrent claimant already won) returns no row."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165551111"
    )
    try:
        result = await db_session.execute(
            text(
                "INSERT INTO notifications (landlord_id, type, channel, status, payload, attempt) "
                "VALUES (:lid, 'number_release', 'push', 'pending', CAST(:payload AS jsonb), 0) "
                "RETURNING id"
            ),
            {
                "lid": landlord_id,
                "payload": f'{{"twilio_sid": "PN0008", "property_id": "{property_id}"}}',
            },
        )
        notification_id = result.scalar_one()
        await db_session.commit()

        # A genuine concurrent claimant wins first, advancing attempt to 1.
        winner = (
            (
                await db_session.execute(
                    pp._CLAIM_NUMBER_RELEASE_SQL,  # noqa: SLF001
                    {
                        "id": notification_id,
                        "old_attempt": 0,
                        "new_attempt": 1,
                        "next_attempt_at": datetime.now(UTC) + timedelta(minutes=15),
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        await db_session.commit()
        assert winner is not None

        # A second claim attempt using the NOW-STALE old_attempt=0 (as a
        # reader whose own SELECT ran before the winner's claim committed
        # would use) must find nothing -- the exact "loser does nothing"
        # guarantee.
        loser = (
            (
                await db_session.execute(
                    pp._CLAIM_NUMBER_RELEASE_SQL,  # noqa: SLF001
                    {
                        "id": notification_id,
                        "old_attempt": 0,
                        "new_attempt": 1,
                        "next_attempt_at": datetime.now(UTC) + timedelta(minutes=15),
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        await db_session.commit()
        assert loser is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sweep_concurrent_calls_release_the_same_due_row_exactly_once(
    db_session: AsyncSession, fake_provisioner: _FakeProvisioner
) -> None:
    """Higher-level proof of the same guarantee, via genuine concurrency:
    two overlapping ``sweep_pending_number_releases()`` calls racing the
    SAME due row must result in exactly ONE actual Twilio release call --
    the CAS claim (not application-level locking) is what makes this safe
    regardless of how the two calls happen to interleave."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number="+14165551111"
    )
    try:
        past = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.execute(
            text(
                "INSERT INTO notifications "
                "(landlord_id, type, channel, status, payload, next_attempt_at) "
                "VALUES (:lid, 'number_release', 'push', 'pending', "
                "CAST(:payload AS jsonb), :next_attempt_at)"
            ),
            {
                "lid": landlord_id,
                "payload": f'{{"twilio_sid": "PN0009", "property_id": "{property_id}"}}',
                "next_attempt_at": past,
            },
        )
        await db_session.commit()

        results = await asyncio.gather(
            pp.sweep_pending_number_releases(), pp.sweep_pending_number_releases()
        )
        combined_released = results[0] + results[1]
        assert combined_released == ["PN0009"]  # exactly one caller won the claim
        assert fake_provisioner.release_calls == 1  # Twilio's release endpoint hit exactly once
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Per-tick batch LIMIT (safety review M1/L1)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sweep_respects_per_tick_batch_limit(
    db_session: AsyncSession, fake_provisioner: _FakeProvisioner
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    try:
        past = datetime.now(UTC) - timedelta(seconds=1)
        property_ids = []
        for i in range(pp._NUMBER_RELEASE_SWEEP_BATCH_LIMIT + 5):  # noqa: SLF001
            property_id = await factories.insert_property(
                db_session, landlord_id, twilio_number=f"+141655500{i:02d}"
            )
            property_ids.append(property_id)
            payload = f'{{"twilio_sid": "PNBATCH{i:03d}", "property_id": "{property_id}"}}'
            await db_session.execute(
                text(
                    "INSERT INTO notifications "
                    "(landlord_id, type, channel, status, payload, next_attempt_at) "
                    "VALUES (:lid, 'number_release', 'push', 'pending', "
                    "CAST(:payload AS jsonb), :next_attempt_at)"
                ),
                {"lid": landlord_id, "payload": payload, "next_attempt_at": past},
            )
        await db_session.commit()

        released = await pp.sweep_pending_number_releases()
        assert len(released) == pp._NUMBER_RELEASE_SWEEP_BATCH_LIMIT  # noqa: SLF001
    finally:
        await _cleanup(db_session, landlord_id)
