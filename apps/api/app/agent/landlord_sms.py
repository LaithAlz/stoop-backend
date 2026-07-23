"""Landlord-facing SMS outbox (#122, approve-by-SMS) — the draft-ready
notice and every reply confirmation (approved / rejected / stale / undo).

Reuses the EXISTING ``notifications`` table (``type='draft_ready'``,
``channel='sms'`` — both already legal per the ORIGINAL CHECK constraint,
migration 0002; schema-v1.md v1.16 amendment 4, no migration needed) rather
than a new table, mirroring the durable-outbox convention this codebase
already uses for ``push_outbox``/``tenant_ack``/``emergency_sms``: enqueue
now (inside the SAME transaction as whatever DB event produced the
notice), drain later via a dedicated sweep tick. This is REQUIRED, not
just consistent style — the enqueue call sites
(``app/agent/nodes/await_approval.py::mark_awaiting_approval``,
``app/agent/approve_by_sms.py``) both run SYNCHRONOUSLY inside a request
(the graph run / the Twilio webhook handler), and "never hold a DB session
across a network call" + "Twilio webhooks answer 200 fast" both forbid a
real Twilio send inline there.

``payload.kind`` distinguishes the five SMS shapes this module ever
enqueues (:data:`KIND_READY` / :data:`KIND_APPROVED` / :data:`KIND_REJECTED`
/ :data:`KIND_STALE` / :data:`KIND_UNDO`) — one ``notifications.type`` value
carries all of them rather than inventing four more CHECK values for what
is, mechanically, the exact same "one rendered SMS body to one landlord"
shape every time. Only :data:`KIND_READY` rows are ever read back for
reply-correlation (``app/agent/approve_by_sms.py``'s "most recent
draft-ready notification" lookup, api-contracts.md) — the confirmation
kinds exist purely to be drained and sent, never correlated against again.

A NEW sanctioned Twilio-send call site (deliberately, not a silent reuse)
--------------------------------------------------------------------------
``apps/api/CLAUDE.md``: "Send to tenant/vendor happens only through the
draft flow or the emergency safety path. There is no other code path that
calls ``twilio.send``." That rule is scoped to sends TO A TENANT/VENDOR —
this module never sends to either (every send here goes to the case's own
LANDLORD, at the property's own ``twilio_number``, resolved the same way
``app/agent/draft_sender.py`` resolves its own ``from_e164``). It is,
nonetheless, a genuinely NEW ``get_twilio_sender()`` call site, and
``tests/test_twilio_send_allowlist.py``'s allowlist is extended
DELIBERATELY (with this module's own comment there) to name it as the
THIRD sanctioned sender, exactly as that test's own docstring anticipates
("EXTEND THIS DELIBERATELY... if a THIRD sanctioned sender ever needs to
reference it").

Idempotency — a NOT EXISTS guard, not a unique index (accepted, scoped
tradeoff)
------------------------------------------------------------------------
:func:`enqueue_landlord_sms` guards against double-enqueue on a REDELIVERED
event (both call sites can genuinely re-run on crash/redelivery — see
``mark_awaiting_approval``'s own docstring, and the Twilio ``/sms`` webhook's
own "Transaction design" for the recovery/redelivery path that re-invokes
the SAME post-persist side effects) via ``INSERT ... WHERE NOT EXISTS``,
keyed on ``(draft_id, kind)`` in ``payload``. This is application-level, not
a real Postgres unique index — schema-v1.md's own v1.3 amendments document
why that distinction matters for the emergency notification dedupe (a
genuinely CONCURRENT redelivery can race an app-level check). Accepted here
because the failure mode is bounded and non-safety-critical: at worst, a
genuinely-concurrent redelivery of the SAME event sends a landlord ONE
duplicate "your reply is ready"/confirmation text — a nuisance, never a
missed emergency, a missed approval, or a double SEND to a tenant/vendor
(this module never touches ``drafts``/the tenant-facing send path at all).

DB access
---------
Admin engine throughout (background/webhook context, no request/landlord
JWT) — same rationale as every other node/sweep in this package.
Allowlisted in ``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager as _acm
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import sentry_sdk
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_admin_session
from app.integrations.twilio_send import get_twilio_sender

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# payload.kind vocabulary
# ---------------------------------------------------------------------------

KIND_READY = "ready"
KIND_APPROVED = "approved"
KIND_REJECTED = "rejected"
KIND_STALE = "stale"
KIND_UNDO = "undo"

_ALL_KINDS = frozenset({KIND_READY, KIND_APPROVED, KIND_REJECTED, KIND_STALE, KIND_UNDO})

_DRAFT_EXCERPT_CHARS = 200
"""plain-language-rules.md's "Approve-by-SMS" example: "first ~200 chars"
of the draft body."""


# ---------------------------------------------------------------------------
# Rendering — pure functions, no I/O. plain-language-rules.md's "Approve-by
# -SMS" section is the template source; the eval grader doesn't cover this
# surface (it's not a `draft_response`/rubric prompt), but the copy rules
# (grade-5 reading level, no jargon, concrete over relative where it
# applies) are followed by hand.
# ---------------------------------------------------------------------------


def _truncate(text_value: str, limit: int) -> str:
    stripped = text_value.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "…"


def render_tenant_label(*, tenant_name: str | None, unit: str | None, property_label: str) -> str:
    """ "Maria (Unit 2, Palmerston)" — plain-language-rules.md's own
    example shape. Falls back to "Your tenant" when the tenant has no
    stored name (``tenants.name`` is nullable, schema-v1.md), and omits the
    unit entirely when absent (also nullable) rather than printing
    "Unit None"."""
    name = tenant_name or "Your tenant"
    if unit:
        return f"{name} (Unit {unit}, {property_label})"
    return f"{name} ({property_label})"


def render_draft_ready_sms(*, tenant_label: str, draft_body: str) -> str:
    """The draft-ready notice (issue #122 AC 1 / plain-language-rules.md).
    ``draft_body`` is truncated to the first ~200 chars per that doc's own
    example."""
    excerpt = _truncate(draft_body, _DRAFT_EXCERPT_CHARS)
    return (
        f'Stoop: {tenant_label} — draft ready: "{excerpt}" '
        "Reply 1 to send · 2 to skip · or open the app to edit."
    )


def render_approved_confirmation_sms() -> str:
    """AC 2: confirms the send + restates the 5-minute reply-UNDO window
    (SMS has no undo bar — plain-language-rules.md)."""
    return "Stoop: Sent! Reply UNDO within 5 minutes if you want to stop it."


def render_rejected_confirmation_sms() -> str:
    """AC 3: "draft archived, case stays open" — landlord-facing copy."""
    return "Stoop: Got it — I won't send that one. The conversation is still open in the app."


def render_stale_notice_sms(*, tenant_name: str | None) -> str:
    """AC 4 (the stale-draft race) — plain-language-rules.md/issue #122's
    own exact wording, tenant name substituted (falls back to "Your
    tenant" like :func:`render_tenant_label` when unnamed)."""
    name = tenant_name or "Your tenant"
    return f"Stoop: {name} sent a new message — fresh draft coming."


def render_already_handled_sms() -> str:
    """A reply ("1"/"2") that referenced a draft no longer in a state that
    reply can act on, but NOT because a newer tenant message superseded it
    (already approved/sent/rejected by another channel) — distinct copy
    from the stale-draft race above, which is specifically about a NEW
    tenant message."""
    return "Stoop: That one's already been handled — nothing changed."


def render_undo_confirmed_sms() -> str:
    return "Stoop: Undone — that reply won't go out. It's back to pending in the app."


def render_nothing_to_undo_sms() -> str:
    return "Stoop: Nothing to undo right now."


def render_already_sent_cannot_undo_sms() -> str:
    return "Stoop: That one already went out — too late to undo."


# ---------------------------------------------------------------------------
# Enqueue (durable outbox write) — called from mark_awaiting_approval
# (KIND_READY) and approve_by_sms (every confirmation kind).
# ---------------------------------------------------------------------------

_ENQUEUE_LANDLORD_SMS_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    SELECT :landlord_id, :case_id, 'draft_ready', 'sms', 'pending', CAST(:payload AS jsonb)
    WHERE NOT EXISTS (
      SELECT 1 FROM notifications
      WHERE type = 'draft_ready' AND channel = 'sms'
        AND payload ->> 'draft_id' = :draft_id
        AND payload ->> 'kind' = :kind
    )
    RETURNING id
    """
)


async def enqueue_landlord_sms(
    session: AsyncSession,
    *,
    landlord_id: UUID,
    case_id: UUID,
    draft_id: UUID,
    kind: str,
    body: str,
) -> UUID | None:
    """Durably queue one landlord-facing SMS (module docstring). Returns
    the new ``notifications.id`` on a genuine new enqueue, ``None`` on an
    idempotent no-op (see module docstring "Idempotency"). *kind* must be
    one of :data:`KIND_READY`/:data:`KIND_APPROVED`/:data:`KIND_REJECTED`/
    :data:`KIND_STALE`/:data:`KIND_UNDO`."""
    if kind not in _ALL_KINDS:  # pragma: no cover — defensive, every caller uses the constants
        raise ValueError(f"unrecognized landlord SMS kind: {kind!r}")

    payload = {"draft_id": str(draft_id), "kind": kind, "body": body}
    row = (
        (
            await session.execute(
                _ENQUEUE_LANDLORD_SMS_SQL,
                {
                    "landlord_id": str(landlord_id),
                    "case_id": str(case_id),
                    "draft_id": str(draft_id),
                    "kind": kind,
                    "payload": json.dumps(payload),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return cast("UUID", row["id"]) if row is not None else None


# ---------------------------------------------------------------------------
# Correlation read — "the landlord's most recent draft-ready notification,
# scoped to the property owning the To number" (api-contracts.md).
# ---------------------------------------------------------------------------

_SELECT_MOST_RECENT_READY_DRAFT_SQL = text(
    """
    SELECT n.payload ->> 'draft_id' AS draft_id, n.case_id AS case_id
    FROM notifications n
    JOIN cases c ON c.id = n.case_id
    WHERE n.landlord_id = :landlord_id
      AND n.type = 'draft_ready' AND n.channel = 'sms'
      AND n.payload ->> 'kind' = 'ready'
      AND c.property_id = :property_id
    ORDER BY n.created_at DESC
    LIMIT 1
    """
)


@dataclass(frozen=True)
class ReferencedDraft:
    draft_id: UUID
    case_id: UUID


async def most_recent_ready_draft(
    session: AsyncSession, *, landlord_id: UUID, property_id: UUID
) -> ReferencedDraft | None:
    """api-contracts.md: "Replies correlate to the draft id carried in
    that landlord's most recent draft-ready notification..., scoped to the
    property owning the To number (via case_id -> cases.property_id)" —
    this is also the AC's own "disambiguation when multiple drafts
    pending" rule: whichever draft this landlord was MOST RECENTLY told
    about, for THIS property, wins, regardless of how many other cases/
    properties also have pending drafts. Returns ``None`` when this
    landlord has never had a draft-ready notice for this property (an
    honest "nothing to correlate against" — callers fall back to the
    existing needs_eyes surfacing, never a 500/silently-dropped reply)."""
    row = (
        (
            await session.execute(
                _SELECT_MOST_RECENT_READY_DRAFT_SQL,
                {"landlord_id": str(landlord_id), "property_id": str(property_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["draft_id"] is None:
        return None
    return ReferencedDraft(
        draft_id=UUID(str(row["draft_id"])), case_id=cast("UUID", row["case_id"])
    )


_SELECT_MOST_RECENT_READY_DRAFT_FOR_CASE_SQL = text(
    """
    SELECT payload ->> 'draft_id' AS draft_id
    FROM notifications
    WHERE case_id = :case_id AND type = 'draft_ready' AND channel = 'sms'
      AND payload ->> 'kind' = 'ready'
    ORDER BY created_at DESC
    LIMIT 1
    """
)


async def ready_draft_for_case(session: AsyncSession, *, case_id: UUID) -> UUID | None:
    """Webhook conflict/recovery-path helper (``app/routers/webhooks/
    twilio.py``, ``_SELECT_MESSAGE_FOR_RECOVERY_SQL``): a REDELIVERED
    landlord reply recovers its OWN already-durably-stored ``case_id``
    from the ``messages`` row itself (never re-resolved — that would risk
    a DIFFERENT case than the one this exact message was originally
    correlated to, if a newer draft-ready notice arrived in between
    deliveries). This re-derives ONLY the referenced ``draft_id``, scoped
    to that ALREADY-KNOWN ``case_id`` — consistent with the original
    resolution by construction, since a case's draft-ready notices always
    carry that SAME case's own ``case_id``."""
    row = (
        (
            await session.execute(
                _SELECT_MOST_RECENT_READY_DRAFT_FOR_CASE_SQL, {"case_id": str(case_id)}
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["draft_id"] is None:
        return None
    return UUID(str(row["draft_id"]))


# ---------------------------------------------------------------------------
# Drain sweep — the THIRD sanctioned Twilio-send call site (module
# docstring "A NEW sanctioned Twilio-send call site").
# ---------------------------------------------------------------------------

_SELECT_DUE_LANDLORD_SMS_SQL = text(
    """
    SELECT id, landlord_id, case_id, attempt, payload
    FROM notifications
    WHERE type = 'draft_ready' AND channel = 'sms' AND status IN ('pending', 'failed')
    ORDER BY created_at
    """
)

_CLAIM_LANDLORD_SMS_SQL = text(
    """
    UPDATE notifications SET attempt = :new_attempt, updated_at = now()
    WHERE id = :id AND status IN ('pending', 'failed') AND attempt = :old_attempt
    RETURNING id
    """
)

_MARK_LANDLORD_SMS_SENT_SQL = text(
    "UPDATE notifications SET status = 'sent', updated_at = now() WHERE id = :id"
)

_MARK_LANDLORD_SMS_FAILED_SQL = text(
    "UPDATE notifications SET status = 'failed', updated_at = now() WHERE id = :id"
)

# Terminal — mirrors app/agent/emergency_chain.py's own 'no_tenant_phone'
# precedent: no amount of retrying supplies a phone number/twilio_number
# this row never had. 'exhausted' is excluded from the SELECT above's
# `status IN ('pending', 'failed')`, so a row marked this way is never
# re-attempted.
_MARK_LANDLORD_SMS_EXHAUSTED_SQL = text(
    "UPDATE notifications SET status = 'exhausted', updated_at = now() WHERE id = :id"
)

_SELECT_LANDLORD_SMS_CONTEXT_SQL = text(
    """
    SELECT l.phone AS landlord_phone, p.twilio_number AS twilio_number
    FROM cases c
    JOIN landlords l ON l.id = c.landlord_id
    JOIN properties p ON p.id = c.property_id
    WHERE c.id = :case_id
    """
)


@dataclass(frozen=True)
class LandlordSmsCandidate:
    notification_id: UUID
    landlord_id: UUID
    case_id: UUID
    attempt: int
    body: str


@dataclass(frozen=True)
class LandlordSmsOutcome:
    notification_id: UUID
    outcome: str  # "sent" | "failed" | "lost_race" | "context_missing" | "no_phone"


def _candidate_from_row(row: dict[str, Any]) -> LandlordSmsCandidate | None:
    payload = cast("dict[str, Any]", row["payload"])
    body = payload.get("body")
    if body is None:  # pragma: no cover — invariant: enqueue_landlord_sms always sets it
        return None
    return LandlordSmsCandidate(
        notification_id=cast("UUID", row["id"]),
        landlord_id=cast("UUID", row["landlord_id"]),
        case_id=cast("UUID", row["case_id"]),
        attempt=cast("int", row["attempt"]),
        body=str(body),
    )


async def _process_candidate(candidate: LandlordSmsCandidate) -> str:
    new_attempt = candidate.attempt + 1

    async with _acm(get_admin_session)() as session:
        claim_row = (
            (
                await session.execute(
                    _CLAIM_LANDLORD_SMS_SQL,
                    {
                        "id": str(candidate.notification_id),
                        "old_attempt": candidate.attempt,
                        "new_attempt": new_attempt,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if claim_row is None:
            return "lost_race"

        ctx_row = (
            (
                await session.execute(
                    _SELECT_LANDLORD_SMS_CONTEXT_SQL, {"case_id": str(candidate.case_id)}
                )
            )
            .mappings()
            .one_or_none()
        )

    if ctx_row is None:  # pragma: no cover — invariant: cases are never deleted
        log.error("landlord_sms_context_missing", notification_id=str(candidate.notification_id))
        sentry_sdk.capture_message(
            "landlord_sms: drain context missing for a claimed attempt",
            level="error",
            extras={"notification_id": str(candidate.notification_id)},
        )
        return "context_missing"

    landlord_phone = ctx_row["landlord_phone"]
    twilio_number = ctx_row["twilio_number"]
    if not landlord_phone or not twilio_number:
        # Terminal — see _MARK_LANDLORD_SMS_EXHAUSTED_SQL's own comment.
        async with _acm(get_admin_session)() as session:
            await session.execute(
                _MARK_LANDLORD_SMS_EXHAUSTED_SQL, {"id": str(candidate.notification_id)}
            )
        log.warning("landlord_sms_no_phone", notification_id=str(candidate.notification_id))
        return "no_phone"

    sender = get_twilio_sender()  # sanctioned call site (allowlisted, #122)
    try:
        await sender.send_sms(to=landlord_phone, from_=twilio_number, body=candidate.body)
    except Exception as exc:
        log.error(
            "landlord_sms_send_failed",
            notification_id=str(candidate.notification_id),
            exc_type=type(exc).__name__,
        )
        sentry_sdk.capture_message(
            "landlord_sms: send failed",
            level="error",
            extras={
                "notification_id": str(candidate.notification_id),
                "exc_type": type(exc).__name__,
            },
        )
        async with _acm(get_admin_session)() as session:
            await session.execute(
                _MARK_LANDLORD_SMS_FAILED_SQL, {"id": str(candidate.notification_id)}
            )
        return "failed"

    async with _acm(get_admin_session)() as session:
        await session.execute(_MARK_LANDLORD_SMS_SENT_SQL, {"id": str(candidate.notification_id)})
    log.info("landlord_sms_sent", notification_id=str(candidate.notification_id))
    return "sent"


async def _process_candidate_safely(candidate: LandlordSmsCandidate) -> str:
    """Never-raises wrapper — same rationale as
    ``app/agent/emergency_chain.py``'s own SMS-drain safety wrapper: a
    row's own claim (or lack thereof) is the only durable state this sweep
    depends on, so there is no "stuck forever" risk from one candidate's
    exception blocking others."""
    try:
        return await _process_candidate(candidate)
    except Exception as exc:
        log.error(
            "landlord_sms_candidate_processing_failed",
            notification_id=str(candidate.notification_id),
            exc_type=type(exc).__name__,
        )
        sentry_sdk.capture_message(
            "landlord_sms: candidate processing raised",
            level="error",
            extras={
                "notification_id": str(candidate.notification_id),
                "exc_type": type(exc).__name__,
            },
        )
        return "processing_error"


async def run_landlord_sms_drain_sweep() -> list[LandlordSmsOutcome]:
    """DB entrypoint for one sweep tick — drains every ``pending``/
    ``failed`` landlord-facing ``draft_ready``/``sms`` row, resending until
    genuinely delivered or terminally un-deliverable (module docstring).
    Wire into ``app/scheduler.py``'s 60s ticker alongside the other
    sweeps."""
    async with _acm(get_admin_session)() as session:
        rows = (await session.execute(_SELECT_DUE_LANDLORD_SMS_SQL)).mappings().all()
        candidates = [c for row in rows if (c := _candidate_from_row(dict(row))) is not None]

    outcomes: list[LandlordSmsOutcome] = []
    for candidate in candidates:
        outcome = await _process_candidate_safely(candidate)
        outcomes.append(
            LandlordSmsOutcome(notification_id=candidate.notification_id, outcome=outcome)
        )

    log.info("landlord_sms_drain_sweep_complete", candidates_processed=len(outcomes))
    return outcomes


__all__: list[str] = [
    "KIND_APPROVED",
    "KIND_READY",
    "KIND_REJECTED",
    "KIND_STALE",
    "KIND_UNDO",
    "LandlordSmsOutcome",
    "ReferencedDraft",
    "enqueue_landlord_sms",
    "most_recent_ready_draft",
    "ready_draft_for_case",
    "render_already_handled_sms",
    "render_already_sent_cannot_undo_sms",
    "render_approved_confirmation_sms",
    "render_draft_ready_sms",
    "render_nothing_to_undo_sms",
    "render_rejected_confirmation_sms",
    "render_stale_notice_sms",
    "render_tenant_label",
    "render_undo_confirmed_sms",
    "run_landlord_sms_drain_sweep",
]
