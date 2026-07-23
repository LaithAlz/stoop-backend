"""Tests for ``app/agent/landlord_sms.py`` (#122, approve-by-SMS) — the
landlord-facing SMS outbox: rendering (pure), enqueue idempotency,
draft-ready correlation, and the drain sweep (the THIRD sanctioned
Twilio-send call site, ``tests/test_twilio_send_allowlist.py``).

Marker: ``integration`` for anything touching Postgres; the rendering
tests are plain ``unit`` (no DB, no network).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from typing import Any

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

        await landlord_sms.enqueue_landlord_sms(
            db_session,
            landlord_id=uuid.UUID(landlord_id),
            case_id=uuid.UUID(case_id),
            draft_id=uuid.UUID(draft_id),
            kind=landlord_sms.KIND_READY,
            body="Draft ready...",
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
