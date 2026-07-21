"""``degraded_mode`` node (#34 G1 seam, #109 fills in the classification
-failed leg) — the durable landlord-notification (and, for
``classification_failed``, tenant-holding-ack + retry) seam for every "a
person needs to look at this one" condition the classify/draft pipeline
can hit.

Routed to by ``app/agent/graph.py``'s conditional edges, NEVER called
directly by any other node, for any of THREE independent triggers (see
that module's own docstring "The degraded-mode routing" for the full
routing rationale):

- after ``classify_severity`` when ``state["classification_failed"]`` is
  ``True`` (the Anthropic call failed twice). ``draft_response`` is
  SKIPPED entirely in that case (there is no severity to draft against).
  **This is the leg #109 (this issue) fills in** — see "The
  classification_failed leg (#109)" below.
- after ``draft_response``, when ``state["severity"].severity`` is
  ``Severity.EMERGENCY`` — an LLM-classified emergency Tier-0 itself
  missed. INTERIM behavior until #108's real escalation chain exists —
  UNCHANGED by this issue.
- after ``draft_response`` when ``state["draft_guard_failed"]`` is
  ``True`` — UNCHANGED by this issue.

The EMERGENCY trigger and ``draft_guard_failed`` CAN co-occur; both are
handled by :func:`_handle_generic_degraded`, unchanged from #34.
``classification_failed`` is mutually exclusive with the other two today
(``draft_response``, and therefore ``severity``, never runs when
classification already failed) — :func:`_resolve_reasons` still returns
every applicable reason defensively, but in practice
``reasons == [REASON_CLASSIFICATION_FAILED]`` is the only way this module
ever takes the #109 branch below.

The classification_failed leg (#109)
-------------------------------------
``docs/02-product/emergency-prefilter.md``'s degraded-mode table, using the
prefilter's DURABLE ``messages.prefilter`` snapshot (never re-run — same
"never re-run Tier-0" invariant every other node in this package honors):

- **Tier-0 HARD hit already fired** — "already handled": the webhook's
  ``fire_emergency_protocol`` seam already recorded the emergency and
  (once #108 ships) already called/texted. Nothing NEW happens here: no
  tenant ack (the tenant already gets the real category-templated safety
  SMS via that path — a second, generic "I'm on it" text would be
  confusing on top of actual safety instructions), no additional
  ``needs_eyes`` (the landlord is already being called, which is more
  urgent than a push notification). Still writes an ``audit_log``
  ``degraded_mode`` row EVERY time this leg is reached (deliberately NOT
  idempotency-gated, since there is no notification artifact to gate on
  here — mirrors ``app/routers/webhooks/twilio.py``'s own
  ``_alert_tenant_hard_fire`` precedent: "continued alerting ... is the
  correct failure mode, not noise-suppression"; in practice
  ``app/agent/graph_entry.py``'s own completion gate already prevents a
  webhook-redelivered run from reaching this twice for the same message).
- **SOFT annotation present** (and no HARD hit) — "escalate blind": an
  IMMEDIATE ``needs_eyes`` notification carrying the raw tenant text and
  the soft-annotation categories (DB payload only — never app logs/Sentry,
  never-break rule #5), AND a ``tenant_ack`` row (the templated holding-ack
  SMS, queued as a durable send-intent — see "Tenant holding ack" below).
  Both idempotent (dedupe indexes below); the ``audit_log`` row is written
  once, gated on either artifact actually being new.
- **No keywords at all** (no HARD hit, no SOFT annotations) — a
  ``tenant_ack`` row (same holding ack) PLUS a ``degraded_retry`` marker
  that schedules re-classification attempts at ``failed_at + {1, 5, 15}
  minutes`` via ``notifications.next_attempt_at`` (the existing sweeper-key
  column — no new column). The landlord is NOT paged yet. The
  ``degraded_retry`` row ALSO snapshots the case's CURRENT status into
  ``payload.case_status_at_failure`` (``None`` when no case was ever
  attached) — the re-animation guard
  (``app/agent/degraded_mode_sweep.py::_case_has_moved_on``) compares
  against this later to detect "the case moved on before the retry became
  due" and refuses to re-run classification against a stale case state.
  See ``app/agent/degraded_mode_sweep.py`` for the sweep that drives these
  attempts and escalates to a genuine ``needs_eyes`` row if all three
  fail — this node only creates the INITIAL marker.

Tenant holding ack — durable send-intent, no send (#108 drains it)
--------------------------------------------------------------------
Per the "one send seam" fence (``apps/api/CLAUDE.md``: sends happen ONLY
via the draft flow or the emergency safety path — neither exists here), a
``tenant_ack`` ``notifications`` row (``channel='sms'``, ``status=
'pending'``) is the durable artifact recording INTENT to send the holding
ack — nothing in this codebase calls Twilio send anywhere, this issue
included. :func:`render_holding_ack` renders
``docs/02-product/emergency-prefilter.md``'s template VERBATIM (landlord
first name substituted, or a plain fallback label if the landlord's
``full_name`` is unset) into the row's ``payload.body`` — the exact text
copy-guardian reviews. Idempotent via ``uq_notifications_tenant_ack_dedupe``
(schema-v1.md v1.8, migration 0009), same ``ON CONFLICT ... DO NOTHING
RETURNING id`` pattern as every other notification insert in this
codebase.

Why NOT ``needs_eyes`` for either the tenant ack or the retry marker (a
design decision made and reverted during this issue, recorded here so it
isn't re-litigated)
------------------------------------------------------------------------
``uq_notifications_message_dedupe`` keys on ``(message_id, type)`` only —
reusing ``needs_eyes`` for a tenant-facing row would consume the SAME
dedupe slot the landlord-facing notification for that message needs,
silently dropping whichever insert loses the race. Reusing it for the
retry marker would ALSO mean a row that must NOT be treated as "ready to
notify" for up to 15 minutes shares a type with rows that always ARE ready
— a future notification-delivery consumer would have no schema-level way
to tell them apart short of inspecting a payload flag on every row, for
every type, forever. Two DEDICATED types (``tenant_ack``, ``degraded_retry``
— schema-v1.md v1.8, migration 0009) keep both meanings unambiguous: a
``needs_eyes`` row ALWAYS means "ready to tell a person," full stop.

What this node does NOT do (scope, unchanged from #34 + this issue): it
does NOT actually send the holding ack (no Twilio send call site exists —
#108/#44), does NOT run the escalation chain (#108), and does NOT drive
the retry sweep itself (``app/agent/degraded_mode_sweep.py`` does that,
callable but not yet wired to any cron — same seam pattern as
``app/agent/case_lifecycle.py::sweep_cases``).

Cost accounting on the classification_failed leg (#208) — payload-only,
never a routing/notification change
------------------------------------------------------------------------
``classify_severity.py`` never writes its own audit row on failure
(unchanged — see that module's docstring). When its double-failed
attempt(s) genuinely reached the API and consumed billed tokens, it hands
that usage forward via ``state["classification_failed_usage"]``
(``{model, tokens_in, tokens_out, cost_cents}``, absent when neither
attempt ever reached the API). :func:`degraded_mode` folds those keys
into the SAME ``payload`` dict every leg of
:func:`_handle_classification_failed` below already spreads into its own
``audit_log`` ``'degraded_mode'`` INSERT — so the cost keys ride along on
whichever leg's row actually gets written, with zero changes to routing,
notification content, or idempotency logic anywhere in this module (see
``app/cost_reporting.py``'s new ``action = 'degraded_mode'`` CTE branch,
schema-v1.md v1.14 amendment). This key is NEVER read by
:func:`_handle_generic_degraded` (the ``severity_emergency``/
``draft_guard_failed`` legs) — those already get their own cost recorded
unconditionally via ``draft_response.py``'s own ``'drafted'`` row, so
there is nothing for THIS node to add there.

Idempotency
-----------
Every notification INSERT in this module targets its own partial unique
index directly via ``ON CONFLICT ... DO NOTHING RETURNING id`` — the SAME
pattern ``app/routers/webhooks/twilio.py`` and every other node in this
package already use. At most one artifact of each type ever exists per
``message_id``. The ``degraded_mode`` ``audit_log`` row for the SOFT/no
-keyword legs is gated on at least one artifact having been newly created
this call (never duplicated on a redelivered/retried run); the HARD-hit
leg's audit row is deliberately NOT gated this way (see above).

Two DIFFERENT Sentry alerts — do not conflate them (safety-review round,
2026-07-12, corrected a MAJOR spec gap: the AC's "Sentry alert on
degraded-mode activation" was previously ONLY implemented for the write
-FAILURE case below, never for a successful activation itself)
------------------------------------------------------------------------
1. **Activation alert** (:func:`_alert_degraded_mode_activation`,
   ``level="warning"``) — fires on a GENUINE NEW degraded-mode event: the
   SOFT-annotation escalation, the no-keyword retry-queue, and (in
   ``app/agent/degraded_mode_sweep.py``) the sweep's own
   ``retry_exhausted`` escalation. This IS the AC's "Sentry alert on
   degraded-mode activation" — a person should be paged that classification
   fell back to degraded mode, not just that this node's own writes broke.
2. **Write-failure alert** (below, ``level="error"``) — fires when this
   node's OWN notification/audit writes THEMSELVES raise. A completely
   different, worse failure mode (the ONE node whose job is "make sure a
   person finds out" failing silently) — kept as its own page, at a
   higher severity, never merged with (1).

Never silent on the node's OWN DB failure either (safety review MEDIUM,
carried over from #34)
------------------------------------------------------------------------
The ENTIRE DB-touching body below (both the unchanged generic path and the
new classification_failed leg) is wrapped in one try/except: on failure
this node logs, pages via ``sentry_sdk.capture_message`` (uuids/reason
strings/exception type name only — never a message body or phone number,
rule #5), and still returns a normal partial state update (never raises) —
the graph run itself is never aborted by a degraded-mode write failure.

Never-break rule #5: only uuids/booleans/short reason strings/exception
type names/category names ever reach ``log.*`` calls or Sentry here.
Raw message bodies and the rendered holding-ack text may go into
DB payloads (``notifications.payload``, same as every other node's raw
-text-in-a-DB-row precedent — e.g. the SOFT-annotation ``needs_eyes`` row
here, or the emergency webhook's own category payloads) — never into a log
line or a Sentry event.

DB access
---------
Admin engine (background/graph context), same pattern as every other #30/
#110/#32/#33 node. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sentry_sdk
import structlog
from pydantic import ValidationError
from sqlalchemy import text

from app.agent.schemas import CaseContext, PrefilterResult, Severity
from app.agent.state import AgentState
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

REASON_CLASSIFICATION_FAILED = "classification_failed"
REASON_DRAFT_GUARD_FAILED = "draft_guard_failed"
REASON_SEVERITY_EMERGENCY = "severity_emergency"
"""An LLM-classified EMERGENCY Tier-0 itself missed — see module docstring.
Interim trigger until #108's real escalation chain exists."""

