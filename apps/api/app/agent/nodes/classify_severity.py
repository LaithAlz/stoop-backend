"""``classify_severity`` node (#32) — the core of the product.

Runs after ``classify_intent`` (architecture.md §5 / apps/api/CLAUDE.md's
node layout order). Direct Anthropic SDK call (no wrapper beyond
``app/integrations/anthropic.py``'s single call site), rubric v1.0
embedded VERBATIM via ``app.agent.prompts.v1.get_classify_system_prompt()``
(frozen — never edited here), ``tool_choice`` forced to
``classify_severity`` (#29), output validated by
``app.agent.schemas.SeverityResult`` (#27).

Sends the tenant's message text to Anthropic BY DESIGN — see
``app/integrations/anthropic.py``'s module docstring. Never-break rule #5
is respected in this module's own logging: only uuids, category names,
booleans, and severity enum VALUES ever reach ``log.*`` calls — never the
message body.

The degraded-mode seam (#109 isn't built yet)
-----------------------------------------------
Mirrors ``app/agent/emergency.py``'s seam pattern exactly: this node makes
ONE attempt, and on failure (timeout, Anthropic API error, or a Pydantic
validation failure on the tool's output) makes exactly ONE retry — both
drawing from a SINGLE shared 20-second END-TO-END deadline
(``app/integrations/anthropic.py``'s ``new_deadline``/``attempt_timeout``;
see that module's docstring "Budget / retry" for the 12s-cap/2s-floor
split), not 20 seconds each
(``docs/02-product/emergency-prefilter.md``'s "Classification budget: 20
seconds ... on timeout, API error, or hard failure after one retry"). A
retry that would fall below the 2s floor is skipped entirely — treated
exactly like a second failure, never attempted with a near-zero timeout.
If BOTH attempts fail (or the retry is skipped for budget exhaustion),
this node does EXACTLY three things — no more, no less — and stops:

1. sets ``state["classification_failed"] = True``,
2. appends a plain reasoning_log line,
3. logs a structlog error.

It does NOT invent a fallback severity, and it does NOT write an
``audit_log`` row for the failure — ``'degraded_mode'`` exists in
``schema-v1.md``'s ``audit_log.action`` vocabulary, but creating that
DURABLE record is #109's job, once a future graph (#34) routes this flag
to #109's actual protocol (holding ack + landlord notification per
emergency-prefilter.md's degraded-mode table). Exactly like
``app/agent/emergency.py``'s ``fire_emergency_protocol`` seam, this module
does the minimum honest thing today and leaves the real protocol for the
issue that owns it.

The Tier-0 clamp — never de-escalate a Tier-0 fire
------------------------------------------------------
``app/agent/prefilter.py``'s ``PrefilterResult.hard_hit`` (snapshotted onto
``messages.prefilter`` by the webhook, BEFORE this graph ever runs) is the
one thing this node is never allowed to override downward. After a
successful classification, if the prefilter fired but the model's own
severity came back anything other than EMERGENCY, this node CLAMPS the
severity to EMERGENCY, records the clamp in ``rules_fired``, and appends
the mandated reasoning_log line verbatim: "The alarm phrasing already made
this an emergency — I kept it there." (mirrors
``app/agent/nodes/identify_case.py``'s own "never re-run, never de-escalate
Tier-0" precedent for the exact same invariant). The reverse direction is
always allowed: the model may escalate past a Tier-0 miss with no
special-casing at all.

Canonical classification record — ``audit_log`` 'classified' (not
``messages`` columns)
------------------------------------------------------------------------
``schema-v1.md`` lists ``messages.classification`` / ``tokens_in`` /
``tokens_out`` / ``model`` / ``llm_cost_cents`` — but ``messages`` is
append-only (never-break rule #2) and the row is INSERTED by the webhook
BEFORE classification ever runs, so writing those columns after the fact
would require an UPDATE on an append-only table. Exactly the same
contradiction class as the (already-resolved) ``messages.twilio_status``
precedent (schema-v1.md's v1.1 amendments: superseded by an append-only
event table instead of an in-place UPDATE). The CANONICAL record here is
instead an ``audit_log`` row: ``actor='agent'``, ``action='classified'``
(existing vocabulary — schema-v1.md's ``audit_log.action`` CHECK already
lists it), ``payload = {message_id, case_id, severity, rules_fired,
modifier, refusal_flags, model, tokens_in, tokens_out, cost_cents,
prompt_version}``. This matches ``docs/03-engineering/api-contracts.md``'s
own timeline example (``GET /v1/cases/{id}``: ``{"kind": "audit", "actor":
"agent", "action": "classified", "payload": {"severity": "urgent",
"rules_fired": ["…"]}}``), extended with the token/cost/model fields this
issue also needs recorded somewhere durable. NO message body ever enters
this payload — only the structured fields above (per-issue reasoning
sentences stay in ``reasoning_log``/``state["severity"]``, landlord-visible
on the approval card, never duplicated into the audit trail).

**Doc-first, applied:** the five ``messages`` columns above are marked
DEPRECATED in ``schema-v1.md`` (v1.6 amendments — never written; canonical
record is ``audit_log`` 'classified'; DROP in a future migration), both in
the amendments prose and as inline column comments — mirroring the
``twilio_status`` deprecation wording exactly. Done in this same PR, not
deferred.

Cost accounting: ``Severity.db_value`` is used for the audit payload's
``severity`` field (lowercase, matching ``cases.severity``'s CHECK) per
schema-v1.md rule #6 — never ``.value`` directly.

DB access
---------
Admin engine (background/graph context), same pattern as the other #30/
#110 nodes — one session to read the message row, the Anthropic call made
OUTSIDE any open session (mirrors ``load_context.py``'s weather-lookup
pattern: never hold a pooled DB connection across a slow external call),
then a second session to write the audit row. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import text

from app.agent.prompts.v1 import PROMPT_VERSION, get_classify_system_prompt
from app.agent.schemas import CaseContext, PrefilterResult, Severity, SeverityResult
from app.agent.state import AgentState
from app.agent.tools import CLASSIFY_SEVERITY_TOOL
from app.db.session import get_admin_session
from app.integrations import anthropic as anthropic_mod

log = structlog.get_logger(__name__)

_SEVERITY_DISPLAY: dict[Severity, str] = {
    Severity.EMERGENCY: "an emergency",
    Severity.URGENT: "urgent",
    Severity.ROUTINE: "routine",
}

_SELECT_MESSAGE_SQL = text("SELECT body, prefilter FROM messages WHERE id = :message_id")

_INSERT_CLASSIFIED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'agent', 'classified', CAST(:payload AS jsonb))"
)


def _parse_prefilter(raw: Any) -> PrefilterResult:
    """Same fallback as ``identify_case.py``'s ``_parse_prefilter``: the
    webhook always writes a snapshot, so a missing one is an anomaly to log,
    not a reason to block classification. Duplicated rather than imported —
    project convention (see ``tests/test_agent_nodes.py``'s own docstring)."""
    if raw is None:
        log.error("classify_severity_prefilter_snapshot_missing")
        return PrefilterResult(hard_hit=False)
    return PrefilterResult.model_validate(raw)


def _build_user_content(
    *,
    body: str,
    weather: Any,
    vulnerable_occupant: Any,
    heating_season: dict[str, Any] | None,
    prefilter_result: PrefilterResult,
    now: datetime,
) -> str:
    lines: list[str] = [f"Tenant's inbound message:\n{body}", "", "Context:"]

    if weather is not None:
        current = weather.current_temp_c
        overnight = weather.overnight_low_c
        lines.append(
            f"- Current outdoor temperature: {current}°C"
            if current is not None
            else "- Current outdoor temperature: unavailable"
        )
        lines.append(
            f"- Forecast overnight low: {overnight}°C"
            if overnight is not None
            else "- Forecast overnight low: unavailable"
        )
        lines.append(f"- Official heat warning active: {'yes' if weather.heat_warning else 'no'}")
    else:
        lines.append("- Weather data is unavailable for this property right now.")

    if vulnerable_occupant is not None:
        lines.append(f"- This unit has a vulnerable occupant on file: {vulnerable_occupant.value}")
    else:
        lines.append("- No vulnerable occupant on file for this unit.")

    if heating_season:
        lines.append(
            f"- Property's heating season: {heating_season.get('start')} to "
            f"{heating_season.get('end')}"
        )

    lines.append(f"- Current date/time (UTC): {now.isoformat()}")

    if prefilter_result.soft_annotations:
        lines.append(
            "- Automatic keyword hints detected (non-binding, do not override your own "
            f"judgment): {', '.join(prefilter_result.soft_annotations)}"
        )

    return "\n".join(lines)


async def _classify_severity_once(
    *, user_content: str, budget_seconds: float
) -> tuple[SeverityResult, anthropic_mod.ToolCallResult]:
    call_result = await anthropic_mod.call_tool_forced(
        system=get_classify_system_prompt(),
        user_content=user_content,
        tool=CLASSIFY_SEVERITY_TOOL,
        tool_name="classify_severity",
        timeout_seconds=budget_seconds,
    )
    severity_result = SeverityResult.model_validate(call_result.tool_input)
    return severity_result, call_result


async def _insert_classified_audit(
    *,
    landlord_id: UUID,
    case_id: UUID | None,
    message_id: UUID,
    severity_result: SeverityResult,
    call_result: anthropic_mod.ToolCallResult,
    cost_cents: float,
) -> None:
    payload = {
        "message_id": str(message_id),
        "case_id": str(case_id) if case_id is not None else None,
        "severity": severity_result.severity.db_value,
        "rules_fired": severity_result.rules_fired,
        "modifier": severity_result.modifier,
        "refusal_flags": [flag.value for flag in severity_result.refusal_flags],
        "model": call_result.model,
        "tokens_in": call_result.tokens_in,
        "tokens_out": call_result.tokens_out,
        "cost_cents": cost_cents,
        "prompt_version": PROMPT_VERSION,
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


async def classify_severity(state: AgentState) -> dict[str, Any]:
    """Classify the inbound message's severity. Returns a partial state
    update. Never invents a severity on failure — see module docstring."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    weather = state.get("weather")

    async with asynccontextmanager(get_admin_session)() as session:
        message_row = (
            (await session.execute(_SELECT_MESSAGE_SQL, {"message_id": str(message_id)}))
            .mappings()
            .one()
        )
    body: str = message_row["body"]
    prefilter_result = _parse_prefilter(message_row["prefilter"])

    user_content = _build_user_content(
        body=body,
        weather=weather,
        vulnerable_occupant=case_context.vulnerable_occupant,
        heating_season=case_context.heating_season,
        prefilter_result=prefilter_result,
        now=datetime.now(UTC),
    )

    deadline = anthropic_mod.new_deadline()
    severity_result: SeverityResult | None = None
    call_result: anthropic_mod.ToolCallResult | None = None
    for attempt in range(2):
        timeout = anthropic_mod.attempt_timeout(deadline, is_retry=attempt == 1)
        if timeout is None:
            log.error(
                "classify_severity_retry_skipped_budget_exhausted", message_id=str(message_id)
            )
            break
        try:
            severity_result, call_result = await _classify_severity_once(
                user_content=user_content, budget_seconds=timeout
            )
            break
        except (anthropic_mod.AnthropicCallError, ValidationError) as exc:
            log.error(
                "classify_severity_attempt_failed",
                message_id=str(message_id),
                attempt=attempt,
                exc_type=type(exc).__name__,
            )

    if severity_result is None or call_result is None:
        reasoning_log.append(
            "I couldn't finish classifying this message right now — flagging it so a "
            "person can take a look."
        )
        log.error("classify_severity_failed_after_retry", message_id=str(message_id))
        return {"classification_failed": True, "reasoning_log": reasoning_log}

    # Tier-0 clamp: never de-escalate a hard prefilter fire.
    if prefilter_result.hard_hit and severity_result.severity != Severity.EMERGENCY:
        llm_severity = severity_result.severity
        clamp_note = f"Tier-0 keyword filter fired ({', '.join(prefilter_result.categories)})"
        severity_result = severity_result.model_copy(
            update={
                "severity": Severity.EMERGENCY,
                "rules_fired": [*severity_result.rules_fired, clamp_note],
            }
        )
        reasoning_log.append("The alarm phrasing already made this an emergency — I kept it there.")
        log.warning(
            "classify_severity_tier0_clamp",
            message_id=str(message_id),
            llm_severity=llm_severity.value,
            categories=prefilter_result.categories,
        )

    reasoning_log.append(f"I'm treating this as {_SEVERITY_DISPLAY[severity_result.severity]}.")
    for line in severity_result.reasoning:
        reasoning_log.append(line)
    if severity_result.modifier:
        reasoning_log.append(severity_result.modifier)

    cost_cents = anthropic_mod.estimate_cost_cents(
        tokens_in=call_result.tokens_in, tokens_out=call_result.tokens_out
    )

    if case_context.landlord_id is not None:
        await _insert_classified_audit(
            landlord_id=case_context.landlord_id,
            case_id=case_context.case_id,
            message_id=message_id,
            severity_result=severity_result,
            call_result=call_result,
            cost_cents=cost_cents,
        )
    else:  # pragma: no cover — invariant: landlord_id is always known by this point
        log.error("classify_severity_missing_landlord_id", message_id=str(message_id))

    return {
        "severity": severity_result,
        "classification_failed": False,
        "reasoning_log": reasoning_log,
    }


__all__: list[str] = ["classify_severity"]
