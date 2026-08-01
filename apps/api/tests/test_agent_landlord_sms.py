"""Tests for ``app/agent/landlord_sms.py`` (#122, approve-by-SMS) — the
landlord-facing SMS outbox: rendering (pure), enqueue idempotency,
draft-ready correlation, and the drain sweep (the THIRD sanctioned
Twilio-send call site, ``tests/test_twilio_send_allowlist.py``).

Marker: ``integration`` for anything touching Postgres; the rendering
tests are plain ``unit`` (no DB, no network).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.agent import landlord_sms
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
    await session.execute(text("DELETE FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM notifications WHERE landlord_id = :lid"), {"lid": landlord_id}
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


class _FakeTwilioSender:
    """Implements just the ``send_sms`` half of ``TwilioSender`` (a
    ``Protocol`` — structural typing, no runtime enforcement) — this
    module never places voice calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        if self.fail:
            raise RuntimeError("simulated provider failure")
        self.calls.append({"to": to, "from_": from_, "body": body})
        return f"SM{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Rendering — pure, no DB.
# ---------------------------------------------------------------------------


def test_render_tenant_label_with_name_and_unit() -> None:
    label = landlord_sms.render_tenant_label(
        tenant_name="Maria", unit="2", property_label="41 Palmerston"
    )
    assert label == "Maria (Unit 2, 41 Palmerston)"


def test_render_tenant_label_falls_back_when_unnamed() -> None:
    label = landlord_sms.render_tenant_label(
        tenant_name=None, unit=None, property_label="41 Palmerston"
    )
    assert label == "Your tenant (41 Palmerston)"


def test_render_draft_ready_sms_truncates_to_200_chars() -> None:
    long_body = "a" * 400
    body = landlord_sms.render_draft_ready_sms(
        tenant_label="Maria (Unit 2, Palmerston)", draft_body=long_body
    )
    assert "Reply 1 to send · 2 to skip · or open the app to edit." in body
    # First ~200 chars of the draft, plus an ellipsis marker -- never the
    # full 400-char body.
    assert "a" * 200 in body
    assert "a" * 400 not in body
    assert "…" in body


def test_render_draft_ready_sms_short_draft_not_truncated() -> None:
    body = landlord_sms.render_draft_ready_sms(
        tenant_label="Maria (Unit 2, Palmerston)", draft_body="Hi Maria, sorry about that."
    )
    assert "Hi Maria, sorry about that." in body
    assert "…" not in body


def test_render_draft_ready_sms_no_issue_snippet_falls_back_to_original_form() -> None:
    """``issue_snippet=None`` (the default) -- no tenant message body was
    available to quote -- must render EXACTLY the original issue-less
    notice, never a blank or broken issue line."""
    body = landlord_sms.render_draft_ready_sms(
        tenant_label="Maria (Unit 2, Palmerston)", draft_body="Hi Maria, sorry about that."
    )
    assert body == (
        'Stoop: Maria (Unit 2, Palmerston) — draft ready: "Hi Maria, sorry about that." '
        "Reply 1 to send · 2 to skip · or open the app to edit."
    )


def test_render_draft_ready_sms_quotes_the_tenants_issue_snippet() -> None:
    """The founder-approved format (issue #122 copy fix): a verbatim
    snippet of the tenant's own message, quoted ahead of the draft
    excerpt."""
    body = landlord_sms.render_draft_ready_sms(
        tenant_label="Maria (Unit 2, Palmerston)",
        draft_body="Hi Maria — so sorry to hear that, I'll get someone out…",
        issue_snippet="the heat isnt working since last night",
    )
    assert body == (
        'Stoop: Maria (Unit 2, Palmerston): "the heat isnt working since last night". '
        'Draft ready: "Hi Maria — so sorry to hear that, I\'ll get someone out…" '
        "Reply 1 to send · 2 to skip · or open the app to edit."
    )