_REASON_UNKNOWN = "unknown"
"""Defensive-only fallback (see :func:`_resolve_reasons`) — should never
actually appear: it means this node was routed to without any of the
three known triggers being true, a routing-logic invariant violation, not
a real production scenario."""

RETRY_SCHEDULE: tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
)
"""``emergency-prefilter.md``'s "retry at 1, 5, 15 min" — absolute offsets
from the ORIGINAL failure time (``payload.failed_at``), not relative
deltas between successive attempts. ``app/agent/degraded_mode_sweep.py``'s
:func:`next_retry_at` is the pure function that turns
``(attempt_count, failed_at)`` into the next due time (or ``None`` once
all three are exhausted) — imported here only for the FIRST scheduling at
creation time (``attempt=0``)."""

HOLDING_ACK_TEMPLATE = (
    "Got your message — it's been passed to {first_name}. If this is a "
    "life-threatening emergency, call 911."
)
"""Verbatim from ``docs/02-product/emergency-prefilter.md``'s "Holding ack
(template, no LLM)" section — copy-guardian reviews this exact string.

Copy-guardian ruling (safety-review round, 2026-07-12): the original
template's trailing "...and you'll hear back soon" clause is REMOVED.
``docs/02-product/plain-language-rules.md`` rule 4 ("concrete over
relative ... never 'soon'") applies here with extra force, not less: this
template fires PRECISELY when classification is down — "soon" is a
timeline this code has no way to honor at the moment it's sent. Same
reasoning the prompts-v2 "soon" removal already established for
``draft_response.py``'s own copy. Not versioned like ``prompts/v{n}.py``
(this is not an LLM prompt, it is a fixed SMS template with one
substitution slot); a further wording change is a copy-guardian-reviewed
edit to this constant (and to emergency-prefilter.md's own template text,
in the same commit), not a new version file."""

