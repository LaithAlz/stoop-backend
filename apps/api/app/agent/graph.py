"""The v1 LangGraph pipeline (#34) — wires the six existing node functions
(``app/agent/nodes/``) plus the ``degraded_mode`` seam into an executable,
Postgres-checkpointed graph, and exposes :func:`run_graph` as the single
entry point ``app/agent/graph_entry.py`` invokes per inbound message.

Node order (issue #34's AC / ``apps/api/CLAUDE.md``'s ``agent/nodes/``
layout; #43 added ``mark_awaiting_approval``/``await_approval``, see
"Shadow mode (#43)" below)::

    identify_property -> load_context -> identify_case -> classify_intent
      -> classify_severity -> [classification_failed?] -> degraded_mode
                            -> draft_response
                                 -> [draft_guard_failed? OR severity==EMERGENCY?]
                                      -> degraded_mode
                                      -> mark_awaiting_approval -> await_approval
                                                                     -> interrupt()

LINEAR pipeline, no fan-out (founder directive, 2026-07-06): every edge
above is a plain or conditional edge between exactly one predecessor and
one successor per step — nothing here ever schedules two nodes to run
concurrently from a single source (that is a deliberate v1 scope
boundary, not a LangGraph limitation). Two DIFFERENT future issues own
two DIFFERENT things this graph deliberately does not build yet — keep
them straight (a senior review caught an earlier revision of this
docstring conflating them):

- **#43** owns ``interrupt()`` (from ``langgraph.types``) pausing the
  urgent/routine draft for landlord approval before any send — see
  "Shadow mode (#43)" below.
- **#108** owns the actual EMERGENCY execution (the voice call, the
  tenant safety SMS, the escalation chain — ``app/agent/emergency.py``'s
  still-a-no-op seam). This graph does NOT invoke that seam and never
  will from here (the Tier-0 HARD-hit path already calls it from the
  WEBHOOK, before the graph even runs — ``app/routers/webhooks/twilio.py``).
  What THIS graph is responsible for is the case #108 does not cover: an
  EMERGENCY severity the model itself assigned that Tier-0 DIDN'T catch
  (escalating past a Tier-0 miss, never de-escalating a Tier-0 fire — see
  "The degraded-mode routing" below). Until #108 ships the real
  escalation chain, routing that case to ``degraded_mode`` (a durable
  ``needs_eyes`` notification) is the honest interim behavior — never
  silent, never a fabricated call.

Two compiled graphs, not one — the thread-id timing tension
------------------------------------------------------------------------
LangGraph's checkpointer needs a stable ``thread_id`` in ``config`` at
``ainvoke()`` time. This pipeline's thread is ``cases.langgraph_thread_id``
(one thread per CASE, never per tenant/phone — ``app/agent/checkpointer
.py``'s module docstring) — but the case a message belongs to is only
decided partway through the pipeline, by ``identify_case``. Two designs
were on the table (flagged for spec-guardian/senior review, not invented
silently):

(a) **Pre-routing segment + case-scoped graph (CHOSEN).** Split the six
    nodes into two separately-compiled ``StateGraph`` objects at exactly
    the ``identify_case`` boundary:

    - :data:`_pre_routing_graph`: ``identify_property -> load_context ->
      identify_case``. Compiled WITHOUT a checkpointer. This segment is
      fast, DB-only (no Anthropic calls, no multi-second external I/O to
      get interrupted mid-flight), and — critically — doesn't yet know
      the thread it should be checkpointed under. A crash here simply
      means the NEXT invocation for the same message starts over; nothing
      it does is unsafe to re-run (``identify_property``/``load_context``
      are read-mostly plus idempotent notification inserts;
      ``identify_case``'s own re-invocation risk — e.g. opening two
      cases for one message under genuine concurrent retries — is the
      SAME pre-existing, documented, accepted race as
      ``app/agent/graph_entry.py``'s "Known, accepted race" section, not
      something this issue introduces or fixes).
    - :data:`_case_graph`: ``classify_intent -> classify_severity ->
      [draft_response | degraded_mode]``. Compiled WITH the Postgres
      checkpointer (:func:`app.agent.checkpointer.get_checkpointer`),
      invoked with ``{"configurable": {"thread_id": <resolved thread>}}``
      — resolved via :func:`_resolve_thread_id` using the case row
      ``identify_case`` just wrote/attached (a small, dedicated SELECT;
      see that function's docstring for why this doesn't re-derive
      anything ``identify_case`` itself already computed).

(b) Interrupt the FULL graph after ``identify_case`` (LangGraph's static
    ``interrupt_after=[...]`` compile option) under a provisional
    thread_id, resolve the real thread, then resume/"transplant" the
    accumulated state onto the case's real thread. Rejected: transplanting
    a checkpoint from one thread_id to another is not a first-class
    LangGraph operation, and reusing the STATIC ``interrupt_after``
    machinery here would blur the line with #43's upcoming DYNAMIC
    ``interrupt()`` primitive (a different mechanism, for a different
    purpose — pausing for landlord approval).

Both graphs share the exact same ``AgentState`` schema and are invoked in
strict sequence within one :func:`run_graph` call — from the outside this
is still one linear pipeline run per message, and LangSmith renders both
graphs' traces (tracing is process-wide via
``app/observability.py::init_langsmith_tracing()``, already called at
app startup — no additional wiring needed here).

Unknown-sender fallback thread
------------------------------
When ``identify_case`` cannot attach the message to any case (tenant_id
unresolved — see ``identify_property``'s own docstring), there is no
``cases.langgraph_thread_id`` to key by. :func:`_resolve_thread_id` falls
back to a per-MESSAGE thread id (``f"message:{message_id}"``) in that one
case only — never per tenant/phone (the invariant that matters), and
there is no ongoing case to correlate across messages anyway.

reasoning_log accumulation — option (a), no reducer (documented per
``app/agent/state.py``'s own "Accumulation note", which explicitly left
this decision to #34)
------------------------------------------------------------------------
Every node in ``app/agent/nodes/`` already reads the FULL incoming
``reasoning_log`` and returns the FULL (accumulated) list — the
"defensive convention" ``state.py`` describes. This graph relies on
LangGraph's default last-write-wins TypedDict merge semantics and adds NO
``Annotated[list[str], operator.add]`` reducer. This is safe specifically
BECAUSE the pipeline is linear (no fan-out): each node runs strictly after
its predecessor and always sees that predecessor's full log. Mixing a
reducer in on top of this convention would DUPLICATE every prior line on
every node (each node's OWN return value already contains the full prior
list; a reducer would then concatenate two overlapping full lists) — the
regression test for this (``tests/test_agent_graph.py``) runs two
consecutive nodes and asserts no duplicated lines.

The degraded-mode routing (#34 G1 — merge-blocking, PR #173/#175 senior
review; EMERGENCY leg added after a second senior-review round on THIS
issue)
------------------------------------------------------------------------
Three independent triggers all route to ``app.agent.nodes.degraded_mode``
— never combined into one condition, each checked on its own, per the
senior review's own wording ("classification_failed AND draft_guard_failed
must EACH route to an explicit degraded-mode edge"):

1. ``classify_severity`` sets ``state["classification_failed"] = True`` on
   a double Anthropic failure and otherwise leaves ``severity`` unset —
   letting the pipeline continue to ``draft_response`` in that state
   would either crash (no severity to draft against) or silently no-op
   (``draft_response`` already guards this and returns early with only a
   reasoning_log note — see its own docstring). :func:`_route_after_
   classify_severity` intercepts exactly that flag and routes to
   ``degraded_mode`` INSTEAD of ``draft_response`` — no silent dead end.
2. **EMERGENCY severity the model itself assigned** (a genuine Tier-0
   MISS the LLM caught — architecture.md §5/§8 and
   ``docs/02-product/emergency-prefilter.md``'s escalate-past-a-miss
   doctrine: the agent may escalate past a miss, it may never de-escalate
   a fire). A first revision of this graph let an LLM-classified
   EMERGENCY fall through to an ORDINARY approval-queued draft with NO
   notification at all — silent, exactly the failure mode this whole
   gate exists to prevent (senior review, CRITICAL). Fixed by
   :func:`_route_after_draft_response` checking ``state["severity"]`` for
   ``Severity.EMERGENCY`` IN ADDITION TO ``draft_guard_failed`` — checked
   AFTER ``draft_response`` runs (not instead of it), so the draft is
   composed first (a draft plus a needs_eyes notification beats a
   notification alone) and the notification is what actually gates
   "did a person get told" — see "Interim, not #108" below for why this
   lives here instead of a real escalation.
3. ``draft_response`` sets ``state["draft_guard_failed"] = True`` when the
   model's own text failed the hard safety guards twice (a draft IS
   still inserted, using the safe generic fallback) —
   :func:`_route_after_draft_response` routes that case to
   ``degraded_mode`` too, so a person is durably notified either way.

Triggers 2 and 3 can co-occur (an EMERGENCY draft whose OWN guard also
failed) — ``degraded_mode`` records every applicable reason, never just
one (see that module's own docstring).

Interim, not #108 — this is NOT the real escalation chain
------------------------------------------------------------------------
Routing an LLM-classified EMERGENCY to ``degraded_mode`` is an INTERIM
behavior, not #108's actual voice-call/safety-SMS/escalation-chain seam
(``app/agent/emergency.py``). It exists because "silent" is strictly
worse than "a needs_eyes notification, no voice call yet" — #108 replaces
this edge (or adds a second one) once the real execution seam exists;
until then, a durable, queryable ``needs_eyes`` row is the honest floor.

Shadow mode (#43) — interrupt() before any send
------------------------------------------------------------------------
The non-degraded exit from ``draft_response`` (the plain
``NODE_DRAFT_RESPONSE -> END`` edge the previous section's docstring left
as a documented seam) is now ``NODE_DRAFT_RESPONSE -> await_approval ->
END``. :func:`app.agent.nodes.await_approval.await_approval` owns the
``cases.status = 'awaiting_approval'`` transition ``draft_response``
deliberately does not (see that node's own docstring) and calls
``interrupt()`` (from ``langgraph.types``) to pause the thread — see that
node's own module docstring for the full design, including why it never
gets trapped behind the EMERGENCY/``draft_guard_failed`` degraded-mode
exit (that edge is checked FIRST by ``_route_after_draft_response`` and
bypasses ``await_approval`` entirely, independent of whether any earlier
thread on this case is still paused).

Draining a pending interrupt before a stale-draft re-run (the #34
spec-review PINNED WARNING — resolved by direct experiment, not assumption)
------------------------------------------------------------------------
conversation-model.md's stale-draft rule: a new inbound message on a case
with a pending draft must mark that draft ``stale`` and re-run the graph
from ``load_context`` with the new message. Because the case-scoped
checkpointed thread is keyed on ``cases.langgraph_thread_id`` (one thread
per case, reused across every message that case ever gets), the SAME
thread that is currently paused at ``await_approval``'s ``interrupt()``
(case status ``awaiting_approval``) is the thread the NEXT inbound message
on that case must also run on.

The #34 spec review flagged this interaction as UNVERIFIED: does a plain
``ainvoke(new_state, config)`` on a thread that still holds a PENDING
interrupt behave like the stale-draft rule needs? **Verified empirically
against a real ``AsyncPostgresSaver``/Postgres checkpointer (three-call
probe, not assumed):** it does NOT restart the run from ``START`` with the
new input at all. LangGraph sees the thread already has a pending task
(the interrupted ``await_approval`` attempt) and simply RE-EXECUTES THAT
SAME PENDING TASK using the OLD checkpointed values — the new input is
silently discarded, no exception is raised, and a brand-new (but
functionally identical) ``Interrupt`` is produced. Left alone, this would
mean a new tenant message arriving while a draft awaits approval would
NEVER actually produce a fresh draft — the exact silent-staleness failure
mode the stale-draft rule exists to prevent, just moved one layer down
into LangGraph's own resume semantics.

Fixed by :func:`_drain_pending_interrupt_if_any`, called by :func:`run_graph`
immediately before every ``case_graph.ainvoke(...)`` call (unconditionally
— a fresh thread's snapshot has no tasks and this is a cheap no-op, see its
own docstring): if the thread's current state snapshot shows a task with a
recorded ``interrupt`` (``StateSnapshot.interrupts`` non-empty — this is
specifically HOW a live interrupt is distinguished from any other reason a
task might be pending), resume it FIRST with a private sentinel value via
``Command(resume=...)`` — discarding that resume's result entirely — before
the real ``ainvoke(pre_routing_state, config)`` call. Verified (same
probe, extended): draining first and then issuing a plain ``ainvoke`` with
the new message's state on the now-unblocked thread DOES restart the run
from ``START`` and DOES produce a fresh draft (``draft_response``'s
existing stale-then-insert logic marks the old pending draft ``stale`` and
inserts the new one, exactly as it already does for two back-to-back
non-paused messages) and a fresh pause. This is the ONLY place that drain
happens — ``await_approval`` itself has no branching logic on the resume
value it receives (see that module's own docstring): the sentinel is
purely a mechanism to unstick the checkpoint, never mistaken for a real
approval (no approve/reject logic exists yet; that is #44/#45's resume
value, delivered through :func:`resume_case_thread` instead, which never
uses this sentinel).

The resume seam for #44/#45 (implemented here, no HTTP endpoint — #43 scope)
------------------------------------------------------------------------
:func:`resume_case_thread` is the documented entry point #44 (approve) and
#45 (reject/edit-and-send) will call once those endpoints exist. Per
conversation-model.md's "staleness wins" edge case ("the approve action
carries the draft id; if that id is already stale, the send is rejected"):
it re-checks, at call time, that ``draft_id`` is STILL the case's one
``pending`` draft before ever touching the thread. If a new message
superseded it in the meantime (the drain-and-rerun above already marked it
``stale`` and produced a fresh pending draft), this raises
:class:`DraftStaleError` (carrying the fresh draft's id) and the thread is
left completely untouched — a stale resume must never resolve the WRONG
(current, fresh) interrupt with a value meant for an old one. Only when
the id still matches does it call ``Command(resume=...)`` on the thread.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy import text

from app.agent.checkpointer import get_checkpointer
from app.agent.nodes.await_approval import await_approval, mark_awaiting_approval
from app.agent.nodes.classify_intent import classify_intent
from app.agent.nodes.classify_severity import classify_severity
from app.agent.nodes.degraded_mode import degraded_mode
from app.agent.nodes.draft_response import draft_response
from app.agent.nodes.identify_case import identify_case
from app.agent.nodes.identify_property import identify_property
from app.agent.nodes.load_context import load_context
from app.agent.schemas import CaseContext, Severity
from app.agent.state import AgentState
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Node name constants — used as both the graph's node keys and (for the
# conditional routers) the ``path_map`` targets.
# ---------------------------------------------------------------------------

NODE_IDENTIFY_PROPERTY = "identify_property"
NODE_LOAD_CONTEXT = "load_context"
NODE_IDENTIFY_CASE = "identify_case"
NODE_CLASSIFY_INTENT = "classify_intent"
NODE_CLASSIFY_SEVERITY = "classify_severity"
NODE_DRAFT_RESPONSE = "draft_response"
NODE_DEGRADED_MODE = "degraded_mode"
NODE_MARK_AWAITING_APPROVAL = "mark_awaiting_approval"
NODE_AWAIT_APPROVAL = "await_approval"

_UNKNOWN_SENDER_THREAD_PREFIX = "message:"
"""Fallback checkpoint thread for a message that never attaches to a case
(unknown sender) — see module docstring "Unknown-sender fallback thread"."""

_STALE_DRAFT_DRAIN_SENTINEL: dict[str, Any] = {"__stale_draft_drain__": True}
"""Resume value used ONLY by :func:`_drain_pending_interrupt_if_any` to
unstick a thread whose PREVIOUS run is still paused at ``await_approval``'s
``interrupt()`` — see module docstring "Draining a pending interrupt
before a stale-draft re-run". Never used for a real approval/reject
(that is :func:`resume_case_thread`'s resume value instead, which is never
this sentinel) — ``await_approval`` itself never branches on the resume
value it receives either way (see that node's own docstring), so this
sentinel's only job is to let the paused attempt complete and reach
``END``."""


# ---------------------------------------------------------------------------
# Conditional routers (#34 G1 — see module docstring)
# ---------------------------------------------------------------------------


def _route_after_classify_severity(state: AgentState) -> str:
    """``classification_failed`` skips ``draft_response`` entirely (there
    is no severity to draft against) and routes straight to the durable
    degraded-mode notification — never a silent dead end."""
    if state.get("classification_failed"):
        return NODE_DEGRADED_MODE
    return NODE_DRAFT_RESPONSE


def _route_after_draft_response(state: AgentState) -> str:
    """Two INDEPENDENT triggers route to ``degraded_mode`` here, checked
    separately (never combined into one condition):

    - ``draft_guard_failed`` — the model's OWN acknowledgment text failed
      the hard safety guards twice.
    - ``severity == EMERGENCY`` — an LLM-classified emergency Tier-0
      missed (see module docstring "The degraded-mode routing", trigger
      2). Checked AFTER ``draft_response`` runs, not instead of it: the
      draft is still composed and inserted either way (a draft plus a
      needs_eyes notification beats a notification alone) — this router
      only decides whether a person ALSO gets durably notified.

    Either way this is IN ADDITION TO (not instead of) the draft that
    ``draft_response`` already inserted. Checked FIRST, before the plain
    ``mark_awaiting_approval -> await_approval`` exit below — #43's
    approval pause never traps this interim emergency/degraded-mode path
    (module docstring "Shadow mode (#43)")."""
    if state.get("draft_guard_failed"):
        return NODE_DEGRADED_MODE
    severity_result = state.get("severity")
    if severity_result is not None and severity_result.severity is Severity.EMERGENCY:
        return NODE_DEGRADED_MODE
    return NODE_MARK_AWAITING_APPROVAL


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------


def build_pre_routing_graph() -> StateGraph[AgentState, None, AgentState, AgentState]:
    """``identify_property -> load_context -> identify_case``. See module
    docstring "Two compiled graphs, not one" for why this segment is
    separate and uncheckpointed."""
    graph: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    graph.add_node(NODE_IDENTIFY_PROPERTY, identify_property)
    graph.add_node(NODE_LOAD_CONTEXT, load_context)
    graph.add_node(NODE_IDENTIFY_CASE, identify_case)

    graph.add_edge(START, NODE_IDENTIFY_PROPERTY)
    graph.add_edge(NODE_IDENTIFY_PROPERTY, NODE_LOAD_CONTEXT)
    graph.add_edge(NODE_LOAD_CONTEXT, NODE_IDENTIFY_CASE)
    graph.add_edge(NODE_IDENTIFY_CASE, END)
    return graph


def build_case_graph() -> StateGraph[AgentState, None, AgentState, AgentState]:
    """``classify_intent -> classify_severity -> [draft_response |
    degraded_mode]`` -> ``draft_response``'s own conditional exit to either
    ``degraded_mode`` (emergency/guard-failed, interim) or
    ``mark_awaiting_approval -> await_approval`` (#43's shadow-mode pause,
    plain urgent/routine exit — TWO nodes, see
    ``app/agent/nodes/await_approval.py``'s module docstring "TWO nodes,
    not one" for why the DB write/reasoning_log commit and the actual
    ``interrupt()`` pause cannot be the same node). Compiled (by
    :func:`compile_case_graph`) WITH the Postgres checkpointer, keyed on
    ``cases.langgraph_thread_id``."""
    graph: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    graph.add_node(NODE_CLASSIFY_INTENT, classify_intent)
    graph.add_node(NODE_CLASSIFY_SEVERITY, classify_severity)
    graph.add_node(NODE_DRAFT_RESPONSE, draft_response)
    graph.add_node(NODE_DEGRADED_MODE, degraded_mode)
    graph.add_node(NODE_MARK_AWAITING_APPROVAL, mark_awaiting_approval)
    graph.add_node(NODE_AWAIT_APPROVAL, await_approval)

    graph.add_edge(START, NODE_CLASSIFY_INTENT)
    graph.add_edge(NODE_CLASSIFY_INTENT, NODE_CLASSIFY_SEVERITY)
    graph.add_conditional_edges(
        NODE_CLASSIFY_SEVERITY,
        _route_after_classify_severity,
        {NODE_DRAFT_RESPONSE: NODE_DRAFT_RESPONSE, NODE_DEGRADED_MODE: NODE_DEGRADED_MODE},
    )
    graph.add_conditional_edges(
        NODE_DRAFT_RESPONSE,
        _route_after_draft_response,
        {
            NODE_DEGRADED_MODE: NODE_DEGRADED_MODE,
            NODE_MARK_AWAITING_APPROVAL: NODE_MARK_AWAITING_APPROVAL,
        },
    )
    graph.add_edge(NODE_MARK_AWAITING_APPROVAL, NODE_AWAIT_APPROVAL)
    graph.add_edge(NODE_DEGRADED_MODE, END)
    graph.add_edge(NODE_AWAIT_APPROVAL, END)
    return graph


def compile_pre_routing_graph() -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """Compiled WITHOUT a checkpointer — see module docstring."""
    return build_pre_routing_graph().compile()


def compile_case_graph() -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """Compiled WITH the process-wide Postgres checkpointer.

    ORDERING CONTRACT (``app/agent/checkpointer.py``'s own docstring):
    :func:`app.agent.checkpointer.get_checkpointer` must only be called
    AFTER ``setup_checkpointer()`` has run (the app lifespan does this
    once at startup, before any webhook can reach this code path) — this
    function does not itself call ``setup_checkpointer()``. Cheap and
    synchronous to call per invocation (documented safe by that module).
    """
    return build_case_graph().compile(checkpointer=get_checkpointer())


# ---------------------------------------------------------------------------
# Thread resolution — see module docstring "Two compiled graphs, not one"
# ---------------------------------------------------------------------------

_SELECT_CASE_THREAD_ID_SQL = text("SELECT langgraph_thread_id FROM cases WHERE id = :case_id")


async def _select_case_thread_id(case_id: UUID) -> str:
    """``cases.langgraph_thread_id`` for an already-known case — the
    checkpoint thread lookup shared by both :func:`_resolve_thread_id`
    (mid-pipeline, ``case_id`` may still be ``None``) and
    :func:`resume_case_thread` (#44/#45's seam, always has a real
    ``case_id`` and no ``message_id`` to fall back on at all)."""
    async with asynccontextmanager(get_admin_session)() as session:
        row = (
            (await session.execute(_SELECT_CASE_THREAD_ID_SQL, {"case_id": str(case_id)}))
            .mappings()
            .one()
        )
    return str(row["langgraph_thread_id"])


async def _resolve_thread_id(*, message_id: UUID, case_id: UUID | None) -> str:
    """The checkpoint thread for the case-scoped graph half — always
    ``cases.langgraph_thread_id`` when a case was attached, one thread per
    case (never per tenant/phone). Falls back to a per-message id only for
    the unknown-sender case, where no case exists to key by at all — see
    module docstring "Unknown-sender fallback thread"."""
    if case_id is None:
        return f"{_UNKNOWN_SENDER_THREAD_PREFIX}{message_id}"
    return await _select_case_thread_id(case_id)


# ---------------------------------------------------------------------------
# Draining a pending interrupt before a stale-draft re-run — see module
# docstring "Draining a pending interrupt before a stale-draft re-run" for
# the full design and the empirical finding behind it.
# ---------------------------------------------------------------------------


async def _drain_pending_interrupt_if_any(
    case_graph: CompiledStateGraph[AgentState, None, AgentState, AgentState],
    config: RunnableConfig,
) -> None:
    """If *config*'s thread is currently paused at a live ``interrupt()``
    (``StateSnapshot.interrupts`` non-empty — this is specifically how a
    genuinely-interrupted task is distinguished from any other reason a
    task might be pending), resume it with a private, discarded sentinel
    FIRST so the pending ``await_approval`` attempt completes and the
    thread reaches ``END`` before the caller's own fresh ``ainvoke`` runs.

    A no-op (cheap: one ``aget_state`` round trip) for a brand-new thread
    (no checkpoint yet — ``snapshot.tasks`` is empty) or a thread that has
    already run to completion (no pending tasks) — both verified
    empirically to return an empty snapshot/no pending tasks, never an
    error. Safe to call unconditionally before every
    ``case_graph.ainvoke(...)`` in :func:`run_graph`.
    """
    snapshot = await case_graph.aget_state(config)
    if not snapshot.interrupts:
        return
    log.info(
        "graph_drained_pending_interrupt_before_rerun",
        thread_id=config["configurable"]["thread_id"],
    )
    await case_graph.ainvoke(Command(resume=_STALE_DRAFT_DRAIN_SENTINEL), config=config)


# ---------------------------------------------------------------------------
# The resume seam for #44/#45 — see module docstring "The resume seam for
# #44/#45". No HTTP endpoint here (#43 scope); this is the function those
# issues' endpoints will call.
# ---------------------------------------------------------------------------

_SELECT_PENDING_DRAFT_ID_SQL = text(
    "SELECT id FROM drafts WHERE case_id = :case_id AND status = 'pending'"
)


class DraftStaleError(RuntimeError):
    """Raised by :func:`resume_case_thread` when *draft_id* is no longer
    the case's pending draft — conversation-model.md's "staleness wins"
    edge case. The thread is left completely untouched: a stale resume
    must never resolve whatever the CURRENT (fresh) interrupt actually is
    with a value meant for a superseded one. ``fresh_draft_id`` is the
    case's current pending draft, if any (``None`` if the case somehow has
    no pending draft at all) — #44 maps this to the documented 409
    ``draft_stale`` response body's ``fresh_draft_id`` field."""

    def __init__(self, *, case_id: UUID, draft_id: UUID, fresh_draft_id: UUID | None) -> None:
        self.case_id = case_id
        self.draft_id = draft_id
        self.fresh_draft_id = fresh_draft_id
        super().__init__(
            f"draft {draft_id} is no longer the pending draft for case {case_id} "
            f"(fresh_draft_id={fresh_draft_id})"
        )


class CaseNotAwaitingApprovalError(RuntimeError):
    """Raised by :func:`resume_case_thread` when *draft_id* IS the case's
    current pending draft, but the case's thread has no live interrupt to
    resume at all — a KNOWN, discovered gap (not fixed here, out of #43's
    scope): ``draft_response``'s degraded-mode exit (EMERGENCY /
    ``draft_guard_failed``) still inserts a ``pending`` draft, but routes
    to ``degraded_mode`` instead of ``await_approval`` (module docstring
    "Shadow mode (#43)"), so that draft is never actually paused behind an
    interrupt. Distinct from :class:`DraftStaleError`: the draft id is
    CORRECT here, there is simply nothing paused to approve."""

    def __init__(self, *, case_id: UUID, draft_id: UUID) -> None:
        self.case_id = case_id
        self.draft_id = draft_id
        super().__init__(
            f"case {case_id}'s draft {draft_id} is pending but the thread has no live "
            "interrupt to resume"
        )


async def resume_case_thread(*, case_id: UUID, draft_id: UUID, resume_value: Any) -> AgentState:
    """Resume a case's paused ``await_approval`` interrupt with
    *resume_value* — the documented entry point #44 (approve) and #45
    (reject/edit-and-send) call once those endpoints exist (this issue,
    #43, implements the mechanics and tests only; no HTTP surface).

    Re-checks, at call time, that *draft_id* is STILL the case's one
    ``pending`` draft before ever touching the thread (raises
    :class:`DraftStaleError` otherwise), AND that the thread actually has a
    live interrupt to resume (raises :class:`CaseNotAwaitingApprovalError`
    otherwise — see that class's own docstring). Only when both hold does
    this call ``Command(resume=resume_value)`` on the resolved thread and
    return the resulting state.
    """
    async with asynccontextmanager(get_admin_session)() as session:
        pending_row = (
            (await session.execute(_SELECT_PENDING_DRAFT_ID_SQL, {"case_id": str(case_id)}))
            .mappings()
            .one_or_none()
        )
    fresh_draft_id: UUID | None = pending_row["id"] if pending_row is not None else None
    if fresh_draft_id != draft_id:
        raise DraftStaleError(case_id=case_id, draft_id=draft_id, fresh_draft_id=fresh_draft_id)

    thread_id = await _select_case_thread_id(case_id)
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    case_graph = compile_case_graph()

    snapshot = await case_graph.aget_state(config)
    if not snapshot.interrupts:
        raise CaseNotAwaitingApprovalError(case_id=case_id, draft_id=draft_id)

    result = await case_graph.ainvoke(Command(resume=resume_value), config=config)
    return cast("AgentState", result)


# ---------------------------------------------------------------------------
# Top-level entry point — the one function app/agent/graph_entry.py calls.
# ---------------------------------------------------------------------------


async def run_graph(message_id: UUID) -> AgentState:
    """Run the full v1 pipeline once for the persisted ``messages`` row
    ``message_id``: ``identify_property -> load_context -> identify_case``
    (uncheckpointed), then ``classify_intent -> classify_severity ->
    [draft_response | degraded_mode | await_approval]`` (checkpointed under
    the resolved case thread). Returns the final ``AgentState`` — on the
    plain urgent/routine exit this return value includes LangGraph's own
    ``__interrupt__`` marker (the run paused, it did not raise; see
    :func:`app.agent.nodes.await_approval.await_approval`'s docstring).

    Before invoking the case-scoped graph, drains any PENDING interrupt
    already sitting on this case's thread from an earlier message — see
    module docstring "Draining a pending interrupt before a stale-draft
    re-run" for why this must happen first.

    Only ``message_id`` is needed — ``identify_property`` re-derives every
    other identifier (``landlord_id``/``property_id``/``tenant_id``) from
    the persisted ``messages`` row itself (that node's own docstring: the
    message row is the source of truth, not anything the caller might
    separately believe).

    Raises whatever the underlying nodes raise (e.g.
    ``identify_property.MessageNotFoundError`` for a ``message_id`` with
    no persisted row) — this function does not swallow exceptions; its
    caller (``app/agent/graph_entry.py::enqueue_classification``) is the
    one with the "never raise outward" contract, and owns catching this.
    """
    pre_routing_graph = compile_pre_routing_graph()
    initial_state: AgentState = {"message_id": message_id, "reasoning_log": []}
    pre_routing_result = await pre_routing_graph.ainvoke(initial_state)
    pre_routing_state = cast("AgentState", pre_routing_result)

    case_context = pre_routing_state.get("case_context") or CaseContext()
    thread_id = await _resolve_thread_id(message_id=message_id, case_id=case_context.case_id)

    log.info(
        "graph_run_pre_routing_complete",
        message_id=str(message_id),
        case_id=str(case_context.case_id) if case_context.case_id is not None else None,
    )

    case_graph = compile_case_graph()
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    # Stale-draft rule (#43): a new message may land on a case whose
    # thread is still paused at await_approval's interrupt() from an
    # earlier message. See module docstring "Draining a pending interrupt
    # before a stale-draft re-run" for why this must run BEFORE the
    # ainvoke below, not after.
    await _drain_pending_interrupt_if_any(case_graph, config)

    case_result = await case_graph.ainvoke(pre_routing_state, config=config)
    final_state = cast("AgentState", case_result)

    log.info(
        "graph_run_complete",
        message_id=str(message_id),
        case_id=str(case_context.case_id) if case_context.case_id is not None else None,
        classification_failed=bool(final_state.get("classification_failed")),
        draft_guard_failed=bool(final_state.get("draft_guard_failed")),
    )
    return final_state


__all__: list[str] = [
    "NODE_AWAIT_APPROVAL",
    "NODE_CLASSIFY_INTENT",
    "NODE_CLASSIFY_SEVERITY",
    "NODE_DEGRADED_MODE",
    "NODE_DRAFT_RESPONSE",
    "NODE_IDENTIFY_CASE",
    "NODE_IDENTIFY_PROPERTY",
    "NODE_LOAD_CONTEXT",
    "NODE_MARK_AWAITING_APPROVAL",
    "CaseNotAwaitingApprovalError",
    "DraftStaleError",
    "build_case_graph",
    "build_pre_routing_graph",
    "compile_case_graph",
    "compile_pre_routing_graph",
    "resume_case_thread",
    "run_graph",
]
