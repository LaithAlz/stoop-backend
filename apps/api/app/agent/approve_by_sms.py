"""Approve-by-SMS (#122) — parsing a landlord's inbound "1"/"2"/"UNDO"
reply and funneling it through the SAME approve/reject writer the
dashboard uses.

Read ``docs/03-engineering/api-contracts.md``'s "Webhooks" section
(approve-by-SMS) and ``docs/02-product/plain-language-rules.md``'s
"Approve-by-SMS" section first — this module implements exactly those,
never inventing a token/rule either doesn't already state.

Two-phase design, driven by ``messages`` being append-only
------------------------------------------------------------------------
``app/routers/webhooks/twilio.py`` needs a real ``case_id`` at the INSTANT
it inserts the landlord's reply row (schema-v1.md: "case_id = the
referenced draft's case" for a landlord-party row — and, unlike a tenant
message, ``messages`` is append-only so this can NEVER be backfilled onto
that row afterward). That means the draft-correlation lookup must run
BEFORE the INSERT, not after. Split into two functions:

1. :func:`resolve_reply` — pure DB reads only (parses the token, then
   correlates it to a draft via
   ``app.agent.landlord_sms.most_recent_ready_draft`` if the token is
   recognized). Called BEFORE the message INSERT; its ``case_id`` becomes
   that INSERT's own ``case_id`` value.
2. :func:`handle_reply` — the actual write (approve/reject/undo via the
   SAME writer the dashboard/app use, plus the landlord-SMS confirmation
   enqueue). Called AFTER the message INSERT commits, exactly like every
   other post-persist side effect in that webhook.

A reply whose token isn't recognized, OR whose token IS recognized but
correlates to NOTHING (this landlord has never had a draft-ready notice
for this property), is the caller's (the webhook's) job to fall back to
the EXISTING ``_ensure_needs_eyes_notification`` side effect — this module
never silently drops anything, and never invents a notification of its
own for that case; "logged + surfaced" (issue #122 AC) already means
"the existing needs_eyes path" per this codebase's pre-#122 behavior for
EVERY landlord-authored message.

Funnels through the ONE approve/reject writer — never a second one
------------------------------------------------------------------------
:func:`handle_reply`'s approve/reject branches call
``app.agent.graph.resolve_draft_decision`` — the EXACT function
``app/routers/drafts.py``'s dashboard endpoints call, which is itself the
only entry point to ``app.agent.nodes.finalize_draft_decision``'s
``apply_approve_or_edit``/``apply_rejection`` (schema-v1.md, "the ONE
writer of ``drafts.status='approved'``/``'rejected'``" — see that
module's own docstring). This module adds NO second writer: the ONLY new
DB write here beyond that shared seam is the UNDO revert (:data:`_UNDO_SQL`
below), which mirrors ``app/routers/drafts.py``'s own ``DELETE
.../approve`` handler's ``_REVERT_TO_PENDING_SQL`` — undo, on EITHER
channel, never touches the resume seam at all (that router's own
docstring: "by the time a draft is 'approved', the graph thread has
already fully resumed to completion; undo is a pure DB transition").

``From`` is a weak authenticator (api-contracts.md, stated explicitly)
------------------------------------------------------------------------
This module never treats ``From`` as strong auth for anything beyond
ROUTING (the webhook's own ``_is_landlord_command_channel`` already
decided this message is a command-channel reply from a phone number that
matches this landlord's stored ``phone`` and no active tenant's). Once
routed here, a reply can ONLY EVER act on the single draft
:func:`resolve_reply` correlates it to (this landlord's own most recent
draft-ready notice, scoped to this property) — never any other
landlord's state, never a draft on a DIFFERENT property, never an
arbitrary draft id an attacker might guess. The 5-minute undo window
further bounds the blast radius of a spoofed ``From`` approving something
real (api-contracts.md's own stated mitigation) — no additional
token/quote-back factor is layered on top; the contract's own resolution
of this risk is the single-draft correlation + the bounded window, not a
second factor, and this implementation follows that contract exactly.

Session discipline
-------------------
Every function here is DB-only (no Twilio send, no Anthropic call) — safe
to run synchronously inside the Twilio webhook request, exactly like the
existing ``_ensure_needs_eyes_notification`` side effect it sits alongside.
The actual landlord-facing confirmation SMS is enqueued (durable,
``app.agent.landlord_sms``), never sent inline here — see that module's
own docstring for why.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager as _acm
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import landlord_sms
from app.agent.graph import CaseNotAwaitingApprovalError, DraftStaleError, resolve_draft_decision
from app.agent.nodes.finalize_draft_decision import ACTION_APPROVE, ACTION_REJECT
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

COMMAND_APPROVE = "approve"
COMMAND_REJECT = "reject"
COMMAND_UNDO = "undo"

_APPROVED_FAMILY_STATUSES = frozenset({"approved", "sending", "sent"})
_SUPERSEDED_STATUSES = frozenset({"stale", "cancelled"})


def parse_command(body: str) -> str | None:
    """Pure — the api-contracts.md token vocabulary EXACTLY: ``"1"`` /
    ``"2"`` / ``"UNDO"`` (case-insensitive, surrounding whitespace
    tolerated — a phone keyboard can add a trailing space/newline; the
    digit tokens themselves are matched exactly, no other variant is
    documented anywhere this doc cites). Anything else returns ``None``
    ("anything else replied" — issue #122 AC)."""
    stripped = body.strip()
    if stripped == "1":
        return COMMAND_APPROVE
    if stripped == "2":
        return COMMAND_REJECT
    if stripped.casefold() == "undo":
        return COMMAND_UNDO
    return None


@dataclass(frozen=True)
class ParsedReply:
    command: str | None
    case_id: UUID | None
    draft_id: UUID | None


async def resolve_reply(
    session: AsyncSession, *, landlord_id: UUID, property_id: UUID, body: str
) -> ParsedReply:
    """Phase 1 (module docstring) — parse + correlate, called BEFORE the
    message INSERT (the FRESH-delivery path). Pure reads only."""
    command = parse_command(body)
    if command is None:
        return ParsedReply(command=None, case_id=None, draft_id=None)

    referenced = await landlord_sms.most_recent_ready_draft(
        session, landlord_id=landlord_id, property_id=property_id
    )
    if referenced is None:
        # Recognized token, nothing to correlate against -- the caller
        # falls back to needs_eyes (module docstring).
        return ParsedReply(command=command, case_id=None, draft_id=None)
    return ParsedReply(command=command, case_id=referenced.case_id, draft_id=referenced.draft_id)


async def resolve_reply_for_recovered_case(
    session: AsyncSession, *, case_id: UUID | None, body: str
) -> ParsedReply:
    """The CONFLICT/redelivery-path counterpart to :func:`resolve_reply`
    (``app/routers/webhooks/twilio.py``'s conflict-recovery branch): the
    message row ALREADY exists (this is a Twilio redelivery or crash
    -recovery retry of the SAME ``MessageSid``), so its ``case_id`` is
    ALREADY durably fixed (``messages`` is append-only) — never re-resolved
    via ``most_recent_ready_draft`` here, which could legitimately return a
    DIFFERENT case if a newer draft-ready notice arrived between
    deliveries. Only the referenced ``draft_id`` is re-derived, scoped to
    that already-known *case_id* (:func:`app.agent.landlord_sms.
    ready_draft_for_case`) — consistent with the original resolution by
    construction. *case_id* is ``None`` for a recovered TENANT-party row or
    a landlord row that itself had nothing to correlate against
    originally; either way this returns the same "nothing to correlate"
    shape :func:`resolve_reply` would."""
    command = parse_command(body)
    if command is None or case_id is None:
        return ParsedReply(command=command, case_id=None, draft_id=None)

    draft_id = await landlord_sms.ready_draft_for_case(session, case_id=case_id)
    if draft_id is None:
        return ParsedReply(command=command, case_id=None, draft_id=None)
    return ParsedReply(command=command, case_id=case_id, draft_id=draft_id)


# ---------------------------------------------------------------------------
# Phase 2 — the actual dispatch (approve / reject / undo), called
# POST-PERSIST (after the landlord's reply message row is durably stored).
# ---------------------------------------------------------------------------

_SELECT_DRAFT_STATUS_SQL = text(
    "SELECT status FROM drafts WHERE id = :draft_id AND landlord_id = :landlord_id"
)

_SELECT_TENANT_NAME_FOR_CASE_SQL = text(
    "SELECT t.name AS tenant_name FROM cases c JOIN tenants t ON t.id = c.tenant_id "
    "WHERE c.id = :case_id"
)

# Mirrors app/routers/drafts.py's own _REVERT_TO_PENDING_SQL exactly, plus
# clearing `approved_via` (a clean slate -- a later, fresh approval on
# either channel sets it again). Undo, on EITHER channel, never touches
# the resume seam (see module docstring) -- a pure, atomically-guarded DB
# transition, same as the dashboard's own DELETE .../approve.
_UNDO_SQL = text(
    "UPDATE drafts SET status = 'pending', scheduled_send_at = NULL, edited = false, "
    "final_body = NULL, approved_via = NULL, updated_at = now() "
    "WHERE id = :draft_id AND landlord_id = :landlord_id AND status = 'approved' "
    "RETURNING id"
)

_INSERT_SMS_UNDO_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'landlord', 'send_cancelled', CAST(:payload AS jsonb))"
)