_FALLBACK_LANDLORD_LABEL = "your landlord"
"""Used when ``landlords.full_name`` is unset/blank — keeps the template
grammatical ("...passed to your landlord.") without inventing a name."""


def render_holding_ack(first_name: str | None) -> str:
    """Render :data:`HOLDING_ACK_TEMPLATE` — *first_name* is the
    landlord's first name (already split from ``full_name`` by
    :func:`_first_name`), or ``None``/blank to use
    :data:`_FALLBACK_LANDLORD_LABEL`. Pure, no I/O — the exact function
    copy-guardian/tests exercise directly."""
    label = first_name if first_name else _FALLBACK_LANDLORD_LABEL
    return HOLDING_ACK_TEMPLATE.format(first_name=label)


def _first_name(full_name: str | None) -> str | None:
    """First whitespace-separated token of *full_name*, or ``None`` if
    unset/blank. No locale-aware name parsing attempted — a plain split is
    sufficient for a first-name greeting and matches how the rest of this
    codebase treats display names (e.g. ``tenants.name`` used verbatim in
    ``draft_response.py``)."""
    if not full_name:
        return None
    stripped = full_name.strip()
    if not stripped:
        return None
    return stripped.split()[0]


def _parse_prefilter(raw: Any) -> PrefilterResult:
    """Same fallback convention as every other node's own local copy
    (``classify_severity.py``, ``identify_case.py``): a missing OR
    malformed snapshot falls back to ``hard_hit=False,
    soft_annotations=[]`` (the "no keywords" leg) rather than raising or
    blocking — this can never de-escalate a real Tier-0 fire (the webhook
    already acted on its own copy of the snapshot; this fallback only
    affects what THIS node does when its own read of the snapshot fails).
    Duplicated rather than imported — established project convention."""
    if raw is None:
        log.error("degraded_mode_prefilter_snapshot_missing")
        return PrefilterResult(hard_hit=False)
    try:
        return PrefilterResult.model_validate(raw)
    except (ValidationError, TypeError) as exc:
        log.warning("degraded_mode_prefilter_snapshot_malformed", exc_type=type(exc).__name__)
        return PrefilterResult(hard_hit=False)


