"""``classify_intent`` node (#31) — maintenance / admin / question / other.

Runs after ``identify_case`` (architecture.md §5 / apps/api/CLAUDE.md's
``agent/nodes/`` layout order: identify_property -> load_context ->
identify_case -> classify_intent -> classify_severity -> draft_response),
so ``state["case_context"].case_id`` is already set when this node runs
(``None`` only for the unknown-sender case, which has nothing to classify
against a case anyway — this node still classifies the message itself,
since intent classification doesn't depend on case routing).

Anthropic SDK call, forced tool-use
---------------------------------------
Goes through ``app/integrations/anthropic.py``'s single call site
(``call_tool_forced``) — never constructs its own client. Tool schema is
``app.agent.tools.CLASSIFY_INTENT_TOOL`` (#29); the model's forced response
is validated by ``app.agent.schemas.IntentResult`` (#27) before it ever
touches ``AgentState``.

Sends the tenant's message text to Anthropic BY DESIGN — see
``app/integrations/anthropic.py``'s module docstring. Never-break rule #5
(no message bodies/phone numbers in OUR OWN logs) is respected below: only
uuids/booleans/enum values are ever passed to ``log.*`` calls.

Prompt-file gap (reported, not resolved unilaterally)
--------------------------------------------------------
``app/agent/prompts/v1.py`` is FROZEN and, as shipped, only defines a
system prompt for ``classify_severity`` (the rubric-embedded one) and for
``draft_response`` (the voice-profile one) — it has NO dedicated system
prompt for intent classification; there is no "intent rubric" doc to
embed verbatim the way ``rubric.py`` is embedded for severity. Rather than
editing the frozen file (forbidden — a v2 bump requiring a full eval run
is a governed decision this issue does not make unilaterally) or blocking
this node's delivery entirely, :data:`_INTENT_SYSTEM_PROMPT` below is a
small, INLINE, unversioned system prompt defined in this module — not a
``prompts/vN.py`` file, and not subject to the rubric-style checksum/eval-
gate machinery (there is nothing rubric-like or safety-critical being
embedded here; it is ordinary tool-use guidance, comparable to the prose
already living in each tool's ``description`` field in ``app/agent/
tools.py``). Flagged in the issue report for the spec owner to decide
whether a future, explicitly-versioned prompt file should own this text
instead.

20 s END-TO-END budget / retry
----------------------------------
Uses the shared deadline arithmetic in ``app/integrations/anthropic.py``
(``new_deadline`` / ``attempt_timeout``) -- ONE 20-second deadline for the
initial attempt AND its single retry TOGETHER, not 20 seconds each (see
that module's docstring "Budget / retry" for the full split rationale:
first attempt capped at 12s, retry gets whatever remains, skipped
entirely below a 2s floor).

Unlike ``classify_severity`` (#32), a double failure here does NOT set a
dedicated state flag or block anything safety-critical -- no downstream
code consumes ``state["intent"]`` yet (``app/agent/case_lifecycle.py``'s
own docstring: "no node populates a multi-issue/intent signal yet
(#31/#32)" -- wiring that in is explicitly future work, not this issue).
On double failure (including a skipped retry when the budget is
exhausted) this node logs, appends a plain reasoning_log line, and
returns with ``intent`` left unset -- no invented category.

Cost accounting (audit_log 'classified' payload)
-----------------------------------------------------
On a SUCCESSFUL classification, this node writes its OWN ``audit_log``
row: ``actor='agent'``, ``action='classified'`` (the SAME existing
vocabulary entry ``classify_severity.py`` uses — schema-v1.md's
``audit_log.action`` CHECK doesn't have a separate "intent classified"
action, and doesn't need one), ``payload = {kind: 'intent', intent,
summary, model, tokens_in, tokens_out, cost_cents, prompt_version:
'inline-v0'}``. ``kind`` disambiguates this row from a
severity-classification 'classified' row on the same case's timeline.
``prompt_version`` is ``'inline-v0'``, not ``'v1'`` — this node's system
prompt is the small INLINE, unversioned string above (see "Prompt-file
gap"), never the governed ``prompts/v1.py`` module, so it gets its own,
clearly-distinct version tag rather than borrowing the governed one. No
message body ever enters this payload.

Cost accounting on TOTAL failure (#208)
-------------------------------------------
Unlike ``classify_severity``, a double failure here has NO downstream
node to piggyback a cost record on — ``app.agent.graph`` never routes an
intent-only failure to ``degraded_mode`` (this node's own failure doesn't
set any dedicated state flag; see this module's own docstring above,
"20 s END-TO-END budget / retry", and the graph's own routing docstring
for why: no consumer of ``state["intent"]`` exists yet, so nothing gates
on it). Before #208, that
meant an Anthropic attempt which reached the API and consumed billed
tokens (this node's own ``IntentResult`` validation rejecting the model's
output, or the SDK's forced-``tool_choice`` response carrying no usable
``tool_use`` block — see ``app/integrations/anthropic.py``'s
``AnthropicCallError`` docstring "Reached-the-API usage, when it exists")
left literally no durable trace anywhere, understating real spend.

Reusing the EXISTING ``'classified'`` action (no new ``audit_log.action``
CHECK value, no migration) is the minimal honest fix: when BOTH attempts
fail AND at least one genuinely reached the API (real usage exists), this
node writes ONE additional ``'classified'`` row itself —
``payload = {kind: 'intent_classification_failed', message_id, case_id,
model, tokens_in, tokens_out, cost_cents}`` — summed across every failed
attempt's reached-the-API usage, never fabricated when neither attempt
ever reached the API (a pure connection/timeout failure writes nothing,
same as before #208). No ``intent``/``summary``/``is_new_issue`` keys —
this payload never claims a classification happened, only that an attempt
was made and here is what it cost. ``app/cost_reporting.py``'s existing
``action IN ('classified', 'drafted') AND payload ? 'cost_cents'`` branch
already matches this shape unmodified (schema-v1.md v1.13 amendment) —
no CTE change needed for this half of #208's fix (contrast
``classify_severity.py``'s failure path, which DOES need a new CTE branch
because it piggybacks on the ``'degraded_mode'`` action instead).

**Scope, honestly stated (same caveat as ``classify_severity.py``):** only
the DOUBLE-failure case is covered. A first attempt that reaches the API
and fails, followed by a second that succeeds, still records only the
winning attempt's usage on the SUCCESS row above — unchanged, not this
issue's scope.

DB access
---------
Admin engine (background/graph context) — same pattern as the other #30/
#110 nodes. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import text

from app.agent.schemas import CaseContext, Intent, IntentResult
from app.agent.state import AgentState
from app.agent.tools import CLASSIFY_INTENT_TOOL
from app.db.session import get_admin_session
from app.integrations import anthropic as anthropic_mod

log = structlog.get_logger(__name__)

_INTENT_PROMPT_VERSION: str = "inline-v0"
"""Distinct from ``app.agent.prompts.v1.PROMPT_VERSION`` ("v1") — this
node's system prompt is the inline, unversioned string above, never the
governed ``prompts/v1.py`` module (see "Prompt-file gap")."""

# ---------------------------------------------------------------------------
# Inline system prompt — see module docstring "Prompt-file gap".
# ---------------------------------------------------------------------------

_INTENT_SYSTEM_PROMPT: str = """\
You are Stoop, an AI assistant that helps landlords handle tenant maintenance
requests. Your ONLY job in this step is to classify the INTENT of the
inbound tenant message and decide whether it describes a new issue.

