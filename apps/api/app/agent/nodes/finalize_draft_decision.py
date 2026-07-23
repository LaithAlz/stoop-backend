"""``finalize_approval`` / ``finalize_rejection`` nodes (#44/#45) — the
SEPARATE, send-adjacent nodes ``app/agent/nodes/await_approval.py``'s own
docstring ("Structural invariant for #44/#45") mandates: everything that
happens once a landlord acts on a paused draft (approve / reject /
edit-and-send) lives HERE, reached only via
``app/agent/graph.py``'s ``_route_after_await_approval`` conditional edge,
never as code appended after ``interrupt()`` inside ``await_approval``
itself.

Vocabulary — the ``resume_value`` shape (#44/#45's own, #43 left this
undefined on purpose — see ``app/agent/graph.py``'s "resume seam" module
docstring section)
------------------------------------------------------------------------
``{"action": ACTION_APPROVE}``
``{"action": ACTION_REJECT, "note": str | None}``
``{"action": ACTION_EDIT_AND_SEND, "body": str}``

Any other shape (missing/malformed ``action``, or a value outside this
vocabulary) is a "drain-sentinel-class" value — ``app/agent/graph.py``'s
router logs it as an anomaly and routes straight to ``END`` without
reaching either node in this module, so an unrecognized resume can never
approve or send anything.

**#122 addition — an optional ``"source": "sms"`` key on
``ACTION_APPROVE``/``ACTION_EDIT_AND_SEND``:** approve-by-SMS
(``app/agent/approve_by_sms.py``) is a THIRD caller of the shared
approve/reject writer below (alongside the dashboard's
``resume_case_thread`` graph-resume path and the never-paused fallback,
``app/agent/graph.py::_finalize_never_paused_draft`` — both already
existed before #122). It sets ``resume_value["source"] = "sms"`` so
:func:`finalize_approval` (and, for the never-paused path,
``_finalize_never_paused_draft`` directly) can pass the SMS undo window
(:data:`SMS_UNDO_WINDOW`, +5 minutes — api-contracts.md, "approve-by-SMS:
+5min") and ``approved_via='sms'`` (schema-v1.md v1.16) through to
:func:`apply_approve_or_edit`, instead of the dashboard's default +5s/
``'dashboard'``. Absent entirely (every OTHER caller, unchanged) means the
ORIGINAL dashboard behavior exactly — this key is additive, never a
behavior change for an existing caller that doesn't set it. Reject
(``ACTION_REJECT``) needs no such key: the reject write itself
(:func:`apply_rejection`) has no undo-window/approved_via concept at
all, on either channel.

What this module does NOT do
-----------------------------
It never calls an SMS-sending client. Approving/editing a draft here only
marks it ``drafts.status = 'approved'`` and sets
``scheduled_send_at = now() + UNDO_WINDOW`` — the actual Twilio send is a
LATER, independent event driven by ``app/agent/draft_sender.py``'s ticker,
which claims ``approved`` rows once their ``scheduled_send_at`` is due
(schema-v1.md: "the sender only sends rows whose time has come and whose
status is still approved" — the undo window is data, never a sleep here).
This keeps the per-case advisory lock's critical section
(``app/agent/graph._case_lock``) short: no network call, no external
dependency, ever runs while that lock is held.

Two entry paths, ONE shared write path
----------------------------------------
:func:`finalize_approval` / :func:`finalize_rejection` are the GRAPH NODES,
invoked by a live ``Command(resume=...)`` via
``app.agent.graph.resume_case_thread``. A SECOND, narrower entry path
exists for degraded-/emergency-drafted pending drafts that were never
actually paused behind a live interrupt at all (see
``CaseNotAwaitingApprovalError``'s own docstring, cause 1, and
``app/agent/graph.py``'s ``resolve_draft_decision``/
``_finalize_never_paused_draft`` for the founder-directed decision on
this #44-pinned open question) — that path reuses
:func:`apply_approve_or_edit` / :func:`apply_rejection` DIRECTLY, under the
SAME per-case advisory lock, so there is exactly ONE place in the codebase
that ever writes ``drafts.status = 'approved'``/``'rejected'`` regardless
of which path got there.

cases.status
------------
Approve / edit-and-send do NOT touch ``cases.status`` here — by the time
this node runs on the NORMAL path, ``mark_awaiting_approval`` already set
it to ``'awaiting_approval'`` (this issue's own module docstring precedent:
``draft_response``/``mark_awaiting_approval`` split cleanly by
responsibility, never re-touched downstream). ``cases.status`` next moves
to ``'awaiting_tenant'`` only once the sender ticker actually sends the
message (a case whose draft merely got approved hasn't heard back from the
tenant yet — that transition belongs to the send event, not the approval
event). Reject moves ``cases.status`` back to ``'open'`` — the AC's own
words ("draft archived, case stays open"): the landlord's approval-queue
backlog item for this case is gone, but the case itself is still active
work.

Audit vocabulary (schema-v1.md, no new values needed)
-------------------------------------------------------
``approved`` / ``edited`` / ``rejected`` — all already in ``audit_log.
action``'s CHECK constraint. ``actor='landlord'`` for all three (a human
decision), matching every other landlord-authored audit row precedent
in this codebase style (contrast with ``actor='system'`` for the
sender's own eventual ``'sent'`` row).

Never-break rule #5: only uuids/booleans/short reason strings ever reach
``log.*`` calls here — an edited draft's landlord-authored replacement
text and a reject ``note`` may go into ``audit_log.payload`` (an ordinary
DB row, same "raw text in DB payloads is fine, never in log lines"
precedent every other node in this package already relies on), never into
a log line.

DB access
---------
Admin engine, same pattern as every other node in this package —
allowlisted in ``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sentry_sdk
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import CaseContext
from app.agent.state import AgentState
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# resume_value vocabulary — see module docstring.
# ---------------------------------------------------------------------------

ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_EDIT_AND_SEND = "edit_and_send"

UNDO_WINDOW = timedelta(seconds=5)
"""Dashboard undo window (api-contracts.md: "+5s")."""

SMS_UNDO_WINDOW = timedelta(minutes=5)
"""Approve-by-SMS's own undo window (api-contracts.md: "+5min" — SMS has no
undo bar, so the window is wider to give a landlord a realistic chance to
reply ``UNDO``). Selected via ``resume_value["source"] == "sms"`` — see
module docstring "#122 addition"."""

_APPROVED_VIA_DASHBOARD = "dashboard"
_APPROVED_VIA_SMS = "sms"

# ---------------------------------------------------------------------------
# SQL — shared by both the graph-node path and the non-graph fallback path
# (app/agent/graph.py's _finalize_never_paused_draft).
# ---------------------------------------------------------------------------

_SELECT_PENDING_DRAFT_FOR_CASE_SQL = text(
    "SELECT id FROM drafts WHERE case_id = :case_id AND status = 'pending'"
)

_APPROVE_OR_EDIT_DRAFT_SQL = text(
    "UPDATE drafts SET status = 'approved', scheduled_send_at = :scheduled_send_at, "
    "approved_via = :approved_via, "
    "edited = :edited, final_body = :final_body, updated_at = now() "
    "WHERE id = :draft_id AND status = 'pending' "
    "RETURNING id"
)

_INSERT_APPROVED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'landlord', 'approved', CAST(:payload AS jsonb))"
)

_INSERT_EDITED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'landlord', 'edited', CAST(:payload AS jsonb))"
)

_REJECT_DRAFT_SQL = text(
    "UPDATE drafts SET status = 'rejected', updated_at = now() "
    "WHERE id = :draft_id AND status = 'pending' RETURNING id"
)

_REOPEN_CASE_TO_OPEN_SQL = text(
    "UPDATE cases SET status = 'open', last_activity_at = now(), updated_at = now() "
    "WHERE id = :case_id"
)

_INSERT_REJECTED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'landlord', 'rejected', CAST(:payload AS jsonb))"
)

# #60 (AC-1) — a rejection resets the SAME consecutive_clean streak an
# edit resets (draft_sender.py's own upsert), and increments `rejections`.
# Upserted (never select-then-insert) exactly like every other trust_metrics
# writer in this codebase — a case can be rejected before any send has ever
# happened for its (property, severity) pairing, so the row may not exist
# yet.
_SELECT_CASE_PROPERTY_SEVERITY_SQL = text(
    "SELECT property_id, severity FROM cases WHERE id = :case_id"
)

_UPSERT_TRUST_METRICS_REJECTION_SQL = text(
    "INSERT INTO trust_metrics (landlord_id, property_id, severity, rejections, consecutive_clean) "
    "VALUES (:landlord_id, :property_id, :severity, 1, 0) "
    "ON CONFLICT (property_id, severity) DO UPDATE SET "
    "rejections = trust_metrics.rejections + 1, "
    "consecutive_clean = 0, "
    "updated_at = now()"
)


async def apply_approve_or_edit(
    session: AsyncSession,
    *,
    landlord_id: UUID,
    case_id: UUID,
    draft_id: UUID,
    action: str,
    edited_body: str | None,
    now: datetime | None = None,
    undo_window: timedelta = UNDO_WINDOW,
    approved_via: str = _APPROVED_VIA_DASHBOARD,
) -> datetime | None:
    """Mark *draft_id* ``approved`` + schedule its send — shared by
    :func:`finalize_approval` (graph node) and
    ``app.agent.graph._finalize_never_paused_draft`` (the degraded-path
    fallback). Returns the computed ``scheduled_send_at`` on success,
    ``None`` if *draft_id* was no longer ``'pending'`` at write time
    (structurally shouldn't happen — both callers re-verify pending-ness
    under the SAME per-case advisory lock immediately before this call;
    kept as a defensive, non-raising signal rather than an assumed
    invariant, matching this codebase's established style elsewhere).

    ``action == ACTION_EDIT_AND_SEND`` sets ``edited=true`` and
    ``final_body=edited_body`` (the ORIGINAL ``drafts.body`` is never
    touched — "original + edit both retained", #45's own AC); any other
    action leaves both at their non-edited defaults.

    *undo_window*/*approved_via* (#122) default to the dashboard's own
    +5s/``'dashboard'`` — every pre-#122 caller is byte-for-byte unchanged.
    Approve-by-SMS is the only caller that ever passes
    ``undo_window=SMS_UNDO_WINDOW, approved_via='sms'`` (see module
    docstring "#122 addition").
    """
    effective_now = now or datetime.now(UTC)
    scheduled_send_at = effective_now + undo_window
    edited = action == ACTION_EDIT_AND_SEND

    row = (
        (
            await session.execute(
                _APPROVE_OR_EDIT_DRAFT_SQL,
                {
                    "draft_id": str(draft_id),
                    "scheduled_send_at": scheduled_send_at,
                    "approved_via": approved_via,
                    "edited": edited,
                    "final_body": edited_body if edited else None,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None

    audit_sql = _INSERT_EDITED_AUDIT_SQL if edited else _INSERT_APPROVED_AUDIT_SQL
    await session.execute(
        audit_sql,
        {
            "landlord_id": str(landlord_id),
            "case_id": str(case_id),
            "payload": json.dumps({"draft_id": str(draft_id)}),
        },
    )
    return scheduled_send_at


async def apply_rejection(
    session: AsyncSession,
    *,
    landlord_id: UUID,
    case_id: UUID,
    draft_id: UUID,
    note: str | None,
) -> bool:
    """Mark *draft_id* ``rejected`` and move the case back to ``'open'`` —
    shared by :func:`finalize_rejection` (graph node) and the degraded-path
    fallback. Returns ``True`` on success, ``False`` if *draft_id* was no
    longer ``'pending'`` (defensive — see :func:`apply_approve_or_edit`'s
    own docstring for why this is structurally unexpected, not assumed
    impossible).

    #60 (AC-1): also updates ``trust_metrics`` in this SAME transaction —
    ``rejections + 1``, ``consecutive_clean = 0`` — for the case's own
    ``(property_id, severity)`` pairing, mirroring the SAME upsert
    discipline ``app/agent/draft_sender.py``'s clean/edited path already
    uses. Skipped (logged + paged, same pattern as that module's own
    missing-severity branch) when ``cases.severity IS NULL`` — a legacy
    pre-#197 case, or a genuine anomaly — there is no ``(property,
    severity)`` key to upsert against.
    """
    row = (
        (await session.execute(_REJECT_DRAFT_SQL, {"draft_id": str(draft_id)}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return False

    await session.execute(_REOPEN_CASE_TO_OPEN_SQL, {"case_id": str(case_id)})

    case_row = (
        (await session.execute(_SELECT_CASE_PROPERTY_SEVERITY_SQL, {"case_id": str(case_id)}))
        .mappings()
        .one()
    )
    property_id: UUID = case_row["property_id"]
    severity: str | None = case_row["severity"]
    if severity is not None:
        await session.execute(
            _UPSERT_TRUST_METRICS_REJECTION_SQL,
            {
                "landlord_id": str(landlord_id),
                "property_id": str(property_id),
                "severity": severity,
            },
        )
    else:
        log.error("finalize_rejection_missing_severity_for_trust_metrics", case_id=str(case_id))
        sentry_sdk.capture_message(
            "finalize_rejection: cases.severity is NULL on reject -- trust_metrics not "
            "updated for this rejection",
            level="error",
            extras={"case_id": str(case_id), "draft_id": str(draft_id)},
        )

    await session.execute(
        _INSERT_REJECTED_AUDIT_SQL,
        {
            "landlord_id": str(landlord_id),
            "case_id": str(case_id),
            "payload": json.dumps({"draft_id": str(draft_id), "note": note}),
        },
    )
    return True


# ---------------------------------------------------------------------------
# Graph nodes — reached ONLY via app/agent/graph.py's
# _route_after_await_approval conditional edge.
# ---------------------------------------------------------------------------


def undo_window_and_approved_via(resume: dict[str, Any] | None) -> tuple[timedelta, str]:
    """#122 addition — see module docstring "#122 addition". Public (not
    module-private) because ``app/agent/graph.py``'s
    ``_finalize_never_paused_draft`` also needs it, for the SAME
    ``resume_value["source"]`` vocabulary, on its own never-paused-draft
    entry path. Absent/anything-other-than-``"sms"`` ``resume["source"]``
    is the ORIGINAL dashboard behavior, byte-for-byte: +5s /
    ``'dashboard'``."""
    source = resume.get("source") if isinstance(resume, dict) else None
    if source == "sms":
        return SMS_UNDO_WINDOW, _APPROVED_VIA_SMS
    return UNDO_WINDOW, _APPROVED_VIA_DASHBOARD


async def finalize_approval(state: AgentState) -> dict[str, Any]:
    """Handles BOTH ``ACTION_APPROVE`` and ``ACTION_EDIT_AND_SEND`` — the
    two actions differ only in whether a landlord-authored replacement
    body is recorded (see :func:`apply_approve_or_edit`); both schedule the
    same undo-delayed send (dashboard: 5s; approve-by-SMS: 5min — see
    :func:`undo_window_and_approved_via`)."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    resume = state.get("approval_resume")
    action = resume.get("action") if isinstance(resume, dict) else None
    undo_window, approved_via = undo_window_and_approved_via(resume)
    case_id = case_context.case_id
    landlord_id = case_context.landlord_id

    if case_id is None or landlord_id is None:  # pragma: no cover — defensive
        log.error("finalize_approval_missing_case_context", message_id=str(message_id))
        return {"reasoning_log": reasoning_log}

    async with asynccontextmanager(get_admin_session)() as session:
        pending_row = (
            (await session.execute(_SELECT_PENDING_DRAFT_FOR_CASE_SQL, {"case_id": str(case_id)}))
            .mappings()
            .one_or_none()
        )
        if pending_row is None:
            # Structurally shouldn't happen: resume_case_thread/
            # resolve_draft_decision already re-verified pending-ness under
            # the SAME per-case lock this node runs inside — defensive only.
            log.error("finalize_approval_no_pending_draft", case_id=str(case_id))
            reasoning_log.append(
                "I couldn't find the reply to finish approving — nothing was sent."
            )
            return {"reasoning_log": reasoning_log}
        draft_id = pending_row["id"]

        edited_body = (
            resume.get("body")
            if action == ACTION_EDIT_AND_SEND and isinstance(resume, dict)
            else None
        )
        scheduled_send_at = await apply_approve_or_edit(
            session,
            landlord_id=landlord_id,
            case_id=case_id,
            draft_id=draft_id,
            action=action or ACTION_APPROVE,
            edited_body=edited_body,
            undo_window=undo_window,
            approved_via=approved_via,
        )

    if scheduled_send_at is None:  # pragma: no cover — see apply_approve_or_edit docstring
        log.error(
            "finalize_approval_draft_no_longer_pending",
            case_id=str(case_id),
            draft_id=str(draft_id),
        )
        reasoning_log.append("This reply was already handled, so I didn't change anything.")
        return {"reasoning_log": reasoning_log}

    if action == ACTION_EDIT_AND_SEND:
        reasoning_log.append(
            "You edited this reply before sending it — it'll go out in a few seconds "
            "unless you undo it."
        )
    else:
        reasoning_log.append(
            "You approved this reply — it'll go out in a few seconds unless you undo it."
        )
    log.info(
        "finalize_approval_done",
        message_id=str(message_id),
        case_id=str(case_id),
        draft_id=str(draft_id),
        action=action,
    )
    return {"reasoning_log": reasoning_log}


async def finalize_rejection(state: AgentState) -> dict[str, Any]:
    """Handles ``ACTION_REJECT`` — marks the pending draft ``rejected`` and
    moves the case back to ``'open'`` (AC: "draft archived, case stays
    open"). Never schedules anything — this node's own graph edge goes
    straight to ``END``."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    resume = state.get("approval_resume")
    note = resume.get("note") if isinstance(resume, dict) else None
    case_id = case_context.case_id
    landlord_id = case_context.landlord_id

    if case_id is None or landlord_id is None:  # pragma: no cover — defensive
        log.error("finalize_rejection_missing_case_context", message_id=str(message_id))
        return {"reasoning_log": reasoning_log}

    async with asynccontextmanager(get_admin_session)() as session:
        pending_row = (
            (await session.execute(_SELECT_PENDING_DRAFT_FOR_CASE_SQL, {"case_id": str(case_id)}))
            .mappings()
            .one_or_none()
        )
        if pending_row is None:  # pragma: no cover — defensive, see module docstring
            log.error("finalize_rejection_no_pending_draft", case_id=str(case_id))
            reasoning_log.append("I couldn't find the reply to reject — nothing changed.")
            return {"reasoning_log": reasoning_log}
        draft_id = pending_row["id"]

        applied = await apply_rejection(
            session, landlord_id=landlord_id, case_id=case_id, draft_id=draft_id, note=note
        )

    if not applied:  # pragma: no cover — see apply_rejection docstring
        log.error(
            "finalize_rejection_draft_no_longer_pending",
            case_id=str(case_id),
            draft_id=str(draft_id),
        )
        reasoning_log.append("This reply was already handled, so I didn't change anything.")
        return {"reasoning_log": reasoning_log}

    reasoning_log.append("You didn't send this reply, so I've kept the conversation open.")
    log.info(
        "finalize_rejection_done",
        message_id=str(message_id),
        case_id=str(case_id),
        draft_id=str(draft_id),
    )
    return {"reasoning_log": reasoning_log}


__all__: list[str] = [
    "ACTION_APPROVE",
    "ACTION_EDIT_AND_SEND",
    "ACTION_REJECT",
    "SMS_UNDO_WINDOW",
    "UNDO_WINDOW",
    "apply_approve_or_edit",
    "apply_rejection",
    "finalize_approval",
    "finalize_rejection",
    "undo_window_and_approved_via",
]