# Mirrors app/routers/webhooks/twilio.py's `_INSERT_NEEDS_EYES_SQL` /
# app/agent/nodes/identify_property.py's `_INSERT_NEEDS_EYES_SQL` exactly —
# same partial unique index (`uq_notifications_message_dedupe`, migration
# 0006), same ON CONFLICT target. Reproduced locally per this codebase's
# established convention (small, stable SQL; not worth a cross-module
# private import).
_INSERT_NEEDS_EYES_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, :case_id, 'needs_eyes', 'push', 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'message_id'), type) WHERE type IN ('emergency_call', 'needs_eyes')
    DO NOTHING
    RETURNING id
    """
)

_INSERT_DEGRADED_MODE_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'system', 'degraded_mode', CAST(:payload AS jsonb))"
)

_SELECT_MESSAGE_FOR_DEGRADED_SQL = text(
    "SELECT body, prefilter FROM messages WHERE id = :message_id"
)

_SELECT_LANDLORD_FULL_NAME_SQL = text("SELECT full_name FROM landlords WHERE id = :landlord_id")

# Safety-review round (re-animation guard, see app/agent/degraded_mode_
# sweep.py's own docstring "Re-animation guard"): snapshotted into the
# degraded_retry row's payload at CREATION time so the sweep can later
# detect "the case moved on since this failure was recorded" by comparing
# its CURRENT status against this snapshot -- never a hardcoded absolute
# check against 'open' (a case can legitimately be in any open-family
# status the moment classification fails for a NEW message on it).
_SELECT_CASE_STATUS_SQL = text("SELECT status FROM cases WHERE id = :case_id")

# Idempotent via uq_notifications_tenant_ack_dedupe (schema-v1.md v1.8,
# migration 0009) — see module docstring "Why NOT needs_eyes...".
_INSERT_TENANT_ACK_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, :case_id, 'tenant_ack', 'sms', 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'message_id')) WHERE type = 'tenant_ack'
    DO NOTHING
    RETURNING id
    """
)