async def _tenant_name_for_case(session: AsyncSession, *, case_id: UUID) -> str | None:
    row = (
        (await session.execute(_SELECT_TENANT_NAME_FOR_CASE_SQL, {"case_id": str(case_id)}))
        .mappings()
        .one_or_none()
    )
    return row["tenant_name"] if row is not None else None


async def _enqueue(
    *, landlord_id: UUID, case_id: UUID, draft_id: UUID, kind: str, body: str
) -> None:
    async with _acm(get_admin_session)() as session:
        await landlord_sms.enqueue_landlord_sms(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            kind=kind,
            body=body,
        )


async def _handle_approve(*, landlord_id: UUID, case_id: UUID, draft_id: UUID) -> None:
    async with _acm(get_admin_session)() as session:
        row = (
            (
                await session.execute(
                    _SELECT_DRAFT_STATUS_SQL,
                    {"draft_id": str(draft_id), "landlord_id": str(landlord_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:  # pragma: no cover -- defensive: correlated via this landlord's own notice
        return
    status = row["status"]

    if status in _APPROVED_FAMILY_STATUSES:
        # Idempotent repeat -- a landlord replying "1" twice (or a Twilio
        # redelivery) re-confirms, never re-triggers the writer.
        await _enqueue(
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            kind=landlord_sms.KIND_APPROVED,
            body=landlord_sms.render_approved_confirmation_sms(),
        )
        return

    if status in _SUPERSEDED_STATUSES:
        tenant_name = await _tenant_name_for_case_admin(case_id=case_id)
        await _enqueue(
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            kind=landlord_sms.KIND_STALE,
            body=landlord_sms.render_stale_notice_sms(tenant_name=tenant_name),
        )
        return

    if status != "pending":  # 'rejected' -- nothing left to approve
        await _enqueue(
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            kind=landlord_sms.KIND_APPROVED,
            body=landlord_sms.render_already_handled_sms(),
        )
        return

    try:
        await resolve_draft_decision(
            case_id=case_id,
            draft_id=draft_id,
            resume_value={"action": ACTION_APPROVE, "source": "sms"},
        )
    except (DraftStaleError, CaseNotAwaitingApprovalError):
        # Genuinely superseded (a newer tenant message re-drafted this
        # case) OR a concurrent approve already won -- either way, the SAME
        # idempotent-family re-check the dashboard's own reconciliation
        # logic applies (app/routers/drafts.py::_reconcile_concurrent_conflict).
        async with _acm(get_admin_session)() as session:
            refreshed = (
                (
                    await session.execute(
                        _SELECT_DRAFT_STATUS_SQL,
                        {"draft_id": str(draft_id), "landlord_id": str(landlord_id)},
                    )
                )
                .mappings()
                .one_or_none()
            )
        refreshed_status = refreshed["status"] if refreshed is not None else None
        if refreshed_status in _APPROVED_FAMILY_STATUSES:
            await _enqueue(
                landlord_id=landlord_id,
                case_id=case_id,
                draft_id=draft_id,
                kind=landlord_sms.KIND_APPROVED,
                body=landlord_sms.render_approved_confirmation_sms(),
            )
        else:
            tenant_name = await _tenant_name_for_case_admin(case_id=case_id)
            await _enqueue(
                landlord_id=landlord_id,
                case_id=case_id,
                draft_id=draft_id,
                kind=landlord_sms.KIND_STALE,
                body=landlord_sms.render_stale_notice_sms(tenant_name=tenant_name),
            )
        return

    await _enqueue(
        landlord_id=landlord_id,
        case_id=case_id,
        draft_id=draft_id,
        kind=landlord_sms.KIND_APPROVED,
        body=landlord_sms.render_approved_confirmation_sms(),
    )


async def _handle_reject(*, landlord_id: UUID, case_id: UUID, draft_id: UUID) -> None:
    async with _acm(get_admin_session)() as session:
        row = (
            (
                await session.execute(
                    _SELECT_DRAFT_STATUS_SQL,
                    {"draft_id": str(draft_id), "landlord_id": str(landlord_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:  # pragma: no cover -- defensive
        return
    status = row["status"]

    if status == "rejected":
        await _enqueue(
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            kind=landlord_sms.KIND_REJECTED,
            body=landlord_sms.render_rejected_confirmation_sms(),
        )
        return

    if status in _APPROVED_FAMILY_STATUSES:
        # Already approved/sending/sent -- reject no longer applies (same
        # reality app/routers/drafts.py's own _reject encodes).
        await _enqueue(
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            kind=landlord_sms.KIND_REJECTED,
            body=landlord_sms.render_already_handled_sms(),
        )
        return

    if status in _SUPERSEDED_STATUSES:
        tenant_name = await _tenant_name_for_case_admin(case_id=case_id)
        await _enqueue(
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            kind=landlord_sms.KIND_STALE,
            body=landlord_sms.render_stale_notice_sms(tenant_name=tenant_name),
        )
        return

    try:
        await resolve_draft_decision(
            case_id=case_id,
            draft_id=draft_id,
            resume_value={"action": ACTION_REJECT, "note": None},
        )
    except (DraftStaleError, CaseNotAwaitingApprovalError):
        async with _acm(get_admin_session)() as session:
            refreshed = (
                (
                    await session.execute(
                        _SELECT_DRAFT_STATUS_SQL,
                        {"draft_id": str(draft_id), "landlord_id": str(landlord_id)},
                    )
                )
                .mappings()
                .one_or_none()
            )
        refreshed_status = refreshed["status"] if refreshed is not None else None
        if refreshed_status == "rejected":
            await _enqueue(
                landlord_id=landlord_id,
                case_id=case_id,
                draft_id=draft_id,
                kind=landlord_sms.KIND_REJECTED,
                body=landlord_sms.render_rejected_confirmation_sms(),
            )
        elif refreshed_status in _APPROVED_FAMILY_STATUSES:
            await _enqueue(
                landlord_id=landlord_id,
                case_id=case_id,
                draft_id=draft_id,
                kind=landlord_sms.KIND_REJECTED,
                body=landlord_sms.render_already_handled_sms(),
            )
        else:
            tenant_name = await _tenant_name_for_case_admin(case_id=case_id)
            await _enqueue(
                landlord_id=landlord_id,
                case_id=case_id,
                draft_id=draft_id,
                kind=landlord_sms.KIND_STALE,
                body=landlord_sms.render_stale_notice_sms(tenant_name=tenant_name),
            )
        return

    await _enqueue(
        landlord_id=landlord_id,
        case_id=case_id,
        draft_id=draft_id,
        kind=landlord_sms.KIND_REJECTED,
        body=landlord_sms.render_rejected_confirmation_sms(),
    )


async def _handle_undo(*, landlord_id: UUID, case_id: UUID, draft_id: UUID) -> None:
    async with _acm(get_admin_session)() as session:
        undone_row = (
            (
                await session.execute(
                    _UNDO_SQL, {"draft_id": str(draft_id), "landlord_id": str(landlord_id)}
                )
            )
            .mappings()
            .one_or_none()
        )
        if undone_row is not None:
            await session.execute(
                _INSERT_SMS_UNDO_AUDIT_SQL,
                {
                    "landlord_id": str(landlord_id),
                    "case_id": str(case_id),
                    "payload": json.dumps({"draft_id": str(draft_id)}),
                },
            )

        status_row = None
        if undone_row is None:
            status_row = (
                (
                    await session.execute(
                        _SELECT_DRAFT_STATUS_SQL,
                        {"draft_id": str(draft_id), "landlord_id": str(landlord_id)},
                    )
                )
                .mappings()
                .one_or_none()
            )

    if undone_row is not None:
        await _enqueue(
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            kind=landlord_sms.KIND_UNDO,
            body=landlord_sms.render_undo_confirmed_sms(),
        )
        return

    status = status_row["status"] if status_row is not None else None

    if status in ("sending", "sent"):
        await _enqueue(
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            kind=landlord_sms.KIND_UNDO,
            body=landlord_sms.render_already_sent_cannot_undo_sms(),
        )
        return

    # 'pending' (never approved / already undone), 'stale', 'cancelled',
    # 'rejected', or the draft genuinely vanished (structurally shouldn't
    # happen, FK RESTRICT) -- nothing was approved to undo.
    await _enqueue(
        landlord_id=landlord_id,
        case_id=case_id,
        draft_id=draft_id,
        kind=landlord_sms.KIND_UNDO,
        body=landlord_sms.render_nothing_to_undo_sms(),
    )


async def _tenant_name_for_case_admin(*, case_id: UUID) -> str | None:
    async with _acm(get_admin_session)() as session:
        return await _tenant_name_for_case(session, case_id=case_id)


async def handle_reply(*, landlord_id: UUID, parsed: ParsedReply) -> bool:
    """Phase 2 (module docstring) — dispatch on *parsed*, called
    POST-PERSIST. Caller (the Twilio webhook) MUST already have verified
    ``parsed.command is not None and parsed.case_id is not None and
    parsed.draft_id is not None`` — the "nothing to correlate against"
    case is the CALLER's fallback-to-needs_eyes responsibility, never
    something this function decides on its own (module docstring).

    Returns ``True``/``False`` purely so this can be passed to the
    webhook's own ``_safe_step`` (which expects a ``bool``-returning
    awaitable, matching ``_ensure_needs_eyes_notification``'s own shape) —
    the value itself carries no further meaning here (unlike that
    function's "did this create a new row", every branch below already
    enqueues idempotently on its own terms)."""
    if parsed.command is None or parsed.case_id is None or parsed.draft_id is None:
        # pragma: no cover -- defensive; the webhook's own routing predicate
        # already guarantees this before ever calling handle_reply.
        log.error("approve_by_sms_handle_reply_called_without_resolution")
        return False

    if parsed.command == COMMAND_APPROVE:
        await _handle_approve(
            landlord_id=landlord_id, case_id=parsed.case_id, draft_id=parsed.draft_id
        )
    elif parsed.command == COMMAND_REJECT:
        await _handle_reject(
            landlord_id=landlord_id, case_id=parsed.case_id, draft_id=parsed.draft_id
        )
    elif parsed.command == COMMAND_UNDO:
        await _handle_undo(
            landlord_id=landlord_id, case_id=parsed.case_id, draft_id=parsed.draft_id
        )
    else:  # pragma: no cover -- defensive, parse_command's own vocabulary is exhaustive
        log.error("approve_by_sms_unrecognized_command", command=parsed.command)
        return False
    return True


__all__: list[str] = [
    "COMMAND_APPROVE",
    "COMMAND_REJECT",
    "COMMAND_UNDO",
    "ParsedReply",
    "handle_reply",
    "parse_command",
    "resolve_reply",
    "resolve_reply_for_recovered_case",
]
