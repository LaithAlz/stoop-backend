"""Integration tests for ``POST /v1/cases/{id}/resolve`` (#206) —
``app/routers/cases.py::resolve_case``, the landlord-direct resolve path
that was documented in ``api-contracts.md`` but had zero implementation
and zero caller before this issue.

Harness: direct handler-function calls with a synthetic ``(Landlord,
AsyncSession)`` tuple, matching ``tests/test_cases_router.py``'s /
``tests/test_routers_trust.py``'s own convention (no HTTP round-trip
needed for the request/response shape — RLS route-scoping is already
proven generically by ``tests/test_rls_isolation_matrix.py``).

The safety-edge tests additionally drive ``app/agent/draft_sender.py``'s
REAL ``sender_tick`` against a fake ``SmsSender`` (mirroring
``tests/test_agent_draft_sender.py``'s own harness) — this is the only way
to actually prove "the tick sends nothing" rather than merely asserting a
status column changed.

Marker: ``integration`` — requires a running Postgres instance + ``alembic
upgrade head``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.case_lifecycle import STATUS_RESOLVED, CaseSnapshot, decide_reopen_or_new
from app.agent.draft_sender import _claim_draft, _process_claimed_draft, sender_tick
from app.db.session import get_admin_session
from app.deps import Landlord
from app.errors import AppError
from app.routers.cases import get_case, resolve_case
from tests import factories

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
async def session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine) as sess:
        yield sess


@pytest_asyncio.fixture(autouse=True)
async def dispose_app_engine() -> AsyncGenerator[None, None]:
    """``sender_tick`` (``app/agent/draft_sender.py``) reads/writes through
    the app's OWN module-level admin engine — a separate connection pool
    from this module's own ``session`` fixture. Disposed before/after every
    test so no stale pooled connection from a previous test leaks into this
    one, matching ``tests/test_agent_draft_sender.py``'s own fixture."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