Call the classify_intent tool with:
- intent: one of
  - "maintenance" — a repair/upkeep problem with the unit or building
  - "admin" — paperwork, receipts, parking, guests, amenities, move logistics
  - "question" — the tenant is asking about something, not reporting a problem
  - "other" — anything that does not fit the categories above
- is_new_issue: true when this message describes something NOT already
  covered by one of the tenant's currently open conversations listed below;
  false when it clearly continues or updates one of them.
- summary: one short, plain-English sentence describing the issue or
  update, for the landlord's approval card.

Respond only by calling the tool — no other text.
"""

_SELECT_MESSAGE_SQL = text("SELECT body FROM messages WHERE id = :message_id")

_INSERT_CLASSIFIED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'agent', 'classified', CAST(:payload AS jsonb))"
)

_INTENT_DISPLAY: dict[Intent, str] = {
    Intent.maintenance: "a maintenance issue",
    Intent.admin: "an admin request",
    Intent.question: "a question",
    Intent.other: "something else",
}


def _format_open_cases(open_cases: list[dict[str, Any]]) -> str:
    if not open_cases:
        return "(none)"
    lines = [
        f"- {case.get('title') or 'untitled'} "
        f"(status: {case.get('status')}, severity: {case.get('severity') or 'unclassified'})"
        for case in open_cases
    ]
    return "\n".join(lines)


def _format_channel_history(channel_history: list[dict[str, Any]], *, limit: int = 6) -> str:
    if not channel_history:
        return "(no prior messages)"
    recent = channel_history[-limit:]
    lines = [f"{entry['role']}: {entry['body']}" for entry in recent]
    return "\n".join(lines)


def _build_user_content(
    *, body: str, open_cases: list[dict[str, Any]], channel_history: list[dict[str, Any]]
) -> str:
    return (
        f"Tenant's currently open conversations:\n{_format_open_cases(open_cases)}\n\n"
        f"Recent channel history:\n{_format_channel_history(channel_history)}\n\n"
        f"New inbound message to classify:\n{body}"
    )


async def _classify_intent_once(
    *, user_content: str, budget_seconds: float
) -> anthropic_mod.ToolCallResult:
    """Make ONE Anthropic call and return its raw, parsed result — does NOT
    validate ``tool_input`` into :class:`IntentResult` (#208 fix: see
    ``classify_severity.py``'s identical split for the full rationale —
    bundling validation into this function meant a call that reached the
    API and consumed billed tokens but then failed OUR OWN schema
    validation never surfaced that usage to the caller)."""
    return await anthropic_mod.call_tool_forced(
        system=_INTENT_SYSTEM_PROMPT,
        user_content=user_content,
        tool=CLASSIFY_INTENT_TOOL,
        tool_name="classify_intent",
        timeout_seconds=budget_seconds,
    )


async def _insert_intent_classified_audit(
    *,
    landlord_id: UUID,
    case_id: UUID | None,
    intent_result: IntentResult,
    call_result: anthropic_mod.ToolCallResult,
    cost_cents: float,
) -> None:
    payload = {
        "kind": "intent",
        "intent": intent_result.intent.value,
        "summary": intent_result.summary,
        "model": call_result.model,
        "tokens_in": call_result.tokens_in,
        "tokens_out": call_result.tokens_out,
        "cost_cents": cost_cents,
        "prompt_version": _INTENT_PROMPT_VERSION,
    }
    async with asynccontextmanager(get_admin_session)() as session:
        await session.execute(
            _INSERT_CLASSIFIED_AUDIT_SQL,
            {
                "landlord_id": str(landlord_id),
                "case_id": str(case_id) if case_id is not None else None,
                "payload": json.dumps(payload),
            },
        )


async def _insert_intent_classification_failed_audit(
    *,
    landlord_id: UUID,
    case_id: UUID | None,
    message_id: UUID,
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    cost_cents: float,
) -> None:
    """#208 — reuses the SAME ``'classified'`` action as a successful
    intent classification (no new ``audit_log.action`` CHECK value), but a
    payload shape that never claims a classification happened: no
    ``intent``/``summary``/``is_new_issue`` keys, only the cost of a
    DOUBLE-failed attempt that genuinely reached the API at least once. See
    this module's docstring "Cost accounting on TOTAL failure (#208)"."""
    payload = {
        "kind": "intent_classification_failed",
        "message_id": str(message_id),
        "case_id": str(case_id) if case_id is not None else None,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_cents": cost_cents,
    }
    async with asynccontextmanager(get_admin_session)() as session:
        await session.execute(
            _INSERT_CLASSIFIED_AUDIT_SQL,
            {
                "landlord_id": str(landlord_id),
                "case_id": str(case_id) if case_id is not None else None,
                "payload": json.dumps(payload),
            },
        )


