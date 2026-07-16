"""Integration tests for #44/#45's sender ticker (``app/agent/
draft_sender.py``) — the FIRST outbound-send call site in this codebase.
Every test uses a FAKE :class:`app.integrations.sms_sender.SmsSender` — no
real Twilio client exists in this codebase at all (#108's parallel
branch), and this module is a completely separate file from
``app/integrations/twilio.py`` on purpose (scope boundary).

Marker: ``integration`` — requires a running Postgres instance + ``alembic
upgrade head``.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import sentry_sdk
from anthropic.types import ToolUseBlock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent.draft_sender import DEFAULT_BATCH_SIZE, run_sender_loop, sender_tick
from app.agent.nodes.classify_severity import classify_severity
from app.agent.schemas import CaseContext, PrefilterResult
from app.agent.state import AgentState
from app.integrations import anthropic as anthropic_mod
from app.integrations.sms_sender import SmsSender
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
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine) as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def dispose_app_engine() -> AsyncGenerator[None, None]:
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(
        text("DELETE FROM audit_log WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM trust_metrics WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM messages WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM tenants WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM properties WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


class _FakeSmsSender:
    """Records every call; never touches a network. ``fail_next`` lets a
    test simulate a provider error on the NEXT ``send_sms`` call only."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next = False

    async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated provider failure")
        self.calls.append({"to_e164": to_e164, "from_e164": from_e164, "body": body})
        return f"SM{uuid.uuid4().hex}"


async def _seed_approved_draft(
    session: AsyncSession,
    *,
    due_in_seconds: float = -1.0,
    edited: bool = False,
    final_body: str | None = None,
    severity: str | None = "urgent",
    provision_twilio_number: bool = True,
) -> tuple[str, str, str]:
    """Returns ``(landlord_id, case_id, draft_id)`` — a draft already
    ``'approved'`` with ``scheduled_send_at`` *due_in_seconds* from now
    (negative = already due). ``provision_twilio_number=True`` (default)
    gives the property a fresh, collision-free ``twilio_number`` (the
    column is ``UNIQUE`` — never a fixed literal across calls) so every
    existing test exercises the realistic case: a property that already
    has a provisioned outbound number. Pass ``False`` for the "not yet
    provisioned" guard test. ``severity=None`` seeds a case the way every
    real case looked before #197 (or any legacy case that predates it) —
    used by the missing-severity anomaly test below."""
    landlord_id = await factories.insert_landlord(session)
    twilio_number = factories.fresh_phone() if provision_twilio_number else None
    property_id = await factories.insert_property(session, landlord_id, twilio_number=twilio_number)
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id)
    case_id = await factories.insert_case(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
        severity=severity,
    )
    scheduled_send_at = datetime.now(UTC) + timedelta(seconds=due_in_seconds)
    draft_id = await factories.insert_draft(
        session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="approved",
        scheduled_send_at=scheduled_send_at,
        edited=edited,
        final_body=final_body,
    )
    return landlord_id, case_id, draft_id


