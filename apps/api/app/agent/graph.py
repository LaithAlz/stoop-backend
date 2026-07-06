"""The v1 LangGraph pipeline (#34) — wires the six existing node functions
(``app/agent/nodes/``) plus the ``degraded_mode`` seam into an executable,
Postgres-checkpointed graph, and exposes :func:`run_graph` as the single
entry point ``app/agent/graph_entry.py`` invokes per inbound message.

Node order (issue #34's AC / ``apps/api/CLAUDE.md``'s ``agent/nodes/``
layout)::

    identify_property -> load_context -> identify_case -> classify_intent
      -> classify_severity -> [classification_failed?] -> degraded_mode
                            -> draft_response -> [draft_guard_failed?] -> degraded_mode

LINEAR pipeline, no fan-out (founder directive, 2026-07-06): every edge
above is a plain or conditional edge between exactly one predecessor and
one successor per step — nothing here ever schedules two nodes to run
concurrently from a single source (that is a deliberate v1 scope
boundary, not a LangGraph limitation). ``interrupt()`` before any send,
and the ``emergency_protocol``/approval branch the AC also mentions, are
#43's job — seam noted below, not built here.

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
review)
------------------------------------------------------------------------
``classify_severity`` sets ``state["classification_failed"] = True`` on a
double Anthropic failure and otherwise leaves ``severity`` unset — letting
the pipeline continue to ``draft_response`` in that state would either
crash (no severity to draft against) or silently no-op
(``draft_response`` already guards this and returns early with only a
reasoning_log note — see its own docstring). :func:`_route_after_classify_
severity` intercepts exactly that flag and routes to
``app.agent.nodes.degraded_mode`` INSTEAD of ``draft_response`` — no
silent dead end. Symmetrically, ``draft_response`` sets
``state["draft_guard_failed"] = True`` when the model's own text failed
the hard safety guards twice (a draft IS still inserted, using the safe
generic fallback) — :func:`_route_after_draft_response` routes that case
to the SAME ``degraded_mode`` node afterward, so a person is durably
notified either way. Both flags are checked independently (never
combined into one condition) per the senior review's own wording:
"classification_failed AND draft_guard_failed must EACH route to an
explicit degraded-mode edge."

Seam for #43 (left obvious, not built here)
------------------------------------------------------------------------
This graph's non-degraded exit from ``draft_response`` is a plain edge to
``END`` — #43 replaces that edge with ``interrupt()`` (from
``langgraph.types``) before any send, and adds the
``cases.status = 'awaiting_approval'`` transition ``draft_response``
deliberately does not own (see that node's own docstring). Nothing in
this module needs to change shape for that — only the one edge
(``NODE_DRAFT_RESPONSE -> END``) needs to become
``NODE_DRAFT_RESPONSE -> interrupt() -> END``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import text

from app.agent.checkpointer import get_checkpointer
from app.agent.nodes.classify_intent import classify_intent
from app.agent.nodes.classify_severity import classify_severity
from app.agent.nodes.degraded_mode import degraded_mode
from app.agent.nodes.draft_response import draft_response
from app.agent.nodes.identify_case import identify_case
from app.agent.nodes.identify_property import identify_property
from app.agent.nodes.load_context import load_context
from app.agent.schemas import CaseContext
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

_UNKNOWN_SENDER_THREAD_PREFIX = "message:"
"""Fallback checkpoint thread for a message that never attaches to a case
(unknown sender) — see module docstring "Unknown-sender fallback thread"."""


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
    """``draft_guard_failed`` ALSO routes to degraded_mode, in addition to
    (not instead of) the draft that ``draft_response`` already inserted
    using the safe generic fallback text."""
    if state.get("draft_guard_failed"):
        return NODE_DEGRADED_MODE
    return END


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
    degraded_mode]``. Compiled (by :func:`compile_case_graph`) WITH the
    Postgres checkpointer, keyed on ``cases.langgraph_thread_id``."""
    graph: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    graph.add_node(NODE_CLASSIFY_INTENT, classify_intent)
    graph.add_node(NODE_CLASSIFY_SEVERITY, classify_severity)
    graph.add_node(NODE_DRAFT_RESPONSE, draft_response)
    graph.add_node(NODE_DEGRADED_MODE, degraded_mode)

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
        {NODE_DEGRADED_MODE: NODE_DEGRADED_MODE, END: END},
    )
    graph.add_edge(NODE_DEGRADED_MODE, END)
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


async def _resolve_thread_id(*, message_id: UUID, case_id: UUID | None) -> str:
    """The checkpoint thread for the case-scoped graph half — always
    ``cases.langgraph_thread_id`` when a case was attached, one thread per
    case (never per tenant/phone). Falls back to a per-message id only for
    the unknown-sender case, where no case exists to key by at all — see
    module docstring "Unknown-sender fallback thread"."""
    if case_id is None:
        return f"{_UNKNOWN_SENDER_THREAD_PREFIX}{message_id}"

    async with asynccontextmanager(get_admin_session)() as session:
        row = (
            (await session.execute(_SELECT_CASE_THREAD_ID_SQL, {"case_id": str(case_id)}))
            .mappings()
            .one()
        )
    return str(row["langgraph_thread_id"])


# ---------------------------------------------------------------------------
# Top-level entry point — the one function app/agent/graph_entry.py calls.
# ---------------------------------------------------------------------------


async def run_graph(message_id: UUID) -> AgentState:
    """Run the full v1 pipeline once for the persisted ``messages`` row
    ``message_id``: ``identify_property -> load_context -> identify_case``
    (uncheckpointed), then ``classify_intent -> classify_severity ->
    [draft_response | degraded_mode]`` (checkpointed under the resolved
    case thread). Returns the final ``AgentState``.

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
    "NODE_CLASSIFY_INTENT",
    "NODE_CLASSIFY_SEVERITY",
    "NODE_DEGRADED_MODE",
    "NODE_DRAFT_RESPONSE",
    "NODE_IDENTIFY_CASE",
    "NODE_IDENTIFY_PROPERTY",
    "NODE_LOAD_CONTEXT",
    "build_case_graph",
    "build_pre_routing_graph",
    "compile_case_graph",
    "compile_pre_routing_graph",
    "run_graph",
]
