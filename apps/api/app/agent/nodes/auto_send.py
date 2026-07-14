"""``auto_send_draft`` node (#60) — the trust ladder's auto-send exit.

This is the ONLY sanctioned exception to landlord approval besides the
emergency safety path (``apps/api/CLAUDE.md`` / root ``CLAUDE.md`` rule 3:
"Nothing sends to a tenant or vendor without landlord approval, except
emergency safety instructions. Auto-send exists only via the trust ladder,
only for `routine`, per `(property, severity)`.").

Reached from
------------
``app/agent/graph.py``'s ``_route_after_draft_response`` — inserted BETWEEN
``draft_response``'s conditional exit and ``mark_awaiting_approval``, for
the ``ROUTINE``, non-degraded case only. The EMERGENCY /
``draft_guard_failed`` degraded-mode exit is checked FIRST by that router
and unconditionally bypasses this node entirely — same as it already
bypasses ``mark_awaiting_approval`` (see that router's own docstring).

Two-layer eligibility check — a cheap READ picks the edge, an ATOMIC WRITE
re-verifies before ever touching a draft (belt-and-braces, #60's explicit
safety-review requirement)
------------------------------------------------------------------------
1. ``_route_after_draft_response`` does a plain, unlocked SELECT
   (``app.trust.is_routine_autonomy_unlocked``) to decide WHETHER to route
   here at all — ``state["severity"]`` must be ``Severity.ROUTINE`` AND
   that read must return ``True``. ANY exception during that read is
   treated as "not eligible" (fail-closed to ``mark_awaiting_approval`` —
   never to auto-send) — see that router's own docstring.
2. This node's OWN write (:func:`apply_auto_send`) re-verifies the
   IDENTICAL condition, hardcoded in SQL (``severity = 'routine'`` is a
   literal in the query text, never a bound parameter — CLAUDE.md rule 3's
   "only for routine" is enforced at the database level, not just by
   whichever edge the router happened to take), in the SAME statement that
   flips the draft to ``'approved'``. A landlord revoking autonomy in the
   (sub-millisecond) window between the router's read and this write loses
   the race correctly: the ``UPDATE``'s ``EXISTS`` clause simply matches
   zero rows, :func:`apply_auto_send` returns ``None``, and this node falls
   back to the normal approval pause instead of ever writing anything.
   The ``EXISTS`` also carries an explicit ``c.landlord_id = :landlord_id``
   predicate (safety review LOW-4) — *landlord_id* is already in scope
   here for the ``auto_sent`` audit row, so this send-fence writer never
   relies on transitive scoping through ``case_id`` alone, matching this
   codebase's "every multi-tenant query scoped by landlord_id" convention
   (``apps/api/CLAUDE.md``) even in a query that would already be correct
   without it.

Fail-closed, never fail-open
------------------------------
Both layers above resolve any uncertainty (an exception, a lost race, a
missing row) to the SAME outcome: fall back to ``mark_awaiting_approval``.
A draft is never left neither auto-sent NOR pending landlord review — see
``state["auto_send_fallback"]``'s own docstring
(``app/agent/state.py``) and ``app/agent/graph.py``'s
``_route_after_auto_send_draft`` conditional edge, which reads that flag.

What this node does NOT do
-----------------------------
It never calls an SMS-sending client (mirrors ``finalize_draft_decision
.py``'s own "What this module does NOT do"). The actual Twilio send is a
LATER, independent event driven by ``app/agent/draft_sender.py``'s ticker,
UNCHANGED by this feature: the ticker claims ANY ``'approved'`` row whose
``scheduled_send_at`` is due, regardless of whether a landlord's approve
tap or the trust ladder put it there. No new send call site — this
codebase's allowlisted three files
(``tests/test_twilio_send_allowlist.py``) are untouched by this issue.

``cases.status`` — deliberately left untouched
--------------------------------------------------
Unlike ``mark_awaiting_approval``, this node never sets ``cases.status =
'awaiting_approval'`` — there is nothing for a landlord to approve. The
case stays whatever ``draft_response`` left it at (``'open'`` in the
ordinary case) until the sender ticker actually sends the message and
flips it to ``'awaiting_tenant'`` — exactly the same transition an
ordinary landlord-approved send goes through. ``GET /v1/queue`` therefore
does NOT surface an auto-sent case (its own module docstring already flags
this as a deferred "auto-handled feed", #56/#60) — the case remains
visible and informational via ``GET /v1/cases``/``GET /v1/cases/{id}``
throughout, and its ``audit_log`` carries the ``'auto_sent'`` row.

DB access
---------
Admin engine — same background/graph-context, pre-identity rationale as
every other node in this package. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.nodes.finalize_draft_decision import UNDO_WINDOW
from app.agent.schemas import CaseContext
from app.agent.state import AgentState
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

_SELECT_PENDING_DRAFT_ID_SQL = text(
    "SELECT id FROM drafts WHERE case_id = :case_id AND status = 'pending'"
)

# Belt-and-braces (module docstring, point 2): 'routine' is a LITERAL in
# this query text, never a bound parameter -- an urgent/emergency case can
# never satisfy `c.severity = 'routine'` no matter what state this node is
# accidentally invoked with, and a revoked/never-graduated property can
# never satisfy the trust_metrics predicate either.
_AUTO_SEND_DRAFT_SQL = text(
    "UPDATE drafts SET status = 'approved', scheduled_send_at = :scheduled_send_at, "
    "auto_send = true, edited = false, final_body = NULL, updated_at = now() "
    "WHERE id = :draft_id AND status = 'pending' "
    "AND EXISTS ("
    "  SELECT 1 FROM cases c "
    "  JOIN trust_metrics tm ON tm.property_id = c.property_id AND tm.severity = 'routine' "
    "  WHERE c.id = :case_id AND c.landlord_id = :landlord_id AND c.severity = 'routine' "
    "    AND tm.autonomy_unlocked = true AND tm.revoked_at IS NULL"
    ") "
    "RETURNING id"
)

_INSERT_AUTO_SENT_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'agent', 'auto_sent', CAST(:payload AS jsonb))"
)


async def apply_auto_send(
    session: AsyncSession,
    *,
    landlord_id: UUID,
    case_id: UUID,
    draft_id: UUID,
    now: datetime | None = None,
) -> datetime | None:
    """Auto-approve *draft_id* on behalf of the trust ladder (#60).
    Schedules the SAME 5s undo window ``apply_approve_or_edit`` uses (a
    landlord can still undo an auto-sent draft within the window) and
    stamps the audit trail ``'auto_sent'``/``actor='agent'`` instead of
    ``'approved'``/``actor='landlord'``.

    Returns the computed ``scheduled_send_at`` on success, ``None`` if
    *draft_id* was no longer ``'pending'`` OR the belt-and-braces ``EXISTS``
    predicate in :data:`_AUTO_SEND_DRAFT_SQL` no longer holds (module
    docstring, "Two-layer eligibility check") — the caller's contract
    (mirroring ``apply_approve_or_edit``) is to fall back to the normal
    approval path when this returns ``None``, never to retry or assume
    success.
    """
    effective_now = now or datetime.now(UTC)
    scheduled_send_at = effective_now + UNDO_WINDOW

    row = (
        (
            await session.execute(
                _AUTO_SEND_DRAFT_SQL,
                {
                    "draft_id": str(draft_id),
                    "case_id": str(case_id),
                    "landlord_id": str(landlord_id),
                    "scheduled_send_at": scheduled_send_at,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None

    await session.execute(
        _INSERT_AUTO_SENT_AUDIT_SQL,
        {
            "landlord_id": str(landlord_id),
            "case_id": str(case_id),
            "payload": json.dumps({"draft_id": str(draft_id)}),
        },
    )
    return scheduled_send_at


async def auto_send_draft(state: AgentState) -> dict[str, Any]:
    """The graph node — see module docstring. Always sets
    ``state["auto_send_fallback"]`` explicitly (never leaves it unset),
    both to signal ``app/agent/graph.py``'s ``_route_after_auto_send_draft``
    conditional edge and to defuse the "stale checkpoint value carries
    forward" hazard ``app/agent/nodes/await_approval.py``'s own "Hardening"
    section documents for the analogous ``approval_resume`` key."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    case_id = case_context.case_id
    landlord_id = case_context.landlord_id

    if case_id is None or landlord_id is None:  # pragma: no cover — defensive
        # The router that routes here only does so once it has already
        # resolved a real property_id (and therefore a real case_id/
        # landlord_id -- case_context is populated as a unit by
        # identify_property/load_context) -- kept as a non-raising
        # fallback rather than an assumed invariant, matching this
        # package's established style elsewhere.
        log.error("auto_send_draft_missing_case_context", message_id=str(message_id))
        return {"reasoning_log": reasoning_log, "auto_send_fallback": True}

    async with asynccontextmanager(get_admin_session)() as session:
        pending_row = (
            (await session.execute(_SELECT_PENDING_DRAFT_ID_SQL, {"case_id": str(case_id)}))
            .mappings()
            .one_or_none()
        )
        if pending_row is None:  # pragma: no cover — defensive, see await_approval.py precedent
            log.error("auto_send_draft_no_pending_draft", case_id=str(case_id))
            reasoning_log.append(
                "I couldn't find a reply to send automatically — nothing was sent; this "
                "will be retried."
            )
            return {"reasoning_log": reasoning_log, "auto_send_fallback": True}
        draft_id = pending_row["id"]

        scheduled_send_at = await apply_auto_send(
            session, landlord_id=landlord_id, case_id=case_id, draft_id=draft_id
        )

    if scheduled_send_at is None:
        # The belt-and-braces SQL predicate didn't match (a revoke raced
        # this write, or some other genuinely-no-longer-eligible edge) --
        # fail closed to the normal human-approval pause rather than
        # silently doing nothing.
        log.info(
            "auto_send_draft_ineligible_falling_back",
            case_id=str(case_id),
            draft_id=str(draft_id),
        )
        reasoning_log.append(
            "This looked routine enough to send automatically, but I'm waiting for your "
            "approval on this one instead."
        )
        return {"reasoning_log": reasoning_log, "auto_send_fallback": True}

    reasoning_log.append(
        "This is routine and you've trusted me with replies like this before, so I sent "
        "it automatically — you can undo it for a few seconds if you want to stop it."
    )
    log.info(
        "auto_send_draft_done",
        message_id=str(message_id),
        case_id=str(case_id),
        draft_id=str(draft_id),
    )
    return {"reasoning_log": reasoning_log, "auto_send_fallback": False}


__all__: list[str] = ["apply_auto_send", "auto_send_draft"]
