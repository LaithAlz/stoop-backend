"""AgentState TypedDict — the shared state threaded through every graph node.

Architecture reference: ``docs/03-engineering/architecture.md`` §5.

Convention: every node that modifies state MUST append at least one
human-readable line to ``reasoning_log``.  This list is displayed to the
landlord on the approval card ("WHY URGENT — …") and drives the LangSmith
trace readability.  It is not a debug artifact; it is a product feature.

LangGraph compatibility: LangGraph requires the graph state to be a plain
``TypedDict`` (or a class built on top of it).  Do not convert this to a
Pydantic ``BaseModel`` — the graph's checkpoint serialiser (Postgres)
expects standard dict semantics.  Pydantic validation happens at the
*boundary* of each node (construct a result model, validate, then write its
dict into state).

``total=False`` means every key is optional at construction time.  Nodes
populate fields as the graph runs; unvisited branches leave them as
``None``.  Callers should treat missing keys (``state.get("field")``) as
``None``, not as an error.
"""

from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

from app.agent.schemas import (
    CaseContext,
    DraftResult,
    IntentResult,
    PrefilterResult,
    SeverityResult,
    WeatherSnapshot,
)


class AgentState(TypedDict, total=False):
    """LangGraph graph state — one instance per in-flight message.

    Fields
    ------
    message_id:
        The ``messages.id`` of the inbound message that triggered this graph
        run.  Set by the CALLER (``app/agent/graph_entry.py``, #30 onward)
        before the graph starts — this is the one piece of state that is
        never re-derived by a node, it is the entry point every node re-
        derives FROM.  ``identify_property`` loads the message row keyed on
        this id and re-derives ``case_context``'s identifiers from the
        row's own persisted ``landlord_id``/``property_id``/``tenant_id``
        rather than trusting anything else the caller might have set (the
        message row, written by the webhook before the graph ever runs, is
        the source of truth — see ``app/agent/nodes/identify_property.py``).

    case_context:
        Identifiers and property metadata populated by ``identify_property``
        and ``load_context``.  Present on every run.

    open_cases:
        The tenant's currently OPEN cases (``open``/``awaiting_approval``/
        ``awaiting_tenant``/``reopened`` — never ``resolved``), extracted by
        ``load_context`` and consumed by ``identify_case``'s routing
        decision (conversation-model.md's ambiguity rule: 0 → new case, 1 →
        attach, >1 → ambiguous, attach to most recent + note).  Ordered most
        -recently-active first.  Each entry is an ``OpenCaseSummary`` (or its
        ``model_dump()`` dict equivalent) — same "store as dicts for
        checkpoint serialisation" convention as ``channel_history`` below.
        ``None``/absent before ``load_context`` runs, or when the message
        has no known tenant (unknown sender — no case can ever be opened,
        see ``app/agent/nodes/identify_case.py``).

    channel_history:
        A recent slice of the channel's message history (tenant ↔ property
        thread), extracted by ``load_context`` for the LLM's conversation
        window.  Each entry is a ``ChannelMessage`` (or its ``model_dump()``
        dict equivalent for checkpoint serialisation).

        Implementation note: store as ``list[dict[str, Any]]`` in the state
        dict so the LangGraph Postgres checkpointer can serialise it without
        custom serialisers.  Nodes that need typed access should call
        ``ChannelMessage(**msg)`` on each entry.

    prefilter:
        Result of the Tier-0 deterministic keyword filter, set by the webhook
        handler *before* the graph is invoked.  ``None`` only if the webhook
        was received before the prefilter was wired (should not happen in
        production).  ``hard_hit=True`` means the emergency protocol was
        already triggered; the agent may escalate further but never
        de-escalate.

    weather:
        Current and forecast weather for the property, fetched by
        ``load_context``.  ``None`` when the weather API is unavailable; the
        severity node must treat missing weather conservatively (bias rule).

    severity:
        Output of ``classify_severity``.  ``None`` until that node completes.

    intent:
        Output of ``classify_intent``.  ``None`` until that node completes.
        Stored separately from ``severity`` because ``identify_case`` needs
        intent before severity is known.

    draft:
        Output of ``draft_response``.  ``None`` until the draft node runs
        (emergency branch may skip it in favour of templated safety
        instructions).

    classification_failed:
        Set ``True`` by ``classify_severity`` (#32) ONLY when the Anthropic
        call fails twice (initial attempt + one retry — timeout, API error,
        or Pydantic validation failure) within the 20 s per-attempt budget
        (``docs/02-product/emergency-prefilter.md``'s "Classification
        budget"). ``severity`` is left unset in that case — there is NO
        silent fallback severity, ever. This is a SEAM: #109 (the degraded-
        mode protocol) is not built yet, so today this flag is set, logged,
        and left for a future graph (#34) to route to that protocol — see
        ``app/agent/nodes/classify_severity.py``'s module docstring, which
        documents the seam the same way ``app/agent/emergency.py`` does for
        #108. Absent/``False`` in every other case.

    draft_guard_failed:
        Set ``True`` by ``draft_response`` (#33) when the model's own
        acknowledgment text still violates a hard guard (dollar amounts/
        compensation, access codes, or a legal position) after ONE
        regeneration attempt. The draft that IS stored in that case
        replaces just that acknowledgment with a safe generic fallback
        (the mandated deferral, if any, is still appended verbatim
        regardless) — this flag is the "needs a person's eyes on this one"
        signal for a future node/notification to act on (same seam pattern
        as ``classification_failed`` above). Absent/``False`` otherwise.

    length_over_budget:
        Set ``True`` by ``draft_response`` (#33, senior review 2026-07-05)
        when the final drafted body still exceeds the plain-language
        length budget (``docs/02-product/plain-language-rules.md`` rule
        5 — routine/urgent/emergency drafts, ~300 chars; refusal-topic
        drafts with an appended deferral are exempt, see that node's
        module docstring) after ONE regeneration attempt asking the model
        to shorten it. TRUNCATION IS FORBIDDEN: the long draft is kept
        as-is (never cut mid-sentence) — this flag is a landlord-review
        signal ("you can shorten this before it sends"), never a reason to
        replace or hide the draft's content. Absent/``False`` otherwise.
        Independent of ``draft_guard_failed`` — a draft can be guard-clean
        but still over length, or vice versa.

    approval_resume:
        Set by ``app.agent.nodes.await_approval.await_approval`` (#44/#45)
        to whatever value LangGraph's ``interrupt()`` call returns on the
        attempt that actually gets resumed (i.e. the dict passed to
        ``Command(resume=...)`` by ``app.agent.graph.resume_case_thread``/
        ``resolve_draft_decision``) — ``{"action": "approve" | "reject" |
        "edit_and_send", ...}`` (see
        ``app/agent/nodes/finalize_draft_decision.py`` for the exact
        vocabulary). Explicitly ``None`` on every run that never completes a
        FRESH resume this invocation — seeded ``None`` in ``run_graph``'s
        initial state, and explicitly reset to ``None`` by BOTH of
        ``await_approval``'s skip-the-pause branches (see that module's own
        docstring "Hardening" — safety review finding: LangGraph's
        last-write-wins merge means a key nothing explicitly overwrites
        otherwise CARRIES FORWARD in the checkpoint indefinitely, not just
        "for the resumed attempt" as an earlier revision of this docstring
        incorrectly claimed — a genuinely stale value from an EARLIER
        resume on the same thread could otherwise reach a LATER,
        unrelated pass through this same node). Consumed EXCLUSIVELY by
        ``app.agent.graph``'s ``_route_after_await_approval`` conditional
        edge to pick the next node; no node reads this key for any other
        purpose. Never a message body or phone number — the
        ``edit_and_send`` action's ``body`` key is landlord-authored
        replacement SMS text, already stored durably in ``drafts.
        final_body``, not something this key introduces a new home for.

    reasoning_log:
        Append-only list of human-readable trace lines.  Every node MUST
        append at least one entry describing what it observed and decided.
        This is landlord-visible copy (the approval card), not a debug log —
        warm, plain English, no ``node_name:`` prefixes, no field=value
        reprs, no raw ids (#30/#110 review). Example entries:
          "This message came in from Maria at 41 Palmerston."
          "Right now it's -8°C outside, with an overnight low of -12°C."
          "This looks like it continues Maria's open conversation, so I
           added it there."
        This list is shown verbatim on the landlord's approval card and is
        included in LangSmith traces.  Do NOT include tenant phone numbers,
        message bodies, or any other PII in these strings — put ids/booleans
        in structlog calls instead, never in a ``reasoning_log`` string.

        Accumulation note (until #34 wires the graph): plain TypedDict keys
        have LangGraph's default "last write wins" merge semantics, not
        list-append, unless a node's return value is annotated with a
        reducer (e.g. ``Annotated[list[str], operator.add]``) — a decision
        left to #34 ("wire state graph"), not this issue. Every node in
        ``app/agent/nodes/`` is therefore written defensively: it reads the
        FULL incoming ``reasoning_log``, appends its own line(s), and
        returns the FULL list — so the log accumulates correctly whether or
        not #34 later adds a reducer annotation (a reducer would simply make
        this belt-and-braces pattern redundant, never incorrect).
    """

    message_id: UUID
    case_context: CaseContext
    open_cases: list[dict[str, Any]]
    channel_history: list[dict[str, Any]]
    prefilter: PrefilterResult | None
    weather: WeatherSnapshot | None
    intent: IntentResult | None
    severity: SeverityResult | None
    draft: DraftResult | None
    classification_failed: bool
    draft_guard_failed: bool
    length_over_budget: bool
    approval_resume: dict[str, Any] | None
    reasoning_log: list[str]
