"""``mark_awaiting_approval`` + ``await_approval`` nodes (#43) — shadow
mode: pause the graph with LangGraph's dynamic ``interrupt()`` BEFORE any
send, on the normal (non-degraded, non-emergency) exit of
``draft_response``.

Routed to by ``app/agent/graph.py``'s ``_route_after_draft_response`` —
the ONE edge that module's own docstring ("Seam for #43") already flagged:
``NODE_DRAFT_RESPONSE -> END`` (the plain exit) becomes
``NODE_DRAFT_RESPONSE -> mark_awaiting_approval -> await_approval -> END``.
The ``draft_guard_failed`` / LLM-classified-EMERGENCY exit to
``degraded_mode -> END`` is UNCHANGED and completely independent of this
pair — see "Never trapped behind the pause" below.

TWO nodes, not one — why the commit and the pause are split (empirical
finding, not assumed)
------------------------------------------------------------------------
``interrupt()`` is not a mid-function continuation primitive: LangGraph
RE-EXECUTES A PAUSED NODE FUNCTION FROM ITS OWN TOP on every attempt (the
first pause, and any later drain/resume) until the specific ``interrupt()``
call in that attempt receives a resume value instead of raising. Critically
(probed against a real Postgres checkpointer): **nothing a node does BEFORE
an ``interrupt()`` call that then raises is ever committed to the
checkpoint** — the node's eventual ``return`` value is what gets written to
state, and that write never happens on an attempt that raises. A first
revision of this module put the ``cases.status`` UPDATE, the
``reasoning_log`` append, AND the ``interrupt()`` call all in ONE node —
which meant the "waiting for your approval" reasoning_log line was NEVER
actually durable while the case sat paused (exactly the state a landlord's
approval-card read would see): it only would have been committed on the
attempt that finally got resumed, by which point the case is no longer
waiting. Caught by this issue's own integration test asserting the line is
present in the state returned WHILE PAUSED, not just after resuming.

Fixed by splitting into two nodes:

- :func:`mark_awaiting_approval` — a PLAIN node (no ``interrupt()`` call
  anywhere in it). Sets ``cases.status = 'awaiting_approval'`` (idempotent
  UPDATE) and appends the landlord-facing reasoning_log line, then
  RETURNS NORMALLY. Because it never raises, LangGraph commits its return
  value to the checkpoint and never re-executes it again for this task
  (verified: a plain node that fully completes before a downstream node's
  interrupt() is reached is NOT replayed by later drain/resume attempts on
  that downstream node — its own committed output persists across them).
- :func:`await_approval` — the actual pause. Has no side effects of its
  own other than ``interrupt()`` (its interrupt payload's ``draft_id`` is
  a plain re-query, safe to repeat on every attempt), so re-execution on
  drain/resume is a complete non-issue.

Owns the ``cases.status`` transition ``draft_response`` deliberately does
not (see that node's own docstring, "Reported gap: ``drafts.status``
vocabulary vs. the issue text"). Vocabulary split, stated once here because
it is easy to trip over: ``cases.status`` HAS ``awaiting_approval``;
``drafts.status`` does NOT (a draft stays ``'pending'`` the whole time it
sits behind this pause — schema-v1.md's CHECK constraints for each table
differ on purpose).

Never trapped behind the pause — the degraded/emergency exit bypasses this
pair entirely
------------------------------------------------------------------------
``app/agent/graph.py``'s ``_route_after_draft_response`` checks
``draft_guard_failed`` and LLM-classified ``Severity.EMERGENCY`` BEFORE
ever reaching ``mark_awaiting_approval``'s edge — either trigger routes
straight to ``degraded_mode -> END`` instead, so a ``needs_eyes``
notification for those cases is written and the run reaches ``END``
WITHOUT ever pausing here. The interim emergency/degraded-mode path (#34's
own documented seam, "Interim, not #108") is therefore never blocked
behind an unresumed approval interrupt — verified directly (see
``tests/test_agent_shadow_interrupt.py``'s
``test_llm_emergency_bypasses_the_pause_never_traps_needs_eyes`` and
``test_draft_guard_failed_bypasses_the_pause``).

Stale-draft re-run interaction (the #34 spec-review pinned warning) — see
``app/agent/graph.py``'s module docstring "Draining a pending interrupt
before a stale-draft re-run" for the full design and the empirical finding
that made it necessary. In short: ``await_approval``'s interrupt payload
carries ``case_id``/``draft_id`` so a later drain/resume call can be
matched against the CURRENT pending draft, but the actual staleness check
lives in ``app/agent/graph.py::resume_case_thread`` (the #44/#45 resume
seam), not here — neither node has any branching logic on the resume
value at all in #43's scope (that is #44/#45's territory).

Never-break rule #5: only uuids/booleans ever reach ``log.*`` calls here —
never a message body or phone number. The ``reasoning_log`` line is
landlord-facing copy (approval-card, CLAUDE.md rule #8): warm, plain
English, no ids.

DB access
---------
Admin engine, same pattern as every other node in this package.
Allowlisted in ``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import structlog
from langgraph.types import interrupt
from sqlalchemy import text

from app.agent.schemas import CaseContext
from app.agent.state import AgentState
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

_MARK_AWAITING_APPROVAL_SQL = text(
    "UPDATE cases SET status = 'awaiting_approval', updated_at = now() WHERE id = :case_id"
)

_SELECT_PENDING_DRAFT_ID_SQL = text(
    "SELECT id FROM drafts WHERE case_id = :case_id AND status = 'pending'"
)


async def mark_awaiting_approval(state: AgentState) -> dict[str, Any]:
    """Set ``cases.status = 'awaiting_approval'`` and append the
    landlord-facing reasoning_log line. A PLAIN node — no ``interrupt()``
    here, so this always completes and commits exactly once (see module
    docstring "TWO nodes, not one")."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    case_id = case_context.case_id

    if case_id is None:  # pragma: no cover — invariant: draft_response already required this
        log.error("mark_awaiting_approval_missing_case_id", message_id=str(message_id))
        return {"reasoning_log": reasoning_log}

    async with asynccontextmanager(get_admin_session)() as session:
        await session.execute(_MARK_AWAITING_APPROVAL_SQL, {"case_id": str(case_id)})

    reasoning_log.append("Your reply is ready — I'm waiting for your approval before it goes out.")
    log.info("mark_awaiting_approval_done", message_id=str(message_id), case_id=str(case_id))
    return {"reasoning_log": reasoning_log}