def test_render_draft_ready_sms_collapses_whitespace_and_truncates_issue_snippet_to_60_chars() -> (
    None
):
    """A tenant's raw message can carry newlines/runs of internal
    whitespace (real input, plain-language-rules.md rule #8 -- never
    corrected) -- the issue line must still read as ONE line, truncated to
    ~60 chars with the same ellipsis convention as the draft excerpt."""
    messy_snippet = "the   heat\n\nisnt working  since\tlast night and it is freezing in here too"
    body = landlord_sms.render_draft_ready_sms(
        tenant_label="Maria (Unit 2, Palmerston)",
        draft_body="Hi Maria, sorry about that.",
        issue_snippet=messy_snippet,
    )
    assert "\n" not in body
    assert "  " not in body
    assert '"the heat isnt working since last night and it is freezing in…"' in body


def test_render_draft_ready_sms_blank_issue_snippet_falls_back_gracefully() -> None:
    """A snippet that collapses to nothing (e.g. all whitespace) is treated
    the same as ``None`` -- fall back rather than render an empty quoted
    issue line."""
    body = landlord_sms.render_draft_ready_sms(
        tenant_label="Maria (Unit 2, Palmerston)",
        draft_body="Hi Maria, sorry about that.",
        issue_snippet="   \n  ",
    )
    assert body == (
        'Stoop: Maria (Unit 2, Palmerston) — draft ready: "Hi Maria, sorry about that." '
        "Reply 1 to send · 2 to skip · or open the app to edit."
    )


def test_render_stale_notice_uses_tenants_name() -> None:
    body = landlord_sms.render_stale_notice_sms(tenant_name="Maria")
    assert body == "Stoop: Maria sent a new message — fresh draft coming."


def test_render_stale_notice_falls_back_when_unnamed() -> None:
    body = landlord_sms.render_stale_notice_sms(tenant_name=None)
    assert "Your tenant" in body


