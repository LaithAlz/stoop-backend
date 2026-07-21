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

Where severity/reasoning/``why`` come from — STILL NOT ``cases.severity``
---------------------------------------------------------------------------
**#197 update:** ``classify_severity.py`` now writes the post-clamp
severity to ``cases.severity`` (never downgrading a case away from
``'emergency'`` — see that module's own docstring). This endpoint
deliberately keeps sourcing severity from the ``audit_log`` row anyway,
NOT switched to ``cases.severity`` in that same change: the card also
needs ``rules_fired`` (→ ``reasoning``), ``refusal_flags``, and — schema-v1
v1.7, #183 — ``summary`` (→ ``why``), none of which live on ``cases`` at
all, only on the classified audit payload. Reading severity from a SECOND
place (``cases.severity``) while every other field on the same card still
comes from the audit row would risk the two disagreeing on some future
edge case (e.g. a reclassification race) for zero benefit — one row, one
source of truth, for this endpoint. This endpoint instead reads the LATEST
``audit_log`` row with ``action='classified'`` for each case (a case can be
reclassified more than once via the stale-draft re-run, so "latest"
matters) and takes ``severity``, ``rules_fired``, ``refusal_flags``, and
``summary`` all from that ONE row, matching
``docs/03-engineering/api-contracts.md``'s own note that ``reasoning`` and
``why`` "serve different surfaces... from the same payload." Revisiting
this (reading ``cases.severity`` directly, now that it's populated, and
dropping the per-card audit lookup) is a plausible FOLLOW-UP, not done here
— out of #197's scope (that issue's own recommended shape explicitly keeps
the read side unchanged) and not needed to unblock #60's trust ladder,
which reads ``trust_metrics``, not this endpoint. ``cases.title`` remains
unwritten (deferred, same as before #197).

**"Latest 'classified' row" must be severity-bearing, not just latest
(#208 spec review, MAJOR-2)** — ``classify_intent.py`` also writes
``action='classified'`` rows (disambiguated only by the payload's own
``kind`` key: ``'intent'`` on success, ``'intent_classification_failed'``
on total failure, #208), and a case is reclassified via the same
stale-draft re-run that can reorder which node's row is newest. The
LATERAL subquery above was previously "latest 'classified' row, full
stop" — kind-blind — so an intent row winning "latest" over a genuine
severity row would blank the card's severity chip (``_severity_from_
payload`` correctly returns ``None`` for a payload with no ``severity``
key, sorting it to "unknown" per this module's own "never fabricated,
never dropped" convention — a safe-direction bug, never a crash, but
still a wrong/missing chip on an otherwise-known-severity case). Fixed by
adding ``AND (a.payload ? 'severity')`` to the LATERAL's ``WHERE`` — the
card only ever wants the latest row that actually carries a severity
classification; every OTHER field this endpoint reads from the SAME row
(``rules_fired``, ``refusal_flags``, ``summary``) only ever exists
alongside ``severity`` on a ``classify_severity.py``-written row anyway,
so this predicate doesn't exclude anything the card actually consumes.
This also closes the identical, PRE-EXISTING shadow from a successful
``kind='intent'`` row (which already carried its own, differently
-scoped ``summary`` key that could have won "latest" and silently
overwritten the severity row's ``why`` even before #208 — not introduced
by #208, just found and fixed alongside it).

``tenant_message``/``received_at``
-----------------------------------
The latest INBOUND, tenant-party message on the case — correlated via
``message_cases`` (OR'd with a direct ``case_id`` match for any future
write path that sets it at insert time), the exact pattern
``app/routers/cases.py``'s ``_SELECT_MESSAGES_SQL`` already established for
the same reason: ``messages.case_id`` is always ``NULL`` in production
(``messages`` is append-only; the webhook, the sole writer, inserts before
case identity is known). Both this LATERAL subquery and the 'classified'
``audit_log`` one below carry an explicit ``landlord_id = :landlord_id``
predicate (safety review, belt-and-braces) — transitively redundant given
the outer ``c.id`` correlation already scopes to a case that's already
landlord-filtered, but matching ``app/routers/cases.py``'s own convention
(``_SELECT_MESSAGES_SQL``/``_SELECT_AUDIT_SQL``, lines ~220/243) of never
relying on transitivity alone for a cross-tenant-sensitive query, so these
stay correct even with RLS off.

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

``notification_id`` (#213 — wiring the emergency ack path for clients)
------------------------------------------------------------------------
api-contracts.md's v1.15 amendment. Populated only when the card's case
has an unacknowledged ``emergency_call`` notification
(``type = 'emergency_call' AND acknowledged_at IS NULL``); ``null``
otherwise (no such notification ever existed, or it's already
acknowledged). A third LATERAL, same belt-and-braces shape as the two
above (explicit ``landlord_id`` predicate on ``n``).

Correlation: ``notifications.case_id`` is ``NULL`` at the webhook's
original INSERT (no case exists yet, pre-routing —
``app/routers/webhooks/twilio.py``'s own note), but
``app/agent/nodes/identify_case.py`` backfills it directly the moment a
case is assigned to that message (an ordinary, non-append-only column —
unlike ``messages.case_id``/``audit_log.case_id``, which stay ``NULL``
forever for their own pre-case rows). This LATERAL matches on
``n.case_id = c.id`` directly, OR'd with the SAME ``message_cases``
correlation (via ``payload ->> 'message_id'``) this module already uses
for ``tenant_message`` and ``app/routers/cases.py``'s
``_SELECT_AUDIT_SQL`` uses for the sibling ``emergency_triggered`` audit
row — belt-and-braces for the narrow window before that backfill runs,
honest regardless of whether it ever does. By the time a case has a queue
card at all (``awaiting_approval``), ``identify_case`` has already run for
it, so the direct match is what resolves this in practice; the
``message_cases`` fallback is defense-in-depth only.

Latest wins: a case could in principle have more than one unacknowledged
``emergency_call`` notification (a reopened case, or a second emergency
report before the first is acknowledged) — ``ORDER BY n.created_at DESC
LIMIT 1`` picks the newest, matching the contract's own documented
tie-break.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.case_lifecycle import STATUS_AWAITING_APPROVAL, STATUS_AWAITING_TENANT
from app.deps import Landlord, require_landlord

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
    notification_id: UUID | None


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
        audit.payload AS classified_payload,
        notif.id AS notification_id
    FROM cases c
    JOIN properties p ON p.id = c.property_id
    JOIN tenants t ON t.id = c.tenant_id
    JOIN drafts d ON d.case_id = c.id AND d.status = 'pending' AND d.landlord_id = :landlord_id
    LEFT JOIN LATERAL (
        SELECT m.body, m.created_at
        FROM messages m
        WHERE m.landlord_id = :landlord_id
          AND m.party = 'tenant'
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
        WHERE a.landlord_id = :landlord_id AND a.case_id = c.id AND a.action = 'classified'
          AND (a.payload ? 'severity')
        ORDER BY a.created_at DESC
        LIMIT 1
    ) audit ON true
    LEFT JOIN LATERAL (
        SELECT n.id
        FROM notifications n
        WHERE n.landlord_id = :landlord_id
          AND n.type = 'emergency_call'
          AND n.acknowledged_at IS NULL
          AND (
            n.case_id = c.id
            OR EXISTS (
              SELECT 1 FROM message_cases mc
              WHERE mc.case_id = c.id AND mc.message_id::text = n.payload ->> 'message_id'
            )
          )
        ORDER BY n.created_at DESC
        LIMIT 1
    ) notif ON true
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
        notification_id=row["notification_id"],
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
