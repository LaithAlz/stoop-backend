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
first pause, and any later resume) until the specific ``interrupt()`` call
in that attempt receives a resume value instead of raising. Critically
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
  interrupt() is reached is NOT replayed by later resume attempts on that
  downstream node — its own committed output persists across them).
- :func:`await_approval` — the actual pause. Has no side effects of its
  own other than the ``draft_id`` lookup (a plain, repeatable read) and
  the ``interrupt()`` call itself, so re-execution on resume is a complete
  non-issue.

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
``app/agent/graph.py``'s module docstring "Stale-draft re-run" for the
full design and the empirical finding behind it: a plain, fresh
``run_graph`` call for a NEW message on the same case supersedes an
in-progress pause on its own (no draining needed). ``await_approval``'s
interrupt payload carries ``case_id``/``draft_id`` so a later resume call
can be matched against the CURRENT pending draft, but the actual
staleness check (and the per-case advisory lock that closes the
concurrent-resume race) lives in ``app/agent/graph.py::resume_case_thread``
(the #44/#45 resume seam), not here — neither node has any branching
logic on the resume value at all in #43's scope (that is #44/#45's
territory — see "Structural invariant for #44/#45" below).

``case_id`` unexpectedly ``None`` — a REAL reachable path, not just
defense (safety review LOW, #43 fix round)
------------------------------------------------------------------------
An earlier revision marked both early-return branches below
``# pragma: no cover``, claiming ``draft_response`` already guarantees a
real ``case_id`` by the time this pair runs. That claim is FALSE: an
UNKNOWN-SENDER message (``identify_property``'s own "unknown sender"
branch — ``case_context.case_id`` stays ``None`` for the whole run, no
case is ever created) that classifies as ROUTINE/URGENT (i.e., NOT
EMERGENCY) reaches ``draft_response``, which returns early WITHOUT setting
``draft_guard_failed`` (there is nothing to guard-check), and
``_route_after_draft_response`` — seeing neither ``draft_guard_failed``
nor an EMERGENCY severity — routes to ``mark_awaiting_approval`` exactly
as it would for a normal case. Both nodes below handle this correctly
(log, do nothing else, return without ever calling ``interrupt()`` — the
run reaches ``END`` unpaused, exactly as it must: there is no case to
attach an approval to). Exercised directly by
``tests/test_agent_shadow_interrupt.py::
test_unknown_sender_never_pauses_at_interrupt``.

``draft_id`` unexpectedly ``None`` at pause time — defensive-only, no
known live trigger under the per-case lock (safety review LOW, #43 fix
round)
------------------------------------------------------------------------
:func:`await_approval` re-queries the case's ``pending`` draft rather than
trusting anything threaded through state, because ``draft_response`` can
(rarely, defensively) finish WITHOUT ever inserting a row at all — its own
``DraftInsertRaceExhaustedError`` path (see that module's docstring,
"Race-safety against a genuinely CONCURRENT insert") logs an error and
returns without persisting a draft after exhausting its stale-then-insert
retries. Under ``app/agent/graph.py``'s per-case ``pg_advisory_xact_lock``
(module docstring "Per-case serialization"), that specific race should no
longer be reachable in practice (only ONE case-graph invocation for a
given case ever runs at a time now) — but this node does NOT assume that
invariant holds forever elsewhere in the codebase (defense in depth, same
philosophy as the hard guards in ``draft_response.py``). If the lookup
finds no pending draft, calling ``interrupt()`` anyway would create an
UNAPPROVABLE stuck pause (nothing for #44/#45 to ever resume with a real
draft id). Instead: skip the pause entirely, log the anomaly, append a
plain reasoning_log line, and let the run reach ``END`` unpaused — the
same "silence is worse, but a fabricated approval card is worse still"
tradeoff this codebase applies everywhere else. Exercised directly by
``tests/test_agent_shadow_interrupt.py::
test_await_approval_skips_the_pause_when_no_pending_draft_exists``.

Structural invariant for #44/#45 — NO side effects after ``interrupt()``
------------------------------------------------------------------------
:func:`await_approval` returns IMMEDIATELY once ``interrupt()`` returns a
resume value — no send, no DB write, nothing else happens in this node
after that call, on purpose. When #44/#45 need to act differently on
approve vs. reject vs. edit-and-send, that logic (and the eventual send
call site) belongs in a SEPARATE node reached by a NEW conditional edge
that inspects the resume value ``await_approval`` returns via
:func:`app.agent.graph.resume_case_thread` — never by growing an
``if``/``else`` inside this function. Keeping the pause node itself
permanently side-effect-free after ``interrupt()`` is what makes it safe
for :func:`app.agent.graph._case_lock`\\ 's per-case serialization to
reason about "the critical section ends when ``ainvoke`` returns" — a
future send appended directly here would extend that critical section in
a way this issue never reviewed.

**#44/#45 implementation of the above**: :func:`await_approval` now
CAPTURES what ``interrupt()`` returns (``resume_value``, whatever
``Command(resume=...)`` supplied — see
``app/agent/nodes/finalize_draft_decision.py`` for the ``{"action": ...}``
vocabulary) and returns it as ``{"approval_resume": resume_value}``. This
is still "no side effects" in the sense that matters here: it is a plain
dict construction from a value ``interrupt()`` already handed back on
THIS attempt, not a DB write, not a send, and it only ever happens on the
one attempt that actually resumes (every earlier attempt raises inside
``interrupt()`` before reaching this line, so nothing about it is
replayed). ``app/agent/graph.py``'s ``_route_after_await_approval``
conditional edge reads ``state["approval_resume"]`` to pick the SEPARATE
node (``finalize_approval`` / ``finalize_rejection``) this docstring's
previous paragraph already mandated — the actual DB writes/audit rows/
send-scheduling live there, never here.

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
    docstring "TWO nodes, not one").

    ``case_id`` can genuinely be ``None`` here (the unknown-sender path —
    see module docstring) — handled explicitly, not defensively-only: logs
    and returns without touching the DB, since there is no case to update."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    case_id = case_context.case_id

    if case_id is None:
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
    attempt (any resume that doesn't yet supply this call's value) — has
    no side effects of its own besides the ``draft_id`` lookup (a plain,
    repeatable read) and the ``interrupt()`` call itself, so that
    re-execution is harmless (see module docstring).

    TWO cases skip the pause entirely rather than calling ``interrupt()``
    (both documented above, both with dedicated tests): ``case_id is
    None`` (unknown sender — nothing to attach an approval to) and
    ``draft_id is None`` (no pending draft found — pausing anyway would be
    an unapprovable stuck interrupt, defensive-only under the per-case
    lock). Either way this node returns a plain dict and the run reaches
    ``END`` unpaused.
    """
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    case_id = case_context.case_id

    if case_id is None:
        log.error("await_approval_missing_case_id", message_id=str(message_id))
        return {}

    async with asynccontextmanager(get_admin_session)() as session:
        pending_row = (
            (await session.execute(_SELECT_PENDING_DRAFT_ID_SQL, {"case_id": str(case_id)}))
            .mappings()
            .one_or_none()
        )
    draft_id = pending_row["id"] if pending_row is not None else None

    if draft_id is None:
        # See module docstring "draft_id unexpectedly None at pause time" —
        # pausing with nothing approvable would be a stuck interrupt.
        log.error("await_approval_no_pending_draft_skipping_pause", case_id=str(case_id))
        reasoning_log.append(
            "I couldn't find a reply to hold for your approval just now — nothing was sent; "
            "this will be retried."
        )
        return {"reasoning_log": reasoning_log}

    log.info(
        "await_approval_paused",
        message_id=str(message_id),
        case_id=str(case_id),
        draft_id=str(draft_id),
    )

    resume_value = interrupt(
        {
            "case_id": str(case_id),
            "draft_id": str(draft_id),
            "reason": "awaiting_approval",
        }
    )

    # See module docstring "#44/#45 implementation of the above" — the
    # ONLY thing this node does with a resume value: hand it to state so
    # the graph's OWN conditional edge (never this node) can dispatch to
    # the right finalize node.
    return {"approval_resume": resume_value}


__all__: list[str] = ["await_approval", "mark_awaiting_approval"]