# ---------------------------------------------------------------------------
# Enqueue idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_enqueue_landlord_sms_is_idempotent_per_draft_and_kind(
    db_session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)

    try:
        first_id = await landlord_sms.enqueue_landlord_sms(
            db_session,
            landlord_id=uuid.UUID(landlord_id),
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            kind=landlord_sms.KIND_READY,
            body="Draft ready...",
        )
        await db_session.commit()
        assert first_id is not None

        second_id = await landlord_sms.enqueue_landlord_sms(
            db_session,
            landlord_id=uuid.UUID(landlord_id),
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            kind=landlord_sms.KIND_READY,
            body="Draft ready... (redelivered)",
        )
        await db_session.commit()
        assert second_id is None  # idempotent no-op

        count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM notifications WHERE landlord_id = :lid "
                    "AND type = 'draft_ready' AND channel = 'sms'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert count == 1

        # A DIFFERENT kind for the SAME draft is a genuinely new enqueue.
        third_id = await landlord_sms.enqueue_landlord_sms(
            db_session,
            landlord_id=uuid.UUID(landlord_id),
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            kind=landlord_sms.KIND_APPROVED,
            body="Sent!",
        )
        await db_session.commit()
        assert third_id is not None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_most_recent_ready_draft_scoped_to_property(db_session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)

    try:
        # Nothing enqueued yet -- honest "nothing to correlate".
        none_yet = await landlord_sms.most_recent_ready_draft(
            db_session, landlord_id=uuid.UUID(landlord_id), property_id=uuid.UUID(property_id)
        )
        assert none_yet is None

        notification_id = await landlord_sms.enqueue_landlord_sms(
            db_session,
            landlord_id=uuid.UUID(landlord_id),
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            kind=landlord_sms.KIND_READY,
            body="Draft ready...",
        )
        await db_session.commit()
        assert notification_id is not None

        # BLOCKING-3 (safety re-review, 2026-08-01): still 'pending' --
        # never actually delivered to this landlord's phone -- must NOT
        # correlate yet. See test_most_recent_ready_draft_only_correlates
        # _to_sent_notices below for the full pending/failed/exhausted
        # matrix.
        still_pending = await landlord_sms.most_recent_ready_draft(
            db_session, landlord_id=uuid.UUID(landlord_id), property_id=uuid.UUID(property_id)
        )
        assert still_pending is None

        await db_session.execute(
            text("UPDATE notifications SET status = 'sent' WHERE id = :id"),
            {"id": str(notification_id)},
        )
        await db_session.commit()

        referenced = await landlord_sms.most_recent_ready_draft(
            db_session, landlord_id=uuid.UUID(landlord_id), property_id=uuid.UUID(property_id)
        )
        assert referenced is not None
        assert str(referenced.draft_id) == draft_id
        assert str(referenced.case_id) == case_id

        # A DIFFERENT property -- even for the SAME landlord -- correlates
        # to nothing.
        other_property_id = await factories.insert_property(db_session, landlord_id)
        none_for_other_property = await landlord_sms.most_recent_ready_draft(
            db_session, landlord_id=uuid.UUID(landlord_id), property_id=uuid.UUID(other_property_id)
        )
        assert none_for_other_property is None
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
@pytest.mark.parametrize("never_delivered_status", ["pending", "failed", "exhausted"])
async def test_most_recent_ready_draft_only_correlates_to_sent_notices(
    db_session: AsyncSession, never_delivered_status: str
) -> None:
    """Safety re-review, blocking finding 3, 2026-08-01 — reproduces the
    PoC'd approve-by-SMS correlation bug: WITHOUT the ``status = 'sent'``
    filter on ``_SELECT_MOST_RECENT_READY_DRAFT_SQL``, a NEWER draft-ready
    notice this landlord was never actually shown (``'pending'``,
    transiently ``'failed'``, or terminally ``'exhausted'`` — issue #229
    item 4's own attempt cap) could out-rank an OLDER notice that genuinely
    reached their phone, purely by sorting newer in ``created_at`` — a bare
    "1" reply would then approve-and-send a DIFFERENT draft, on a
    DIFFERENT case, the landlord never saw or referenced. This directly
    touches ``app/agent/approve_by_sms.py``'s reply-correlation call site
    (``most_recent_ready_draft`` is its sole disambiguation mechanism, per
    api-contracts.md)."""
    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)

    # Case A: the notice the landlord ACTUALLY received (older, 'sent').
    case_id_a = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id_a = await factories.insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id_a
    )
    notification_id_a = await landlord_sms.enqueue_landlord_sms(
        db_session,
        landlord_id=uuid.UUID(landlord_id),
        case_id=uuid.UUID(case_id_a),
        draft_id=uuid.UUID(draft_id_a),
        kind=landlord_sms.KIND_READY,
        body="A ready",
    )
    await db_session.commit()
    await db_session.execute(
        text("UPDATE notifications SET status = 'sent' WHERE id = :id"),
        {"id": str(notification_id_a)},
    )
    await db_session.commit()

    # Case B: a NEWER notice the landlord was NEVER shown.
    case_id_b = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id_b = await factories.insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id_b
    )
    notification_id_b = await landlord_sms.enqueue_landlord_sms(
        db_session,
        landlord_id=uuid.UUID(landlord_id),
        case_id=uuid.UUID(case_id_b),
        draft_id=uuid.UUID(draft_id_b),
        kind=landlord_sms.KIND_READY,
        body="B ready",
    )
    await db_session.commit()
    if never_delivered_status != "pending":
        await db_session.execute(
            text("UPDATE notifications SET status = :status WHERE id = :id"),
            {"status": never_delivered_status, "id": str(notification_id_b)},
        )
        await db_session.commit()

    try:
        referenced = await landlord_sms.most_recent_ready_draft(
            db_session, landlord_id=uuid.UUID(landlord_id), property_id=uuid.UUID(property_id)
        )
        assert referenced is not None
        # Correlation must resolve to A (actually delivered), NEVER to B
        # (newer, but never shown to this landlord) -- regardless of
        # whether B is 'pending', 'failed', or 'exhausted'.
        assert str(referenced.draft_id) == draft_id_a
        assert str(referenced.case_id) == case_id_a
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Drain sweep — the THIRD sanctioned Twilio-send call site.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_drain_sweep_sends_and_marks_sent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_phone = factories.fresh_phone()
    landlord_id = await factories.insert_landlord(db_session, phone=landlord_phone)
    twilio_number = factories.fresh_phone()
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number=twilio_number
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)
    await landlord_sms.enqueue_landlord_sms(
        db_session,
        landlord_id=uuid.UUID(landlord_id),
        case_id=uuid.UUID(case_id),
        draft_id=uuid.UUID(draft_id),
        kind=landlord_sms.KIND_READY,
        body="Draft ready — reply 1 to send.",
    )
    await db_session.commit()

    fake_sender = _FakeTwilioSender()
    monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: fake_sender)

    try:
        outcomes = await landlord_sms.run_landlord_sms_drain_sweep()
        own_outcomes = [o for o in outcomes if o.outcome == "sent"]
        assert len(own_outcomes) >= 1

        own_calls = [c for c in fake_sender.calls if c["to"] == landlord_phone]
        assert len(own_calls) == 1
        assert own_calls[0]["from_"] == twilio_number
        assert own_calls[0]["body"] == "Draft ready — reply 1 to send."

        status = (
            await db_session.execute(
                text(
                    "SELECT status FROM notifications WHERE landlord_id = :lid "
                    "AND type = 'draft_ready' AND channel = 'sms'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert status == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_drain_sweep_marks_failed_on_send_exception_and_retries_next_tick(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_phone = factories.fresh_phone()
    landlord_id = await factories.insert_landlord(db_session, phone=landlord_phone)
    twilio_number = factories.fresh_phone()
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number=twilio_number
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)
    await landlord_sms.enqueue_landlord_sms(
        db_session,
        landlord_id=uuid.UUID(landlord_id),
        case_id=uuid.UUID(case_id),
        draft_id=uuid.UUID(draft_id),
        kind=landlord_sms.KIND_READY,
        body="Draft ready — reply 1 to send.",
    )
    await db_session.commit()

    failing_sender = _FakeTwilioSender(fail=True)
    monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: failing_sender)

    try:
        await landlord_sms.run_landlord_sms_drain_sweep()

        status = (
            await db_session.execute(
                text(
                    "SELECT status FROM notifications WHERE landlord_id = :lid "
                    "AND type = 'draft_ready' AND channel = 'sms'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert status == "failed"  # transient -- retried, never exhausted

        # Next tick, with a working sender, actually delivers it.
        working_sender = _FakeTwilioSender()
        monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: working_sender)
        await landlord_sms.run_landlord_sms_drain_sweep()

        status_after_retry = (
            await db_session.execute(
                text(
                    "SELECT status FROM notifications WHERE landlord_id = :lid "
                    "AND type = 'draft_ready' AND channel = 'sms'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert status_after_retry == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_drain_sweep_exhausts_when_landlord_has_no_phone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    landlord_id = await factories.insert_landlord(db_session, phone=None)
    twilio_number = factories.fresh_phone()
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number=twilio_number
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)
    await landlord_sms.enqueue_landlord_sms(
        db_session,
        landlord_id=uuid.UUID(landlord_id),
        case_id=uuid.UUID(case_id),
        draft_id=uuid.UUID(draft_id),
        kind=landlord_sms.KIND_READY,
        body="Draft ready — reply 1 to send.",
    )
    await db_session.commit()

    fake_sender = _FakeTwilioSender()
    monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: fake_sender)

    try:
        await landlord_sms.run_landlord_sms_drain_sweep()

        status = (
            await db_session.execute(
                text(
                    "SELECT status FROM notifications WHERE landlord_id = :lid "
                    "AND type = 'draft_ready' AND channel = 'sms'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert status == "exhausted"  # terminal -- no phone will ever appear
        assert fake_sender.calls == []
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Bounded-retry attempt cap (issue #229 item 4, PR #228 senior-review
# advisory 4) -- a has-phone row that keeps failing every tick must
# eventually stop retrying, not fail forever.
# ---------------------------------------------------------------------------


async def _seed_draft_ready_row(db_session: AsyncSession, *, kind: str | None = None) -> str:
    """Seed one full has-phone landlord/property/tenant/case/draft chain
    plus a pending ``draft_ready``/``sms`` row -- the shared shape every
    drain-sweep test in this module needs. *kind* defaults to
    :data:`landlord_sms.KIND_READY` (the historical default); pass one of
    the four confirmation kinds explicitly for a test that specifically
    exercises the attempt cap (issue #229 advisory 1 — KIND_READY is
    EXEMPT from exhaustion, see ``_LANDLORD_SMS_MAX_ATTEMPTS``'s own
    docstring)."""
    effective_kind = kind or landlord_sms.KIND_READY
    landlord_phone = factories.fresh_phone()
    landlord_id = await factories.insert_landlord(db_session, phone=landlord_phone)
    twilio_number = factories.fresh_phone()
    property_id = await factories.insert_property(
        db_session, landlord_id, twilio_number=twilio_number
    )
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(db_session, landlord_id=landlord_id, case_id=case_id)
    await landlord_sms.enqueue_landlord_sms(
        db_session,
        landlord_id=uuid.UUID(landlord_id),
        case_id=uuid.UUID(case_id),
        draft_id=uuid.UUID(draft_id),
        kind=effective_kind,
        body="Draft ready — reply 1 to send.",
    )
    await db_session.commit()
    return landlord_id


async def _row_status_and_attempt(db_session: AsyncSession, landlord_id: str) -> tuple[str, int]:
    row = (
        (
            await db_session.execute(
                text(
                    "SELECT status, attempt FROM notifications WHERE landlord_id = :lid "
                    "AND type = 'draft_ready' AND channel = 'sms'"
                ),
                {"lid": landlord_id},
            )
        )
        .mappings()
        .one()
    )
    return row["status"], row["attempt"]


@pytest.mark.integration
async def test_drain_sweep_exhausts_after_max_attempts_despite_valid_phone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landlord WITH a valid phone/twilio_number whose Twilio send keeps
    failing every tick must eventually reach ``'exhausted'`` — never retried
    forever — after ``_LANDLORD_SMS_MAX_ATTEMPTS`` attempts. Uses a
    CONFIRMATION kind, not KIND_READY (issue #229 advisory 1 — KIND_READY
    is deliberately EXEMPT from this cap, see
    test_drain_sweep_kind_ready_never_exhausts below)."""
    landlord_id = await _seed_draft_ready_row(db_session, kind=landlord_sms.KIND_APPROVED)
    failing_sender = _FakeTwilioSender(fail=True)
    monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: failing_sender)

    max_attempts = landlord_sms._LANDLORD_SMS_MAX_ATTEMPTS  # noqa: SLF001

    try:
        last_outcomes: list[landlord_sms.LandlordSmsOutcome] = []
        for _ in range(max_attempts):
            last_outcomes = await landlord_sms.run_landlord_sms_drain_sweep()

        assert [o.outcome for o in last_outcomes] == ["exhausted"]

        status, attempt = await _row_status_and_attempt(db_session, landlord_id)
        assert status == "exhausted"
        assert attempt == max_attempts

        # A further tick must be a true no-op -- 'exhausted' rows are
        # excluded from the sweep's own retry set (status IN ('pending',
        # 'failed')).
        further_outcomes = await landlord_sms.run_landlord_sms_drain_sweep()
        assert further_outcomes == []
        status_after, attempt_after = await _row_status_and_attempt(db_session, landlord_id)
        assert status_after == "exhausted"
        assert attempt_after == max_attempts, "a no-op tick must not re-claim the row"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_drain_sweep_kind_ready_never_exhausts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #229 advisory 1 (adjudicated 2026-08-01): KIND_READY is
    deliberately EXEMPT from ``_LANDLORD_SMS_MAX_ATTEMPTS`` -- it is the
    SOLE notice of a pending approval for an SMS-only landlord, unlike the
    four confirmation kinds (covered by
    test_drain_sweep_exhausts_after_max_attempts_despite_valid_phone
    above). Well past the attempt cap, the row must still be ``'failed'``
    (transient, retried), never ``'exhausted'`` -- and a healthy sender
    still delivers it whenever the fault eventually clears."""
    landlord_id = await _seed_draft_ready_row(db_session, kind=landlord_sms.KIND_READY)
    failing_sender = _FakeTwilioSender(fail=True)
    monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: failing_sender)

    max_attempts = landlord_sms._LANDLORD_SMS_MAX_ATTEMPTS  # noqa: SLF001

    try:
        # THREE TIMES the cap -- still never exhausted.
        last_outcomes: list[landlord_sms.LandlordSmsOutcome] = []
        for _ in range(max_attempts * 3):
            last_outcomes = await landlord_sms.run_landlord_sms_drain_sweep()
            assert [o.outcome for o in last_outcomes] == ["failed"]

        status, attempt = await _row_status_and_attempt(db_session, landlord_id)
        assert status == "failed"  # never 'exhausted', however many attempts
        assert attempt == max_attempts * 3

        working_sender = _FakeTwilioSender()
        monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: working_sender)
        outcomes_after = await landlord_sms.run_landlord_sms_drain_sweep()
        assert [o.outcome for o in outcomes_after] == ["sent"]
        status_after, _ = await _row_status_and_attempt(db_session, landlord_id)
        assert status_after == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_drain_sweep_below_max_attempts_stays_failed_and_still_retries(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below the attempt cap, a failure stays transient ('failed', in the
    retry set) -- and the cap never blocks a row that eventually succeeds
    once the fault clears."""
    landlord_id = await _seed_draft_ready_row(db_session)
    failing_sender = _FakeTwilioSender(fail=True)
    monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: failing_sender)

    max_attempts = landlord_sms._LANDLORD_SMS_MAX_ATTEMPTS  # noqa: SLF001
    assert max_attempts > 1, "test assumes at least one non-terminal failed attempt"

    try:
        outcomes = await landlord_sms.run_landlord_sms_drain_sweep()
        assert [o.outcome for o in outcomes] == ["failed"]

        status, attempt = await _row_status_and_attempt(db_session, landlord_id)
        assert status == "failed"  # transient -- stays in the retry set
        assert attempt == 1

        working_sender = _FakeTwilioSender()
        monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: working_sender)
        outcomes_after = await landlord_sms.run_landlord_sms_drain_sweep()
        assert [o.outcome for o in outcomes_after] == ["sent"]

        status_after, _attempt_after = await _row_status_and_attempt(db_session, landlord_id)
        assert status_after == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_drain_sweep_exhaustion_pages_sentry_distinct_from_per_attempt_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every send failure already pages Sentry (level='error') -- the LAST
    (exhausting) attempt must ALSO fire a second, distinct, level='warning'
    page (mirrors ``app/agent/degraded_mode_sweep.py``'s own dedicated
    exhaustion-alert convention), metadata-only (never a phone number/body,
    rule #5). Uses a CONFIRMATION kind, not KIND_READY -- see issue #229
    advisory 1."""
    landlord_id = await _seed_draft_ready_row(db_session, kind=landlord_sms.KIND_APPROVED)
    failing_sender = _FakeTwilioSender(fail=True)
    monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: failing_sender)

    mock_capture = MagicMock()
    monkeypatch.setattr(landlord_sms.sentry_sdk, "capture_message", mock_capture)

    max_attempts = landlord_sms._LANDLORD_SMS_MAX_ATTEMPTS  # noqa: SLF001

    try:
        for _ in range(max_attempts):
            await landlord_sms.run_landlord_sms_drain_sweep()

        # One "send failed" page per attempt, PLUS one distinct "exhausted"
        # page on the last -- never a phone number/message body in either.
        calls = mock_capture.call_args_list
        assert len(calls) == max_attempts + 1
        exhaustion_calls = [c for c in calls if "exhausted" in c.args[0]]
        assert len(exhaustion_calls) == 1
        assert exhaustion_calls[0].kwargs["level"] == "warning"
        for call in calls:
            for value in call.kwargs.get("extras", {}).values():
                assert value is None or "+1" not in str(value)
    finally:
        await _cleanup(db_session, landlord_id)


class _CancellingTwilioSender:
    """Simulates the scheduler task being cancelled mid-Twilio-send (Fly
    deploy / machine restart / ``stop_scheduler()``) — ``CancelledError``
    is a ``BaseException``, not an ``Exception``, so
    ``except Exception`` in ``_process_candidate`` never sees it and it
    propagates out of the sweep call entirely (matching real asyncio task
    -cancellation semantics — see safety re-review, blocking finding 2,
    2026-08-01)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        self.calls.append(to)
        raise asyncio.CancelledError


@pytest.mark.integration
async def test_drain_sweep_cancellation_never_burns_send_failures_only_genuine_failures_do(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety re-review, blocking finding 2, 2026-08-01 -- regression for a
    reproduced attempt-burn bug: the claim/CAS ``attempt`` column used to
    ALSO gate exhaustion, so five crashes/``CancelledError``s between a
    successful claim and the Twilio call -- each of which burns one
    ``attempt`` via the claim itself but NEVER reaches ``send_sms``'s own
    outcome -- could exhaust a row that placed ZERO real Twilio calls.
    Fixed by tracking genuine send failures separately in
    ``payload.send_failures`` (see :data:`landlord_sms.
    _MARK_LANDLORD_SMS_SEND_OUTCOME_SQL`), incremented ONLY from the
    ``except Exception`` block around the actual ``sender.send_sms(...)``
    call — never from a claim, and never from a ``CancelledError`` (a
    ``BaseException``, not caught by that ``except Exception`` at all)."""
    landlord_id = await _seed_draft_ready_row(db_session)
    canceller = _CancellingTwilioSender()
    monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: canceller)

    max_attempts = landlord_sms._LANDLORD_SMS_MAX_ATTEMPTS  # noqa: SLF001

    try:
        # Five cancellations -- MORE than _LANDLORD_SMS_MAX_ATTEMPTS worth
        # of claims -- yet the row must never be exhausted: no genuine send
        # failure has ever been observed.
        for _ in range(max_attempts):
            with pytest.raises(asyncio.CancelledError):
                await landlord_sms.run_landlord_sms_drain_sweep()

        status, attempt = await _row_status_and_attempt(db_session, landlord_id)
        assert status == "pending"  # never marked 'failed'/'exhausted' by a cancellation
        assert attempt == max_attempts  # the claim CAS still advanced every time
        assert canceller.calls, "the sender was genuinely invoked each time"

        # NOW a single genuine Twilio failure -- the FIRST one this row has
        # ever actually observed -- must be transient ('failed'), never
        # exhausted, even though `attempt` is already at the cap.
        failing_sender = _FakeTwilioSender(fail=True)
        monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: failing_sender)
        outcomes = await landlord_sms.run_landlord_sms_drain_sweep()
        assert [o.outcome for o in outcomes] == ["failed"]
        status_after_one_real_failure, _ = await _row_status_and_attempt(db_session, landlord_id)
        assert status_after_one_real_failure == "failed"

        # A fully healthy sender still delivers it -- the cancellations
        # never counted against the row at all.
        working_sender = _FakeTwilioSender()
        monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: working_sender)
        outcomes_after = await landlord_sms.run_landlord_sms_drain_sweep()
        assert [o.outcome for o in outcomes_after] == ["sent"]
        status_final, _ = await _row_status_and_attempt(db_session, landlord_id)
        assert status_final == "sent"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Wall-clock tick deadline (issue #229, PR #228 senior-review advisory 1) --
# mirrors tests/test_agent_draft_sender.py's / tests/test_push_outbox_sweep
# .py's own deadline test pattern exactly. Reuses _seed_draft_ready_row /
# _row_status_and_attempt from the attempt-cap section above.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_landlord_sms_drain_sweep_default_deadline_is_25_seconds() -> None:
    assert landlord_sms.DEFAULT_TICK_DEADLINE_SECONDS == 25.0


class _FakeClock:
    """A mutable, injectable time source for the sweep's deadline check —
    advanced explicitly by the fake sender below rather than sleeping for
    real seconds. Mirrors ``tests/test_agent_draft_sender.py``'s own
    ``_FakeClock``."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _DeadlineBlowingTwilioSender:
    """Records every call (by recipient phone); advances a shared
    :class:`_FakeClock` past the tick's deadline on its FIRST send,
    simulating a slow/hanging Twilio round-trip that must not be allowed to
    also delay claiming every OTHER due row in the same tick."""

    def __init__(self, clock: _FakeClock, *, advance_by: float) -> None:
        self._clock = clock
        self._advance_by = advance_by
        self.calls: list[str] = []

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        self.calls.append(to)
        self._clock.now += self._advance_by
        return f"SM{uuid.uuid4().hex}"


@pytest.mark.integration
async def test_drain_sweep_stops_claiming_after_deadline_then_resumes_next_tick(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two due rows; the first send blows the (tiny, test-only) deadline.
    The SECOND due row must NOT be claimed in the same tick -- it stays
    'pending' and due, claimed whole by the very next tick call. Nothing
    lost -- this is what stops a hung Twilio call chain here (this sweep
    runs LAST in the tick) from delaying the NEXT tick's emergency chain
    sweep."""
    landlord_id_a = await _seed_draft_ready_row(db_session)
    landlord_id_b = await _seed_draft_ready_row(db_session)
    clock = _FakeClock(start=0.0)
    sender = _DeadlineBlowingTwilioSender(clock, advance_by=10.0)
    monkeypatch.setattr(landlord_sms, "get_twilio_sender", lambda: sender)

    try:
        outcomes = await landlord_sms.run_landlord_sms_drain_sweep(
            deadline_seconds=5.0, time_source=clock
        )
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "sent"
        assert len(sender.calls) == 1  # bounded: NOT both due rows attempted this tick

        status_a, _ = await _row_status_and_attempt(db_session, landlord_id_a)
        status_b, _ = await _row_status_and_attempt(db_session, landlord_id_b)
        statuses = {status_a, status_b}
        assert statuses == {"sent", "pending"}  # exactly one sent, one left due

        # The next tick call (clock already past the first deadline window,
        # but the sweep recomputes its OWN start from time_source() every
        # call) claims and sends the leftover row.
        outcomes_second_tick = await landlord_sms.run_landlord_sms_drain_sweep(
            deadline_seconds=5.0, time_source=clock
        )
        assert len(outcomes_second_tick) == 1
        assert outcomes_second_tick[0].outcome == "sent"
        assert len(sender.calls) == 2

        status_a_after, _ = await _row_status_and_attempt(db_session, landlord_id_a)
        status_b_after, _ = await _row_status_and_attempt(db_session, landlord_id_b)
        assert status_a_after == "sent"
        assert status_b_after == "sent"
    finally:
        await _cleanup(db_session, landlord_id_a)
        await _cleanup(db_session, landlord_id_b)
