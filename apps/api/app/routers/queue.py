"""``GET /v1/queue`` (#56) — the approval-queue read, the dashboard's main
screen.

Shape matches ``docs/03-engineering/api-contracts.md``'s "Queue" section
(v1.1 amendments) exactly. Column/table names are ``schema-v1.md``'s,
verbatim.

Which cases populate the queue
-------------------------------
``cases.status = 'awaiting_approval'`` is set by exactly one place in this
codebase (``app/agent/nodes/await_approval.py::mark_awaiting_approval``)
and ONLY when a pending draft genuinely exists for that case — the
stale-draft invariant (``conversation-model.md``, ``uq_drafts_one_pending``
in schema-v1.md) guarantees at most one ``status='pending'`` draft per
case. So "one card per case needing action" (the AC) is exactly: cases in
``awaiting_approval``, INNER JOINed to their one pending draft. This
INNER JOIN is deliberate, not merely an optimization — it also defensively
excludes the (should-never-happen, but see ``await_approval.py``'s own
"draft_id unexpectedly None" note) anomaly of a case sitting in
``awaiting_approval`` with no live pending draft: same "no draft, no card"
philosophy already established there, rather than emitting a card with
nothing to approve.

``awaiting_tenant`` cases are excluded by construction (the status filter
above) — they never reach this predicate at all. ``open``/``reopened``
cases (no ready draft yet — still mid-graph or blocked in degraded mode)
and ``resolved`` cases are excluded the same way.

Where severity/reasoning/``why`` come from — NOT ``cases.severity``
---------------------------------------------------------------------
``cases.severity`` is a real column, but no code path in this codebase
ever writes it (grep confirms — ``classify_severity.py``'s own module
docstring explains why the canonical classification record is the
``audit_log`` ``'classified'`` row instead, not a ``cases``/``messages``
column). Sourcing the queue card's severity from ``cases.severity`` would
therefore return ``null`` for every real card today. This endpoint instead
reads the LATEST ``audit_log`` row with ``action='classified'`` for each
case (a case can be reclassified more than once via the stale-draft
re-run, so "latest" matters) and takes ``severity``, ``rules_fired``
(→ the card's ``reasoning`` array), ``refusal_flags``, and — schema-v1
v1.7, #183 — ``summary`` (→ ``why``, ``null`` for pre-v1.7 rows that
predate the key) all from that ONE row, matching
``docs/03-engineering/api-contracts.md``'s own note that ``reasoning`` and
``why`` "serve different surfaces... from the same payload." This is a
pre-existing gap in the codebase (``cases.severity``/``cases.title`` are
write-once-never columns today), not something this issue's scope covers
fixing — flagged here rather than silently working around it.

``tenant_message``/``received_at``
-----------------------------------
The latest INBOUND, tenant-party message on the case — correlated via
``message_cases`` (OR'd with a direct ``case_id`` match for any future
write path that sets it at insert time), the exact pattern
``app/routers/cases.py``'s ``_SELECT_MESSAGES_SQL`` already established for
the same reason: ``messages.case_id`` is always ``NULL`` in production
(``messages`` is append-only; the webhook, the sole writer, inserts before
case identity is known).

Ordering — computed in Python, not SQL
-----------------------------------------
``GET /v1/queue`` is deliberately UNPAGINATED (api-contracts.md's v1.1
amendments, "Ordering/pagination") — bounded by open cases, not message
volume. That makes it simple and safe to fetch every eligible row in one
query, then sort in Python: emergency-followup → urgent → routine (oldest
first within each tier, per ``conversation-model.md``'s "Approval queue
ordering"), keyed on the correlated tenant message's ``created_at``
(falling back to ``cases.last_activity_at`` on the rare case with no
correlated tenant message at all). A case whose classified severity is
missing or unrecognized sorts after ``routine`` (never fabricated, never
silently dropped from the queue — same "silence is worse" precedent as
``await_approval.py``).

``counts`` — embedded in this SAME response, not a separate endpoint
------------------------------------------------------------------------
Issue #56's own acceptance criteria says "counts endpoint for the 'N OF M'
header" — but ``api-contracts.md``'s actual, doc-first "Queue" shape has
NO separate counts endpoint anywhere; ``counts`` is a field on THIS
response (``{"items": [...], "counts": {...}}``). Implemented as
specified rather than inventing a second endpoint the contract doesn't
define. ``counts.total`` is the number of returned ``items`` (this
endpoint is unpaginated, so that count is exact); ``counts.awaiting_tenant``
is a separate, genuine ``COUNT(*)`` — NOT included in ``total`` per the
contract's own note.

``has_media``/``media_note``
-------------------------------
Always ``false``/``null`` — MMS (#46) hasn't landed, matching the
contract's own "``null`` until MMS lands" note.

Auto-handled feed
--------------------
Deferred per the contract (needs the trust ladder, #60) — not implemented
here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.case_lifecycle import STATUS_AWAITING_APPROVAL, STATUS_AWAITING_TENANT
from app.deps import Landlord, require_landlord

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["queue"])

QueueSeverity = Literal["emergency", "urgent", "routine"]
DraftRecipient = Literal["tenant", "vendor"]

_SEVERITY_RANK: dict[str, int] = {"emergency": 0, "urgent": 1, "routine": 2}
_UNKNOWN_SEVERITY_RANK = 3  # sorts after routine — never fabricated, never dropped.


# ---------------------------------------------------------------------------
# Response models — field names from api-contracts.md's "Queue" section.
# ---------------------------------------------------------------------------


class QueueCard(BaseModel):
    case_id: UUID
    draft_id: UUID
    severity: QueueSeverity | None
    title: str | None
    property_label: str
    tenant_name: str | None
    unit: str | None
    received_at: datetime | None
    tenant_message: str | None
    draft_body: str
    draft_recipient: DraftRecipient
    why: str | None
    reasoning: list[str]
    refusal_flags: list[str]
    has_media: bool
    media_note: str | None


class QueueCounts(BaseModel):
    total: int
    emergency: int
    urgent: int
    routine: int
    awaiting_tenant: int


class QueueResponse(BaseModel):
    items: list[QueueCard]
    counts: QueueCounts


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# One row per case needing action, INNER JOINed to its one pending draft
# (see module docstring — this join IS the "needing action" predicate).
# The two LATERAL subqueries pull the latest correlated tenant message and
# the latest 'classified' audit row per case, without an N+1 round trip
# per case (the pattern app/routers/cases.py uses in a loop instead,
# acceptable there for a single case's timeline, not for a whole queue).
_SELECT_QUEUE_SQL = text(
    """
    SELECT
        c.id AS case_id,
        c.title,
        c.last_activity_at,
        p.label AS property_label,
        t.name AS tenant_name,
        t.unit,
        d.id AS draft_id,
        d.body AS draft_body,
        d.recipient AS draft_recipient,
        msg.body AS tenant_message_body,
        msg.created_at AS tenant_message_at,
        audit.payload AS classified_payload
    FROM cases c
    JOIN properties p ON p.id = c.property_id
    JOIN tenants t ON t.id = c.tenant_id
    JOIN drafts d ON d.case_id = c.id AND d.status = 'pending' AND d.landlord_id = :landlord_id
    LEFT JOIN LATERAL (
        SELECT m.body, m.created_at
        FROM messages m
        WHERE m.party = 'tenant'
          AND m.direction = 'inbound'
          AND (
            m.case_id = c.id
            OR EXISTS (
              SELECT 1 FROM message_cases mc
              WHERE mc.message_id = m.id AND mc.case_id = c.id
            )
          )
        ORDER BY m.created_at DESC
        LIMIT 1
    ) msg ON true
    LEFT JOIN LATERAL (
        SELECT a.payload
        FROM audit_log a
        WHERE a.case_id = c.id AND a.action = 'classified'
        ORDER BY a.created_at DESC
        LIMIT 1
    ) audit ON true
    WHERE c.landlord_id = :landlord_id AND c.status = :awaiting_approval_status
    """
)

_SELECT_AWAITING_TENANT_COUNT_SQL = text(
    "SELECT COUNT(*) FROM cases "
    "WHERE landlord_id = :landlord_id AND status = :awaiting_tenant_status"
)


def _severity_from_payload(payload: dict[str, Any] | None) -> QueueSeverity | None:
    """Defensive parse (never fabricates, never raises): a missing payload,
    a missing ``severity`` key, or a value outside the vocabulary all read
    as ``None`` rather than crashing the endpoint or inventing a value."""
    if payload is None:
        return None
    value = payload.get("severity")
    if isinstance(value, str) and value in _SEVERITY_RANK:
        return cast(QueueSeverity, value)
    return None


def _row_to_card(row: RowMapping) -> QueueCard:
    payload: dict[str, Any] | None = row["classified_payload"]
    severity = _severity_from_payload(payload)
    why = payload.get("summary") if payload is not None else None
    reasoning = payload.get("rules_fired") if payload is not None else None
    refusal_flags = payload.get("refusal_flags") if payload is not None else None

    return QueueCard(
        case_id=row["case_id"],
        draft_id=row["draft_id"],
        severity=severity,
        title=row["title"],
        property_label=row["property_label"],
        tenant_name=row["tenant_name"],
        unit=row["unit"],
        received_at=row["tenant_message_at"],
        tenant_message=row["tenant_message_body"],
        draft_body=row["draft_body"],
        draft_recipient=row["draft_recipient"],
        why=why if isinstance(why, str) else None,
        reasoning=list(reasoning) if isinstance(reasoning, list) else [],
        refusal_flags=list(refusal_flags) if isinstance(refusal_flags, list) else [],
        has_media=False,
        media_note=None,
    )


def _sort_key(row: RowMapping) -> tuple[int, datetime]:
    severity = _severity_from_payload(row["classified_payload"])
    rank = _SEVERITY_RANK[severity] if severity is not None else _UNKNOWN_SEVERITY_RANK
    order_at: datetime = row["tenant_message_at"] or row["last_activity_at"]
    return (rank, order_at)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/queue", response_model=QueueResponse)
async def get_queue(
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> QueueResponse:
    """One card per case needing action, ordered emergency-followup ->
    urgent (oldest first) -> routine (oldest first); see module docstring
    for the full predicate/ordering/source rationale."""
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)

    rows = (
        (
            await session.execute(
                _SELECT_QUEUE_SQL,
                {
                    "landlord_id": landlord_id,
                    "awaiting_approval_status": STATUS_AWAITING_APPROVAL,
                },
            )
        )
        .mappings()
        .all()
    )
    ordered_rows = sorted(rows, key=_sort_key)

    items = [_row_to_card(row) for row in ordered_rows]

    counts = QueueCounts(
        total=len(items),
        emergency=sum(1 for item in items if item.severity == "emergency"),
        urgent=sum(1 for item in items if item.severity == "urgent"),
        routine=sum(1 for item in items if item.severity == "routine"),
        awaiting_tenant=(
            await session.execute(
                _SELECT_AWAITING_TENANT_COUNT_SQL,
                {"landlord_id": landlord_id, "awaiting_tenant_status": STATUS_AWAITING_TENANT},
            )
        ).scalar_one(),
    )

    return QueueResponse(items=items, counts=counts)


__all__: list[str] = ["router"]
