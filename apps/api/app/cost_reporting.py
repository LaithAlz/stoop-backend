"""Cost rollups (#111) — "unit economics must be a query, not a guess"
(``architecture.md`` §9). Cost-per-case / cost-per-door(property) /
cost-per-month, each answerable by ONE query over ``audit_log`` — no view,
no migration (schema-v1.md's v1.12 amendment explains why a view would only
add a migration to keep hand-in-sync with the same small query, not reduce
complexity).

Session-only module — never on the admin-session allowlist
------------------------------------------------------------------------
Every function here takes an ALREADY-OPEN ``AsyncSession`` and only ever
``SELECT``s — mirrors ``app/trust.py``'s own "session-only helper"
convention exactly (see that module's docstring). No entry needed in
``tests/test_migrations_0005.py``'s ``_ADMIN_SESSION_ALLOWLIST``.

Where the cost data comes from
------------------------------------------------------------------------
Two cost "kinds", both living in existing ``audit_log`` payloads (never
``messages`` — its LLM-cost columns are DEPRECATED, v1.6; its
``sms_cost_cents`` column is deliberately still not written either, see
schema-v1.md's v1.12 amendment note 3):

- **``"llm"``** — the ``'classified'`` (``classify_intent``/
  ``classify_severity``, disambiguated by the payload's own ``kind`` key,
  irrelevant here) and ``'drafted'`` (``draft_response``) audit rows each
  carry a top-level ``cost_cents`` key (``app/integrations/anthropic.py``'s
  ``estimate_cost_cents``). Always case-scoped (``audit_log.case_id``).
- **``"sms"``** — the draft-flow ``'sent'`` row (``app/agent/
  draft_sender.py``) carries a top-level ``sms_cost_cents`` key, also
  case-scoped; the emergency safety path's ``'emergency_call_attempt'`` row
  (``app/agent/emergency_chain.py``) carries ``sms_cost_cents`` inside EACH
  entry of its ``actions`` jsonb array (one attempt can fire several SMS/
  call actions) and is NEVER case-scoped (fired before ``identify_case``
  runs) — it carries its own ``property_id`` instead (v1.12 amendment).

Because the emergency-chain cost events have no ``case_id``, they
contribute to the per-door and per-month rollups but NOT the per-case one
(there is structurally no case to attribute them to) — documented, not a
bug: the per-case rollup naturally omits rows whose ``case_id`` column is
``NULL`` on the shared UNION below by construction of the ``WHERE
case_id = :case_id`` filter.

Old rows never crash a rollup
------------------------------------------------------------------------
Every branch of :data:`_COST_EVENTS_CTE_SQL` guards its numeric cast with a
``?`` (jsonb key-existence) check FIRST — a row written before this issue's
payload-key amendments landed (missing the new keys entirely) is excluded
from the UNION outright, never reaches the ``::numeric`` cast, and
therefore never raises. Its contribution reads as the honest ``0`` a
``COALESCE(SUM(...), 0)`` produces over zero matching rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# The shared cost-events CTE — embedded (never executed standalone) into
# each of the three concrete queries below via an f-string, the SAME
# shared-SQL-snippet pattern ``app/agent/draft_sender.py``'s own
# ``_NEWER_INBOUND_EXISTS_SQL`` already established (static, Python-authored
# text only — no caller-controlled input is ever interpolated here).
# ---------------------------------------------------------------------------
_COST_EVENTS_CTE_SQL = """
    SELECT
        a.landlord_id AS landlord_id,
        a.case_id     AS case_id,
        c.property_id AS property_id,
        'llm'::text   AS kind,
        (a.payload ->> 'cost_cents')::numeric AS cost_cents,
        a.created_at  AS created_at
    FROM audit_log a
    LEFT JOIN cases c ON c.id = a.case_id
    WHERE a.action IN ('classified', 'drafted')
      AND a.payload ? 'cost_cents'

    UNION ALL

    SELECT
        a.landlord_id AS landlord_id,
        a.case_id     AS case_id,
        c.property_id AS property_id,
        'sms'::text   AS kind,
        (a.payload ->> 'sms_cost_cents')::numeric AS cost_cents,
        a.created_at  AS created_at
    FROM audit_log a
    LEFT JOIN cases c ON c.id = a.case_id
    WHERE a.action = 'sent'
      AND a.payload ? 'sms_cost_cents'

    UNION ALL

    SELECT
        a.landlord_id AS landlord_id,
        NULL::uuid AS case_id,
        (a.payload ->> 'property_id')::uuid AS property_id,
        'sms'::text AS kind,
        (act ->> 'sms_cost_cents')::numeric AS cost_cents,
        a.created_at AS created_at
    FROM audit_log a
    CROSS JOIN LATERAL jsonb_array_elements(a.payload -> 'actions') act
    WHERE a.action = 'emergency_call_attempt'
      AND jsonb_typeof(a.payload -> 'actions') = 'array'
      AND act ? 'sms_cost_cents'
