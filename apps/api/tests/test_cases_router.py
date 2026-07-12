"""Integration tests for Cases read endpoints (#55).

Marker: ``integration``. Same direct-handler-call harness as
``tests/test_properties_router.py`` — see that file's module docstring.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.deps import Landlord
from app.errors import AppError
from app.routers.cases import get_case, list_cases
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
    for table in ("audit_log", "drafts", "messages", "cases", "tenants", "vendors", "properties"):
        await session.execute(text(f"DELETE FROM {table} WHERE landlord_id = :lid"), params)  # noqa: S608
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


async def _seed_case(
    session: AsyncSession,
    *,
    landlord_id: str,
    status: str = "open",
    severity: str | None = "urgent",
    title: str | None = "No heat — Unit 2",
) -> tuple[str, str, str]:
    property_id = await factories.insert_property(session, landlord_id)
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id, name="Maria")
    case_id = await factories.insert_case(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status=status,
        severity=severity,
        title=title,
    )
    await session.commit()
    return property_id, tenant_id, case_id


@pytest.mark.integration
async def test_list_cases_summary_shape(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        property_id, _tenant_id, case_id = await _seed_case(session, landlord_id=landlord_id)

        result = await list_cases((landlord, session))
        assert len(result.items) == 1
        summary = result.items[0]
        assert str(summary.id) == case_id
        assert summary.title == "No heat — Unit 2"
        assert summary.status == "open"
        assert summary.severity == "urgent"
        assert summary.tenant_name == "Maria"
        assert summary.property_label == "Test Property"
        assert str(summary.unit) or summary.unit is None
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_list_cases_filters_by_status_severity_property(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        prop_a, _tenant_a, case_open_urgent = await _seed_case(
            session, landlord_id=landlord_id, status="open", severity="urgent"
        )
        await session.commit()
        prop_b, _tenant_b, case_resolved_routine = await _seed_case(
            session, landlord_id=landlord_id, status="resolved", severity="routine"
        )
        await session.commit()

        by_status = await list_cases((landlord, session), status="resolved")
        assert {str(c.id) for c in by_status.items} == {case_resolved_routine}

        by_severity = await list_cases((landlord, session), severity="urgent")
        assert {str(c.id) for c in by_severity.items} == {case_open_urgent}

        by_property = await list_cases((landlord, session), property_id=uuid.UUID(prop_b))
        assert {str(c.id) for c in by_property.items} == {case_resolved_routine}

        assert prop_a != prop_b
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_list_cases_newest_activity_first_and_pagination(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        case_ids = []
        for i in range(3):
            _p, _t, case_id = await _seed_case(session, landlord_id=landlord_id, title=f"Case {i}")
            case_ids.append(case_id)
            await session.commit()

        page1 = await list_cases((landlord, session), limit=2)
        assert len(page1.items) == 2
        assert page1.next_cursor is not None
        assert str(page1.items[0].id) == case_ids[-1]

        page2 = await list_cases((landlord, session), limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 1
        assert page2.next_cursor is None

        all_ids = {str(c.id) for c in page1.items} | {str(c.id) for c in page2.items}
        assert all_ids == set(case_ids)
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_list_cases_invalid_cursor_returns_400(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await list_cases((landlord, session), cursor="not-a-valid-cursor!!")
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "invalid_cursor"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_get_case_not_found_returns_404(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        with pytest.raises(AppError) as exc_info:
            await get_case(uuid.uuid4(), (landlord, session))
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "case_not_found"
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_cross_tenant_case_access_returns_404(session: AsyncSession) -> None:
    landlord_a_id = await factories.insert_landlord(session)
    landlord_b_id = await factories.insert_landlord(session)
    landlord_a = Landlord(id=uuid.UUID(landlord_a_id))
    landlord_b = Landlord(id=uuid.UUID(landlord_b_id))
    try:
        _p, _t, case_id = await _seed_case(session, landlord_id=landlord_a_id)

        with pytest.raises(AppError) as exc_info:
            await get_case(uuid.UUID(case_id), (landlord_b, session))
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "case_not_found"

        b_list = await list_cases((landlord_b, session))
        assert case_id not in {str(c.id) for c in b_list.items}

        # A can still see it.
        detail = await get_case(uuid.UUID(case_id), (landlord_a, session))
        assert str(detail.id) == case_id
    finally:
        await _cleanup(session, landlord_a_id)
        await _cleanup(session, landlord_b_id)


@pytest.mark.integration
async def test_get_case_full_timeline_oldest_first_interleaved(session: AsyncSession) -> None:
    """Production-shaped seed (senior review on PR #195, B1): ``messages``
    is append-only and its ``case_id`` is ALWAYS ``NULL`` in production
    (the webhook, the sole writer, never sets it — see
    ``app/agent/nodes/identify_case.py``'s module docstring). The only
    durable link is ``message_cases``, so this test seeds through that join
    table instead of the impossible-in-production (and impossible under
    real ``app_role`` — ``messages`` REVOKEs UPDATE) ``UPDATE messages SET
    case_id`` this test used before. Also seeds a pre-case
    ``message_received`` audit_log row (``case_id`` NULL forever,
    correlated only via ``message_cases`` on the shared ``message_id`` —
    the "secondary same-class" finding) to prove that correlation path too.
    """
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        property_id, tenant_id, case_id = await _seed_case(
            session, landlord_id=landlord_id, status="awaiting_approval"
        )

        # `insert_message` never sets `case_id` — it stays NULL, exactly
        # like every real inbound message the webhook ever persists.
        message_id = await factories.insert_message(
            session,
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
            body="No heat since last night!",
        )
        await factories.insert_message_case(session, message_id=message_id, case_id=case_id)

        # Pre-case audit row (app/agent/graph_entry.py's real shape):
        # case_id NULL forever, correlated only via message_cases on the
        # shared message_id in its payload.
        await factories.insert_audit_log(
            session,
            landlord_id=landlord_id,
            case_id=None,
            actor="system",
            action="message_received",
            payload={"message_id": message_id},
        )
        await session.commit()

        await factories.insert_audit_log(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            actor="agent",
            action="classified",
            payload={
                "severity": "urgent",
                "summary": "No heat overnight can't wait, so I treated it as urgent.",
                "rules_fired": ["no_heat_overnight"],
            },
        )
        await session.commit()

        draft_id = await factories.insert_draft(
            session, landlord_id=landlord_id, case_id=case_id, body="Hi Maria — so sorry..."
        )
        await session.commit()

        detail = await get_case(uuid.UUID(case_id), (landlord, session))

        assert detail.property.id == uuid.UUID(property_id)
        assert detail.tenant.id == uuid.UUID(tenant_id)
        assert detail.tenant.name == "Maria"
        assert detail.vendor is None
        assert detail.status == "awaiting_approval"

        # Non-empty timeline is the whole point of this regression: before
        # the message_cases-join fix, a `WHERE case_id = :case_id` query
        # against production-shaped data (case_id always NULL) silently
        # returned zero message/pre-case-audit rows.
        assert detail.timeline, "timeline must not be empty for production-shaped seed data"

        kinds = [entry.kind for entry in detail.timeline]
        assert kinds == ["message", "audit", "audit", "draft"]

        # oldest-first
        ats = [entry.at for entry in detail.timeline]
        assert ats == sorted(ats)

        message_entry = detail.timeline[0]
        assert message_entry.kind == "message"
        assert message_entry.body == "No heat since last night!"  # type: ignore[union-attr]

        received_entry = detail.timeline[1]
        assert received_entry.kind == "audit"
        assert received_entry.action == "message_received"  # type: ignore[union-attr]
        assert received_entry.payload["message_id"] == message_id  # type: ignore[union-attr]

        audit_entry = detail.timeline[2]
        assert audit_entry.kind == "audit"
        assert audit_entry.action == "classified"  # type: ignore[union-attr]
        # payload.summary surfaces automatically — the trivially-resolved
        # contract gap (PR #190 review), see cases.py's module docstring.
        assert audit_entry.payload["summary"] == (  # type: ignore[union-attr]
            "No heat overnight can't wait, so I treated it as urgent."
        )

        draft_entry = detail.timeline[3]
        assert draft_entry.kind == "draft"
        assert str(draft_entry.id) == draft_id  # type: ignore[union-attr]
        assert draft_entry.status == "pending"  # type: ignore[union-attr]
    finally:
        await _cleanup(session, landlord_id)


@pytest.mark.integration
async def test_get_case_with_vendor(session: AsyncSession) -> None:
    landlord_id = await factories.insert_landlord(session)
    landlord = Landlord(id=uuid.UUID(landlord_id))
    try:
        property_id, tenant_id, case_id = await _seed_case(session, landlord_id=landlord_id)
        vendor_id = await factories.insert_vendor(session, landlord_id, trade="hvac")
        await session.execute(
            text("UPDATE cases SET vendor_id = :vendor_id WHERE id = :id"),
            {"vendor_id": vendor_id, "id": case_id},
        )
        await session.commit()

        detail = await get_case(uuid.UUID(case_id), (landlord, session))
        assert detail.vendor is not None
        assert str(detail.vendor.id) == vendor_id
        assert detail.vendor.trade == "hvac"
    finally:
        await _cleanup(session, landlord_id)
