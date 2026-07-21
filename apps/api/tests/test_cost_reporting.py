"""Integration tests for ``app/cost_reporting.py`` (#111) — cost-per-case/
per-door(property)/per-month rollups over ``audit_log``. Every row is
seeded directly (``tests/factories.py``'s ``insert_audit_log`` plus one
local raw-SQL helper for backdated ``created_at`` values) — no graph/agent
code runs here, this module only exercises the READ side.

Marker: ``integration`` — requires a running Postgres instance + ``alembic
upgrade head``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
from app.cost_reporting import cost_per_case, cost_per_month, cost_per_property
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
    await session.execute(text("DELETE FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM tenants WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM properties WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


async def _insert_audit_log_at(
    session: AsyncSession,
    *,
    landlord_id: str,
    case_id: str | None,
    action: str,
    payload: dict[str, object],
    created_at: datetime,
) -> None:
    """Like ``factories.insert_audit_log`` but with an explicit
    ``created_at`` — needed to exercise the per-month bucketing without
    waiting real time. A plain INSERT with a caller-chosen timestamp is not
    an append-only violation (rule #2 only forbids UPDATE/DELETE)."""
    await session.execute(
        text(
            "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload, created_at) "
            "VALUES (:landlord_id, :case_id, 'agent', :action, CAST(:payload AS jsonb), "
            ":created_at)"
        ),
        {
            "landlord_id": landlord_id,
            "case_id": case_id,
            "action": action,
            "payload": json.dumps(payload),
            "created_at": created_at,
        },
    )
    await session.commit()


async def _seed_case(session: AsyncSession) -> tuple[str, str, str]:
    """Returns ``(landlord_id, property_id, case_id)``."""
    landlord_id = await factories.insert_landlord(session)
    property_id = await factories.insert_property(session, landlord_id)
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id)
    case_id = await factories.insert_case(
        session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    return landlord_id, property_id, case_id


@pytest.mark.integration
async def test_cost_per_case_sums_llm_and_sms_costs(db_session: AsyncSession) -> None:
    landlord_id, _property_id, case_id = await _seed_case(db_session)
    try:
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="classified",
            payload={"kind": "intent", "cost_cents": 0.5},
        )
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="classified",
            payload={"cost_cents": 1.5},
        )
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="drafted",
            payload={"cost_cents": 2.0},
        )
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="sent",
            payload={"segments": 1, "sms_cost_cents": 0.75},
        )
        # Noise: a non-cost audit row on the SAME case must never contribute.
        await factories.insert_audit_log(
            db_session, landlord_id=landlord_id, case_id=case_id, action="approved", payload={}
        )

        rollup = await cost_per_case(
            db_session, landlord_id=uuid.UUID(landlord_id), case_id=uuid.UUID(case_id)
        )
        assert rollup.llm_cost_cents == pytest.approx(4.0)
        assert rollup.sms_cost_cents == pytest.approx(0.75)
        assert rollup.total_cost_cents == pytest.approx(4.75)
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_cost_per_case_missing_cost_key_reads_as_zero_never_crashes(
    db_session: AsyncSession,
) -> None:
    landlord_id, _property_id, case_id = await _seed_case(db_session)
    try:
        # Simulates a pre-#111 row: 'classified' but no cost_cents key at all.
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="classified",
            payload={"severity": "urgent"},
        )
        rollup = await cost_per_case(
            db_session, landlord_id=uuid.UUID(landlord_id), case_id=uuid.UUID(case_id)
        )
        assert rollup.llm_cost_cents == 0.0
        assert rollup.sms_cost_cents == 0.0
        assert rollup.total_cost_cents == 0.0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_cost_per_case_counts_degraded_mode_cost_cents(db_session: AsyncSession) -> None:
    """#208 (schema-v1.md v1.14): a 'degraded_mode' audit row that carries
    cost_cents (classify_severity's failed-attempt usage, folded in by
    app/agent/nodes/degraded_mode.py) counts as 'llm' cost, exactly like a
    'classified'/'drafted' row -- the new CTE branch this issue added."""
    landlord_id, _property_id, case_id = await _seed_case(db_session)
    try:
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="degraded_mode",
            payload={
                "reasons": ["classification_failed"],
                "leg": "queued_for_retry",
                "model": "claude-sonnet-5",
                "tokens_in": 400,
                "tokens_out": 120,
                "cost_cents": 1.98,
            },
        )
        # Noise: a 'degraded_mode' row with NO cost_cents key (the common
        # case -- neither failed attempt ever reached the API) must read
        # as zero, never crash.
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="degraded_mode",
            payload={"reasons": ["classification_failed"], "leg": "queued_for_retry"},
        )

        rollup = await cost_per_case(
            db_session, landlord_id=uuid.UUID(landlord_id), case_id=uuid.UUID(case_id)
        )
        assert rollup.llm_cost_cents == pytest.approx(1.98)
        assert rollup.sms_cost_cents == 0.0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_cost_per_property_and_month_count_degraded_mode_cost_cents(
    db_session: AsyncSession,
) -> None:
    landlord_id, property_id, case_id = await _seed_case(db_session)
    try:
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="degraded_mode",
            payload={"reasons": ["classification_failed"], "cost_cents": 0.42},
        )

        property_rollup = await cost_per_property(
            db_session, landlord_id=uuid.UUID(landlord_id), property_id=uuid.UUID(property_id)
        )
        assert property_rollup.llm_cost_cents == pytest.approx(0.42)

        month_rollups = await cost_per_month(db_session, landlord_id=uuid.UUID(landlord_id))
        assert len(month_rollups) == 1
        assert month_rollups[0].llm_cost_cents == pytest.approx(0.42)
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_cost_per_case_scoped_by_landlord_id(db_session: AsyncSession) -> None:
    landlord_id, _property_id, case_id = await _seed_case(db_session)
    other_landlord_id = await factories.insert_landlord(db_session)
    try:
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="drafted",
            payload={"cost_cents": 9.0},
        )
        # Wrong landlord_id for a real case_id -- must read as zero, never
        # leak another landlord's cost (every multi-tenant query in this
        # codebase is landlord_id-scoped, apps/api/CLAUDE.md).
        rollup = await cost_per_case(
            db_session, landlord_id=uuid.UUID(other_landlord_id), case_id=uuid.UUID(case_id)
        )
        assert rollup.total_cost_cents == 0.0
    finally:
        await _cleanup(db_session, landlord_id)
        await db_session.execute(
            text("DELETE FROM landlords WHERE id = :lid"), {"lid": other_landlord_id}
        )
        await db_session.commit()


