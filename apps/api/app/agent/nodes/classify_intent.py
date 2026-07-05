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

DB access
---------
Admin engine (background/graph context) — same pattern as the other #30/
#110 nodes. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import text

from app.agent.schemas import Intent, IntentResult
from app.agent.state import AgentState
from app.agent.tools import CLASSIFY_INTENT_TOOL
from app.db.session import get_admin_session
from app.integrations import anthropic as anthropic_mod

log = structlog.get_logger(__name__)

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
) -> tuple[IntentResult, anthropic_mod.ToolCallResult]:
    call_result = await anthropic_mod.call_tool_forced(
        system=_INTENT_SYSTEM_PROMPT,
        user_content=user_content,
        tool=CLASSIFY_INTENT_TOOL,
        tool_name="classify_intent",
        timeout_seconds=budget_seconds,
    )
    intent_result = IntentResult.model_validate(call_result.tool_input)
    return intent_result, call_result


async def classify_intent(state: AgentState) -> dict[str, Any]:
    """Classify the inbound message's intent. Returns a partial state update."""
    message_id = state["message_id"]
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
    for attempt in range(2):
        timeout = anthropic_mod.attempt_timeout(deadline, is_retry=attempt == 1)
        if timeout is None:
            log.error("classify_intent_retry_skipped_budget_exhausted", message_id=str(message_id))
            break
        try:
            intent_result, _call_result = await _classify_intent_once(
                user_content=user_content, budget_seconds=timeout
            )
            break
        except (anthropic_mod.AnthropicCallError, ValidationError) as exc:
            log.error(
                "classify_intent_attempt_failed",
                message_id=str(message_id),
                attempt=attempt,
                exc_type=type(exc).__name__,
            )

    if intent_result is None:
        reasoning_log.append(
            "I couldn't figure out what kind of message this is right now — moving on without it."
        )
        return {"intent": None, "reasoning_log": reasoning_log}

    display = _INTENT_DISPLAY[intent_result.intent]
    reasoning_log.append(f"This looks like {display}: {intent_result.summary}")

    return {"intent": intent_result, "reasoning_log": reasoning_log}


__all__: list[str] = ["classify_intent"]