# Idempotent via uq_notifications_degraded_retry_dedupe (schema-v1.md
# v1.8, migration 0009). `next_attempt_at` is the FIRST scheduled retry
# (failed_at + RETRY_SCHEDULE[0]) — app/agent/degraded_mode_sweep.py owns
# every subsequent advance/escalation.
_INSERT_DEGRADED_RETRY_SQL = text(
    """
    INSERT INTO notifications (
        landlord_id, case_id, type, channel, status, payload, next_attempt_at
    )
    VALUES (
        :landlord_id, :case_id, 'degraded_retry', 'push', 'pending', CAST(:payload AS jsonb),
        :next_attempt_at
    )
    ON CONFLICT ((payload ->> 'message_id')) WHERE type = 'degraded_retry'
    DO NOTHING
    RETURNING id
    """
)


def _resolve_reasons(state: AgentState) -> list[str]:
    """Every G1(+EMERGENCY) trigger that is actually true for this run —
    see module docstring for why this is a list, not a single value, and
    why the two non-classification triggers can coexist. Order is fixed
    (classification_failed, severity_emergency, draft_guard_failed) so the
    payload is deterministic regardless of which combination fired."""
    reasons: list[str] = []
    if state.get("classification_failed"):
        reasons.append(REASON_CLASSIFICATION_FAILED)
    severity_result = state.get("severity")
    if severity_result is not None and severity_result.severity is Severity.EMERGENCY:
        reasons.append(REASON_SEVERITY_EMERGENCY)
    if state.get("draft_guard_failed"):
        reasons.append(REASON_DRAFT_GUARD_FAILED)
    return reasons or [_REASON_UNKNOWN]


def _alert_degraded_mode_activation(*, message_id: UUID, case_id: UUID | None, leg: str) -> None:
    """Sentry alert on a genuine NEW degraded-mode ACTIVATION (issue #109
    AC line 5: "Sentry alert on degraded-mode activation") — ``level=
    "warning"``, deliberately DISTINCT from the ``level="error"`` page in
    :func:`degraded_mode`'s own outer except-block: that one means "this
    node's OWN writes just failed"; this one means "the writes succeeded,
    and a person should know a message just fell back to degraded mode."
    Metadata only (uuids + the ``leg`` name) — never a message body or
    phone number, rule #5.

    Scope (safety-review round, 2026-07-12): called for the SOFT-annotation
    escalation and the no-keyword retry-queue leg (both NEW in this
    issue), and by ``app/agent/degraded_mode_sweep.py`` for the sweep's own
    ``retry_exhausted`` escalation. Deliberately NOT called for the
    HARD-hit leg (the webhook's own ``_alert_tenant_hard_fire`` already
    pages for that exact fact — a second page here would double-alert the
    same event) nor for the UNCHANGED generic leg
    (``_handle_generic_degraded`` — ``severity_emergency``/
    ``draft_guard_failed`` are #34 territory, not this issue's scope)."""
    sentry_sdk.capture_message(
        f"degraded_mode activated: {leg}",
        level="warning",
        extras={
            "message_id": str(message_id),
            "case_id": str(case_id) if case_id is not None else None,
            "leg": leg,
        },
    )