@pytest.mark.integration
async def test_cost_per_property_includes_emergency_chain_sms_cost(
    db_session: AsyncSession,
) -> None:
    landlord_id, property_id, case_id = await _seed_case(db_session)
    try:
        # Case-scoped draft-flow SMS cost.
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="sent",
            payload={"segments": 1, "sms_cost_cents": 0.75},
        )
        # Emergency-chain SMS cost -- NEVER case-scoped (fires before
        # identify_case runs), carries property_id at the top level and
        # per-action sms_cost_cents nested inside `actions`.
        await factories.insert_audit_log(
            db_session,
            landlord_id=landlord_id,
            case_id=None,
            action="emergency_call_attempt",
            payload={
                "notification_id": str(uuid.uuid4()),
                "message_id": str(uuid.uuid4()),
                "property_id": property_id,
                "step": 0,
                "actions": [
                    {"action": "landlord_call", "status": "sent", "sid": "CA1"},
                    {
                        "action": "tenant_safety_sms",
                        "status": "sent",
                        "sid": "SM1",
                        "segments": 2,
                        "sms_cost_cents": 1.5,
                    },
                ],
            },
        )

        property_rollup = await cost_per_property(
            db_session, landlord_id=uuid.UUID(landlord_id), property_id=uuid.UUID(property_id)
        )
        assert property_rollup.sms_cost_cents == pytest.approx(0.75 + 1.5)

        # The emergency row has no case_id -- it must NOT show up in the
        # per-case rollup, only the draft-flow 'sent' row's cost does.
        case_rollup = await cost_per_case(
            db_session, landlord_id=uuid.UUID(landlord_id), case_id=uuid.UUID(case_id)
        )
        assert case_rollup.sms_cost_cents == pytest.approx(0.75)
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_cost_per_month_buckets_by_calendar_month(db_session: AsyncSession) -> None:
    landlord_id, _property_id, case_id = await _seed_case(db_session)
    try:
        this_month = datetime(2026, 7, 15, tzinfo=UTC)
        last_month = datetime(2026, 6, 10, tzinfo=UTC)
        await _insert_audit_log_at(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="drafted",
            payload={"cost_cents": 1.0},
            created_at=this_month,
        )
        await _insert_audit_log_at(
            db_session,
            landlord_id=landlord_id,
            case_id=case_id,
            action="drafted",
            payload={"cost_cents": 3.0},
            created_at=last_month,
        )

        rollups = await cost_per_month(db_session, landlord_id=uuid.UUID(landlord_id))
        by_month = {(r.month.year, r.month.month): r for r in rollups}
        assert by_month[(2026, 7)].llm_cost_cents == pytest.approx(1.0)
        assert by_month[(2026, 6)].llm_cost_cents == pytest.approx(3.0)
        # Ordered oldest-first.
        assert [r.month for r in rollups] == sorted(r.month for r in rollups)
    finally:
        await _cleanup(db_session, landlord_id)
