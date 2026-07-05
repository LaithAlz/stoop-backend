"""``draft_response`` node (#33) — drafts an SMS reply in the landlord's voice.

Runs after ``classify_severity`` (architecture.md §5 / apps/api/CLAUDE.md's
node layout order) for the urgent/routine branch — the emergency branch
(``emergency_protocol``, #40/#108) is architecture.md's OTHER branch off
``classify_severity``; nothing in this issue's acceptance criteria asks
this node to reproduce the emergency safety-instruction flow (see "Reported
gap: the EMERGENCY safety instruction" below), so it drafts a normal reply
regardless of which severity it's handed.

Voice profile + house rules
------------------------------
``app.agent.prompts.v1.build_draft_system_prompt`` (frozen) injects
``case_context.voice_profile`` (``{tone, samples}``) into the SYSTEM
prompt. House rules, the tenant's message, the classified severity/rules/
modifier, and any required refusal-deferral language are all DYNAMIC,
per-request content — they go in the USER message, never the frozen system
prompt (the same "system is frozen, the per-request user content is
dynamic by nature" distinction ``classify_severity.py`` relies on for its
own context injection).

Refusal-deferral templates: sourced from ``prompts/v1.py`` (present)
-------------------------------------------------------------------------
``app.agent.prompts.v1.REFUSAL_TEMPLATES`` / ``get_refusal_deferral`` are
frozen and already cover every ``RefusalFlag`` — this node does NOT define
its own refusal-deferral copy. It DOES define one small, node-local
fallback template (:data:`_GENERIC_SAFE_FALLBACK`) for the (rare) case
where a hard guard trips twice with NO refusal flag to fall back on at all
— see "Hard guards" below.

Hard guards — post-generation, code-enforced (never LLM-trusted alone)
---------------------------------------------------------------------------
Per this issue's own scope: dollar amounts/compensation promises
(including percentage/relative framing — "20% off", "half off your
rent"), access/door codes or PINs (including non-numeric disclosure — a
key/lockbox/spare LOCATED somewhere, e.g. "the lockbox is under the mat"),
and legal positions (LTB, eviction, entitlement claims, and indirect/
negative framings — "our lawyer says", "you have no right to", "you don't
have a case") are checked with plain substring/regex matching AFTER
generation — never left to the model's own restraint, and never solved by
prompt wording alone (the frozen system prompt already tells the model not
to do these things; this is the enforcement backstop, not a duplicate
ask). A fourth guard mirrors the DraftResult schema's own design: the
model self-reports which refusal deferrals it used
(``DraftResult.refusal_templates_used``) — if a refusal flag was set on
the classified severity but the model didn't report using its deferral,
that is ALSO a guard violation (the draft may be silently engaging the
refused topic instead of deferring it).

**Guard/deferral self-collision fix (HIGH, safety review 2026-07-05):**
guards run against the draft text with every ``REFUSAL_TEMPLATES`` string
stripped out FIRST (:func:`_strip_mandated_templates`) — they police
MODEL-ORIGINATED content only, never the node-mandated deferral text this
node itself instructs the model to include. Without the scrub,
``REFUSAL_TEMPLATES['cost_compensation']`` (contains "compensation") and
``REFUSAL_TEMPLATES['legal_rent_ltb']`` (contains "Landlord and Tenant
Board") tripped their OWN guards on the happy path: a correctly-deferred
cost/LTB message would fail, retry, fail again, and get silently swapped
for the generic fallback with ``draft_guard_failed=True`` — corrupting the
"needs a person's eyes" signal on exactly the messages where the deferral
worked. A draft that pairs the mandated deferral WITH a genuine violation
("I'll take $200 off your rent") still fails — the violation survives the
scrub untouched.

A violating draft is REJECTED and regenerated exactly ONCE, with the
violation named in the retry's USER-message suffix (never the system
prompt — frozen). A SECOND violation (of any kind, including a repeated
Anthropic API failure) falls back to a synthesized, always-safe draft:
the REFUSAL_TEMPLATES deferral text for every flag on the classification
(joined), or :data:`_GENERIC_SAFE_FALLBACK` when there were no refusal
flags at all. ``state["draft_guard_failed"] = True`` is set in that case —
a "needs a person's eyes on this one" signal for a future node/
notification to act on (the same seam pattern
``app/agent/nodes/classify_severity.py`` uses for ``classification_failed``).

**v1 pattern-coverage, not the authoritative gate:** these are
deterministic substring/regex checks, not semantic understanding of the
draft — a fast, always-on backstop so a violation is caught and retried/
replaced BEFORE a landlord ever sees the draft. ``#35``'s eval grader
(LLM-as-judge + its own substring assertions) is the AUTHORITATIVE check
for "did this draft actually violate a hard rule"; these guards are the
first line of defense that ships with this issue, not a replacement for
that grader.

Reported gap: the EMERGENCY safety instruction
--------------------------------------------------
``prompts/v1.py``'s frozen draft system prompt says: "For EMERGENCY
severity: prepend the mandatory safety instruction (also provided
separately) before any other content." No canned safety-instruction text
exists ANYWHERE in this codebase yet (no per-category template comparable
to ``REFUSAL_TEMPLATES``) — the actual safety SMS to the tenant is
``app/agent/emergency.py``'s #108 seam, which is still a no-op stub today.
This node does NOT fabricate that text; if it is ever invoked with
``severity=EMERGENCY`` it drafts the best reply it can from the same
dynamic context (severity/rules/modifier are still passed through), simply
without a safety-instruction block to prepend. Flagged in the issue report
for the spec owner — likely #108's deliverable, not #33's.

Reported gap: ``drafts.status`` vocabulary vs. the issue text
-----------------------------------------------------------------
Issue #33 says "Draft stored with status awaiting_approval" — but
``schema-v1.md``'s ``drafts.status`` CHECK is
``('pending','stale','approved','sending','sent','rejected','cancelled')``
— there is no ``awaiting_approval`` value for ``drafts``; that value
belongs to ``cases.status`` instead. This node follows the SCHEMA: new
drafts are inserted with ``status='pending'`` (the CASE separately moves to
``awaiting_approval`` — a case-status transition this issue does not own;
no code here touches ``cases.status``). Flagged as a discrepancy between
the issue text and schema-v1.md, per this task's instructions.

Stale-then-insert (conversation-model.md's stale-draft rule)
------------------------------------------------------------------
"a case has at most one pending draft" (the ``uq_drafts_one_pending``
partial unique index). If a pending draft already exists for this case
when a new draft is produced, it is marked ``stale`` FIRST (audit_log
``draft_stale``, existing vocabulary), then the new ``pending`` row is
inserted — both within the same DB transaction, so the two ``pending``
rows never coexist even momentarily. This is conversation-model.md's own
documented rule ("new inbound → old draft stale → re-run"), implemented
minimally here (no notification, no diffing of what changed — that is
#44/#50 territory per the same doc).

20 s END-TO-END budget / retry
----------------------------------
Same shared-deadline arithmetic as ``classify_intent.py`` /
``classify_severity.py`` (``app/integrations/anthropic.py``'s
``new_deadline`` / ``attempt_timeout``): ONE 20-second deadline for the
initial attempt and its single regeneration TOGETHER, not 20 seconds each.
The regeneration attempt here is triggered by EITHER a transport failure
OR a hard-guard violation (see "Hard guards" above) — either way it draws
from the same shared deadline; a regeneration that would fall below the
2-second floor is skipped entirely and treated exactly like a second
failure (the safe fallback draft is used, ``draft_guard_failed=True``).

DB access
---------
Admin engine, same pattern as the other #30/#110/#32 nodes: one session to
read the message body + tenant name, the Anthropic call(s) made OUTSIDE
any open session (mirrors ``classify_severity.py``/``load_context.py``'s
"never hold a pooled connection across a slow external call"), then a
second session for the stale-then-insert drafts/audit writes. Allowlisted
in ``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.

Never-break rule #5: only uuids, guard names, and boolean flags ever reach
``log.*`` calls in this module — never the tenant's message body or the
drafted reply text.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import text

from app.agent.prompts.v1 import (
    PROMPT_VERSION,
    REFUSAL_TEMPLATES,
    build_draft_system_prompt,
    get_refusal_deferral,
)
from app.agent.schemas import CaseContext, DraftResult, RefusalFlag, SeverityResult
from app.agent.state import AgentState
from app.agent.tools import DRAFT_MESSAGE_TOOL
from app.db.session import get_admin_session
from app.integrations import anthropic as anthropic_mod

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Node-local fallback template — see module docstring "Refusal-deferral
# templates". Rule-#8-clean copy: short, plain, no jargon.
# ---------------------------------------------------------------------------

_GENERIC_SAFE_FALLBACK: str = (
    "Thanks for letting me know — I've passed this along to your landlord, who will "
    "follow up with you directly."
)

# ---------------------------------------------------------------------------
# Dynamic (non-frozen) plain-language reminder — injected into the USER
# message every time, per docs/02-product/plain-language-rules.md. Not a
# prompt-file edit: this is per-request content, exactly like the tenant
# message itself.
# ---------------------------------------------------------------------------

_PLAIN_LANGUAGE_REMINDER: str = (
    "Writing rules for this reply (docs/02-product/plain-language-rules.md):\n"
    "- Grade-5 reading level: short words, short sentences, active voice.\n"
    '- No jargon or idioms (no "touch base", no "ASAP").\n'
    '- Concrete over relative: a specific day and time window, never "soon" or "later '
    'this week".\n'
    "- Ask at most ONE question, if any.\n"
    "- Routine replies: 2 SMS segments or fewer (about 300 characters).\n"
    "- Calm, warm, certain tone — never scolding, never panicked."
)

# ---------------------------------------------------------------------------
# Hard guards — post-generation regex checks (see module docstring).
# Deliberately conservative: false positives here cost one extra
# regeneration; false negatives are the failure mode that matters.
# ---------------------------------------------------------------------------

_DOLLAR_COMPENSATION_RE = re.compile(
    r"\$\s?\d"
    r"|\bcompensat(?:e|ion|ing)\b"
    r"|\breimburs(?:e|ement|ing)\b"
    r"|\brefund(?:s|ed|ing)?\b"
    r"|\brent\s+(?:reduction|abatement)\b"
    r"|\bwaive(?:d|s|r)?\b"
    r"|\bdiscount(?:s|ed|ing)?\b"
    # Percentage/relative compensation — "20% off", "half off your rent".
    r"|\b\d{1,3}\s?%\s*off\b"
    r"|\bhalf\s+off\b",
    re.IGNORECASE,
)

_ACCESS_CODE_RE = re.compile(
    r"\b(?:access|door|gate|lock\s*box|entry|building|keypad|garage)\s*codes?\b\D{0,20}\d{2,}"
    r"|\bcodes?\s*(?:is|:|are)\s*\d{2,}"
    r"|\bpin\s*(?:is|:)\s*\d{2,}"
    # Non-numeric access disclosure — a key/lockbox/spare LOCATED somewhere,
    # either word order ("the lockbox is under the mat" / "hidden behind
    # the key... spare"). No digits required — the location itself is the
    # leak.
    r"|\b(?:key|lockbox|lock\s*box|spare\s*key|spare)\b.{0,30}\b(?:under|behind|hidden|"
    r"beneath|inside|on\s*top\s*of)\b"
    r"|\b(?:under|behind|hidden|beneath|inside|on\s*top\s*of)\b.{0,30}\b(?:the\s+)?"
    r"(?:key|lockbox|lock\s*box|spare\s*key|spare)\b",
    re.IGNORECASE,
)

_LEGAL_POSITION_RE = re.compile(
    r"\bltb\b"
    r"|\blandlord\s+and\s+tenant\s+board\b"
    r"|\beviction\b"
    r"|\byou(?:'re| are)\s+entitled\s+to\b"
    r"|\blegally\s+(?:required|obligated|entitled)\b"
    r"|\b(?:violat(?:es|ion)|against)\s+the\s+law\b"
    # Indirect/negative legal positions — still a substantive legal opinion,
    # just phrased as a third-party claim or a denial of rights.
    r"|\bour\s+lawyer\s+says\b"
    r"|\byou\s+have\s+no\s+right\s+to\b"
    r"|\byou\s+don'?t\s+have\s+a\s+case\b",
    re.IGNORECASE,
)

_SELECT_MESSAGE_SQL = text("SELECT body FROM messages WHERE id = :message_id")
_SELECT_TENANT_NAME_SQL = text("SELECT name FROM tenants WHERE id = :tenant_id")

_SELECT_PENDING_DRAFT_SQL = text(
    "SELECT id FROM drafts WHERE case_id = :case_id AND status = 'pending'"
)
_UPDATE_DRAFT_STALE_SQL = text(
    "UPDATE drafts SET status = 'stale', updated_at = now() WHERE id = :draft_id"
)
_INSERT_DRAFT_SQL = text(
    "INSERT INTO drafts (landlord_id, case_id, recipient, body, prompt_version, status) "
    "VALUES (:landlord_id, :case_id, 'tenant', :body, :prompt_version, 'pending') "
    "RETURNING id"
)
_INSERT_DRAFT_STALE_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'agent', 'draft_stale', CAST(:payload AS jsonb))"
)
_INSERT_DRAFTED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'agent', 'drafted', CAST(:payload AS jsonb))"
)


def _strip_mandated_templates(body: str) -> str:
    """Remove every exact ``REFUSAL_TEMPLATES`` string from *body* before the
    hard guards ever see it.

    Safety-review finding (guard/deferral self-collision, HIGH): the guards
    police MODEL-ORIGINATED content only, never the node-mandated deferral
    text this SAME node instructed the model to include verbatim (see
    ``_build_user_content``'s "Required deferral language" block). Without
    this scrub, ``REFUSAL_TEMPLATES['cost_compensation']`` (contains
    "compensation") and ``REFUSAL_TEMPLATES['legal_rent_ltb']`` (contains
    "Landlord and Tenant Board") trip their OWN guards on the happy path —
    every correctly-deferred cost/LTB message would be rejected, retried,
    rejected again, and silently swapped for the generic fallback, with
    ``draft_guard_failed=True`` — corrupting the "needs a person's eyes"
    signal on the exact messages where the deferral worked as intended.
    Stripping the mandated text FIRST means only text the MODEL added on
    top of (or instead of) the mandated deferral can ever trip a guard.
    """
    scrubbed = body
    for template_text in REFUSAL_TEMPLATES.values():
        scrubbed = scrubbed.replace(template_text, " ")
    return scrubbed


def _check_hard_guards(
    *,
    body: str,
    refusal_flags: list[RefusalFlag],
    refusal_templates_used: list[RefusalFlag],
) -> list[str]:
    """Return the list of violated guard names — empty means clean.

    Guards run against *body* with every mandated ``REFUSAL_TEMPLATES``
    string stripped out first (see :func:`_strip_mandated_templates`) — a
    draft that is JUST the mandated deferral (verbatim or paraphrased
    around it) never trips a guard on the template text itself; a draft
    that pairs the deferral with a genuine violation ("I'll take $200 off
    your rent, but also, per the LTB...") still does, because the genuine
    violation survives the scrub untouched.

    v1 pattern-coverage note: these are plain substring/regex checks, not a
    semantic understanding of the draft — they are a fast, deterministic
    backstop, not the authoritative gate. ``#35``'s eval grader (LLM-as-
    judge + its own substring assertions) is the authoritative check for
    "did this draft actually violate a hard rule"; these guards exist so a
    violation is caught and retried/replaced BEFORE a landlord ever sees
    the draft, not after an eval run days later.
    """
    scrubbed_body = _strip_mandated_templates(body)
    violations: list[str] = []
    if _DOLLAR_COMPENSATION_RE.search(scrubbed_body):
        violations.append("dollar_compensation")
    if _ACCESS_CODE_RE.search(scrubbed_body):
        violations.append("access_code")
    if _LEGAL_POSITION_RE.search(scrubbed_body):
        violations.append("legal_position")
    missing = [flag for flag in refusal_flags if flag not in refusal_templates_used]
    if missing:
        violations.append("missing_refusal_deferral:" + ",".join(f.value for f in missing))
    return violations


def _violation_retry_note(violations: list[str]) -> str:
    return (
        "\n\nIMPORTANT: your previous draft violated the following hard rule(s): "
        f"{', '.join(violations)}. Do not include dollar amounts, compensation promises, "
        "reimbursement/refund/discount language, access codes or PINs, or any legal "
        "position (LTB, eviction, entitlement claims). If a refusal topic applies, you "
        "MUST include its deferral language (provided above) and list that flag in "
        "refusal_templates_used. Revise and resend the FULL reply."
    )


def _fallback_draft(refusal_flags: list[RefusalFlag]) -> DraftResult:
    """The always-safe draft used after a second guard violation — see
    module docstring "Hard guards"."""
    if not refusal_flags:
        return DraftResult(body=_GENERIC_SAFE_FALLBACK, refusal_templates_used=[])
    deferrals = [get_refusal_deferral(flag.value) for flag in refusal_flags]
    return DraftResult(body=" ".join(deferrals), refusal_templates_used=list(refusal_flags))


def _build_user_content(
    *,
    body: str,
    tenant_name: str | None,
    house_rules: str | None,
    severity_result: SeverityResult,
    refusal_deferrals: list[tuple[RefusalFlag, str]],
) -> str:
    lines: list[str] = [f"Tenant's message:\n{body}", ""]
    if tenant_name:
        lines.append(f"Tenant's first name: {tenant_name}")
    lines.append(f"Classified severity: {severity_result.severity.value}")
    if severity_result.rules_fired:
        lines.append(f"Rules that fired: {'; '.join(severity_result.rules_fired)}")
    if severity_result.modifier:
        lines.append(f"Modifier applied: {severity_result.modifier}")
    if house_rules:
        lines.append(f"\nProperty house rules (use only what's relevant):\n{house_rules}")
    if refusal_deferrals:
        lines.append(
            "\nRequired deferral language for flagged topics — include this naturally in "
            "the reply, and list the matching flag(s) in refusal_templates_used:"
        )
        for flag, deferral_text in refusal_deferrals:
            lines.append(f"- [{flag.value}] {deferral_text}")
    lines.append(f"\n{_PLAIN_LANGUAGE_REMINDER}")
    return "\n".join(lines)


async def draft_response(state: AgentState) -> dict[str, Any]:
    """Draft an SMS reply in the landlord's voice. Returns a partial state
    update. Always inserts a draft when a case is known — the fallback path
    (see module docstring) guarantees the queue never goes empty because a
    guard tripped twice."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    severity_result = state.get("severity")

    if case_context.case_id is None:
        reasoning_log.append(
            "There's no conversation to draft a reply on yet, so I skipped drafting."
        )
        log.error("draft_response_missing_case_id", message_id=str(message_id))
        return {"reasoning_log": reasoning_log}

    if severity_result is None:
        reasoning_log.append(
            "I can't draft a reply yet — severity hasn't been figured out for this message."
        )
        log.error(
            "draft_response_missing_severity",
            message_id=str(message_id),
            case_id=str(case_context.case_id),
        )
        return {"reasoning_log": reasoning_log}

    async with asynccontextmanager(get_admin_session)() as session:
        body: str = (
            (await session.execute(_SELECT_MESSAGE_SQL, {"message_id": str(message_id)}))
            .mappings()
            .one()
        )["body"]

        tenant_name: str | None = None
        if case_context.tenant_id is not None:
            tenant_row = (
                (
                    await session.execute(
                        _SELECT_TENANT_NAME_SQL, {"tenant_id": str(case_context.tenant_id)}
                    )
                )
                .mappings()
                .one_or_none()
            )
            tenant_name = tenant_row["name"] if tenant_row is not None else None

    system_prompt = build_draft_system_prompt(case_context.voice_profile)
    refusal_deferrals = [
        (flag, get_refusal_deferral(flag.value)) for flag in severity_result.refusal_flags
    ]
    base_user_content = _build_user_content(
        body=body,
        tenant_name=tenant_name,
        house_rules=case_context.house_rules,
        severity_result=severity_result,
        refusal_deferrals=refusal_deferrals,
    )

    draft_result: DraftResult | None = None
    violations: list[str] = []
    call_errors: list[str] = []
    user_content = base_user_content
    deadline = anthropic_mod.new_deadline()

    for attempt in range(2):
        timeout = anthropic_mod.attempt_timeout(deadline, is_retry=attempt == 1)
        if timeout is None:
            log.error("draft_response_retry_skipped_budget_exhausted", message_id=str(message_id))
            break
        try:
            call_result = await anthropic_mod.call_tool_forced(
                system=system_prompt,
                user_content=user_content,
                tool=DRAFT_MESSAGE_TOOL,
                tool_name="draft_message",
                timeout_seconds=timeout,
            )
            candidate = DraftResult.model_validate(call_result.tool_input)
        except (anthropic_mod.AnthropicCallError, ValidationError) as exc:
            call_errors.append(type(exc).__name__)
            log.error(
                "draft_response_attempt_failed",
                message_id=str(message_id),
                attempt=attempt,
                exc_type=type(exc).__name__,
            )
            continue

        violations = _check_hard_guards(
            body=candidate.body,
            refusal_flags=severity_result.refusal_flags,
            refusal_templates_used=candidate.refusal_templates_used,
        )
        if not violations:
            draft_result = candidate
            break

        log.warning(
            "draft_response_guard_violation",
            message_id=str(message_id),
            attempt=attempt,
            violations=violations,
        )
        user_content = base_user_content + _violation_retry_note(violations)

    guard_failed = False
    if draft_result is None:
        guard_failed = True
        draft_result = _fallback_draft(severity_result.refusal_flags)
        reasoning_log.append(
            "I wasn't confident this draft was safe to send as-is, so I used a safer "
            "standard reply instead — worth a look before it goes out."
        )
        log.error(
            "draft_response_guard_failed_after_retry",
            message_id=str(message_id),
            case_id=str(case_context.case_id),
            violations=violations,
            call_errors=call_errors,
        )
    else:
        reasoning_log.append("I've drafted a reply for you to review.")

    async with asynccontextmanager(get_admin_session)() as session:
        pending_row = (
            (
                await session.execute(
                    _SELECT_PENDING_DRAFT_SQL, {"case_id": str(case_context.case_id)}
                )
            )
            .mappings()
            .one_or_none()
        )

        if pending_row is not None:
            stale_draft_id: UUID = pending_row["id"]
            await session.execute(_UPDATE_DRAFT_STALE_SQL, {"draft_id": str(stale_draft_id)})
            await session.execute(
                _INSERT_DRAFT_STALE_AUDIT_SQL,
                {
                    "landlord_id": str(case_context.landlord_id),
                    "case_id": str(case_context.case_id),
                    "payload": json.dumps({"draft_id": str(stale_draft_id)}),
                },
            )
            reasoning_log.append(
                "A new message came in, so I marked the earlier draft as out of date and "
                "wrote a fresh one."
            )

        new_draft_row = (
            (
                await session.execute(
                    _INSERT_DRAFT_SQL,
                    {
                        "landlord_id": str(case_context.landlord_id),
                        "case_id": str(case_context.case_id),
                        "body": draft_result.body,
                        "prompt_version": PROMPT_VERSION,
                    },
                )
            )
            .mappings()
            .one()
        )
        new_draft_id = new_draft_row["id"]

        await session.execute(
            _INSERT_DRAFTED_AUDIT_SQL,
            {
                "landlord_id": str(case_context.landlord_id),
                "case_id": str(case_context.case_id),
                "payload": json.dumps(
                    {
                        "draft_id": str(new_draft_id),
                        "refusal_templates_used": [
                            flag.value for flag in draft_result.refusal_templates_used
                        ],
                        "guard_failed": guard_failed,
                    }
                ),
            },
        )

    return {
        "draft": draft_result,
        "draft_guard_failed": guard_failed,
        "reasoning_log": reasoning_log,
    }


__all__: list[str] = ["draft_response"]
