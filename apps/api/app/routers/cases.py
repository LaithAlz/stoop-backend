"""Cases read endpoints (#55 — "was 'conversations', renamed per
conversation-model.md").

``GET /v1/cases`` (list, filterable, cursor-paginated) and
``GET /v1/cases/{id}`` (full timeline) per
``docs/03-engineering/api-contracts.md``'s "Cases" section. The
``CaseSummary`` shape is PINNED (2026-07-06 amendment) — every field name
below matches that shape verbatim.

Out of scope for this issue (per the delegating task and
``docs/03-engineering/api-contracts.md``): ``GET /v1/queue`` (#56, depends
on #183), ``POST /v1/cases/{id}/resolve``, ``POST /v1/cases/{id}/ask-vendor``
(write endpoints, not part of #55's read-only scope), and
``GET /v1/messages`` (issue #55's own acceptance criteria mentions a
"channel view per tenant" endpoint, but ``api-contracts.md`` defines no
shape for it anywhere — PR #190's frontend rebuild already consumes
``GET /v1/cases/{id}``'s timeline as the conversation view instead. Per the
hard rule "never invent a shape that isn't documented," this endpoint is
NOT implemented here; flagged in the PR description as a gap needing a
doc-first decision (either amend api-contracts.md with a real shape, or
confirm the case timeline supersedes it and drop the AC bullet).

Timeline contract-gap follow-ups (PR #190 review) addressed here:
- **draft entries need ids** — resolved: ``DraftTimelineEntry.id`` added
  (additive field, proposed as a same-PR api-contracts.md amendment).
- **``payload.summary`` surfacing** — resolved for free: the audit timeline
  entry passes through the full, unmodified ``audit_log.payload`` JSONB
  column rather than reconstructing a narrower shape, so ``summary`` (once
  ``classify_severity`` writes it, schema-v1 v1.7) surfaces automatically.
- **media captions** — NOT resolved here: ``messages.media`` is
  ``[{url, content_type}]`` (schema-v1.md) with no caption sub-key, and
  captioning is MMS-pipeline work (#46) that hasn't landed. Flagged as a
  genuine gap, not trivially resolvable without inventing an undocumented
  JSON key.

**BLOCKING fix (senior review on PR #195): ``messages.case_id`` is ALWAYS
NULL in production.** ``messages`` is append-only (never-break rule #2);
the webhook (the sole writer) always inserts ``case_id = NULL`` because
case identity isn't known at insert time, and no later UPDATE is ever
possible — ``app/agent/nodes/identify_case.py``'s own module docstring
("``messages`` is append-only — case linkage goes through
``message_cases``") is explicit that the durable link is the
``message_cases (message_id, case_id)`` join table, never
``messages.case_id`` itself. The timeline's messages query below
therefore joins through ``message_cases`` (the exact pattern
``app/agent/degraded_mode_sweep.py``'s ``_SELECT_NEWER_INBOUND_EXISTS_SQL``
already uses), OR'd with a direct ``m.case_id = :case_id`` match for any
future write path that ever inserts a message with ``case_id`` already
known (e.g. a same-transaction outbound send). Same story for two
"pre-case" ``audit_log`` action types that are ALSO ``case_id``-NULL
forever by construction (``message_received`` —
``app/agent/graph_entry.py`` — and ``emergency_triggered`` —
``app/routers/webhooks/twilio.py`` — both fire before ``identify_case``
ever assigns a case): both carry ``payload->>'message_id'``, so they are
correlated the same way, via ``message_cases`` on that shared
``message_id``. Every OTHER ``audit_log`` action this codebase writes
(``classified``, ``drafted``, ``draft_stale``, ``degraded_mode``, the
``case_lifecycle`` actions) sets ``case_id`` directly at insert time (a
case already exists by the time they run) and needs no such join.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Landlord, require_landlord
from app.errors import AppError
from app.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursorError,
    decode_cursor,
    paginate_rows,
)

router = APIRouter(prefix="/v1", tags=["cases"])

CaseStatus = Literal["open", "awaiting_approval", "awaiting_tenant", "resolved", "reopened"]
CaseSeverity = Literal["emergency", "urgent", "routine"]
DraftStatus = Literal["pending", "stale", "approved", "sending", "sent", "rejected", "cancelled"]
MessageDirection = Literal["inbound", "outbound"]
MessageParty = Literal["tenant", "vendor", "landlord"]
AuditActor = Literal["agent", "landlord", "system", "prefilter"]

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CaseSummary(BaseModel):
    """PINNED shape (2026-07-06 amendment) — every case-list read uses this."""

    id: UUID
    title: str | None
    status: CaseStatus
    severity: CaseSeverity | None
    tenant_name: str | None
    unit: str | None
    property_label: str
    last_activity_at: datetime


class CaseListResponse(BaseModel):
    items: list[CaseSummary]
    next_cursor: str | None


class PropertyRef(BaseModel):
    id: UUID
    label: str
    address_line1: str
    city: str


class TenantRef(BaseModel):
    id: UUID
    name: str | None
    phone: str
    unit: str | None
    vulnerable_occupant: Literal["infant", "elderly", "medical_device"] | None


class VendorRef(BaseModel):
    id: UUID
    name: str
    trade: str
    phone: str


class MessageTimelineEntry(BaseModel):
    kind: Literal["message"] = "message"
    direction: MessageDirection
    party: MessageParty
    body: str
    media: list[dict[str, Any]]
    at: datetime


class AuditTimelineEntry(BaseModel):
    kind: Literal["audit"] = "audit"
    actor: AuditActor
    action: str
    payload: dict[str, Any]
    at: datetime


class DraftTimelineEntry(BaseModel):
    kind: Literal["draft"] = "draft"
    id: UUID
    status: DraftStatus
    body: str
    at: datetime


_TimelineEntryUnion = MessageTimelineEntry | AuditTimelineEntry | DraftTimelineEntry

TimelineEntry = Annotated[_TimelineEntryUnion, Field(discriminator="kind")]


class CaseDetailResponse(BaseModel):
    id: UUID
    status: CaseStatus
    severity: CaseSeverity | None
    title: str | None
    property: PropertyRef
    tenant: TenantRef
    vendor: VendorRef | None
    opened_at: datetime
    resolved_at: datetime | None
    timeline: list[TimelineEntry]


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SELECT_CASE_SQL = text(
    """
    SELECT id, status, severity, title, property_id, tenant_id, vendor_id,
           created_at AS opened_at, resolved_at
    FROM cases WHERE id = :id AND landlord_id = :landlord_id
    """
)

_SELECT_PROPERTY_REF_SQL = text(
    "SELECT id, label, address_line1, city FROM properties "
    "WHERE id = :id AND landlord_id = :landlord_id"
)

_SELECT_TENANT_REF_SQL = text(
    "SELECT id, name, phone, unit, vulnerable_occupant FROM tenants "
    "WHERE id = :id AND landlord_id = :landlord_id"
)

_SELECT_VENDOR_REF_SQL = text(
    "SELECT id, name, trade, phone FROM vendors WHERE id = :id AND landlord_id = :landlord_id"
)

# All three carry their own `landlord_id` column directly (schema-v1.md) —
# explicitly scoped here too (belt-and-braces per apps/api/CLAUDE.md's
# "every multi-tenant query scoped by landlord_id" convention), so these
# stay safe even if ever reused/called without get_case's own upstream
# ownership check on the `cases` row (spec review, #54/#55/#57).
#
# `messages.case_id = :case_id` is OR'd in for any future write path that
# ever inserts a message with `case_id` already known (module docstring
# "BLOCKING fix") — production rows today all have `case_id IS NULL` and
# match only via the `message_cases` EXISTS clause instead.
_SELECT_MESSAGES_SQL = text(
    """
    SELECT m.direction, m.party, m.body, m.media, m.created_at
    FROM messages m
    WHERE m.landlord_id = :landlord_id
      AND (
        m.case_id = :case_id
        OR EXISTS (
          SELECT 1 FROM message_cases mc
          WHERE mc.message_id = m.id AND mc.case_id = :case_id
        )
      )
    ORDER BY m.created_at ASC
    """
)

# `audit_log.case_id = :case_id` covers every action written WITH a case
# already known (classified/drafted/draft_stale/degraded_mode/case_
# lifecycle actions). The EXISTS clause covers the two "pre-case" action
# types that are case_id-NULL forever by construction (module docstring):
# `message_received` (app/agent/graph_entry.py) and `emergency_triggered`
# (app/routers/webhooks/twilio.py), both of which stash `message_id` in
# their own payload instead.
#
# `ORDER BY ... a.id ASC` (#61 gap-fill): a single inbound message can
# drive several `audit_log` INSERTs in quick succession across separate
# short-lived admin-session transactions (message_received, case_opened,
# two `classified` rows, drafted -- each its own node/transaction/`now()`
# read) -- close enough in wall-clock time that `created_at` alone (a
# timestamptz, not guaranteed sub-microsecond-unique) is not a reliable
# total order. `a.id` is the table's own `GENERATED ALWAYS AS IDENTITY`
# column (schema-v1.md) -- monotonic in INSERT order by construction --
# so it is the correct, free tiebreaker for genuine ties, matching the
# same table's own `ORDER BY id` convention already used elsewhere in this
# codebase (e.g. tests/test_e2e_core_loop.py's audit-sequence assertion).
# Never changes the order for two rows that already have distinct
# `created_at` values.
_SELECT_AUDIT_SQL = text(
    """
    SELECT a.actor, a.action, a.payload, a.created_at, a.id
    FROM audit_log a
    WHERE a.landlord_id = :landlord_id
      AND (
        a.case_id = :case_id
        OR EXISTS (
          SELECT 1 FROM message_cases mc
          WHERE mc.case_id = :case_id
            AND mc.message_id::text = a.payload ->> 'message_id'
        )
      )
    ORDER BY a.created_at ASC, a.id ASC
    """
)

_SELECT_DRAFTS_SQL = text(
    "SELECT id, status, body, created_at FROM drafts "
    "WHERE case_id = :case_id AND landlord_id = :landlord_id ORDER BY created_at ASC"
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/cases", response_model=CaseListResponse)
async def list_cases(
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
    status: CaseStatus | None = None,
    severity: CaseSeverity | None = None,
    property_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> CaseListResponse:
    """Newest-activity-first, cursor-paginated case list."""
    landlord, session = landlord_and_session

    params: dict[str, Any] = {"landlord_id": str(landlord.id), "limit_plus_one": limit + 1}
    predicates = ["c.landlord_id = :landlord_id"]

    if status is not None:
        predicates.append("c.status = :status")
        params["status"] = status
    if severity is not None:
        predicates.append("c.severity = :severity")
        params["severity"] = severity
    if property_id is not None:
        predicates.append("c.property_id = :property_id")
        params["property_id"] = str(property_id)

    if cursor is not None:
        try:
            cursor_at, cursor_id = decode_cursor(cursor)
        except InvalidCursorError as exc:
            raise AppError(
                status_code=400, code="invalid_cursor", message="The cursor is invalid."
            ) from exc
        params["cursor_at"] = cursor_at
        params["cursor_id"] = cursor_id
        predicates.append("(c.last_activity_at, c.id) < (:cursor_at, CAST(:cursor_id AS uuid))")

    where_clause = " AND ".join(predicates)
    sql = text(
        "SELECT c.id, c.title, c.status, c.severity, t.name AS tenant_name, t.unit, "  # noqa: S608
        "p.label AS property_label, c.last_activity_at "
        "FROM cases c "
        "JOIN tenants t ON t.id = c.tenant_id "
        "JOIN properties p ON p.id = c.property_id "
        f"WHERE {where_clause} "
        "ORDER BY c.last_activity_at DESC, c.id DESC "
        "LIMIT :limit_plus_one"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    page, next_cursor = paginate_rows(rows, limit=limit, order_column="last_activity_at")

    items = [
        CaseSummary(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            severity=row["severity"],
            tenant_name=row["tenant_name"],
            unit=row["unit"],
            property_label=row["property_label"],
            last_activity_at=row["last_activity_at"],
        )
        for row in page
    ]
    return CaseListResponse(items=items, next_cursor=next_cursor)


@router.get("/cases/{case_id}", response_model=CaseDetailResponse)
async def get_case(
    case_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> CaseDetailResponse:
    """Full case detail: property/tenant/vendor refs + the oldest-first,
    interleaved messages/audit/draft timeline (the one documented exception
    to the newest-first list convention — api-contracts.md's Cases section)."""
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    cid = str(case_id)

    case_row = (
        (await session.execute(_SELECT_CASE_SQL, {"id": cid, "landlord_id": landlord_id}))
        .mappings()
        .one_or_none()
    )
    if case_row is None:
        raise AppError(status_code=404, code="case_not_found", message="Case not found.")

    property_row = (
        (
            await session.execute(
                _SELECT_PROPERTY_REF_SQL,
                {"id": str(case_row["property_id"]), "landlord_id": landlord_id},
            )
        )
        .mappings()
        .one()
    )
    tenant_row = (
        (
            await session.execute(
                _SELECT_TENANT_REF_SQL,
                {"id": str(case_row["tenant_id"]), "landlord_id": landlord_id},
            )
        )
        .mappings()
        .one()
    )
    vendor_ref: VendorRef | None = None
    if case_row["vendor_id"] is not None:
        vendor_row = (
            (
                await session.execute(
                    _SELECT_VENDOR_REF_SQL,
                    {"id": str(case_row["vendor_id"]), "landlord_id": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        vendor_ref = VendorRef(
            id=vendor_row["id"],
            name=vendor_row["name"],
            trade=vendor_row["trade"],
            phone=vendor_row["phone"],
        )

    timeline_params = {"case_id": cid, "landlord_id": landlord_id}
    message_rows = (await session.execute(_SELECT_MESSAGES_SQL, timeline_params)).mappings().all()
    audit_rows = (await session.execute(_SELECT_AUDIT_SQL, timeline_params)).mappings().all()
    draft_rows = (await session.execute(_SELECT_DRAFTS_SQL, timeline_params)).mappings().all()

    timeline: list[tuple[datetime, int, _TimelineEntryUnion]] = []
    for m in message_rows:
        message_entry: _TimelineEntryUnion = MessageTimelineEntry(
            direction=m["direction"],
            party=m["party"],
            body=m["body"],
            media=m["media"] or [],
            at=m["created_at"],
        )
        timeline.append((m["created_at"], 0, message_entry))
    for a in audit_rows:
        audit_entry: _TimelineEntryUnion = AuditTimelineEntry(
            actor=a["actor"], action=a["action"], payload=a["payload"] or {}, at=a["created_at"]
        )
        timeline.append((a["created_at"], 1, audit_entry))
    for d in draft_rows:
        draft_entry: _TimelineEntryUnion = DraftTimelineEntry(
            id=d["id"], status=d["status"], body=d["body"], at=d["created_at"]
        )
        timeline.append((d["created_at"], 2, draft_entry))

    timeline.sort(key=lambda item: (item[0], item[1]))

    return CaseDetailResponse(
        id=case_row["id"],
        status=case_row["status"],
        severity=case_row["severity"],
        title=case_row["title"],
        property=PropertyRef(
            id=property_row["id"],
            label=property_row["label"],
            address_line1=property_row["address_line1"],
            city=property_row["city"],
        ),
        tenant=TenantRef(
            id=tenant_row["id"],
            name=tenant_row["name"],
            phone=tenant_row["phone"],
            unit=tenant_row["unit"],
            vulnerable_occupant=tenant_row["vulnerable_occupant"],
        ),
        vendor=vendor_ref,
        opened_at=case_row["opened_at"],
        resolved_at=case_row["resolved_at"],
        timeline=[entry for _, _, entry in timeline],
    )