async def _handle_generic_degraded(
    *, landlord_id: UUID, case_id: UUID | None, payload: dict[str, Any]
) -> str:
    """``severity_emergency`` / ``draft_guard_failed`` (and their
    co-occurrence) — UNCHANGED from #34: one idempotent ``needs_eyes``
    insert, audit row gated on it having actually created a new row."""
    async with asynccontextmanager(get_admin_session)() as session:
        notification_row = (
            (
                await session.execute(
                    _INSERT_NEEDS_EYES_SQL,
                    {
                        "landlord_id": str(landlord_id),
                        "case_id": str(case_id) if case_id is not None else None,
                        "payload": json.dumps(payload),
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        created = notification_row is not None
        if created:
            await session.execute(
                _INSERT_DEGRADED_MODE_AUDIT_SQL,
                {
                    "landlord_id": str(landlord_id),
                    "case_id": str(case_id) if case_id is not None else None,
                    "payload": json.dumps(payload),
                },
            )
    log.warning(
        "degraded_mode_notified",
        case_id=str(case_id) if case_id is not None else None,
        reasons=payload["reasons"],
        notification_created=created,
    )
    return "I couldn't finish this one on my own, so I've sent you a notification to take a look."


async def _handle_classification_failed(
    *,
    message_id: UUID,
    landlord_id: UUID,
    case_id: UUID | None,
    payload: dict[str, Any],
) -> str:
    """The #109 leg — see module docstring "The classification_failed leg
    (#109)" for the full three-way branch. Returns the reasoning_log line
    for whichever branch fired."""
    async with asynccontextmanager(get_admin_session)() as session:
        message_row = (
            (
                await session.execute(
                    _SELECT_MESSAGE_FOR_DEGRADED_SQL, {"message_id": str(message_id)}
                )
            )
            .mappings()
            .one_or_none()
        )
        body: str | None = message_row["body"] if message_row is not None else None
        prefilter_result = _parse_prefilter(
            message_row["prefilter"] if message_row is not None else None
        )

        if prefilter_result.hard_hit:
            # Already handled by the webhook's fire_emergency_protocol seam
            # -- deliberately NOT idempotency-gated, see module docstring.
            hard_hit_payload = {**payload, "leg": "hard_hit_already_handled"}
            await session.execute(
                _INSERT_DEGRADED_MODE_AUDIT_SQL,
                {
                    "landlord_id": str(landlord_id),
                    "case_id": str(case_id) if case_id is not None else None,
                    "payload": json.dumps(hard_hit_payload),
                },
            )
            log.info("degraded_mode_hard_hit_already_handled", message_id=str(message_id))
            return (
                "This one already triggered the emergency alert on its own, so there was "
                "nothing extra to do here."
            )

        landlord_row = (
            (
                await session.execute(
                    _SELECT_LANDLORD_FULL_NAME_SQL, {"landlord_id": str(landlord_id)}
                )
            )
            .mappings()
            .one_or_none()
        )
        first_name = _first_name(landlord_row["full_name"] if landlord_row is not None else None)
        ack_body = render_holding_ack(first_name)

        tenant_ack_row = (
            (
                await session.execute(
                    _INSERT_TENANT_ACK_SQL,
                    {
                        "landlord_id": str(landlord_id),
                        "case_id": str(case_id) if case_id is not None else None,
                        "payload": json.dumps({**payload, "body": ack_body}),
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        tenant_ack_created = tenant_ack_row is not None

        if prefilter_result.soft_annotations:
            leg = "soft_annotation_escalated"
            needs_eyes_payload = {
                **payload,
                "leg": leg,
                "soft_annotations": prefilter_result.soft_annotations,
                # Raw text is DB-payload-only (never a log line/Sentry event,
                # rule #5) -- deliberately NOT copied into the audit_log row
                # below, matching classify_severity.py's own "NO message
                # body ever enters this payload" convention for audit_log.
                "raw_text": body,
            }
            needs_eyes_row = (
                (
                    await session.execute(
                        _INSERT_NEEDS_EYES_SQL,
                        {
                            "landlord_id": str(landlord_id),
                            "case_id": str(case_id) if case_id is not None else None,
                            "payload": json.dumps(needs_eyes_payload),
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            any_created = tenant_ack_created or needs_eyes_row is not None
            reasoning_line = (
                "I couldn't classify this one automatically, so I flagged it for you right "
                "away and let the tenant know someone's on it."
            )
        else:
            leg = "queued_for_retry"
            now = datetime.now(UTC)

            # Re-animation guard (safety review, this round): snapshot the
            # case's CURRENT status so app/agent/degraded_mode_sweep.py can
            # later detect "this case moved on since the failure was
            # recorded" by comparing, never by re-deriving from a
            # hardcoded 'open' check -- see that module's own docstring
            # "Re-animation guard".
            case_status_at_failure: str | None = None
            if case_id is not None:
                case_status_row = (
                    (await session.execute(_SELECT_CASE_STATUS_SQL, {"case_id": str(case_id)}))
                    .mappings()
                    .one_or_none()
                )
                case_status_at_failure = (
                    case_status_row["status"] if case_status_row is not None else None
                )

            retry_payload = {
                **payload,
                "leg": leg,
                "failed_at": now.isoformat(),
                "case_status_at_failure": case_status_at_failure,
            }
            # attempt count lives in the real `notifications.attempt` column
            # (schema default 0) -- app/agent/degraded_mode_sweep.py owns
            # every advance from there; no need to duplicate it in payload.
            retry_row = (
                (
                    await session.execute(
                        _INSERT_DEGRADED_RETRY_SQL,
                        {
                            "landlord_id": str(landlord_id),
                            "case_id": str(case_id) if case_id is not None else None,
                            "payload": json.dumps(retry_payload),
                            "next_attempt_at": now + RETRY_SCHEDULE[0],
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            any_created = tenant_ack_created or retry_row is not None
            reasoning_line = (
                "I couldn't classify this one right away, so I let the tenant know someone's "
                "on it and I'll keep trying in the background."
            )

        if any_created:
            await session.execute(
                _INSERT_DEGRADED_MODE_AUDIT_SQL,
                {
                    "landlord_id": str(landlord_id),
                    "case_id": str(case_id) if case_id is not None else None,
                    "payload": json.dumps({**payload, "leg": leg}),
                },
            )

    # Outside the DB transaction on purpose (same convention as
    # app/agent/emergency.py's fire_emergency_protocol call site): only
    # page once the durable artifacts this alert announces are actually
    # committed, and only for a GENUINE new activation (any_created),
    # never a redelivered/retried no-op.
    if any_created:
        _alert_degraded_mode_activation(message_id=message_id, case_id=case_id, leg=leg)

    log.warning(
        "degraded_mode_classification_failed_handled",
        message_id=str(message_id),
        hard_hit=prefilter_result.hard_hit,
        soft_annotation_count=len(prefilter_result.soft_annotations),
    )
    return reasoning_line


async def degraded_mode(state: AgentState) -> dict[str, Any]:
    """Durably notify the landlord (and, for ``classification_failed``,
    queue the tenant's holding ack) that this message needs attention.
    Returns a partial state update (``reasoning_log`` only). Never raises —
    a DB failure here is caught, logged, and paged via Sentry (see module
    docstring), not propagated."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    reasons = _resolve_reasons(state)

    landlord_id = case_context.landlord_id
    if landlord_id is None:  # pragma: no cover — invariant: identify_property always sets this
        log.error("degraded_mode_missing_landlord_id", message_id=str(message_id))
        reasoning_log.append(
            "I couldn't finish this one on my own, and I couldn't reach you either."
        )
        return {"reasoning_log": reasoning_log}

    case_id = case_context.case_id
    payload: dict[str, Any] = {
        "message_id": str(message_id),
        "case_id": str(case_id) if case_id is not None else None,
        "reasons": reasons,
    }
    # #208 — payload-only amendment (schema-v1.md v1.14): when
    # classify_severity's own failed attempt(s) genuinely reached the API
    # and consumed billed tokens, it hands that usage forward via
    # ``state["classification_failed_usage"]`` (absent when neither attempt
    # ever reached the API). Folding those keys into THIS payload means
    # they ride along on the SAME ``'degraded_mode'`` audit row every leg of
    # ``_handle_classification_failed`` below already writes for a genuine
    # new activation — no new audit row, no new action value, no migration.
    # See ``classify_severity.py``'s own module docstring "Cost accounting
    # on the failure path (#208)" for the full design rationale.
    if reasons == [REASON_CLASSIFICATION_FAILED]:
        failed_usage = state.get("classification_failed_usage")
        if failed_usage:
            payload.update(failed_usage)

    try:
        if reasons == [REASON_CLASSIFICATION_FAILED]:
            reasoning_line = await _handle_classification_failed(
                message_id=message_id, landlord_id=landlord_id, case_id=case_id, payload=payload
            )
        else:
            reasoning_line = await _handle_generic_degraded(
                landlord_id=landlord_id, case_id=case_id, payload=payload
            )
    except Exception as exc:
        # Never let the ONE node whose job is "make sure a person finds
        # out" fail completely silently -- see module docstring "Never
        # silent on the node's OWN DB failure either".
        log.error(
            "degraded_mode_write_failed",
            message_id=str(message_id),
            case_id=str(case_id) if case_id is not None else None,
            reasons=reasons,
            exc_type=type(exc).__name__,
        )
        sentry_sdk.capture_message(
            "degraded_mode: failed to write notification/audit row",
            level="error",
            extras={
                "message_id": str(message_id),
                "case_id": str(case_id) if case_id is not None else None,
                "reasons": reasons,
                "exc_type": type(exc).__name__,
            },
        )
        reasoning_log.append(
            "I couldn't finish this one on my own, and I couldn't reach you either."
        )
        return {"reasoning_log": reasoning_log}

    reasoning_log.append(reasoning_line)
    return {"reasoning_log": reasoning_log}


__all__: list[str] = [
    "HOLDING_ACK_TEMPLATE",
    "REASON_CLASSIFICATION_FAILED",
    "REASON_DRAFT_GUARD_FAILED",
    "REASON_SEVERITY_EMERGENCY",
    "RETRY_SCHEDULE",
    "degraded_mode",
    "render_holding_ack",
]
