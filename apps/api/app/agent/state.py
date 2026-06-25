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
    case_context:
        Identifiers and property metadata populated by ``identify_property``
        and ``load_context``.  Present on every run.

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

    reasoning_log:
        Append-only list of human-readable trace lines.  Every node MUST
        append at least one entry describing what it observed and decided.
        Example entries:
          "identify_property: matched property 'id=…' via Twilio number +1416…"
          "classify_severity: EMERGENCY — burst pipe (active uncontained water rule)"
          "vulnerable_occupant modifier: raised URGENT → EMERGENCY (infant)"
        This list is shown verbatim on the landlord's approval card and is
        included in LangSmith traces.  Do NOT include tenant phone numbers,
        message bodies, or any PII in these strings.
    """

    case_context: CaseContext
    channel_history: list[dict[str, Any]]
    prefilter: PrefilterResult | None
    weather: WeatherSnapshot | None
    intent: IntentResult | None
    severity: SeverityResult | None
    draft: DraftResult | None
    reasoning_log: list[str]
