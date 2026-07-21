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
issue that owns it. (#109 has since shipped —
``app/agent/nodes/degraded_mode.py`` is that protocol, and #34's graph
does route ``classification_failed`` there today; this module STILL never
writes its own audit row on failure, unchanged — see "Cost accounting on
the failure path (#208)" immediately below for the one thing that DOES
change on this failure path now.)

Cost accounting on the failure path (#208)
---------------------------------------------
Found by #111's spec review: an attempt that reaches the API and consumes
billed tokens but then fails (this node's own ``SeverityResult`` validation
rejects the model's output, or the SDK's forced-``tool_choice`` response
carries no usable ``tool_use`` block — see
``app/integrations/anthropic.py``'s ``AnthropicCallError`` docstring
"Reached-the-API usage, when it exists") was previously invisible to
``app/cost_reporting.py``'s rollups — real spend, understated. Investigated
end-to-end before picking a design: this node itself deliberately writes NO
audit row on ANY failure (see above, unchanged) — but ``app.agent.graph``
ALWAYS routes ``classification_failed=True`` to
``app.agent.nodes.degraded_mode``, which (for a genuinely NEW activation,
i.e. not a redelivered/retried no-op) reliably writes exactly one
``'degraded_mode'`` audit row for this same failure, in the SAME graph
invocation. That existing, reliable row — never a new one, never a new
``audit_log.action`` CHECK value, never a migration — is the minimal
honest place to record this cost: this node sums whatever reached-the-API
usage its failed attempt(s) produced into
``state["classification_failed_usage"]`` (absent when NEITHER attempt ever
reached the API — a pure connection/timeout failure has no billed cost to
report, never a fabricated one), and ``degraded_mode.py`` folds those keys
into the SAME payload it already writes (schema-v1.md v1.14 amendment;
``app/cost_reporting.py``'s CTE gained one new branch for
``action = 'degraded_mode'``). See ``app/agent/state.py``'s own
``classification_failed_usage`` docstring for the exact shape.

**Scope, honestly stated:** this only covers the DOUBLE-failure case
(``classification_failed=True``). A first attempt that reaches the API and
fails, followed by a SECOND attempt that succeeds, is NOT covered — the
success path's ``'classified'`` audit row (below) is byte-identical to
before this issue and still records only the WINNING attempt's usage; the
first, failed attempt's billed-but-discarded tokens in that scenario
remain unrecorded. Fixing that would mean blending "cost of a rejected
attempt" into a row that represents "this is what got classified," which
is a bigger, separate design question than this issue's "make total
failure visible" scope — flagged here, not resolved unilaterally.

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
lists it), ``payload = {message_id, case_id, severity, summary,
rules_fired, modifier, refusal_flags, model, tokens_in, tokens_out,
cost_cents, prompt_version}``. This matches
``docs/03-engineering/api-contracts.md``'s own timeline example (``GET
/v1/cases/{id}``: ``{"kind": "audit", "actor": "agent", "action":
"classified", "payload": {"severity": "urgent", "rules_fired": ["…"]}}``),
extended with the token/cost/model fields this issue also needs recorded
somewhere durable. NO message body ever enters this payload — only the
structured fields above.

**schema-v1 v1.7 amendment — the one carved-out exception to "never
duplicated into the audit trail":** every reasoning_log line is otherwise
transient graph state, intentionally never persisted verbatim into
``audit_log``. This revises that ruling for exactly ONE derived value: the
payload's new ``summary`` key (spec-guardian review, #56 PR: the margin
note must carry the model's SUBSTANTIVE, case-specific reasoning — "No
heat on a cold night with a baby in the unit can't wait, so I treated it
as urgent.", schema-v1.md's own illustrative example — not a content-free
restatement of the severity chip the dashboard already renders
separately). Concretely: ``summary`` is ``" ".join(severity_result.
reasoning)`` — the model's own per-issue explanation(s), already produced
in its tool output and already appended to ``reasoning_log`` verbatim, no
prompt/rubric change — UNLESS either:

1. the model returned no ``reasoning`` at all (nothing case-specific to
   persist), or