class _FakeSmsSender:
    """Records every call; never touches a network — same shape as
    ``tests/test_agent_draft_sender.py``'s own fake."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str:
        self.calls.append({"to_e164": to_e164, "from_e164": from_e164, "body": body})
        return f"SM{uuid.uuid4().hex}"


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.rollback()
    params = {"lid": landlord_id}
    await session.execute(text("DELETE FROM notifications WHERE landlord_id = :lid"), params)
    # trust_metrics before properties (FK) -- a completed send (safety
    # review MEDIUM-1's own regression test) upserts a trust_metrics row.
    for table in (
        "audit_log",
        "drafts",
        "messages",
        "cases",
        "trust_metrics",
        "tenants",
        "vendors",
        "properties",
    ):
        await session.execute(text(f"DELETE FROM {table} WHERE landlord_id = :lid"), params)  # noqa: S608
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


async def _seed_case(
    session: AsyncSession,
    *,
    status: str = "open",
    severity: str | None = "urgent",
    provision_twilio_number: bool = False,
) -> tuple[str, str, str]:
    landlord_id = await factories.insert_landlord(session)
    twilio_number = factories.fresh_phone() if provision_twilio_number else None
    property_id = await factories.insert_property(session, landlord_id, twilio_number=twilio_number)
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id)
    case_id = await factories.insert_case(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status=status,
        severity=severity,
    )
    await session.commit()
    return landlord_id, property_id, case_id


async def _case_row(session: AsyncSession, case_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    "SELECT status, resolved_reason, resolved_at, pending_resolved_at, "
                    "last_activity_at FROM cases WHERE id = :id"
                ),
                {"id": case_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def _draft_row(session: AsyncSession, draft_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text("SELECT status, sent_message_id FROM drafts WHERE id = :id"),
                {"id": draft_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def _audit_rows(session: AsyncSession, *, case_id: str, action: str) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT actor, payload FROM audit_log WHERE case_id = :cid "
                    "AND action = :action ORDER BY id"
                ),
                {"cid": case_id, "action": action},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_case_happy_path(session: AsyncSession) -> None:
    landlord_id, _property_id, case_id = await _seed_case(session, status="open")
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        response = await resolve_case(uuid.UUID(case_id), (landlord, session))
        assert response.status == "resolved"
        assert response.resolved_at is not None
        await session.commit()

        row = await _case_row(session, case_id)
        assert row["status"] == "resolved"
        assert row["resolved_reason"] == "landlord"
        assert row["resolved_at"] == response.resolved_at
        assert row["pending_resolved_at"] is None

        audit_rows = await _audit_rows(session, case_id=case_id, action="case_resolved")
        assert len(audit_rows) == 1
        assert audit_rows[0]["actor"] == "landlord"
        assert audit_rows[0]["payload"] == {"reason": "landlord"}

        # The timeline surfaces it too (api-contracts.md's Cases section).
        detail = await get_case(uuid.UUID(case_id), (landlord, session))
        assert detail.status == "resolved"
        assert detail.resolved_at == response.resolved_at
        resolved_entries = [
            e
            for e in detail.timeline
            if e.kind == "audit" and e.action == "case_resolved"  # type: ignore[union-attr]
        ]
        assert len(resolved_entries) == 1
        assert resolved_entries[0].actor == "landlord"  # type: ignore[union-attr]
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_resolve_case_not_found_returns_404(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await resolve_case(uuid.uuid4(), (landlord, session))
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "case_not_found"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_resolve_case_cross_tenant_returns_404(session: AsyncSession) -> None:
    landlord_a_id, _p, case_id = await _seed_case(session)
    landlord_b_id = await factories.insert_landlord(session)
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await resolve_case(uuid.UUID(case_id), (landlord_b, session))
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "case_not_found"

        row = await _case_row(session, case_id)
        assert row["status"] == "open"  # untouched by the failed cross-tenant attempt
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)


# ---------------------------------------------------------------------------
# Idempotency precedent: 200, not 409 (see resolve_case's own docstring).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_case_already_resolved_is_idempotent_200(session: AsyncSession) -> None:
    landlord_id, _p, case_id = await _seed_case(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        first = await resolve_case(uuid.UUID(case_id), (landlord, session))
        await session.commit()
        second = await resolve_case(uuid.UUID(case_id), (landlord, session))
        await session.commit()

        assert second.status == "resolved"
        assert second.resolved_at == first.resolved_at

        # No duplicate case_resolved audit row on the repeat.
        audit_rows = await _audit_rows(session, case_id=case_id, action="case_resolved")
        assert len(audit_rows) == 1
    finally:
        await _cleanup(session, landlord_id)


# ---------------------------------------------------------------------------
# The safety edge: draft cancellation.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_case_cancels_pending_draft(session: AsyncSession) -> None:
    landlord_id, _p, case_id = await _seed_case(session, status="awaiting_approval")
    draft_id = await factories.insert_draft(
        session, landlord_id=landlord_id, case_id=case_id, status="pending"
    )
    await session.commit()
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await resolve_case(uuid.UUID(case_id), (landlord, session))
        await session.commit()

        draft_row = await _draft_row(session, draft_id)
        assert draft_row["status"] == "cancelled"
        assert draft_row["sent_message_id"] is None

        cancelled_audit = await _audit_rows(session, case_id=case_id, action="send_cancelled")
        assert len(cancelled_audit) == 1
        assert cancelled_audit[0]["actor"] == "landlord"
        assert cancelled_audit[0]["payload"]["draft_id"] == draft_id
        assert cancelled_audit[0]["payload"]["reason"] == "case_resolved"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_resolve_case_cancels_approved_draft_before_tick_sends_nothing(
    session: AsyncSession,
) -> None:
    """THE edge this issue exists for: a landlord-approved draft sitting in
    its undo window when the landlord resolves the case must never be sent
    by a later ``sender_tick``."""
    landlord_id, _property_id, case_id = await _seed_case(
        session, status="awaiting_approval", provision_twilio_number=True
    )
    scheduled_send_at = datetime.now(UTC) - timedelta(seconds=1)
    draft_id = await factories.insert_draft(
        session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="approved",
        scheduled_send_at=scheduled_send_at,
    )
    await session.commit()
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await resolve_case(uuid.UUID(case_id), (landlord, session))
        await session.commit()

        draft_row = await _draft_row(session, draft_id)
        assert draft_row["status"] == "cancelled"

        sender = _FakeSmsSender()
        claimed = await sender_tick(sender=sender)
        assert claimed == 0
        assert sender.calls == []

        draft_row_after_tick = await _draft_row(session, draft_id)
        assert draft_row_after_tick["status"] == "cancelled"  # never claimed, never sent
        assert await _audit_rows(session, case_id=case_id, action="sent") == []
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_resolve_case_cancels_auto_send_approved_draft_before_tick_sends_nothing(
    session: AsyncSession,
) -> None:
    """Same edge, for a trust-ladder auto-approved (``auto_send=true``)
    draft — resolving the case must cancel it too, not just landlord-
    approved drafts."""
    landlord_id, _property_id, case_id = await _seed_case(
        session, status="open", severity="routine", provision_twilio_number=True
    )
    scheduled_send_at = datetime.now(UTC) - timedelta(seconds=1)
    draft_id = await factories.insert_draft(
        session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="approved",
        scheduled_send_at=scheduled_send_at,
        auto_send=True,
    )
    await session.commit()
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await resolve_case(uuid.UUID(case_id), (landlord, session))
        await session.commit()

        draft_row = await _draft_row(session, draft_id)
        assert draft_row["status"] == "cancelled"

        sender = _FakeSmsSender()
        claimed = await sender_tick(sender=sender)
        assert claimed == 0
        assert sender.calls == []
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_resolve_case_leaves_sending_draft_alone(session: AsyncSession) -> None:
    """A draft the sender ticker already claimed (``'sending'``) at the
    moment of resolve is genuinely mid-flight — see ``resolve_case``'s own
    docstring "The 'sending' race". It must be left untouched, never
    force-cancelled out from under an in-flight send."""
    landlord_id, _p, case_id = await _seed_case(session, status="awaiting_approval")
    draft_id = await factories.insert_draft(
        session, landlord_id=landlord_id, case_id=case_id, status="sending"
    )
    await session.commit()
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        response = await resolve_case(uuid.UUID(case_id), (landlord, session))
        assert response.status == "resolved"
        await session.commit()

        draft_row = await _draft_row(session, draft_id)
        assert draft_row["status"] == "sending"  # untouched

        assert await _audit_rows(session, case_id=case_id, action="send_cancelled") == []
    finally:
        await _cleanup(session, landlord_id)


# ---------------------------------------------------------------------------
# Emergency chain independence.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_case_does_not_touch_emergency_notifications(session: AsyncSession) -> None:
    landlord_id, _p, case_id = await _seed_case(session, status="open", severity="emergency")
    notif_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO notifications "
            "(id, landlord_id, case_id, type, channel, status, attempt, next_attempt_at, payload) "
            "VALUES (:id, :lid, :cid, 'emergency_call', 'voice', 'pending', 1, now(), '{}')"
        ),
        {"id": notif_id, "lid": landlord_id, "cid": case_id},
    )
    await session.commit()
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await resolve_case(uuid.UUID(case_id), (landlord, session))
        await session.commit()

        notif_row = (
            (
                await session.execute(
                    text(
                        "SELECT status, acknowledged_at, attempt FROM notifications WHERE id = :id"
                    ),
                    {"id": notif_id},
                )
            )
            .mappings()
            .one()
        )
        assert notif_row["status"] == "pending"
        assert notif_row["acknowledged_at"] is None
        assert notif_row["attempt"] == 1
    finally:
        await _cleanup(session, landlord_id)


# ---------------------------------------------------------------------------
# Reopen path unaffected — the exact shape decide_reopen_or_new needs.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_then_reopen_decision_still_fires_within_window(
    session: AsyncSession,
) -> None:
    """Verifies ``resolve_case`` writes the EXACT row shape
    ``decide_reopen_or_new`` (the existing reopen rule,
    ``app/agent/case_lifecycle.py``) needs — ``status='resolved'``,
    ``resolved_reason``, ``resolved_at`` all set together — proving the
    reopen path (untouched by this issue) keeps working on a case THIS
    endpoint resolved."""
    landlord_id, _p, case_id = await _seed_case(session, status="open")
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        response = await resolve_case(uuid.UUID(case_id), (landlord, session))
        await session.commit()

        row = await _case_row(session, case_id)
        snapshot = CaseSnapshot(
            case_id=uuid.UUID(case_id),
            status=row["status"],
            resolved_reason=row["resolved_reason"],
            resolved_at=row["resolved_at"],
            last_activity_at=row["last_activity_at"],
        )
        assert snapshot.status == STATUS_RESOLVED

        within_window = decide_reopen_or_new(snapshot, response.resolved_at + timedelta(days=1))
        assert within_window.reopen is True

        past_window = decide_reopen_or_new(snapshot, response.resolved_at + timedelta(days=31))
        assert past_window.reopen is False
    finally:
        await _cleanup(session, landlord_id)


# ---------------------------------------------------------------------------
# Safety review MEDIUM-1 (reproduced empirically): a draft already claimed
# ('sending') at the moment of resolve completes normally afterward -- its
# own bookkeeping must land, but the case-status flip that completion does
# must not drag an explicitly-resolved case back out of 'resolved'.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_case_then_in_flight_send_completes_without_reverting_status(
    session: AsyncSession,
) -> None:
    """Sequence: landlord approves -> sender claims ('sending') -> landlord
    resolves (correctly leaves the 'sending' row alone) -> the send
    completes. The completed send's own durable bookkeeping (outbound
    message, 'sent' audit, sent_message_id, trust_metrics) must still
    land, but the case must stay 'resolved' -- not get dragged back to
    'awaiting_tenant' by app/agent/draft_sender.py's own case-status
    flip."""
    landlord_id, _property_id, case_id = await _seed_case(
        session, status="awaiting_approval", severity="urgent", provision_twilio_number=True
    )
    scheduled_send_at = datetime.now(UTC) - timedelta(seconds=1)
    draft_id = await factories.insert_draft(
        session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="approved",
        scheduled_send_at=scheduled_send_at,
    )
    await session.commit()

    # Drive a REAL claim ('approved' -> 'sending') through the actual claim
    # SQL, exactly as sender_tick would -- BEFORE the case resolves.
    async with asynccontextmanager(get_admin_session)() as claim_session:
        claimed = await _claim_draft(claim_session, uuid.UUID(draft_id))
    assert claimed is not None

    draft_row = await _draft_row(session, draft_id)
    assert draft_row["status"] == "sending"

    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        response = await resolve_case(uuid.UUID(case_id), (landlord, session))
        assert response.status == "resolved"
        await session.commit()

        # The 'sending' row was correctly left alone by the resolve.
        draft_row_after_resolve = await _draft_row(session, draft_id)
        assert draft_row_after_resolve["status"] == "sending"

        # The tick's own per-claim completion step now runs (the send
        # actually goes out -- it was already irreversibly claimed).
        sender = _FakeSmsSender()
        await _process_claimed_draft(sender, claimed)
        assert len(sender.calls) == 1

        draft_row_after_send = await _draft_row(session, draft_id)
        assert draft_row_after_send["status"] == "sent"
        assert draft_row_after_send["sent_message_id"] is not None

        sent_audit = await _audit_rows(session, case_id=case_id, action="sent")
        assert len(sent_audit) == 1

        # THE fix: the case must stay resolved, never dragged back to
        # 'awaiting_tenant' by the completed send's own bookkeeping.
        case_row = await _case_row(session, case_id)
        assert case_row["status"] == "resolved"
        assert case_row["resolved_reason"] == "landlord"
        assert case_row["resolved_at"] == response.resolved_at
    finally:
        await _cleanup(session, landlord_id)
