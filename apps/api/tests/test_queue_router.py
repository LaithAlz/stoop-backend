"""Integration tests for ``GET /v1/queue`` (#56).

Marker: ``integration``. Same direct-handler-call harness as
``tests/test_properties_router.py``/``tests/test_cases_router.py`` — see
those files' module docstrings for why (``require_landlord`` is proven
generically by ``tests/test_rls_isolation_matrix.py``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.deps import Landlord
from app.routers.queue import get_queue
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


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.rollback()
    params = {"lid": landlord_id}
    await session.execute(
        text(
            "DELETE FROM message_status_events WHERE message_id IN "
            "(SELECT id FROM messages WHERE landlord_id = :lid)"
        ),
        params,
    )
    # message_cases has FKs to both messages and cases (no explicit ON
    # DELETE action -> NO ACTION) — must go before either.
    await session.execute(
        text(
            "DELETE FROM message_cases WHERE message_id IN "
            "(SELECT id FROM messages WHERE landlord_id = :lid)"
        ),
        params,
    )
    # notifications has FKs to both landlords (RESTRICT) and cases (no
    # explicit ON DELETE -> NO ACTION) -- must go before "cases" below.
    for table in (
        "audit_log",
        "drafts",
        "notifications",
        "messages",
        "cases",
        "tenants",
        "vendors",
        "properties",
    ):
        await session.execute(text(f"DELETE FROM {table} WHERE landlord_id = :lid"), params)  # noqa: S608
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


async def _seed_awaiting_case(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str | None = None,
    tenant_name: str | None = "Maria",
    status: str = "awaiting_approval",
    tenant_message_body: str = "no heat since last night",
    classified_payload: dict[str, Any] | None = None,
    with_pending_draft: bool = True,
    draft_body: str = "Hi Maria — so sorry to hear...",
) -> tuple[str, str, str]:
    """Seed one case shaped the way production actually writes it: a
    tenant message linked via ``message_cases`` (never ``messages.case_id``
    directly — see ``app/agent/nodes/identify_case.py``'s module
    docstring), a 'classified' ``audit_log`` row, and (usually) exactly one
    pending draft. Returns ``(property_id, tenant_id, case_id)``."""
    if property_id is None:
        property_id = await factories.insert_property(session, landlord_id)
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id, name=tenant_name)
    case_id = await factories.insert_case(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status=status,
    )

    message_id = await factories.insert_message(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body=tenant_message_body,
    )
    await factories.insert_message_case(session, message_id=message_id, case_id=case_id)

    if classified_payload is not None:
        await factories.insert_audit_log(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            actor="agent",
            action="classified",
            payload=classified_payload,
        )

    if with_pending_draft:
        await factories.insert_draft(
            session, landlord_id=landlord_id, case_id=case_id, status="pending", body=draft_body
        )

    await session.commit()
    return property_id, tenant_id, case_id


@pytest.mark.integration
async def test_queue_card_shape_sources_severity_reasoning_why_from_latest_classified_row(
    session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, case_id = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={
                "severity": "urgent",
                "summary": "No heat overnight can't wait, so I treated it as urgent.",
                "rules_fired": ["no_heat_overnight"],
                "refusal_flags": [],
            },
            draft_body="Hi Maria — so sorry to hear, I've flagged this.",
        )

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        card = result.items[0]
        assert str(card.case_id) == case_id
        assert card.severity == "urgent"
        assert card.why == "No heat overnight can't wait, so I treated it as urgent."
        assert card.reasoning == ["no_heat_overnight"]
        assert card.refusal_flags == []
        assert card.tenant_name == "Maria"
        assert card.property_label == "Test Property"
        assert card.tenant_message == "no heat since last night"
        assert card.draft_body == "Hi Maria — so sorry to hear, I've flagged this."
        assert card.draft_recipient == "tenant"
        assert card.has_media is False
        assert card.media_note is None
        assert card.received_at is not None
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_why_is_null_for_pre_v17_rows_missing_summary_key(
    session: AsyncSession,
) -> None:
    """schema-v1 v1.7 / #183: a 'classified' row written before the
    ``summary`` key shipped has no such key — the queue must read that as
    ``why: null``, never synthesize one from ``rules_fired``."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={
                "severity": "routine",
                "rules_fired": ["general maintenance request"],
                "refusal_flags": [],
                # no "summary" key at all -- pre-v1.7 shape.
            },
        )

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        assert result.items[0].why is None
        assert result.items[0].reasoning == ["general maintenance request"]
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_orders_emergency_then_urgent_then_routine(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, routine_case = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            tenant_name="Routine Tenant",
            classified_payload={"severity": "routine", "rules_fired": [], "refusal_flags": []},
        )
        _p, _t, urgent_case = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            tenant_name="Urgent Tenant",
            classified_payload={"severity": "urgent", "rules_fired": [], "refusal_flags": []},
        )
        _p, _t, emergency_case = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            tenant_name="Emergency Tenant",
            classified_payload={"severity": "emergency", "rules_fired": [], "refusal_flags": []},
        )

        result = await get_queue((landlord, session))

        assert [str(item.case_id) for item in result.items] == [
            emergency_case,
            urgent_case,
            routine_case,
        ]
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_orders_oldest_first_within_same_tier(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, first_case = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            tenant_name="First Tenant",
            classified_payload={"severity": "urgent", "rules_fired": [], "refusal_flags": []},
        )
        await session.commit()
        _p, _t, second_case = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            tenant_name="Second Tenant",
            classified_payload={"severity": "urgent", "rules_fired": [], "refusal_flags": []},
        )

        result = await get_queue((landlord, session))

        assert [str(item.case_id) for item in result.items] == [first_case, second_case]
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_excludes_awaiting_tenant_open_reopened_and_resolved(
    session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, awaiting_case = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "urgent", "rules_fired": [], "refusal_flags": []},
        )
        # None of these have a pending draft, matching how each status is
        # actually reached in production (awaiting_tenant/open/resolved/
        # reopened cases never carry a live pending draft).
        for other_status in ("awaiting_tenant", "open", "resolved", "reopened"):
            await _seed_awaiting_case(
                session,
                landlord_id=landlord_id,
                status=other_status,
                with_pending_draft=False,
            )

        result = await get_queue((landlord, session))

        assert [str(item.case_id) for item in result.items] == [awaiting_case]
        assert result.counts.total == 1
        assert result.counts.awaiting_tenant == 1
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_uses_latest_classified_row_when_reclassified(session: AsyncSession) -> None:
    """Stale-draft re-run: a case can be reclassified more than once —
    the queue must reflect the LATEST 'classified' row, not the first."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, case_id = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={
                "severity": "routine",
                "summary": "Old classification.",
                "rules_fired": ["old rule"],
                "refusal_flags": [],
            },
        )
        await session.commit()
        # A newer tenant message triggered a re-classification.
        await factories.insert_audit_log(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            actor="agent",
            action="classified",
            payload={
                "severity": "urgent",
                "summary": "New classification, so I treated it as urgent.",
                "rules_fired": ["new rule"],
                "refusal_flags": [],
            },
        )
        await session.commit()

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        assert result.items[0].severity == "urgent"
        assert result.items[0].why == "New classification, so I treated it as urgent."
        assert result.items[0].reasoning == ["new rule"]
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_latest_classified_row_must_be_severity_bearing(
    session: AsyncSession,
) -> None:
    """#208 spec review (MAJOR-2): ``classify_intent.py`` also writes
    ``action='classified'`` rows (``payload.kind`` disambiguates: 'intent'
    on success, 'intent_classification_failed' on total failure, #208) —
    the queue's "latest classified row per case" LATERAL was previously
    kind-blind (``ORDER BY created_at DESC LIMIT 1`` with no payload-shape
    filter), so an intent row landing AFTER the genuine severity row (e.g.
    a stale-draft re-run that reruns ``classify_intent`` again without a
    fresh severity classification) could win "latest" and blank the
    card's severity chip (and clobber ``why``/``reasoning`` with the
    intent row's own, differently-shaped fields).

    This is NOT a #208-introduced bug — a successful ``kind='intent'`` row
    already carried its own ``summary`` key and could have shadowed the
    severity row's ``why`` the exact same way before #208 ever existed;
    #208's new ``kind='intent_classification_failed'`` row just made it
    easy to notice and worth fixing properly rather than shipping a second
    instance of the same shadow. Fixed by requiring the LATERAL's row to
    carry a ``severity`` key (``AND (a.payload ? 'severity')``) — the only
    payload shape that predicate ever excludes is exactly the intent
    variants, since every OTHER field this endpoint reads
    (``rules_fired``, ``refusal_flags``, ``summary``) only ever
    co-occurs with ``severity`` on a genuine ``classify_severity.py`` row.

    Covers BOTH the pre-existing successful-intent shadow and #208's new
    failed-intent shadow with the same two rows below (a real intent
    'classified' row landing between them would fail identically without
    the fix)."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, case_id = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={
                "severity": "urgent",
                "summary": "No heat overnight can't wait, so I treated it as urgent.",
                "rules_fired": ["no_heat_overnight"],
                "refusal_flags": [],
            },
        )
        await session.commit()
        # A later message on the SAME case re-ran classify_intent (success
        # case) -- no fresh severity classification this run, just an
        # intent 'classified' row with NO "severity" key.
        await factories.insert_audit_log(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            actor="agent",
            action="classified",
            payload={
                "kind": "intent",
                "intent": "maintenance",
                "summary": "Tenant is following up about the same issue.",
                "model": "claude-sonnet-5",
                "tokens_in": 80,
                "tokens_out": 20,
                "cost_cents": 0.03,
                "prompt_version": "inline-v0",
            },
        )
        await session.commit()
        # Then classify_intent failed twice on a STILL LATER message (#208's
        # new failure-path row) -- also no "severity" key.
        await factories.insert_audit_log(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            actor="agent",
            action="classified",
            payload={
                "kind": "intent_classification_failed",
                "message_id": str(uuid.uuid4()),
                "case_id": case_id,
                "model": "claude-sonnet-5",
                "tokens_in": 160,
                "tokens_out": 40,
                "cost_cents": 0.06,
            },
        )
        await session.commit()

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        card = result.items[0]
        # The severity chip/why/reasoning still come from the GENUINE
        # severity row, never blanked or clobbered by either later intent
        # row despite both being newer 'classified' rows on the same case.
        assert card.severity == "urgent"
        assert card.why == "No heat overnight can't wait, so I treated it as urgent."
        assert card.reasoning == ["no_heat_overnight"]
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_counts_tally_by_severity_and_awaiting_tenant_excluded_from_total(
    session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "urgent", "rules_fired": [], "refusal_flags": []},
        )
        await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "routine", "rules_fired": [], "refusal_flags": []},
        )
        await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "routine", "rules_fired": [], "refusal_flags": []},
        )
        await _seed_awaiting_case(
            session, landlord_id=landlord_id, status="awaiting_tenant", with_pending_draft=False
        )

        result = await get_queue((landlord, session))

        assert result.counts.total == 3
        assert result.counts.emergency == 0
        assert result.counts.urgent == 1
        assert result.counts.routine == 2
        assert result.counts.awaiting_tenant == 1
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_case_without_pending_draft_never_shown(session: AsyncSession) -> None:
    """Anomaly defense (mirrors ``await_approval.py``'s own "no pending
    draft, skip the pause" precedent): a case sitting in
    ``awaiting_approval`` with no live pending draft (should never happen
    under the stale-draft invariant) is excluded rather than shown as a
    broken, unapprovable card."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "urgent", "rules_fired": [], "refusal_flags": []},
            with_pending_draft=False,
        )

        result = await get_queue((landlord, session))

        assert result.items == []
        assert result.counts.total == 0
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_severity_missing_or_unrecognized_sorts_last_never_dropped(
    session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, no_classification_case = await _seed_awaiting_case(
            session, landlord_id=landlord_id, classified_payload=None
        )
        _p, _t, routine_case = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "routine", "rules_fired": [], "refusal_flags": []},
        )

        result = await get_queue((landlord, session))

        assert len(result.items) == 2
        assert [str(item.case_id) for item in result.items] == [
            routine_case,
            no_classification_case,
        ]
        missing = next(item for item in result.items if str(item.case_id) == no_classification_case)
        assert missing.severity is None
        assert missing.why is None
        assert missing.reasoning == []
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_scoped_by_landlord_cross_tenant_isolation(session: AsyncSession) -> None:
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    landlord_a = Landlord(id=uuid.UUID(landlord_a_id))
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    try:
        _p, _t, case_a = await _seed_awaiting_case(
            session,
            landlord_id=landlord_a_id,
            tenant_name="Tenant A",
            classified_payload={"severity": "urgent", "rules_fired": [], "refusal_flags": []},
        )
        _p, _t, case_b = await _seed_awaiting_case(
            session,
            landlord_id=landlord_b_id,
            tenant_name="Tenant B",
            classified_payload={"severity": "urgent", "rules_fired": [], "refusal_flags": []},
        )
        assert case_a != case_b

        result_a = await get_queue((landlord_a, session))
        assert [str(item.case_id) for item in result_a.items] == [case_a]
        assert result_a.counts.total == 1

        result_b = await get_queue((landlord_b, session))
        assert [str(item.case_id) for item in result_b.items] == [case_b]
        assert result_b.counts.total == 1
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)


# ---------------------------------------------------------------------------
# notification_id (#213 -- wiring the emergency ack path for clients)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_queue_notification_id_null_when_no_notification_exists(
    session: AsyncSession,
) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "emergency", "rules_fired": [], "refusal_flags": []},
        )

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        assert result.items[0].notification_id is None
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_notification_id_present_via_direct_case_id_backfill(
    session: AsyncSession,
) -> None:
    """The steady-state production path: ``app/agent/nodes/identify_case.py``
    has already backfilled ``notifications.case_id`` directly by the time a
    case reaches ``awaiting_approval`` (a queue card always implies
    ``identify_case`` already ran for it) -- no ``message_cases`` join
    needed for this one."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, case_id = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "emergency", "rules_fired": [], "refusal_flags": []},
        )
        notification_id = await factories.insert_notification(
            session, landlord_id=landlord_id, case_id=case_id, type_="emergency_call"
        )

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        assert str(result.items[0].notification_id) == notification_id
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_notification_id_present_via_message_cases_correlation(
    session: AsyncSession,
) -> None:
    """The narrow pre-backfill window: ``notifications.case_id`` is still
    ``NULL`` (matching how the webhook actually inserts an ``emergency_call``
    row, pre-routing) -- correlates via ``message_cases`` on the shared
    ``payload ->> 'message_id'``, the same pattern ``app/routers/cases.py``'s
    ``_SELECT_AUDIT_SQL`` uses for the sibling ``emergency_triggered`` audit
    row."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        property_id = await factories.insert_property(session, landlord_id)
        tenant_id = await factories.insert_tenant(session, landlord_id, property_id)
        case_id = await factories.insert_case(
            session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            # Explicit: the queue only surfaces awaiting_approval cases, and
            # factories.insert_case deliberately defaults to neutral 'open'.
            status="awaiting_approval",
        )
        message_id = await factories.insert_message(
            session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="there's smoke coming from the wall",
        )
        await factories.insert_message_case(session, message_id=message_id, case_id=case_id)
        await factories.insert_audit_log(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            actor="agent",
            action="classified",
            payload={"severity": "emergency", "rules_fired": [], "refusal_flags": []},
        )
        await factories.insert_draft(session, landlord_id=landlord_id, case_id=case_id)
        notification_id = await factories.insert_notification(
            session,
            landlord_id=landlord_id,
            case_id=None,  # pre-backfill: notifications.case_id is NULL in production
            type_="emergency_call",
            payload={"message_id": message_id},
        )

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        assert str(result.items[0].notification_id) == notification_id
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_notification_id_null_once_acknowledged(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, case_id = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "emergency", "rules_fired": [], "refusal_flags": []},
        )
        await factories.insert_notification(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            type_="emergency_call",
            acknowledged_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        assert result.items[0].notification_id is None
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_notification_id_null_for_non_emergency_call_type(
    session: AsyncSession,
) -> None:
    """An unacknowledged ``needs_eyes`` notification on the same case must
    never leak into ``notification_id`` -- only ``type = 'emergency_call'``
    correlates."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, case_id = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "routine", "rules_fired": [], "refusal_flags": []},
        )
        await factories.insert_notification(
            session, landlord_id=landlord_id, case_id=case_id, type_="needs_eyes", channel="push"
        )

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        assert result.items[0].notification_id is None
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_notification_id_latest_wins_when_multiple_unacked(
    session: AsyncSession,
) -> None:
    """Genuinely ambiguous case (a reopened case, or a second emergency
    report before the first is acknowledged): more than one unacknowledged
    ``emergency_call`` notification on the same case. The most recently
    created one wins."""
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        _p, _t, case_id = await _seed_awaiting_case(
            session,
            landlord_id=landlord_id,
            classified_payload={"severity": "emergency", "rules_fired": [], "refusal_flags": []},
        )
        await factories.insert_notification(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            type_="emergency_call",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        newest_notification_id = await factories.insert_notification(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            type_="emergency_call",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

        result = await get_queue((landlord, session))

        assert len(result.items) == 1
        assert str(result.items[0].notification_id) == newest_notification_id
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_queue_notification_id_cross_tenant_isolation(session: AsyncSession) -> None:
    """Landlord B's notification must never correlate to landlord A's queue
    card, even when it carries landlord A's own real ``case_id`` (simulating
    a hypothetical cross-linkage bug) -- the explicit
    ``n.landlord_id = :landlord_id`` predicate is what protects this, not
    the case-id correlation alone."""
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    landlord_a = Landlord(id=uuid.UUID(landlord_a_id))
    try:
        _p, _t, case_a = await _seed_awaiting_case(
            session,
            landlord_id=landlord_a_id,
            classified_payload={"severity": "emergency", "rules_fired": [], "refusal_flags": []},
        )
        await factories.insert_notification(
            session, landlord_id=landlord_b_id, case_id=case_a, type_="emergency_call"
        )

        result = await get_queue((landlord_a, session))

        assert len(result.items) == 1
        assert result.items[0].notification_id is None
    finally:
        # landlord_b's notification references landlord_a's case (case_a) --
        # must be cleaned up BEFORE landlord_a's own cleanup deletes that
        # case, or the FK (notifications.case_id, no ON DELETE action)
        # blocks it.
        await _cleanup(session, landlord_b_id)
        await _cleanup(session, landlord_a_id)
