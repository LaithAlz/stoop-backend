"""Cases read endpoints (#55 — "was 'conversations', renamed per
conversation-model.md").

``GET /v1/cases`` (list, filterable, cursor-paginated) and
``GET /v1/cases/{id}`` (full timeline) per
``docs/03-engineering/api-contracts.md``'s "Cases" section. The
``CaseSummary`` shape is PINNED (2026-07-06 amendment) — every field name
below matches that shape verbatim.

Out of scope for this issue (per the delegating task and
``docs/03-engineering/api-contracts.md``): ``GET /v1/queue`` (#56, depends
on #183), ``POST /v1/cases/{id}/ask-vendor``
(write endpoints, not part of #55's read-only scope), and
``GET /v1/messages`` (issue #55's own acceptance criteria mentions a
"channel view per tenant" endpoint, but ``api-contracts.md`` defines no
shape for it anywhere — PR #190's frontend rebuild already consumes
``GET /v1/cases/{id}``'s timeline as the conversation view instead. Per the
hard rule "never invent a shape that isn't documented," this endpoint is
NOT implemented here; flagged in the PR description as a gap needing a
doc-first decision (either amend api-contracts.md with a real shape, or
confirm the case timeline supersedes it and drop the AC bullet).

``POST /v1/cases/{id}/resolve`` (#206) — landlord-direct resolve, added by
this module below (was documented but had zero implementation and zero
caller — ``app/agent/case_lifecycle.py::resolve_by_landlord`` existed as a
tested pure function nothing ever invoked). See that endpoint's own
docstring and ``docs/03-engineering/api-contracts.md``'s v1.14 amendment
for the full contract (response shape, idempotency precedent, and the
draft-cancellation safety edge).

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

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.case_lifecycle import resolve_by_landlord
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


class ResolveCaseRequest(BaseModel):
    """``docs/03-engineering/api-contracts.md``'s documented body —
    ``reason`` is the only field, and ``"landlord"`` is the only legal
    value on this endpoint (this is the LANDLORD-direct resolve path; the
    other two ``cases.resolved_reason`` values, ``tenant_confirmed`` and
    ``auto_stale``, are written exclusively by ``app/agent/case_lifecycle.
    py``'s ``sweep_cases()`` — never by this router). Defaults to
    ``"landlord"`` so an empty/omitted body still works, matching
    ``app/routers/trust.py``'s ``RevokeTrustRequest`` "optional body"
    convention for a single-field, single-legal-value request."""

    reason: Literal["landlord"] = "landlord"


class ResolveCaseResponse(BaseModel):
    """v1.14 amendment — previously undocumented beyond "→ 200"."""

    status: Literal["resolved"]
    resolved_at: datetime


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
# POST /v1/cases/{id}/resolve SQL (#206)
# ---------------------------------------------------------------------------

# Self-guarding UPDATE — matches app/agent/case_lifecycle.py's own sweep
# discipline ("the guarded UPDATE decides everything, gate side effects on
# rowcount"): `status != 'resolved'` re-asserts, at UPDATE time, that this
# call is the one actually performing the transition. A concurrent repeat
# (double-tap, redelivered request) or a case some OTHER path (sweep_cases)
# already resolved between this handler starting and this statement
# running simply matches zero rows here — the idempotent-200 branch below
# handles that by re-reading the row instead. `pending_resolved_at = NULL`
# and `last_activity_at = :resolved_at` mirror case_lifecycle.py's own two
# UPDATE statements for the SAME "case -> resolved" transition family (a
# tenant-confirmed proposal pending on a case a landlord resolves directly
# right now is moot; the resolve is itself the most recent activity).
_RESOLVE_CASE_SQL = text(
    "UPDATE cases SET status = :new_status, resolved_reason = :resolved_reason, "
    "resolved_at = :resolved_at, pending_resolved_at = NULL, "
    "last_activity_at = :resolved_at, updated_at = :resolved_at "
    "WHERE id = :case_id AND landlord_id = :landlord_id AND status != 'resolved' "
    "RETURNING id, resolved_at"
)

# Only reached when the guarded UPDATE above matched zero rows — distinguishes
# "doesn't exist / cross-tenant" (404) from "already resolved" (idempotent 200).
_SELECT_CASE_RESOLUTION_SQL = text(
    "SELECT id, resolved_at FROM cases WHERE id = :case_id AND landlord_id = :landlord_id"
)

# THE SAFETY EDGE (#206 review): a draft this endpoint's own transaction
# doesn't cancel here could still be claimed and sent by app/agent/
# draft_sender.py's ticker after this case is resolved. `sent_message_id IS
# NULL` is redundant with the status filter today (only a 'sent' row ever
# gets one) but kept explicit as belt-and-braces matching the issue's own
# wording. `status IN ('pending','approved')` deliberately excludes
# 'sending' — see this router's resolve_case docstring "The 'sending' race"
# for why a mid-flight claim is left alone rather than blocked on.
_CANCEL_OPEN_DRAFTS_SQL = text(
    "UPDATE drafts SET status = 'cancelled', updated_at = now() "
    "WHERE case_id = :case_id AND landlord_id = :landlord_id "
    "AND status IN ('pending', 'approved') AND sent_message_id IS NULL "
    "RETURNING id"
)

_INSERT_CASE_RESOLVED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'landlord', :action, CAST(:payload AS jsonb))"
)

# actor='landlord' (not 'agent', unlike draft_sender.py's OWN superseded-
# auto-send cancellation) — this cancellation is a direct, immediate
# consequence of the landlord's own resolve action, exactly like
# app/routers/drafts.py's undo endpoint recording its own send_cancelled
# row as actor='landlord'.
_INSERT_DRAFT_CANCELLED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'landlord', 'send_cancelled', CAST(:payload AS jsonb))"
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


@router.post("/cases/{case_id}/resolve", response_model=ResolveCaseResponse)
async def resolve_case(
    case_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
    payload: ResolveCaseRequest | None = None,
) -> ResolveCaseResponse:
    """``POST /v1/cases/{id}/resolve`` (#206) — the landlord-direct resolve
    path. ``docs/03-engineering/api-contracts.md``'s v1.14 amendment is the
    canonical contract; this docstring covers the implementation decisions
    that amendment summarizes.

    Request body
    ------------
    ``ResolveCaseRequest.reason`` is accepted but never branched on:
    ``"landlord"`` is the only legal value on THIS endpoint (the other two
    ``cases.resolved_reason`` values are written exclusively by
    ``app/agent/case_lifecycle.py``'s ``sweep_cases()``), so the field
    exists only to match the documented request shape — this handler always
    calls :func:`app.agent.case_lifecycle.resolve_by_landlord`, never forks
    its semantics.

    Scoping / not-found
    --------------------
    ``require_landlord`` scopes ``session``; every SQL statement below ALSO
    carries an explicit ``landlord_id = :landlord_id`` predicate (belt-and-
    braces, matching every other router in this package). A missing case
    and a cross-tenant case are indistinguishable from the caller's point of
    view — both 404 ``case_not_found``.

    Idempotency precedent chosen: 200, not 409
    -------------------------------------------
    This codebase has TWO different precedents for "the caller repeated an
    action that already happened": ``POST /v1/drafts/{id}/approve``
    (and ``POST /v1/notifications/{id}/ack``) answer idempotent 200s,
    while ``POST /v1/drafts/{id}/reject`` answers 409 ``already_sent`` when
    a concurrent approve already won. The distinguishing question is
    whether the repeat contradicts something that happened in between:
    reject-after-approve is a genuine conflict (the draft went somewhere
    the reject caller didn't intend). Resolve-after-resolve is not — the
    caller's requested end state ("this case is resolved") is already
    true, regardless of whether IT was the resolver or someone/something
    else (a tenant confirmation, an auto-stale sweep) got there first. This
    handler therefore follows the approve/ack precedent: 200, the case's
    REAL stored ``resolved_at``, no new ``audit_log`` row, no attempt to
    re-run draft cancellation (nothing new transitioned, so nothing new to
    cancel — see ``_RESOLVE_CASE_SQL``'s own comment for the self-guarding
    ``UPDATE ... WHERE status != 'resolved'`` that decides which branch this
    call takes).

    The safety edge — why a draft-only status flip was not enough
    ------------------------------------------------------------------
    A `pending` draft is never a risk on its own (only `approved` rows are
    ever claimed by ``app/agent/draft_sender.py``'s ticker) — but a draft a
    landlord already approved (or the trust ladder auto-approved,
    ``drafts.auto_send = true``) sits `approved`, due, and CLAIMABLE for up
    to ~60s (the sender's own tick cadence) after this endpoint returns,
    unless something cancels it. ``_CANCEL_OPEN_DRAFTS_SQL`` cancels every
    such draft (`status IN ('pending', 'approved')`, `sent_message_id IS
    NULL`) in the SAME transaction as the case ``UPDATE`` above (this
    router never calls ``session.commit()`` mid-handler — ``get_session``'s
    single commit at request teardown is what makes this atomic: another
    caller either sees the FULLY pre-resolve state or the FULLY
    post-resolve-and-cancelled state, never a torn read). One
    ``send_cancelled`` ``audit_log`` row (``actor='landlord'``, payload
    ``{"draft_id": ..., "reason": "case_resolved"}``) is appended per
    cancelled draft — mirrors ``app/routers/drafts.py``'s own ``undo``
    endpoint's ``send_cancelled`` shape, distinguished only by the payload
    ``reason``.

    The 'sending' race — a documented, deliberate non-block
    -----------------------------------------------------------
    A draft the sender ticker has ALREADY claimed (`status = 'sending'`) at
    the moment this transaction runs is left alone on purpose —
    ``_CANCEL_OPEN_DRAFTS_SQL``'s `status IN ('pending', 'approved')` never
    matches it. Two honest alternatives were considered and rejected:
    blocking this request until the in-flight send finishes (this codebase
    never holds a DB transaction open across a Twilio network call —
    ``app/agent/draft_sender.py``'s own "Session discipline" section is
    explicit about this — and there is no cheap way to await a send from
    inside THIS request without re-introducing exactly that anti-pattern);
    or racing to "cancel" a row that may already be an irreversible SMS in
    the provider's hands (a false "cancelled" state would misrepresent a
    message that actually went out). Standard Postgres row-lock semantics
    make the actual outcome race-free regardless of which UPDATE reaches
    the row first: if the sender's claim (`'approved' -> 'sending'`)
    commits first, this cancel's own `WHERE status IN ('pending',
    'approved')` simply no longer matches that row when THIS transaction's
    UPDATE runs; if this cancel commits first, the sender's claim's own
    `WHERE status = 'approved'` no longer matches either. Either way, the
    draft's actual fate (sent, or cancelled) is unambiguous and durably
    recorded — never lost, never double-counted. A claimed-and-completed
    send on a since-resolved case still writes its normal ``'sent'`` audit
    row exactly as it always does; this is a known, accepted outcome, not
    a bug.

    Belt-and-braces: ``app/agent/draft_sender.py``'s claim SQL additionally
    refuses to claim ANY `'approved'` draft whose case has since become
    `resolved` (see that module's own docstring, "Resolved-case guard
    belt-and-braces (#206)") — a second, independent layer protecting
    against any OTHER code path that might ever resolve a case without
    running this endpoint's own cancellation (e.g. ``sweep_cases()``'s
    tenant-confirmed leg, which is NOT excluded from ``awaiting_approval``
    the way its auto-stale leg is — a pre-existing, out-of-scope gap this
    guard also happens to close).

    Emergency chain — untouched by construction
    -----------------------------------------------
    This handler never reads or writes ``notifications``. Verified: the
    entire escalation sweep (``app/agent/emergency_chain.py``) keys off
    ``notifications.status``/``next_attempt_at``/``acknowledged_at`` only —
    no query in that module ever joins ``cases`` or reads ``cases.status``.
    Resolving a case cannot acknowledge, cancel, or delay an emergency
    call/SMS in flight, by construction, not by a special-case guard here.
    """
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    cid = str(case_id)

    transition = resolve_by_landlord(datetime.now(UTC))

    resolved_row = (
        (
            await session.execute(
                _RESOLVE_CASE_SQL,
                {
                    "case_id": cid,
                    "landlord_id": landlord_id,
                    "new_status": transition.new_status,
                    "resolved_reason": transition.resolved_reason,
                    "resolved_at": transition.resolved_at,
                },
            )
        )
        .mappings()
        .one_or_none()
    )

    if resolved_row is None:
        existing = (
            (
                await session.execute(
                    _SELECT_CASE_RESOLUTION_SQL, {"case_id": cid, "landlord_id": landlord_id}
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            raise AppError(status_code=404, code="case_not_found", message="Case not found.")
        # Idempotent repeat (see docstring "Idempotency precedent chosen") —
        # no new writes, no re-attempted draft cancellation.
        return ResolveCaseResponse(status="resolved", resolved_at=existing["resolved_at"])

    await session.execute(
        _INSERT_CASE_RESOLVED_AUDIT_SQL,
        {
            "landlord_id": landlord_id,
            "case_id": cid,
            "action": transition.audit_action,
            "payload": json.dumps({"reason": transition.resolved_reason}),
        },
    )

    cancel_params = {"case_id": cid, "landlord_id": landlord_id}
    cancelled_draft_rows = (
        (await session.execute(_CANCEL_OPEN_DRAFTS_SQL, cancel_params)).mappings().all()
    )
    for draft_row in cancelled_draft_rows:
        await session.execute(
            _INSERT_DRAFT_CANCELLED_AUDIT_SQL,
            {
                "landlord_id": landlord_id,
                "case_id": cid,
                "payload": json.dumps(
                    {"draft_id": str(draft_row["id"]), "reason": "case_resolved"}
                ),
            },
        )

    return ResolveCaseResponse(status="resolved", resolved_at=resolved_row["resolved_at"])