"""  # noqa: S608 -- static const embedded below via f-string, no user input

_SELECT_COST_PER_CASE_SQL = text(
    f"WITH cost_events AS ({_COST_EVENTS_CTE_SQL}) "  # noqa: S608 -- see note above
    "SELECT kind, COALESCE(SUM(cost_cents), 0) AS cost_cents FROM cost_events "
    "WHERE landlord_id = :landlord_id AND case_id = :case_id GROUP BY kind"
)

_SELECT_COST_PER_PROPERTY_SQL = text(
    f"WITH cost_events AS ({_COST_EVENTS_CTE_SQL}) "  # noqa: S608
    "SELECT kind, COALESCE(SUM(cost_cents), 0) AS cost_cents FROM cost_events "
    "WHERE landlord_id = :landlord_id AND property_id = :property_id GROUP BY kind"
)

_SELECT_COST_PER_MONTH_SQL = text(
    f"WITH cost_events AS ({_COST_EVENTS_CTE_SQL}) "  # noqa: S608
    # UTC pinned explicitly (PR #209 senior review): date_trunc on a bare
    # timestamptz truncates in the SESSION TimeZone GUC, which the app never
    # sets -- a non-UTC Postgres would push boundary-adjacent events into the
    # neighbouring month. AT TIME ZONE 'UTC' makes the bucket deployment-
    # independent (and the returned key a naive-UTC timestamp).
    "SELECT date_trunc('month', created_at AT TIME ZONE 'UTC') AS month, kind, "
    "COALESCE(SUM(cost_cents), 0) AS cost_cents FROM cost_events "
    "WHERE landlord_id = :landlord_id "
    "GROUP BY date_trunc('month', created_at AT TIME ZONE 'UTC'), kind ORDER BY month"
)


@dataclass(frozen=True)
class CostRollup:
    """One grouping key's (a case, a property, ...) cost breakdown."""

    llm_cost_cents: float
    sms_cost_cents: float

    @property
    def total_cost_cents(self) -> float:
        return self.llm_cost_cents + self.sms_cost_cents


@dataclass(frozen=True)
class MonthlyCostRollup:
    """One calendar month's cost breakdown for a landlord."""

    month: datetime
    llm_cost_cents: float
    sms_cost_cents: float

    @property
    def total_cost_cents(self) -> float:
        return self.llm_cost_cents + self.sms_cost_cents


def _rollup_from_kind_rows(rows: list[dict[str, Any]]) -> CostRollup:
    llm_cost_cents = 0.0
    sms_cost_cents = 0.0
    for row in rows:
        cost = float(row["cost_cents"])
        if row["kind"] == "llm":
            llm_cost_cents = cost
        elif row["kind"] == "sms":
            sms_cost_cents = cost
    return CostRollup(llm_cost_cents=llm_cost_cents, sms_cost_cents=sms_cost_cents)


async def cost_per_case(session: AsyncSession, *, landlord_id: UUID, case_id: UUID) -> CostRollup:
    """LLM + SMS cost for one case — ``landlord_id`` scoped (every
    multi-tenant query in this codebase is, per ``apps/api/CLAUDE.md``),
    even though ``case_id`` alone is already globally unique. Emergency-chain
    SMS cost is NOT included here (see module docstring) — only the
    draft-flow ``'sent'`` row's SMS cost and the two LLM-cost audit rows.
    """
    rows = (
        (
            await session.execute(
                _SELECT_COST_PER_CASE_SQL,
                {"landlord_id": str(landlord_id), "case_id": str(case_id)},
            )
        )
        .mappings()
        .all()
    )
    return _rollup_from_kind_rows([dict(row) for row in rows])


async def cost_per_property(
    session: AsyncSession, *, landlord_id: UUID, property_id: UUID
) -> CostRollup:
    """LLM + SMS cost for one door (property) — every case-scoped cost event
    whose case belongs to this property, PLUS every emergency-chain SMS
    action fired for this property (module docstring)."""
    rows = (
        (
            await session.execute(
                _SELECT_COST_PER_PROPERTY_SQL,
                {"landlord_id": str(landlord_id), "property_id": str(property_id)},
            )
        )
        .mappings()
        .all()
    )
    return _rollup_from_kind_rows([dict(row) for row in rows])


async def cost_per_month(session: AsyncSession, *, landlord_id: UUID) -> list[MonthlyCostRollup]:
    """LLM + SMS cost, bucketed by calendar month, for every property this
    landlord has — includes emergency-chain SMS cost (module docstring).
    Ordered oldest month first; a month with zero cost events is simply
    absent (never a zero-filled row) — matching this codebase's "never
    fabricate a data point" convention elsewhere (e.g. ``app/trust.py``'s
    own "a missing row reads as False, never fabricated")."""
    rows = (
        (await session.execute(_SELECT_COST_PER_MONTH_SQL, {"landlord_id": str(landlord_id)}))
        .mappings()
        .all()
    )
    by_month: dict[datetime, dict[str, float]] = {}
    order: list[datetime] = []
    for row in rows:
        month = row["month"]
        if month not in by_month:
            by_month[month] = {"llm": 0.0, "sms": 0.0}
            order.append(month)
        by_month[month][str(row["kind"])] = float(row["cost_cents"])
    return [
        MonthlyCostRollup(
            month=month,
            llm_cost_cents=by_month[month]["llm"],
            sms_cost_cents=by_month[month]["sms"],
        )
        for month in order
    ]


__all__: list[str] = [
    "CostRollup",
    "MonthlyCostRollup",
    "cost_per_case",
    "cost_per_month",
    "cost_per_property",
]