async def await_approval(state: AgentState) -> dict[str, Any]:
    """Pause the graph via ``interrupt()`` — nothing sends past this point
    without a resume (there is no send code anywhere yet regardless; see
    ``app/agent/graph.py``'s module docstring). Re-executed on every
    attempt (drain or real resume) — has no side effects of its own besides
    the ``draft_id`` lookup (a plain, repeatable read) and the
    ``interrupt()`` call itself, so that re-execution is harmless (see
    module docstring)."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    case_id = case_context.case_id

    if case_id is None:  # pragma: no cover — invariant: draft_response already required this
        log.error("await_approval_missing_case_id", message_id=str(message_id))
        return {}

    async with asynccontextmanager(get_admin_session)() as session:
        pending_row = (
            (await session.execute(_SELECT_PENDING_DRAFT_ID_SQL, {"case_id": str(case_id)}))
            .mappings()
            .one_or_none()
        )
    draft_id = pending_row["id"] if pending_row is not None else None

    log.info(
        "await_approval_paused",
        message_id=str(message_id),
        case_id=str(case_id),
        draft_id=str(draft_id) if draft_id is not None else None,
    )

    interrupt(
        {
            "case_id": str(case_id),
            "draft_id": str(draft_id) if draft_id is not None else None,
            "reason": "awaiting_approval",
        }
    )

    return {}


__all__: list[str] = ["await_approval", "mark_awaiting_approval"]
