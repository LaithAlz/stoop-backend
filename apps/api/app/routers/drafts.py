"""``POST/DELETE /v1/drafts/{id}/approve``, ``POST /v1/drafts/{id}/reject``,
``POST /v1/drafts/{id}/edit-and-send`` (#44/#45) — the draft approve/undo/
reject/edit-and-send loop.

Canonical shapes: ``docs/03-engineering/api-contracts.md``'s "Drafts (the
approve loop)" section. Two same-PR contract amendments (spec-guardian,
this round): the error envelope's documented convention now states the
error object MAY carry endpoint-specific extra fields (``fresh_draft_id``
as the worked example — the shape itself was already implemented, only the
CONVENTION note was missing), and a NEW ``draft_not_undoable`` code
(distinct from ``already_sent``) for undoing a draft that is
``stale``/``rejected``/``cancelled`` — "already gone out" is a false
statement for those three; see ``_draft_not_undoable_error``'s own
docstring.

Auth / scoping
--------------
Every endpoint uses ``require_landlord`` (#22) — RLS-scoped reads/writes,
never an admin session in this router. A landlord may only ever act on
their OWN drafts: every query here filters ``landlord_id = :landlord_id``
EXPLICITLY, in addition to whatever RLS enforces server-side (CLAUDE.md:
"code behaves as if [RLS is] already on" — this is the first landlord
-scoped CRUD router in the codebase, so this defense-in-depth is not
optional here, especially since local/CI runs fall back to the admin
engine for request sessions until the ``APP_DATABASE_URL`` operator step
is done — see ``app/db/session.py``'s module docstring). A draft that
doesn't exist, or belongs to a different landlord, is indistinguishable
from the caller's point of view — both 404 ``draft_not_found``.

The actual state transition (approve/reject/edit-and-send) is NEVER
performed directly by this router — it calls
``app.agent.graph.resolve_draft_decision``, which is the ONLY place that
can mark a ``drafts`` row ``'approved'``/``'rejected'`` (see that module's
own docstring "The #44-pinned open design question, decided" for the two
-path design). This router's OWN job is: load-and-validate the draft
(ownership + current status), translate the two seam exceptions
(:class:`DraftStaleError` / :class:`CaseNotAwaitingApprovalError`) into the
documented HTTP responses, and re-read the row afterward to build the
response body — it never itself decides whether something is approvable.

Idempotency / concurrency reconciliation
------------------------------------------
"Idempotent: double-approve doesn't double-send" (#44 AC) is implemented
in TWO layers:

1. A fast pre-check on the draft's CURRENT status before ever touching the
   resume seam: an already-``approved``/``sending``/``sent`` draft short
   -circuits straight to a 200 response (reusing the stored
   ``scheduled_send_at``) without calling ``resolve_draft_decision`` again
   — the common case (a UI double-tap, or a redelivered request).
2. Under GENUINE concurrency (two truly simultaneous requests both pass
   layer 1's pre-check while the draft is still ``'pending'``), the
   per-case advisory lock inside ``resolve_draft_decision`` still
   guarantees only ONE caller actually applies the transition — the loser
   observes either :class:`DraftStaleError` or
   :class:`CaseNotAwaitingApprovalError` (both are legitimate outcomes of
   "someone else already acted on this exact draft" under real
   concurrency — see ``app/agent/graph.py``'s own module docstring). This
   router reconciles BOTH exception types identically: re-read the
   draft's CURRENT row; if it is now in an "already actioned" status,
   return the SAME idempotent 200 a sequential repeat call would have
   gotten; only if it is genuinely superseded by a fresh draft (or simply
   gone) does this raise the documented 409.

Undo (``DELETE .../approve``) never touches the resume seam at all — by
the time a draft is ``'approved'``, the graph thread has already fully
resumed to completion (module docstring above); undo is a pure DB
transition (``'approved' -> 'pending'``, clearing the schedule) gated by
the SAME atomic conditional ``UPDATE ... WHERE status = 'approved'``
pattern the sender ticker uses for its own claim (``app/agent/
draft_sender.py``) — so a landlord's undo tap can never race the sender's
claim into an inconsistent state: whichever wins the UPDATE, the other
sees zero rows affected and responds accordingly (200 idempotent-pending
vs. 409 ``already_sent``).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import CaseNotAwaitingApprovalError, DraftStaleError, resolve_draft_decision
from app.agent.nodes.finalize_draft_decision import (
    ACTION_APPROVE,
    ACTION_EDIT_AND_SEND,
    ACTION_REJECT,
)
from app.deps import Landlord, require_landlord
from app.errors import AppError

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/drafts", tags=["drafts"])

# ---------------------------------------------------------------------------
# Error codes — stable snake_case strings (api-contracts.md convention).
# ---------------------------------------------------------------------------

_DRAFT_NOT_FOUND_CODE = "draft_not_found"
_DRAFT_NOT_FOUND_MESSAGE = "No draft found for this id."
_DRAFT_STALE_CODE = "draft_stale"
_DRAFT_STALE_MESSAGE = "A newer message changed this conversation — review the fresh draft."
_ALREADY_SENT_CODE = "already_sent"
_ALREADY_SENT_MESSAGE = "This reply has already gone out."
_DRAFT_NOT_UNDOABLE_CODE = "draft_not_undoable"
_DRAFT_NOT_UNDOABLE_MESSAGE = "This reply isn't approved, so there's nothing to undo."

# drafts.status values (schema-v1.md CHECK) an approve/edit-and-send call
# treats as "already actioned, respond idempotently" vs. "no longer
# actionable at all, superseded".
_APPROVED_FAMILY_STATUSES = frozenset({"approved", "sending", "sent"})
_UNACTIONABLE_STATUSES = frozenset({"stale", "rejected", "cancelled"})

# ---------------------------------------------------------------------------
# Request / response models — api-contracts.md's Drafts section shapes.
# ---------------------------------------------------------------------------


class ApproveResponse(BaseModel):
    status: Literal["approved"]
    scheduled_send_at: datetime
    undo_until: datetime


class UndoResponse(BaseModel):
    status: Literal["pending"]


class RejectRequest(BaseModel):
    note: str | None = None


class RejectResponse(BaseModel):
    status: Literal["rejected"]


class EditAndSendRequest(BaseModel):
    body: str = Field(min_length=1)

    @field_validator("body")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        """``Field(min_length=1)`` alone lets a whitespace-only string
        ("   ") through — a real edit must have actual content (safety
        review, this round). Validated via strip-then-check; the ORIGINAL
        value (not the stripped one) is returned so a landlord's
        intentional leading/trailing space in an otherwise-real edit is
        never silently altered."""
        if not value.strip():
            raise ValueError("body must not be empty or whitespace-only")
        return value


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SELECT_DRAFT_SQL = text(
    "SELECT id, case_id, status, scheduled_send_at FROM drafts "
    "WHERE id = :draft_id AND landlord_id = :landlord_id"
)

_SELECT_CASE_PENDING_DRAFT_SQL = text(
    "SELECT id FROM drafts WHERE case_id = :case_id AND status = 'pending'"
)

_REVERT_TO_PENDING_SQL = text(
    "UPDATE drafts SET status = 'pending', scheduled_send_at = NULL, edited = false, "
    "final_body = NULL, updated_at = now() "
    "WHERE id = :draft_id AND landlord_id = :landlord_id AND status = 'approved' "
    "RETURNING id, case_id"
)

_INSERT_SEND_CANCELLED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'landlord', 'send_cancelled', CAST(:payload AS jsonb))"
)


async def _load_draft(
    session: AsyncSession, *, draft_id: UUID, landlord_id: UUID
) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                _SELECT_DRAFT_SQL, {"draft_id": str(draft_id), "landlord_id": str(landlord_id)}
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


async def _fresh_pending_draft_id(session: AsyncSession, *, case_id: UUID) -> UUID | None:
    row = (
        (await session.execute(_SELECT_CASE_PENDING_DRAFT_SQL, {"case_id": str(case_id)}))
        .mappings()
        .one_or_none()
    )
    return row["id"] if row is not None else None


def _not_found_error() -> AppError:
    return AppError(status_code=404, code=_DRAFT_NOT_FOUND_CODE, message=_DRAFT_NOT_FOUND_MESSAGE)


def _draft_stale_error(fresh_draft_id: UUID | None) -> AppError:
    return AppError(
        status_code=409,
        code=_DRAFT_STALE_CODE,
        message=_DRAFT_STALE_MESSAGE,
        extra={"fresh_draft_id": str(fresh_draft_id) if fresh_draft_id is not None else None},
    )


def _already_sent_error() -> AppError:
    return AppError(status_code=409, code=_ALREADY_SENT_CODE, message=_ALREADY_SENT_MESSAGE)


def _draft_not_undoable_error() -> AppError:
    """Distinct from :func:`_already_sent_error` (safety-guardian, MAJOR,
    this round): "already gone out" is a FALSE statement for a draft that
    is ``stale``/``rejected``/``cancelled`` — it never sent, it simply
    isn't (or is no longer) in a state undo applies to. Reserve
    ``already_sent`` for the true "the sender already claimed/sent it"
    reality (``sending``/``sent``)."""
    return AppError(
        status_code=409, code=_DRAFT_NOT_UNDOABLE_CODE, message=_DRAFT_NOT_UNDOABLE_MESSAGE
    )


def _approve_response_from_row(draft: dict[str, Any]) -> ApproveResponse:
    return ApproveResponse(
        status="approved",
        scheduled_send_at=draft["scheduled_send_at"],
        undo_until=draft["scheduled_send_at"],
    )


# ---------------------------------------------------------------------------
# approve / edit-and-send — share every mechanic except the resume action
# and whether a replacement body is recorded.
# ---------------------------------------------------------------------------


async def _reconcile_concurrent_conflict(
    session: AsyncSession,
    *,
    landlord: Landlord,
    draft_id: UUID,
    case_id: UUID,
    fallback_fresh_id: UUID | None,
) -> ApproveResponse:
    """Called after :func:`app.agent.graph.resolve_draft_decision` raises
    either seam exception for an approve/edit-and-send call — see module
    docstring "Idempotency / concurrency reconciliation"."""
    refreshed = await _load_draft(session, draft_id=draft_id, landlord_id=landlord.id)
    if refreshed is not None and refreshed["status"] in _APPROVED_FAMILY_STATUSES:
        return _approve_response_from_row(refreshed)
    fresh_id = fallback_fresh_id
    if fresh_id is None:
        fresh_id = await _fresh_pending_draft_id(session, case_id=case_id)
    raise _draft_stale_error(fresh_id)


async def _approve_or_edit(
    *,
    session: AsyncSession,
    landlord: Landlord,
    draft_id: UUID,
    action: str,
    edited_body: str | None,
) -> ApproveResponse:
    draft = await _load_draft(session, draft_id=draft_id, landlord_id=landlord.id)
    if draft is None:
        raise _not_found_error()

    status = draft["status"]
    case_id = draft["case_id"]

    if status in _APPROVED_FAMILY_STATUSES:
        # Idempotent repeat — never re-trigger the resume seam for a draft
        # that's already approved/being sent/sent.
        return _approve_response_from_row(draft)

    if status in _UNACTIONABLE_STATUSES:
        fresh_id = await _fresh_pending_draft_id(session, case_id=case_id)
        raise _draft_stale_error(fresh_id)

    # status == "pending" -- proceed via the resume seam.
    resume_value: dict[str, Any] = {"action": action}
    if action == ACTION_EDIT_AND_SEND:
        resume_value["body"] = edited_body

    try:
        await resolve_draft_decision(case_id=case_id, draft_id=draft_id, resume_value=resume_value)
    except DraftStaleError as exc:
        return await _reconcile_concurrent_conflict(
            session,
            landlord=landlord,
            draft_id=draft_id,
            case_id=case_id,
            fallback_fresh_id=exc.fresh_draft_id,
        )
    except CaseNotAwaitingApprovalError:
        return await _reconcile_concurrent_conflict(
            session, landlord=landlord, draft_id=draft_id, case_id=case_id, fallback_fresh_id=None
        )

    refreshed = await _load_draft(session, draft_id=draft_id, landlord_id=landlord.id)
    if refreshed is None:  # pragma: no cover — can't disappear mid-request (RESTRICT FK, no delete)
        raise _not_found_error()
    return _approve_response_from_row(refreshed)


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


async def _reconcile_reject_conflict(
    session: AsyncSession, *, landlord: Landlord, draft_id: UUID, case_id: UUID
) -> RejectResponse:
    """Called after :func:`resolve_draft_decision` raises either seam
    exception for a reject attempt — mirrors :func:`_reconcile_concurrent_
    conflict`'s reconciliation, but for reject's own outcomes.

    Distinguishes two DIFFERENT realities that both fall through here
    (safety-guardian, MAJOR, this round — the ORIGINAL version conflated
    them into a single, sometimes-inaccurate ``draft_stale``): a concurrent
    APPROVE winning the race is NOT staleness (no newer message superseded
    anything — the draft is simply already approved/being sent/sent), so it
    gets the SAME accurate ``already_sent`` code the normal pre-check in
    :func:`_reject` already uses for that exact status family. Only a
    GENUINE supersession (superseded by a newer message, or otherwise
    gone) falls to ``draft_stale``.
    """
    refreshed = await _load_draft(session, draft_id=draft_id, landlord_id=landlord.id)
    if refreshed is not None and refreshed["status"] == "rejected":
        return RejectResponse(status="rejected")
    if refreshed is not None and refreshed["status"] in _APPROVED_FAMILY_STATUSES:
        raise _already_sent_error()
    fresh_id = await _fresh_pending_draft_id(session, case_id=case_id)
    raise _draft_stale_error(fresh_id)


async def _reject(
    *, session: AsyncSession, landlord: Landlord, draft_id: UUID, note: str | None
) -> RejectResponse:
    draft = await _load_draft(session, draft_id=draft_id, landlord_id=landlord.id)
    if draft is None:
        raise _not_found_error()

    status = draft["status"]
    case_id = draft["case_id"]

    if status == "rejected":
        return RejectResponse(status="rejected")

    if status in _APPROVED_FAMILY_STATUSES:
        # Already approved/being sent/sent — reject no longer applies;
        # undo is the correct action within the window instead.
        raise _already_sent_error()

    if status in ("stale", "cancelled"):
        fresh_id = await _fresh_pending_draft_id(session, case_id=case_id)
        raise _draft_stale_error(fresh_id)

    resume_value: dict[str, Any] = {"action": ACTION_REJECT, "note": note}
    try:
        await resolve_draft_decision(case_id=case_id, draft_id=draft_id, resume_value=resume_value)
    except (DraftStaleError, CaseNotAwaitingApprovalError):
        return await _reconcile_reject_conflict(
            session, landlord=landlord, draft_id=draft_id, case_id=case_id
        )

    return RejectResponse(status="rejected")


# ---------------------------------------------------------------------------
# undo (DELETE .../approve) — never touches the resume seam (module
# docstring above): the graph thread already fully resumed at approve
# time; undo is a pure, atomically-guarded DB revert.
# ---------------------------------------------------------------------------


async def _undo(*, session: AsyncSession, landlord: Landlord, draft_id: UUID) -> UndoResponse:
    row = (
        (
            await session.execute(
                _REVERT_TO_PENDING_SQL,
                {"draft_id": str(draft_id), "landlord_id": str(landlord.id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is not None:
        await session.execute(
            _INSERT_SEND_CANCELLED_AUDIT_SQL,
            {
                "landlord_id": str(landlord.id),
                "case_id": str(row["case_id"]),
                "payload": json.dumps({"draft_id": str(draft_id)}),
            },
        )
        return UndoResponse(status="pending")

    draft = await _load_draft(session, draft_id=draft_id, landlord_id=landlord.id)
    if draft is None:
        raise _not_found_error()
    if draft["status"] == "pending":
        # Already undone, or never approved to begin with — idempotent.
        return UndoResponse(status="pending")
    if draft["status"] in ("sending", "sent"):
        # The TRUE "already gone out" reality.
        raise _already_sent_error()
    # stale / rejected / cancelled — never approved (or no longer is), so
    # "already gone out" would be a FALSE statement (safety-guardian,
    # MAJOR, this round): distinct, accurate code.
    raise _draft_not_undoable_error()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{draft_id}/approve", response_model=ApproveResponse)
async def approve_draft(
    draft_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> ApproveResponse:
    """``POST /v1/drafts/{id}/approve`` — schedules the send 5s from now
    (the undo window is data — ``drafts.scheduled_send_at``); the actual
    Twilio send is a later, independent event (``app/agent/
    draft_sender.py``)."""
    landlord, session = landlord_and_session
    return await _approve_or_edit(
        session=session,
        landlord=landlord,
        draft_id=draft_id,
        action=ACTION_APPROVE,
        edited_body=None,
    )


@router.delete("/{draft_id}/approve", response_model=UndoResponse)
async def undo_approval(
    draft_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> UndoResponse:
    """``DELETE /v1/drafts/{id}/approve`` — cancels within the undo
    window; 409 ``already_sent`` once the sender has claimed/sent it."""
    landlord, session = landlord_and_session
    return await _undo(session=session, landlord=landlord, draft_id=draft_id)


@router.post("/{draft_id}/reject", response_model=RejectResponse)
async def reject_draft(
    draft_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
    payload: RejectRequest | None = None,
) -> RejectResponse:
    """``POST /v1/drafts/{id}/reject`` — draft archived, case stays open."""
    landlord, session = landlord_and_session
    note = payload.note if payload is not None else None
    return await _reject(session=session, landlord=landlord, draft_id=draft_id, note=note)


@router.post("/{draft_id}/edit-and-send", response_model=ApproveResponse)
async def edit_and_send_draft(
    draft_id: UUID,
    payload: EditAndSendRequest,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> ApproveResponse:
    """``POST /v1/drafts/{id}/edit-and-send`` — same response/undo window
    as approve; the ORIGINAL ``drafts.body`` is retained, the edited text
    lands in ``drafts.final_body``, ``edited=true`` (recorded as a
    non-clean approval for trust metrics once the sender actually sends
    it — ``app/agent/draft_sender.py``)."""
    landlord, session = landlord_and_session
    return await _approve_or_edit(
        session=session,
        landlord=landlord,
        draft_id=draft_id,
        action=ACTION_EDIT_AND_SEND,
        edited_body=payload.body,
    )


__all__: list[str] = ["router"]