# ---------------------------------------------------------------------------
# 1. Happy path — a due, clean approval.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sender_tick_sends_due_draft_and_writes_every_durable_side_effect(
    db_session: AsyncSession,
) -> None:
    landlord_id, case_id, draft_id = await _seed_approved_draft(db_session)
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1
        assert len(sender.calls) == 1

        draft_row = (
            (
                await db_session.execute(
                    text("SELECT status, sent_message_id FROM drafts WHERE id = :id"),
                    {"id": draft_id},
                )
            )
            .mappings()
            .one()
        )
        assert draft_row["status"] == "sent"
        assert draft_row["sent_message_id"] is not None

        message_row = (
            (
                await db_session.execute(
                    text("SELECT direction, party, twilio_sid, body FROM messages WHERE id = :id"),
                    {"id": str(draft_row["sent_message_id"])},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["direction"] == "outbound"
        assert message_row["party"] == "tenant"
        assert message_row["twilio_sid"] is not None
        assert message_row["twilio_sid"].startswith("SM")
        assert message_row["body"] == sender.calls[0]["body"]
        # The send goes out from the CASE's OWN property's twilio_number,
        # never a fabricated/omitted "from" (app/integrations/sms_sender.py's
        # module docstring "Why from_e164 is required").
        assert sender.calls[0]["from_e164"] is not None

        case_row = (
            (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :id"), {"id": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert case_row["status"] == "awaiting_tenant"

        trust_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT clean_approvals, edited_approvals FROM trust_metrics "
                        "WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert trust_row["clean_approvals"] == 1
        assert trust_row["edited_approvals"] == 0

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor, action, payload FROM audit_log WHERE case_id = :cid "
                        "AND action = 'sent'"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        # #111 cost metering (schema-v1.md v1.12): the 'sent' audit payload
        # carries the segment count + estimated Twilio cost for THIS send,
        # computed from the same body the fake sender recorded above.
        assert audit_row["payload"]["segments"] == 1
        assert audit_row["payload"]["sms_cost_cents"] == pytest.approx(0.75)
        assert audit_row["actor"] == "system"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_sender_tick_records_edited_approval_in_trust_metrics(
    db_session: AsyncSession,
) -> None:
    landlord_id, case_id, draft_id = await _seed_approved_draft(
        db_session, edited=True, final_body="Edited landlord text."
    )
    sender = _FakeSmsSender()
    try:
        await sender_tick(sender=sender)

        assert sender.calls[0]["body"] == "Edited landlord text."

        trust_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT clean_approvals, edited_approvals FROM trust_metrics "
                        "WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert trust_row["clean_approvals"] == 0
        assert trust_row["edited_approvals"] == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2. Not-yet-due drafts are left alone.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sender_tick_ignores_drafts_not_yet_due(db_session: AsyncSession) -> None:
    landlord_id, _case_id, draft_id = await _seed_approved_draft(db_session, due_in_seconds=60.0)
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 0
        assert sender.calls == []

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "approved"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 3. Idempotent claim — concurrent ticks never double-send.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_ticks_send_exactly_once(db_session: AsyncSession) -> None:
    landlord_id, _case_id, draft_id = await _seed_approved_draft(db_session)
    sender = _FakeSmsSender()
    try:
        results = await asyncio.gather(sender_tick(sender=sender), sender_tick(sender=sender))
        assert sum(results) == 1  # exactly one tick won the claim
        assert len(sender.calls) == 1

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 4. Crash/failure semantics — a stuck 'sending' row, never a silent
#    double-send or a fabricated message.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_send_failure_leaves_draft_stuck_sending_not_lost_or_duplicated(
    db_session: AsyncSession,
) -> None:
    landlord_id, case_id, draft_id = await _seed_approved_draft(db_session)
    sender = _FakeSmsSender()
    sender.fail_next = True
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1  # claimed the row -- the SEND itself then failed
        assert sender.calls == []

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sending"  # stuck, visible -- never silently re-attempted here

        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert message_count == 0  # never fabricated an outbound message row

        # A second tick does NOT re-claim the stuck row (status is no
        # longer 'approved') -- never a silent double-send once it's stuck.
        claimed_again = await sender_tick(sender=sender)
        assert claimed_again == 0
        assert sender.calls == []
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 5. Deployment gating — the worker refuses to run with no sender bound.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_run_sender_loop_disabled_with_no_sender_configured() -> None:
    """Deployment gating (matches #109's own pattern): a ``None`` sender
    means the worker is disabled — this must return promptly (never loop
    forever, never raise), not merely "eventually work" behind a timeout.
    The loud-log assertion lives in ``test_integrations_sms_sender.py``-style
    coverage is out of scope here; the behavioral contract (never loops,
    never touches the claim SQL) is what this test pins."""
    await asyncio.wait_for(run_sender_loop(sender=None), timeout=1.0)


@pytest.mark.integration
async def test_run_sender_loop_stops_on_stop_event(db_session: AsyncSession) -> None:
    landlord_id, _case_id, draft_id = await _seed_approved_draft(db_session)
    sender = _FakeSmsSender()
    stop_event = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    try:
        await asyncio.gather(
            run_sender_loop(sender=sender, interval_seconds=0.01, stop_event=stop_event),
            _stop_soon(),
        )
        # At least one tick ran before the stop fired.
        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_edited_draft_with_empty_final_body_never_sends_original_text(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #4 (safety review, INFO/defensive): an edited draft whose
    ``final_body`` is somehow empty must NEVER silently fall back to
    ``drafts.body`` (the ORIGINAL text the landlord explicitly replaced) —
    refused loudly (logged + Sentry-paged), left stuck ``'sending'``, same
    as any other send failure."""
    import app.agent.draft_sender as draft_sender_mod

    calls: list[dict[str, object]] = []

    def _fake_capture_message(
        message: str, *, level: str | None = None, extras: dict[str, object] | None = None
    ) -> None:
        calls.append({"message": message, "level": level, "extras": extras})

    monkeypatch.setattr(draft_sender_mod.sentry_sdk, "capture_message", _fake_capture_message)

    landlord_id, case_id, draft_id = await _seed_approved_draft(
        db_session, edited=True, final_body=None
    )
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1  # claimed the row -- refused before ever calling send_sms
        assert sender.calls == []  # NEVER sent the original text

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sending"  # stuck, visible -- never silently re-attempted

        assert len(calls) == 1
        assert calls[0]["level"] == "error"

        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert message_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 6. No twilio_number provisioned yet -- refuse to send, never misroute.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_property_twilio_number_refuses_to_send(db_session: AsyncSession) -> None:
    """A property with no ``twilio_number`` provisioned yet must never send
    from a DIFFERENT property's number or a fabricated placeholder —
    mirrors ``app/agent/emergency_chain.py``'s own ``"no_twilio_number"``
    skip reason. Refused loudly, row left stuck ``'sending'`` (same
    stuck-row semantics as any other send failure), no message row
    fabricated."""
    landlord_id, case_id, draft_id = await _seed_approved_draft(
        db_session, provision_twilio_number=False
    )
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1  # claimed the row -- refused before ever calling send_sms
        assert sender.calls == []

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sending"  # stuck, visible -- never silently re-attempted

        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert message_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 7. Wall-clock deadline (safety review, MEDIUM) -- sender_tick bounds its
#    own worst-case duration; leftovers wait for the next tick, never lost.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A mutable, injectable time source for ``sender_tick``'s deadline
    check — advanced explicitly by the fake sender below rather than
    sleeping for real seconds."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _DeadlineBlowingSender:
    """Records every call (by recipient phone); advances a shared
    :class:`_FakeClock` past the tick's deadline on its FIRST send,
    simulating a slow Twilio round-trip that must not be allowed to also
    delay claiming every OTHER due draft in the same tick."""

    def __init__(self, clock: _FakeClock, *, advance_by: float) -> None:
        self._clock = clock
        self._advance_by = advance_by
        self.calls: list[str] = []

    async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str:
        self.calls.append(to_e164)
        self._clock.now += self._advance_by
        return f"SM{uuid.uuid4().hex}"


@pytest.mark.integration
async def test_sender_tick_stops_claiming_after_deadline_then_resumes_next_tick(
    db_session: AsyncSession,
) -> None:
    """Two due drafts; the first send blows the (tiny, test-only) deadline.
    The SECOND due draft must NOT be claimed in the same tick -- it stays
    'approved' and due, claimed whole by the very next tick call. Nothing
    lost; never abandoned mid-claim."""
    landlord_id_a, _case_id_a, draft_id_a = await _seed_approved_draft(
        db_session, due_in_seconds=-2.0
    )
    landlord_id_b, _case_id_b, draft_id_b = await _seed_approved_draft(
        db_session, due_in_seconds=-1.0
    )
    clock = _FakeClock(start=0.0)
    sender = _DeadlineBlowingSender(clock, advance_by=10.0)

    try:
        claimed_first_tick = await sender_tick(
            sender=sender, deadline_seconds=5.0, time_source=clock
        )
        assert claimed_first_tick == 1
        assert len(sender.calls) == 1

        status_a = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id_a}
            )
        ).scalar_one()
        assert status_a == "sent"

        status_b = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id_b}
            )
        ).scalar_one()
        assert status_b == "approved"  # NOT claimed this tick -- deadline already exceeded

        # The next tick call (clock already past the first deadline window,
        # but sender_tick recomputes its OWN start from time_source() every
        # call) claims and sends the leftover draft.
        claimed_second_tick = await sender_tick(
            sender=sender, deadline_seconds=5.0, time_source=clock
        )
        assert claimed_second_tick == 1
        assert len(sender.calls) == 2

        status_b_after = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id_b}
            )
        ).scalar_one()
        assert status_b_after == "sent"
    finally:
        await _cleanup(db_session, landlord_id_a)
        await _cleanup(db_session, landlord_id_b)


# ---------------------------------------------------------------------------
# #197 -- cases.severity is now written by classify_severity, so the
# missing-severity branch below is a genuine anomaly (ERROR + Sentry), and
# trust_metrics genuinely accumulates end-to-end via the real write path.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sender_tick_missing_case_severity_pages_sentry_and_skips_trust_metrics(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case with ``severity IS NULL`` (a legacy pre-#197 case; no
    backfill migration) skips the ``trust_metrics`` upsert -- and now that
    this is no longer expected on 100% of sends, it must page (ERROR log +
    Sentry), not just log quietly."""
    landlord_id, case_id, _draft_id = await _seed_approved_draft(db_session, severity=None)
    sender = _FakeSmsSender()

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda message, **kwargs: captured.append({"message": message, **kwargs}),
    )

    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1
        assert len(sender.calls) == 1  # the send itself still happens

        trust_row = (
            (
                await db_session.execute(
                    text("SELECT 1 FROM trust_metrics WHERE landlord_id = :lid"),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        assert trust_row is None  # no severity -> no (property, severity) key to upsert

        assert len(captured) == 1
        assert captured[0]["level"] == "error"
        assert case_id in str(captured[0]["extras"])
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_severity_written_by_classify_severity_flows_into_trust_metrics_via_sender(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end #197 regression pin: ``classify_severity`` (the REAL
    production write path, not a test-factory shortcut) writes
    ``cases.severity``; the sender then reads that same column and
    ``trust_metrics`` accumulates -- closing the exact gap the #50 e2e
    rehearsal found (``draft_sender_missing_severity_for_trust_metrics``
    firing on every approved send)."""
    landlord_id = await factories.insert_landlord(db_session)
    twilio_number = factories.fresh_phone()
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number=twilio_number
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
        severity=None,  # never classified yet -- exactly like a real new case
    )
    message_id = await factories.insert_message(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the kitchen tap won't stop dripping",
        prefilter=PrefilterResult(hard_hit=False).model_dump_json(),
    )

    def _fake_message(*, tool_input: dict[str, Any]) -> SimpleNamespace:
        block = ToolUseBlock(
            id="toolu_test", input=tool_input, name="classify_severity", type="tool_use"
        )
        usage = SimpleNamespace(input_tokens=100, output_tokens=40)
        return SimpleNamespace(content=[block], usage=usage, model="claude-sonnet-5")

    class _FakeMessages:
        async def create(self, **kwargs: Any) -> Any:
            return _fake_message(
                tool_input={
                    "severity": "ROUTINE",
                    "rules_fired": [],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["A dripping tap can wait for a scheduled visit."],
                }
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = _FakeMessages()

    monkeypatch.setattr(anthropic_mod, "get_client", lambda: _FakeClient())

    try:
        # 1. The real classify_severity node writes cases.severity.
        state: AgentState = {
            "message_id": uuid.UUID(message_id),
            "case_context": CaseContext(
                landlord_id=uuid.UUID(landlord_id),
                property_id=uuid.UUID(property_id),
                tenant_id=uuid.UUID(tenant_id),
                case_id=uuid.UUID(case_id),
            ),
            "reasoning_log": [],
        }
        update = await classify_severity(state)
        assert update["classification_failed"] is False

        case_severity_row = (
            (
                await db_session.execute(
                    text("SELECT severity FROM cases WHERE id = :id"), {"id": case_id}
                )
            )
            .mappings()
            .one()
        )
        assert case_severity_row["severity"] == "routine"

        # 2. An approved draft on that SAME case, drained by the sender.
        draft_id = await factories.insert_draft(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            status="approved",
            scheduled_send_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        sender = _FakeSmsSender()
        claimed = await sender_tick(sender=sender)
        assert claimed == 1
        assert len(sender.calls) == 1

        trust_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT severity, clean_approvals, edited_approvals FROM trust_metrics "
                        "WHERE landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert trust_row["severity"] == "routine"
        assert trust_row["clean_approvals"] == 1
        assert trust_row["edited_approvals"] == 0

        draft_status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert draft_status == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# #60 -- trust ladder graduation: consecutive_clean streak, boundary
# graduation, edit resets, and the SQL-level belt-and-braces pinning that
# a non-'routine' row can never graduate no matter its own streak.
# ---------------------------------------------------------------------------


async def _seed_approved_draft_for_property(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    tenant_id: str,
    severity: str = "routine",
    edited: bool = False,
    final_body: str | None = None,
    due_in_seconds: float = -1.0,
) -> tuple[str, str]:
    """Same shape as ``_seed_approved_draft`` but reuses an EXISTING
    (landlord, property, tenant) — #60's graduation tests need several
    consecutive sends to accumulate against the SAME (property, severity)
    ``trust_metrics`` row, which ``_seed_approved_draft`` (a fresh
    landlord/property every call) cannot exercise. Returns
    ``(case_id, draft_id)``."""
    case_id = await factories.insert_case(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status="awaiting_approval",
        severity=severity,
    )
    scheduled_send_at = datetime.now(UTC) + timedelta(seconds=due_in_seconds)
    draft_id = await factories.insert_draft(
        session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="approved",
        scheduled_send_at=scheduled_send_at,
        edited=edited,
        final_body=final_body,
    )
    return case_id, draft_id


async def _trust_metrics_row(
    session: AsyncSession, *, landlord_id: str, severity: str = "routine"
) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    "SELECT consecutive_clean, autonomy_unlocked, unlocked_at, revoked_at, "
                    "clean_approvals, edited_approvals FROM trust_metrics "
                    "WHERE landlord_id = :lid AND severity = :severity"
                ),
                {"lid": landlord_id, "severity": severity},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


@pytest.mark.integration
async def test_graduation_fires_at_exactly_threshold_boundary_with_trust_unlocked_audit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean-approval streak -> unlock at EXACTLY N (boundary), never one
    send early or late, with a `trust_unlocked` audit row."""
    from app.config import settings

    monkeypatch.setattr(settings, "trust_graduation_threshold", 3)

    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number=factories.fresh_phone()
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    sender = _FakeSmsSender()
    try:
        for _ in range(2):
            await _seed_approved_draft_for_property(
                db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
            )
            claimed = await sender_tick(sender=sender)
            assert claimed == 1
            row = await _trust_metrics_row(db_session, landlord_id=landlord_id)
            assert row["autonomy_unlocked"] is False  # not yet -- below threshold

        # The THIRD consecutive clean send crosses the threshold.
        await _seed_approved_draft_for_property(
            db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
        )
        claimed = await sender_tick(sender=sender)
        assert claimed == 1

        row = await _trust_metrics_row(db_session, landlord_id=landlord_id)
        assert row["consecutive_clean"] == 3
        assert row["autonomy_unlocked"] is True
        assert row["unlocked_at"] is not None

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor, payload FROM audit_log "
                        "WHERE landlord_id = :lid AND action = 'trust_unlocked'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["actor"] == "system"
        assert audit_row["payload"]["severity"] == "routine"
        assert audit_row["payload"]["threshold"] == 3

        # A FOURTH clean send does not re-fire graduation (no second
        # trust_unlocked row) -- the WHERE autonomy_unlocked = false guard.
        await _seed_approved_draft_for_property(
            db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
        )
        await sender_tick(sender=sender)
        audit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE landlord_id = :lid AND action = 'trust_unlocked'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert audit_count == 1
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_edit_resets_consecutive_clean_streak(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "trust_graduation_threshold", 5)

    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number=factories.fresh_phone()
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    sender = _FakeSmsSender()
    try:
        for _ in range(3):
            await _seed_approved_draft_for_property(
                db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
            )
            await sender_tick(sender=sender)

        row = await _trust_metrics_row(db_session, landlord_id=landlord_id)
        assert row["consecutive_clean"] == 3

        await _seed_approved_draft_for_property(
            db_session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            edited=True,
            final_body="Landlord-edited text.",
        )
        await sender_tick(sender=sender)

        row = await _trust_metrics_row(db_session, landlord_id=landlord_id)
        assert row["consecutive_clean"] == 0  # reset by the edit
        assert row["clean_approvals"] == 3
        assert row["edited_approvals"] == 1
        assert row["autonomy_unlocked"] is False
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_urgent_severity_never_graduates_even_at_10x_threshold(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'urgent'/'emergency' rows accumulate the SAME counters (PR #202
    senior review: "treat the emergency/urgent rows as inert counters")
    but can NEVER unlock, no matter how long the streak — the Python-level
    `severity == GRADUATION_SEVERITY` guard skips the graduation query
    entirely for these; see the DIRECT SQL-predicate test below for the
    belt-and-braces enforcement even if that guard were bypassed."""
    from app.config import settings

    # ge=3 (safety review LOW-3) is the floor a real deployment can ever
    # boot with -- use the floor itself so this stays a realistic
    # configuration, not a value production could never actually have.
    monkeypatch.setattr(settings, "trust_graduation_threshold", 3)

    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number=factories.fresh_phone()
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    sender = _FakeSmsSender()
    try:
        for _ in range(30):  # 10x the threshold
            await _seed_approved_draft_for_property(
                db_session,
                landlord_id=landlord_id,
                property_id=property_id,
                tenant_id=tenant_id,
                severity="urgent",
            )
            await sender_tick(sender=sender)

        row = await _trust_metrics_row(db_session, landlord_id=landlord_id, severity="urgent")
        assert row["consecutive_clean"] == 30
        assert row["autonomy_unlocked"] is False
        assert row["unlocked_at"] is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_graduation_sql_predicate_never_matches_non_routine_severity(
    db_session: AsyncSession,
) -> None:
    """SQL-level belt-and-braces (#60): even calling the graduation UPDATE
    DIRECTLY against a non-'routine' row with a streak far past any
    threshold, the query's own hardcoded `severity = 'routine'` literal
    refuses to match — never enforced by Python control flow alone."""
    import app.agent.draft_sender as draft_sender_mod

    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    await factories.insert_trust_metrics(
        db_session,
        landlord_id=landlord_id,
        property_id=property_id,
        severity="emergency",
        consecutive_clean=999,
        autonomy_unlocked=False,
    )
    try:
        result = await db_session.execute(
            draft_sender_mod._GRADUATE_ROUTINE_TRUST_SQL,
            {"property_id": property_id, "threshold": 1},
        )
        assert result.mappings().one_or_none() is None  # never matches

        row = (
            (
                await db_session.execute(
                    text(
                        "SELECT autonomy_unlocked FROM trust_metrics "
                        "WHERE property_id = :pid AND severity = 'emergency'"
                    ),
                    {"pid": property_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["autonomy_unlocked"] is False
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_revoke_then_one_clean_send_does_not_re_unlock(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec review MINOR-2 / re-graduation semantics (app/trust.py's own
    docstring): a revoke resets `consecutive_clean` to 0, so re-earning
    autonomy after a revoke requires a FULL fresh streak, never just one
    more clean send. Graduated -> revoked -> exactly ONE clean send must
    still leave the property locked."""
    from app.config import settings
    from app.trust import revoke_property_autonomy

    monkeypatch.setattr(settings, "trust_graduation_threshold", 3)

    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number=factories.fresh_phone()
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    sender = _FakeSmsSender()
    try:
        for _ in range(3):
            await _seed_approved_draft_for_property(
                db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
            )
            await sender_tick(sender=sender)

        graduated_row = await _trust_metrics_row(db_session, landlord_id=landlord_id)
        assert graduated_row["autonomy_unlocked"] is True

        revoked_count = await revoke_property_autonomy(
            db_session,
            landlord_id=uuid.UUID(landlord_id),
            property_id=uuid.UUID(property_id),
            actor="landlord",
            reason="test",
        )
        assert revoked_count == 1

        revoked_row = await _trust_metrics_row(db_session, landlord_id=landlord_id)
        assert revoked_row["autonomy_unlocked"] is False
        assert revoked_row["consecutive_clean"] == 0

        # Exactly ONE clean send after the revoke -- not a full fresh streak.
        await _seed_approved_draft_for_property(
            db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
        )
        await sender_tick(sender=sender)

        after_one_send = await _trust_metrics_row(db_session, landlord_id=landlord_id)
        assert after_one_send["consecutive_clean"] == 1
        assert after_one_send["autonomy_unlocked"] is False  # still locked -- needs 2 more
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 7. Resolved-case guard belt-and-braces (#206) — see this module's own
#    docstring "Resolved-case guard belt-and-braces (#206)". The PRIMARY
#    fix (app/routers/cases.py's resolve endpoint cancelling the draft
#    itself) is tested end-to-end in tests/test_cases_resolve_router.py;
#    these two tests exercise the claim SQL's own independent guard in
#    isolation, including the case that guard exists FOR (a case resolved
#    by some OTHER path that never cancelled the draft).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_claim_refuses_draft_on_resolved_case_leaves_it_approved(
    db_session: AsyncSession,
) -> None:
    """Simulates a case resolved by a path OTHER than app/routers/cases.py's
    own resolve endpoint (which already cancels the draft itself) — e.g.
    the pre-existing app/agent/case_lifecycle.py sweep_cases() gap this
    guard's own docstring flags. Even with nothing having cancelled the
    draft, the claim's own belt-and-braces predicate must still refuse to
    send it."""
    landlord_id, case_id, draft_id = await _seed_approved_draft(db_session)
    await db_session.execute(
        text(
            "UPDATE cases SET status = 'resolved', resolved_reason = 'landlord', "
            "resolved_at = now() WHERE id = :id"
        ),
        {"id": case_id},
    )
    await db_session.commit()
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 0
        assert sender.calls == []

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        # Never sent -- and never actively cancelled by this guard either
        # (see this module's own docstring for why that's an accepted,
        # strictly-safer-than-sending trade-off).
        assert status == "approved"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_claim_still_sends_normally_on_a_non_resolved_case(
    db_session: AsyncSession,
) -> None:
    """Regression guard for the new predicate: an ordinary due draft on a
    case that is NOT resolved must still send exactly as before."""
    landlord_id, case_id, draft_id = await _seed_approved_draft(db_session)
    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender)
        assert claimed == 1
        assert len(sender.calls) == 1

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_resolved_case_zombie_does_not_starve_the_batch(
    db_session: AsyncSession,
) -> None:
    """MEDIUM-2 (starvation half, #206 follow-up): a resolved-case "zombie"
    draft is never claimable (see the claim SQL's own guard), but with the
    OLDEST ``scheduled_send_at`` it would otherwise sort to the front of
    every tick's candidate window (:data:`DEFAULT_BATCH_SIZE`-limited) and
    permanently occupy one slot, starving a legitimate due draft out of
    that batch forever. The candidate SELECT itself must exclude it, not
    just the claim."""
    zombie_landlord_id, zombie_case_id, zombie_draft_id = await _seed_approved_draft(
        db_session,
        due_in_seconds=-100_000.0,  # oldest by far -- sorts first
    )
    await db_session.execute(
        text(
            "UPDATE cases SET status = 'resolved', resolved_reason = 'landlord', "
            "resolved_at = now() WHERE id = :id"
        ),
        {"id": zombie_case_id},
    )
    await db_session.commit()

    legitimate_landlord_ids: list[str] = []
    for _ in range(DEFAULT_BATCH_SIZE):
        landlord_id, _case_id, _draft_id = await _seed_approved_draft(db_session)
        legitimate_landlord_ids.append(landlord_id)

    sender = _FakeSmsSender()
    try:
        claimed = await sender_tick(sender=sender, batch_size=DEFAULT_BATCH_SIZE)
        assert claimed == DEFAULT_BATCH_SIZE
        assert len(sender.calls) == DEFAULT_BATCH_SIZE

        zombie_status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": zombie_draft_id}
            )
        ).scalar_one()
        assert zombie_status == "approved"  # never claimed, never sent, never a batch slot
    finally:
        await _cleanup(db_session, zombie_landlord_id)
        for landlord_id in legitimate_landlord_ids:
            await _cleanup(db_session, landlord_id)


def test_sms_sender_protocol_is_a_runtime_checkable_shape() -> None:
    """Cheap unit-level pin: SmsSender is the ONE seam the ticker depends
    on — a fake implementing just `send_sms` satisfies it structurally."""

    class _Impl:
        async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str:
            return "sid"

    sender: SmsSender = _Impl()
    assert hasattr(sender, "send_sms")