async def classify_intent(state: AgentState) -> dict[str, Any]:
    """Classify the inbound message's intent. Returns a partial state update."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    open_cases = list(state.get("open_cases") or [])
    channel_history = list(state.get("channel_history") or [])

    async with asynccontextmanager(get_admin_session)() as session:
        body: str = (
            (await session.execute(_SELECT_MESSAGE_SQL, {"message_id": str(message_id)}))
            .mappings()
            .one()
        )["body"]

    user_content = _build_user_content(
        body=body, open_cases=open_cases, channel_history=channel_history
    )

    deadline = anthropic_mod.new_deadline()
    intent_result: IntentResult | None = None
    call_result: anthropic_mod.ToolCallResult | None = None
    # #208: sum of every FAILED attempt's reached-the-API usage -- see this
    # module's docstring "Cost accounting on TOTAL failure (#208)".
    failed_tokens_in = 0
    failed_tokens_out = 0
    failed_model: str | None = None
    for attempt in range(2):
        timeout = anthropic_mod.attempt_timeout(deadline, is_retry=attempt == 1)
        if timeout is None:
            log.error("classify_intent_retry_skipped_budget_exhausted", message_id=str(message_id))
            break
        try:
            call_result = await _classify_intent_once(
                user_content=user_content, budget_seconds=timeout
            )
        except anthropic_mod.AnthropicCallError as exc:
            log.error(
                "classify_intent_attempt_failed",
                message_id=str(message_id),
                attempt=attempt,
                exc_type=type(exc).__name__,
            )
            call_result = None
            if exc.tokens_in is not None:
                failed_tokens_in += exc.tokens_in
                failed_tokens_out += exc.tokens_out or 0
                failed_model = exc.model
            continue

        try:
            intent_result = IntentResult.model_validate(call_result.tool_input)
            break
        except ValidationError as exc:
            log.error(
                "classify_intent_attempt_failed",
                message_id=str(message_id),
                attempt=attempt,
                exc_type=type(exc).__name__,
            )
            failed_tokens_in += call_result.tokens_in
            failed_tokens_out += call_result.tokens_out
            failed_model = call_result.model
            call_result = None

    if intent_result is None or call_result is None:
        reasoning_log.append(
            "I couldn't figure out what kind of message this is right now — moving on without it."
        )
        if (failed_tokens_in or failed_tokens_out) and case_context.landlord_id is not None:
            failed_cost_cents = anthropic_mod.estimate_cost_cents(
                tokens_in=failed_tokens_in, tokens_out=failed_tokens_out
            )
            await _insert_intent_classification_failed_audit(
                landlord_id=case_context.landlord_id,
                case_id=case_context.case_id,
                message_id=message_id,
                model=failed_model,
                tokens_in=failed_tokens_in,
                tokens_out=failed_tokens_out,
                cost_cents=failed_cost_cents,
            )
        return {"intent": None, "reasoning_log": reasoning_log}

    display = _INTENT_DISPLAY[intent_result.intent]
    reasoning_log.append(f"This looks like {display}: {intent_result.summary}")

    cost_cents = anthropic_mod.estimate_cost_cents(
        tokens_in=call_result.tokens_in, tokens_out=call_result.tokens_out
    )
    if case_context.landlord_id is not None:
        await _insert_intent_classified_audit(
            landlord_id=case_context.landlord_id,
            case_id=case_context.case_id,
            intent_result=intent_result,
            call_result=call_result,
            cost_cents=cost_cents,
        )
    else:  # pragma: no cover — invariant: landlord_id is always known by this point
        log.error("classify_intent_missing_landlord_id", message_id=str(message_id))

    return {"intent": intent_result, "reasoning_log": reasoning_log}


__all__: list[str] = ["classify_intent"]