2. a Tier-0 clamp just fired on this call (see "The Tier-0 clamp" above)
   — the model's own reasoning still reflects its PRE-clamp, non-emergency
   judgment call, and persisting it verbatim as ``summary`` would read as
   de-escalating relative to the clamped EMERGENCY severity recorded on
   this same row. The never-break invariant ("the agent may escalate past
   a Tier-0 miss, never de-escalate a Tier-0 fire") extends to the TONE of
   this landlord-facing sentence, not just the enum value.

In either case ``summary`` falls back to the same deterministic sentence
this node always appends to ``reasoning_log`` regardless (``f"I'm
treating this as {…}."``). Rationale for the key existing at all
(schema-v1.md v1.7): ``reasoning_log`` lives only in transient graph state
and opaque checkpoint blobs, so nothing durable/queryable served the
approval card's margin note (``why`` in ``GET /v1/queue``, #56) before
this — the ``audit_log`` row is already the canonical classification
record (v1.6 above), so this belongs on it too. Rows written before this
change lack the key; readers treat a missing ``summary`` as ``null``
(``GET /v1/queue`` then returns ``why: null``). No OTHER reasoning_log
line (the Tier-0 clamp note, the modifier note) is duplicated onto the
audit row — only this one derived ``summary`` value.

**Doc-first, applied:** the five ``messages`` columns above are marked
DEPRECATED in ``schema-v1.md`` (v1.6 amendments — never written; canonical
record is ``audit_log`` 'classified'; DROP in a future migration), both in
the amendments prose and as inline column comments — mirroring the
``twilio_status`` deprecation wording exactly. Done in this same PR, not
deferred.

Cost accounting: ``Severity.db_value`` is used for the audit payload's
``severity`` field (lowercase, matching ``cases.severity``'s CHECK) per
schema-v1.md rule #6 — never ``.value`` directly.

``cases.severity`` — now written, post-clamp only (#197)
----------------------------------------------------------
schema-v1.md's implementer notes (v1.6 amendments era) long flagged that
``cases.severity`` was a real, documented column no code path ever wrote —
``GET /v1/queue``/``GET /v1/cases`` worked around it by sourcing severity
from the latest ``classified`` audit row instead (and keep doing so; that
read-side sourcing is UNCHANGED by this issue — see ``routers/queue.py``'s
own module docstring). #197 closes the write side: immediately after the
``audit_log`` INSERT above, in the SAME admin-session transaction (one
commit, or neither write survives a crash between them), this node also
``UPDATE``s ``cases.severity`` to the exact same post-clamp
``severity_result.severity.db_value`` the audit row just recorded — never
a second, independently-derived value; the two can never disagree because
they're the same read of the same in-memory result. Skipped entirely when
``case_context.case_id`` is ``None`` (the unknown-sender fallback thread —
there is no case row to update). ``cases`` is NOT append-only (schema-v1.md
never-break rule #2 only covers ``messages``/``audit_log``) — an ``UPDATE``
here is legitimate, ordinary application state, not a violation of that
rule.

**Never downgrade a case away from ``'emergency'``** — the CASE-LEVEL
mirror of the Tier-0 clamp above. A case already sitting at
``severity='emergency'`` (set by an earlier message on the same case, Tier
-0-clamped or not) must never be overwritten by a LATER message's own
classification, even a wholly legitimate ``ROUTINE``/``URGENT`` result for
that later message — e.g. a tenant's own "did you get my message about the
gas smell?" follow-up, classified on its own textual merits as routine,
must never erase the case's standing EMERGENCY. This is the same
never-break invariant the Tier-0 clamp already enforces within one
classification call ("the agent may escalate past a Tier-0 miss, it may
never de-escalate a Tier-0 fire"), extended across calls: RECLASSIFICATION
of the same case may only ever escalate or hold, never downgrade FROM
emergency. Enforced at the SQL level, not by reading-then-branching in
Python (a read-then-write in application code would race against a
concurrent classification of the same case; the UPDATE's own ``WHERE``
clause makes the guard atomic and race-free):
``UPDATE cases SET severity = :severity, updated_at = now() WHERE id =
:case_id AND severity IS DISTINCT FROM 'emergency'`` — when the current row
already reads ``'emergency'``, this ``WHERE`` clause excludes it and the
``UPDATE`` matches zero rows (a silent, correct no-op); every other current
value (``NULL``, ``'urgent'``, ``'routine'``) is eligible and gets the new
post-clamp value unconditionally. Note this guard is intentionally
narrower than a full monotonic (never-decrease) clamp: an existing
``'urgent'`` case CAN be overwritten by a later ``'routine'``
classification — only ``'emergency'`` is sticky, mirroring the Tier-0
clamp's own scope (it only ever protects the EMERGENCY level, never
URGENT-vs-ROUTINE ordering).

Scope of the "persistence-only" claim (safety-review LOW, #197): what is
byte-identical is THIS node's LLM interaction — the severity prompt
(``_build_user_content`` never includes ``open_cases``), the rubric, the
tool call, and the returned state. The write itself does feed one other
prompt indirectly: ``load_context`` loads ``cases.severity`` into
``open_cases`` and ``classify_intent`` renders it (``severity: {...}``),
so on a returning tenant's second-or-later message the intent prompt now
shows a real value where it previously always said ``unclassified``.
Harmless today — nothing consumes ``state["intent"]`` yet, and the value
can only add context, never reach or de-escalate severity — but this is a
TRIPWIRE: if ``classify_intent``'s output ever gains a consumer, that
change must re-examine this feedback loop (and the eval gate applies to
that work on its own terms).

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

from app.agent.prompts.v2 import PROMPT_VERSION, get_classify_system_prompt
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

_UPDATE_CASE_SEVERITY_SQL = text(
    "UPDATE cases SET severity = :severity, updated_at = now() "
    "WHERE id = :case_id AND severity IS DISTINCT FROM 'emergency'"
)


def _parse_prefilter(raw: Any) -> PrefilterResult:
    """Same fallback as ``identify_case.py``'s ``_parse_prefilter``: the
    webhook always writes a snapshot, so a missing one is an anomaly to log,
    not a reason to block classification. Duplicated rather than imported —
    project convention (see ``tests/test_agent_nodes.py``'s own docstring).

    Never raises (safety review LOW): a non-null but MALFORMED snapshot
    used to raise ``ValidationError`` outside any try/except here and
    crash the whole graph run. Falls back to ``hard_hit=False``, exactly
    like the "missing" case — this can never de-escalate a real Tier-0
    fire, since the snapshot the webhook actually persisted on ``messages
    .prefilter`` is never touched by this fallback; it only affects
    whether THIS node's Tier-0 clamp (below) has a signal to act on when
    its OWN read of that snapshot fails. Keep it that way: this function
    must never be the thing that decides a fire didn't happen."""
    if raw is None:
        log.error("classify_severity_prefilter_snapshot_missing")
        return PrefilterResult(hard_hit=False)
    try:
        return PrefilterResult.model_validate(raw)
    except (ValidationError, TypeError) as exc:
        log.warning("classify_severity_prefilter_snapshot_malformed", exc_type=type(exc).__name__)
        return PrefilterResult(hard_hit=False)


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
) -> anthropic_mod.ToolCallResult:
    """Make ONE Anthropic call and return its raw, parsed result — does NOT
    validate ``tool_input`` into :class:`SeverityResult` (#208 fix: an
    earlier revision bundled ``SeverityResult.model_validate`` into this
    same function, which meant a call that reached the API and consumed
    billed tokens but then failed OUR OWN schema validation raised before
    ever returning ``call_result`` to the caller — losing that reached-the
    -API usage entirely. Splitting the two steps lets the caller's retry
    loop capture usage from a successful call regardless of what happens to
    it next — see that loop's own comments and this module's docstring
    "Cost accounting on the failure path (#208)")."""
    return await anthropic_mod.call_tool_forced(
        system=get_classify_system_prompt(),
        user_content=user_content,
        tool=CLASSIFY_SEVERITY_TOOL,
        tool_name="classify_severity",
        timeout_seconds=budget_seconds,
    )


async def _insert_classified_audit(
    *,
    landlord_id: UUID,
    case_id: UUID | None,
    message_id: UUID,
    severity_result: SeverityResult,
    call_result: anthropic_mod.ToolCallResult,
    cost_cents: float,
    summary: str,
) -> None:
    payload = {
        "message_id": str(message_id),
        "case_id": str(case_id) if case_id is not None else None,
        "severity": severity_result.severity.db_value,
        "summary": summary,
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
        # #197: write the post-clamp severity onto cases.severity, in the
        # SAME transaction as the audit INSERT above (one commit on this
        # session's clean exit -- see get_admin_session's own "commits on
        # clean exit" contract). Skipped when there is no case to update
        # (the unknown-sender fallback thread). Never-downgrade-from-
        # -emergency is enforced by the UPDATE's own WHERE clause -- see
        # this module's docstring "cases.severity -- now written,
        # post-clamp only (#197)".
        if case_id is not None:
            await session.execute(
                _UPDATE_CASE_SEVERITY_SQL,
                {"case_id": str(case_id), "severity": severity_result.severity.db_value},
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
    # #208: sum of every FAILED attempt's reached-the-API usage (never the
    # eventual SUCCESS row's usage, which is recorded separately below,
    # unchanged) -- see this module's docstring "Cost accounting on the
    # failure path (#208)".
    failed_tokens_in = 0
    failed_tokens_out = 0
    failed_model: str | None = None
    for attempt in range(2):
        timeout = anthropic_mod.attempt_timeout(deadline, is_retry=attempt == 1)
        if timeout is None:
            log.error(
                "classify_severity_retry_skipped_budget_exhausted", message_id=str(message_id)
            )
            break
        try:
            call_result = await _classify_severity_once(
                user_content=user_content, budget_seconds=timeout
            )
        except anthropic_mod.AnthropicCallError as exc:
            log.error(
                "classify_severity_attempt_failed",
                message_id=str(message_id),
                attempt=attempt,
                exc_type=type(exc).__name__,
            )
            call_result = None
            # Only the "no tool_use block" sub-case carries real usage
            # (a full response was received) -- see AnthropicCallError's
            # own docstring. Timeouts/API errors leave these None.
            if exc.tokens_in is not None:
                failed_tokens_in += exc.tokens_in
                failed_tokens_out += exc.tokens_out or 0
                failed_model = exc.model
            continue

        try:
            severity_result = SeverityResult.model_validate(call_result.tool_input)
            break
        except ValidationError as exc:
            log.error(
                "classify_severity_attempt_failed",
                message_id=str(message_id),
                attempt=attempt,
                exc_type=type(exc).__name__,
            )
            # The call itself succeeded (real, billed usage in call_result)
            # even though OUR OWN schema rejected the model's output --
            # this is the OTHER reached-the-API failure mode #208 targets.
            failed_tokens_in += call_result.tokens_in
            failed_tokens_out += call_result.tokens_out
            failed_model = call_result.model
            call_result = None

    if severity_result is None or call_result is None:
        reasoning_log.append(
            "I couldn't finish classifying this message right now — flagging it so a "
            "person can take a look."
        )
        log.error("classify_severity_failed_after_retry", message_id=str(message_id))
        result: dict[str, Any] = {"classification_failed": True, "reasoning_log": reasoning_log}
        if failed_tokens_in or failed_tokens_out:
            result["classification_failed_usage"] = {
                "model": failed_model,
                "tokens_in": failed_tokens_in,
                "tokens_out": failed_tokens_out,
                "cost_cents": anthropic_mod.estimate_cost_cents(
                    tokens_in=failed_tokens_in, tokens_out=failed_tokens_out
                ),
            }
        return result

    # Tier-0 clamp: never de-escalate a hard prefilter fire.
    clamped = False
    if prefilter_result.hard_hit and severity_result.severity != Severity.EMERGENCY:
        llm_severity = severity_result.severity
        clamp_note = f"Tier-0 keyword filter fired ({', '.join(prefilter_result.categories)})"
        severity_result = severity_result.model_copy(
            update={
                "severity": Severity.EMERGENCY,
                "rules_fired": [*severity_result.rules_fired, clamp_note],
            }
        )
        clamped = True
        reasoning_log.append("The alarm phrasing already made this an emergency — I kept it there.")
        log.warning(
            "classify_severity_tier0_clamp",
            message_id=str(message_id),
            llm_severity=llm_severity.value,
            categories=prefilter_result.categories,
        )

    deterministic_summary = f"I'm treating this as {_SEVERITY_DISPLAY[severity_result.severity]}."
    reasoning_log.append(deterministic_summary)
    for line in severity_result.reasoning:
        reasoning_log.append(line)
    if severity_result.modifier:
        reasoning_log.append(severity_result.modifier)

    # audit `summary` (schema-v1 v1.7): prefer the model's own case-specific
    # reasoning sentence(s) -- substantive content for GET /v1/queue's `why`
    # margin note, not just a restatement of the severity chip. Falls back
    # to the deterministic line when the model returned no reasoning at
    # all, OR when a Tier-0 clamp just fired -- the LLM's reasoning still
    # reflects its OWN (pre-clamp, non-emergency) judgment call in that
    # case, and persisting it as `summary` would read as de-escalating
    # relative to the clamped EMERGENCY severity actually recorded on this
    # same row (never-break rule: the agent may escalate past a Tier-0
    # miss, never de-escalate a Tier-0 fire -- that invariant extends to
    # the tone of this landlord-facing sentence, not just the enum value).
    if clamped or not severity_result.reasoning:
        summary_for_audit = deterministic_summary
    else:
        summary_for_audit = " ".join(severity_result.reasoning)

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
            summary=summary_for_audit,
        )
    else:  # pragma: no cover — invariant: landlord_id is always known by this point
        log.error("classify_severity_missing_landlord_id", message_id=str(message_id))

    return {
        "severity": severity_result,
        "classification_failed": False,
        "reasoning_log": reasoning_log,
    }


__all__: list[str] = ["classify_severity"]
