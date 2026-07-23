"""Degraded-mode re-classification sweep (#109) — drives the "no keywords
at all" leg's 1/5/15-minute retry schedule that
``app/agent/nodes/degraded_mode.py::_handle_classification_failed`` queues
(a ``degraded_retry`` notification row, ``schema-v1.md`` v1.8, migration
0009).

Same seam pattern as ``app/agent/case_lifecycle.py::sweep_cases`` (#110)
and ``docs/02-product/emergency-prefilter.md``'s escalation-chain sweep
(#108, not yet built): a pure decision function
(:func:`next_retry_at`) plus a DB entrypoint
(:func:`sweep_degraded_mode_retries`) that a future scheduled job (Fly
machines cron / APScheduler loop, per that doc's "Implementation notes")
should invoke on a regular cadence — nothing calls this today, exactly
like ``sweep_cases``. **The scheduler/cron wiring is explicitly out of
this issue's scope** — this module is the callable unit, not the trigger.

**DEPLOYMENT-GATING FACT — CLOSED 2026-07-12 (#108, spec finding S1; record
the closure here, don't just let it go stale in old prose):** this note
originally recorded that the "no keywords at all" leg's half of the
degraded-mode invariant ("no tenant message ever sits unacknowledged and
invisible because an API was down") was NOT fully closed by issue #109
alone, for two independent reasons: (a) nothing scheduled
:func:`sweep_degraded_mode_retries` itself, and (b) even once scheduled,
the queued ``tenant_ack`` holding-ack SMS had no sender to actually drain
and deliver it — a durable row existed, but nothing ever sent the text.

**Both are now closed:**

(a) ``app/scheduler.py``'s 60-second ticker (#108) calls
    :func:`sweep_degraded_mode_retries` (this module), ``run_emergency_chain_sweep``,
    AND ``run_sms_drain_sweep`` every tick, started/stopped in
    ``app/main.py``'s FastAPI lifespan — a real, running scheduler exists
    for the first time.
(b) ``app/agent/emergency_chain.py::run_sms_drain_sweep`` (safety review,
    2026-07-12, spec finding S1) drains every ``pending``/``failed``
    ``tenant_ack`` row on each tick, sending the holding-ack SMS via the
    injectable Twilio sender and resending on failure until genuinely
    delivered — the SAME drain also handles ``emergency_sms`` (the
    unrelated #108 tenant safety text), closing the analogous "at-most-once"
    gap safety finding 3 identified for that type.

With both closed, the no-keyword leg's landlord-facing escalation
(the genuine ``needs_eyes`` row, if all three re-classification attempts
still fail) is now reached by a real, live sweep loop, and the tenant's
holding ack is now genuinely delivered, not merely queued. This module's
own code did not change to close the gap — only the deployment
(scheduler) and the sender (drain sweep) needed to exist; this note is
updated in place, per its own original instruction, rather than left
stale.

What one sweep tick does, per due row
---------------------------------------
0. Re-animation guard (see below) — skip re-running classification
   entirely if the case has moved on since the failure was recorded.
1. SELECT every ``degraded_retry`` row that is ``status='pending'`` and
   whose ``next_attempt_at`` has arrived (``idx_notifications_sweep``
   already covers ``(status, next_attempt_at)`` — no new index needed for
   the read side).
2. For each candidate (sequentially — never fire concurrent Anthropic
   calls for the same sweep tick, mirroring ``case_lifecycle.sweep_cases``'s
   own sequential loop): re-run the FULL pipeline via
   ``app/agent/graph.py::run_graph`` (NOT
   ``app/agent/graph_entry.py::enqueue_classification`` — that function's
   own completion gate treats ANY existing ``degraded_mode`` audit row as
   "already done" and would skip re-running entirely, which is exactly
   correct for a webhook REDELIVERY but exactly wrong for an intentional
   degraded-mode retry). ``run_graph`` re-derives everything from
   ``message_id`` alone; ``identify_case`` re-attaches the message to the
   SAME case it was already attached to on the first attempt (the tenant's
   now-single open case, per its own ambiguity rule) — verified idempotent
   by this module's own integration tests.
3. Reclassification SUCCEEDED this time (``final_state.get(
   "classification_failed")`` falsy) — the row is marked ``exhausted``
   with ``payload.outcome = "resolved"`` (a landlord-facing notification
   was never needed; the rest of the pipeline, drafting/approval-queueing/
   emergency-escalation, already ran normally inside THIS ``run_graph``
   call, exactly as it would have on the very first attempt). An
   ``audit_log`` row records the recovery.
4. Reclassification FAILED again and a next scheduled time remains
   (attempt count < 3) — the row's ``attempt``/``next_attempt_at`` advance
   to the next checkpoint (:func:`next_retry_at`). An ``audit_log`` row
   records this attempt's failure.
5. Reclassification FAILED on the LAST scheduled attempt (the 15-minute
   one) — ESCALATE: the ``degraded_retry`` row is marked ``exhausted`` with
   ``payload.outcome = "escalated"``, and a genuine, separate
   ``needs_eyes`` notification is inserted (idempotent via
   ``uq_notifications_message_dedupe``, same pattern as everywhere else in
   this codebase), carrying the raw tenant text — the landlord is finally
   told, per ``emergency-prefilter.md``'s "if still failing at 15 min,
   landlord gets the needs-your-eyes notification anyway". An
   ``audit_log`` row records the escalation, and a Sentry activation alert
   fires (``level="warning"``, metadata only) — see "Sentry activation
   alert" below.

Re-animation guard (safety review HIGH/MAJOR, this round)
-----------------------------------------------------------
The retry window is 1–15 minutes long. A LOT can happen to a case in that
window: a newer tenant message can arrive and be handled normally (drafted,
approved, sent), the landlord can resolve the case directly, or the case
can be reopened/re-attached elsewhere. Blindly re-running ``run_graph`` on
the STALE original message (M1) after any of that would re-animate old
state — at best a wasted/confusing extra draft, at worst staling out the
CURRENT pending draft or dragging a resolved case back toward
``awaiting_approval``.

:func:`_case_has_moved_on` gates every candidate BEFORE ``run_graph`` is
ever called, checking two independent signals (either one is sufficient):

1. **Case status drifted since the failure was recorded.** The retry row's
   payload carries ``case_status_at_failure`` — a snapshot of
   ``cases.status`` taken at the MOMENT ``degraded_mode.py`` queued this
   retry (never a hardcoded check against ``'open'``: a case can
   legitimately be in any open-family status the instant classification
   fails for a new message on it). If the case's CURRENT status differs
   from that snapshot, something changed it — treat as moved on. A missing
   snapshot (should never happen for a row this codebase writes) fails
   CLOSED (treated as moved on) rather than risk re-animating a case we
   can't verify.
2. **A newer inbound message exists on the case.** A direct query: is
   there any OTHER ``inbound`` message linked to this case (via
   ``message_cases``) created AFTER M1? If so, M1 is no longer "the case's
   latest unhandled inbound" — retrying its OWN classification now would
   be answering a question the tenant has already moved past.

When either signal fires, the candidate is marked ``exhausted`` with
``payload.outcome = "superseded"`` via :func:`_supersede` (never silent —
an ``audit_log`` row records exactly why) and ``run_graph`` is NEVER
invoked for it. This is fundamentally different from "lost the guard
race" (:data:`_OUTCOME_LOST_RACE`): superseding is a POSITIVE, intentional
decision based on the case's own state, not a concurrency artifact.

Self-guarding WHERE + rowcount-gated side effects (same TOCTOU discipline
as ``app/agent/case_lifecycle.py::_apply_sweep_action``)
------------------------------------------------------------------------
Each outcome above is applied via an ``UPDATE ... WHERE id = :id AND
status = 'pending' AND attempt = :attempt`` — re-asserting, at write time,
that the row is STILL in the exact state this sweep tick read it in. No
true concurrent sweeper exists yet (nothing schedules this function at
all — see above), so this guard is belt-and-braces today, not a
proven-necessary fix for a reproduced race; it costs nothing and follows
the estabished house style. A lost race (``rowcount != 1``) is a silent
no-op for that candidate — no audit row, no notification — exactly
``case_lifecycle``'s own documented "a lost race is a deliberate silent
no-op" precedent.

Sentry activation alert (spec MAJOR, this round — issue #109 AC line 5)
--------------------------------------------------------------------------
:func:`_escalate` pages Sentry (``level="warning"``, metadata only —
uuids/leg name, never a message body or phone number, rule #5) whenever
IT actually performs the escalation (gated on the same rowcount check as
the DB write, never on a lost race) — this is the "Sentry alert on
degraded-mode activation" AC applied to the sweep's own escalation event,
distinct from and in addition to
``app/agent/nodes/degraded_mode.py``'s own activation alerts for the
SOFT-annotation and initial no-keyword legs.

Exception handling never silently loops forever (safety HIGH + spec
MAJOR, this round — THE blocker finding)
------------------------------------------------------------------------
An earlier revision's per-candidate ``except Exception: log.error;
continue`` was a genuine silent-failure loop: ``log.error`` alone never
reaches Sentry (this process's ``LoggingIntegration`` runs with
``event_level=None`` — see ``app/observability.py`` — structlog/stdlib
records are breadcrumbs only), and a raised exception never advances
``attempt``/``next_attempt_at`` on the row, so the SAME row re-selects on
every future tick with no page and no eventual escalation — a
persistently-failing candidate (e.g. a systemic ``run_graph`` bug, not a
transient blip) could sit invisible forever, worse than doing nothing.

Fixed by :func:`_record_candidate_exception`: EVERY exception now (1)
pages Sentry (``level="error"``, metadata only: notification_id/
message_id/exc_type — rule #5) — no silent tick, ever — and (2)
atomically increments a BOUNDED, DEDICATED counter
(``payload.exception_count`` — deliberately NOT the same counter as the
classification-failure ``attempt`` column/schedule, since an
infrastructure exception is a different failure mode than a clean
``classification_failed=True`` result) via a self-guarded
``UPDATE ... WHERE status = 'pending'``. Once
:data:`_MAX_CANDIDATE_EXCEPTIONS` is reached, the row force-escalates by
REUSING :func:`_escalate` (same genuine ``needs_eyes`` insert, same Sentry
activation alert, tagged with its own ``escalation_leg`` so the audit
trail distinguishes "escalated because classification kept saying
ROUTINE/URGENT-but-invalid" from "escalated because the retry mechanism
itself kept raising"). The exception-handling path is ITSELF wrapped so a
failure there (e.g. the counter UPDATE itself erroring) can never re-raise
into the sweep loop.

Never-break rule #5: only uuids/booleans/short reason or outcome
strings/exception type names ever reach ``log.*`` calls or Sentry here.
The raw tenant text on the ESCALATED ``needs_eyes`` row's payload is a DB
write, never a log line or Sentry event (same precedent as every other
raw-text-in-a-payload row in this codebase).

DB access
---------
Admin engine (background/scheduled-job context, no request/landlord JWT —
same rationale as ``case_lifecycle.sweep_cases``). Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import sentry_sdk
import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import run_graph
from app.agent.nodes.degraded_mode import RETRY_SCHEDULE
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

_OUTCOME_RESOLVED = "resolved"
_OUTCOME_RESCHEDULED = "rescheduled"
_OUTCOME_ESCALATED = "escalated"
_OUTCOME_ESCALATED_BY_EXCEPTION = "escalated_by_exception"
_OUTCOME_SUPERSEDED = "superseded"
_OUTCOME_LOST_RACE = "lost_race"

_MAX_CANDIDATE_EXCEPTIONS = 3
"""Bound on repeated ``_process_candidate`` exceptions for the SAME
``degraded_retry`` row before force-escalating — see module docstring
"Exception handling never silently loops forever". Deliberately the same
magnitude as ``RETRY_SCHEDULE``'s length (a reasonable, not load-bearing,
choice) but tracked via an entirely SEPARATE counter
(``payload.exception_count``), never the classification-failure
``attempt`` column."""


@dataclass(frozen=True)
class DegradedRetryCandidate:
    """A read-only, DB-row-shaped snapshot of one due ``degraded_retry``
    notification — decoupled from the DB row type so :func:`next_retry_at`
    stays pure and trivially testable, mirroring
    ``app/agent/case_lifecycle.py``'s ``CaseSnapshot`` convention.

    ``case_status_at_failure`` is the ``cases.status`` snapshot
    ``degraded_mode.py`` took when this retry was queued — ``None`` when
    no case was ever attached (unknown sender) OR, defensively, when an
    older/malformed row lacks the key entirely (see
    :func:`_case_has_moved_on`, which fails CLOSED on that ``None`` case
    when a case_id IS present)."""

    notification_id: UUID
    message_id: UUID
    case_id: UUID | None
    landlord_id: UUID
    attempt: int
    failed_at: datetime
    case_status_at_failure: str | None = None


@dataclass(frozen=True)
class SweepOutcome:
    """One candidate's outcome from a sweep tick."""

    notification_id: UUID
    message_id: UUID
    outcome: str


def next_retry_at(*, attempt: int, failed_at: datetime) -> datetime | None:
    """Pure: the next due time for retry number ``attempt`` (0-indexed —
    ``attempt`` is how many retries have ALREADY been attempted), or
    ``None`` once :data:`app.agent.nodes.degraded_mode.RETRY_SCHEDULE` is
    exhausted (the caller then escalates instead of rescheduling). Offsets
    are ABSOLUTE from *failed_at* (the original classification failure),
    not relative to "now" or to the previous attempt — see
    ``degraded_mode.py``'s own docstring on :data:`RETRY_SCHEDULE`."""
    if attempt >= len(RETRY_SCHEDULE):
        return None
    return failed_at + RETRY_SCHEDULE[attempt]


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SELECT_DUE_CANDIDATES_SQL = text(
    """
    SELECT id, landlord_id, case_id, attempt, payload
    FROM notifications
    WHERE type = 'degraded_retry' AND status = 'pending'
      AND next_attempt_at IS NOT NULL AND next_attempt_at <= :now
    ORDER BY next_attempt_at
    """
)

# Self-guarding: re-asserts the row is STILL pending at this exact attempt
# count at UPDATE time -- see module docstring "Self-guarding WHERE...".
_RESOLVE_RETRY_SQL = text(
    """
    UPDATE notifications
    SET status = 'exhausted', next_attempt_at = NULL, updated_at = now(),
        payload = payload || '{"outcome": "resolved"}'::jsonb
    WHERE id = :id AND status = 'pending' AND attempt = :attempt
    """
)

_RESCHEDULE_RETRY_SQL = text(
    """
    UPDATE notifications
    SET attempt = :new_attempt, next_attempt_at = :next_attempt_at, updated_at = now()
    WHERE id = :id AND status = 'pending' AND attempt = :attempt
    """
)

_ESCALATE_RETRY_SQL = text(
    """
    UPDATE notifications
    SET status = 'exhausted', next_attempt_at = NULL, attempt = :new_attempt, updated_at = now(),
        payload = payload || '{"outcome": "escalated"}'::jsonb
    WHERE id = :id AND status = 'pending' AND attempt = :attempt
    """
)

_SUPERSEDE_RETRY_SQL = text(
    """
    UPDATE notifications
    SET status = 'exhausted', next_attempt_at = NULL, updated_at = now(),
        payload = payload || '{"outcome": "superseded"}'::jsonb
    WHERE id = :id AND status = 'pending' AND attempt = :attempt
    """
)

# Self-guarding on status alone (this operation never touches attempt) --
# see module docstring "Exception handling never silently loops forever".
_INCREMENT_EXCEPTION_COUNT_SQL = text(
    """
    UPDATE notifications
    SET payload = jsonb_set(
          payload, '{exception_count}',
          to_jsonb(COALESCE((payload ->> 'exception_count')::int, 0) + 1)
        ),
        updated_at = now()
    WHERE id = :id AND status = 'pending'
    RETURNING (payload ->> 'exception_count')::int AS exception_count
    """
)

_SELECT_CASE_STATUS_SQL = text("SELECT status FROM cases WHERE id = :case_id")

# Condition B of the re-animation guard: is M1 (candidate.message_id)
# still the case's most recent TENANT-authored INBOUND message? Derived
# entirely from existing data (message_cases + messages.created_at) -- no
# new column, no need to separately track M1's own created_at anywhere.
#
# `AND m.party = 'tenant'` (safety review pin, #122 approve-by-SMS PR --
# forward risk recorded from the #40 safety review): approve-by-SMS
# landlord replies also arrive `direction = 'inbound'` (`party =
# 'landlord'`). Without this filter, a landlord's own reply to a DIFFERENT,
# unrelated draft-ready notification could -- if it were ever linked into
# `message_cases` for THIS case -- read as "a newer inbound message
# exists" and wrongly supersede a still-unclassified TENANT message this
# sweep is retrying, which fails safe (tenant was already acked, case
# stays visible) but neither reclassifies nor escalates when it should
# have. Approve-by-SMS's own reply handler does not in fact link its
# messages into `message_cases` (it sets `messages.case_id` directly at
# insert time instead), so this is belt-and-braces against a future
# change, not a currently-reproduced bug -- cheap enough to fix outright
# rather than merely document.
_SELECT_NEWER_INBOUND_EXISTS_SQL = text(
    """
    SELECT EXISTS (
      SELECT 1 FROM message_cases mc
      JOIN messages m ON m.id = mc.message_id
      WHERE mc.case_id = :case_id
        AND m.direction = 'inbound'
        AND m.party = 'tenant'
        AND m.id != :message_id
        AND m.created_at > (SELECT created_at FROM messages WHERE id = :message_id)
    ) AS newer_inbound_exists
    """
)

_SELECT_MESSAGE_BODY_SQL = text("SELECT body FROM messages WHERE id = :message_id")

# Same partial-unique-index target as every other needs_eyes insert in
# this codebase (uq_notifications_message_dedupe, migration 0006) --
# reproduced locally per established convention.
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


def _candidate_from_row(row: dict[str, object]) -> DegradedRetryCandidate:
    payload = cast("dict[str, object]", row["payload"])
    message_id = UUID(str(payload["message_id"]))
    case_id_raw = payload.get("case_id")
    failed_at_raw = payload.get("failed_at")
    if failed_at_raw is None:  # pragma: no cover -- invariant: always set at creation
        raise ValueError(f"degraded_retry row {row['id']} has no payload.failed_at")
    case_status_at_failure_raw = payload.get("case_status_at_failure")
    return DegradedRetryCandidate(
        notification_id=cast("UUID", row["id"]),
        message_id=message_id,
        case_id=UUID(str(case_id_raw)) if case_id_raw else None,
        landlord_id=cast("UUID", row["landlord_id"]),
        attempt=cast("int", row["attempt"]),
        failed_at=datetime.fromisoformat(str(failed_at_raw)),
        case_status_at_failure=(
            str(case_status_at_failure_raw) if case_status_at_failure_raw is not None else None
        ),
    )


def _alert_degraded_mode_escalation(*, message_id: UUID, case_id: UUID | None, leg: str) -> None:
    """Sentry activation alert for the sweep's OWN escalation — see module
    docstring "Sentry activation alert". ``level="warning"``, metadata
    only (rule #5)."""
    sentry_sdk.capture_message(
        f"degraded_mode activated: {leg}",
        level="warning",
        extras={
            "message_id": str(message_id),
            "case_id": str(case_id) if case_id is not None else None,
            "leg": leg,
        },
    )


async def _case_has_moved_on(session: AsyncSession, candidate: DegradedRetryCandidate) -> bool:
    """Re-animation guard — see module docstring "Re-animation guard" for
    the full rationale. Returns ``True`` (skip re-running classification
    entirely) when EITHER the case's status has drifted from its
    ``case_status_at_failure`` snapshot, or a newer inbound message
    already exists on the case. ``False`` (proceed to ``run_graph``) only
    when NEITHER signal fires."""
    if candidate.case_id is None:
        return False  # no case was ever attached -- nothing to have moved on from

    status_row = (
        (await session.execute(_SELECT_CASE_STATUS_SQL, {"case_id": str(candidate.case_id)}))
        .mappings()
        .one_or_none()
    )
    if status_row is None:  # pragma: no cover -- cases are never deleted
        log.warning(
            "degraded_mode_sweep_case_not_found",
            notification_id=str(candidate.notification_id),
            case_id=str(candidate.case_id),
        )
        return True

    if candidate.case_status_at_failure is None:
        # Defensive: every row THIS codebase writes carries the snapshot.
        # An absent one can't be compared safely -- fail CLOSED rather
        # than risk re-animating a case we can't verify hasn't moved on.
        log.warning(
            "degraded_mode_sweep_missing_case_status_snapshot",
            notification_id=str(candidate.notification_id),
        )
        return True

    if status_row["status"] != candidate.case_status_at_failure:
        return True

    newer_inbound_row = (
        (
            await session.execute(
                _SELECT_NEWER_INBOUND_EXISTS_SQL,
                {"case_id": str(candidate.case_id), "message_id": str(candidate.message_id)},
            )
        )
        .mappings()
        .one()
    )
    return bool(newer_inbound_row["newer_inbound_exists"])


async def _supersede(session: AsyncSession, candidate: DegradedRetryCandidate) -> bool:
    """The case moved on (see :func:`_case_has_moved_on`) -- mark the row
    ``exhausted``/``outcome=superseded`` and record WHY in ``audit_log``
    (never silent), without ever touching ``cases`` or re-running
    classification."""
    result = cast(
        "CursorResult[object]",
        await session.execute(
            _SUPERSEDE_RETRY_SQL,
            {"id": str(candidate.notification_id), "attempt": candidate.attempt},
        ),
    )
    if result.rowcount != 1:
        return False
    await session.execute(
        _INSERT_DEGRADED_MODE_AUDIT_SQL,
        {
            "landlord_id": str(candidate.landlord_id),
            "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
            "payload": json.dumps(
                {
                    "message_id": str(candidate.message_id),
                    "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
                    "reasons": ["classification_failed"],
                    "leg": "retry_superseded",
                }
            ),
        },
    )
    return True


async def _resolve(session: AsyncSession, candidate: DegradedRetryCandidate) -> bool:
    result = cast(
        "CursorResult[object]",
        await session.execute(
            _RESOLVE_RETRY_SQL,
            {"id": str(candidate.notification_id), "attempt": candidate.attempt},
        ),
    )
    if result.rowcount != 1:
        return False
    await session.execute(
        _INSERT_DEGRADED_MODE_AUDIT_SQL,
        {
            "landlord_id": str(candidate.landlord_id),
            "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
            "payload": json.dumps(
                {
                    "message_id": str(candidate.message_id),
                    "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
                    "reasons": ["classification_failed"],
                    "leg": "retry_resolved",
                }
            ),
        },
    )
    return True


async def _reschedule(
    session: AsyncSession, candidate: DegradedRetryCandidate, *, new_attempt: int, next_at: datetime
) -> bool:
    result = cast(
        "CursorResult[object]",
        await session.execute(
            _RESCHEDULE_RETRY_SQL,
            {
                "id": str(candidate.notification_id),
                "attempt": candidate.attempt,
                "new_attempt": new_attempt,
                "next_attempt_at": next_at,
            },
        ),
    )
    if result.rowcount != 1:
        return False
    await session.execute(
        _INSERT_DEGRADED_MODE_AUDIT_SQL,
        {
            "landlord_id": str(candidate.landlord_id),
            "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
            "payload": json.dumps(
                {
                    "message_id": str(candidate.message_id),
                    "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
                    "reasons": ["classification_failed"],
                    "leg": "retry_attempt_failed",
                    "attempt": new_attempt,
                }
            ),
        },
    )
    return True


async def _escalate(
    session: AsyncSession,
    candidate: DegradedRetryCandidate,
    *,
    new_attempt: int,
    escalation_leg: str = "retry_exhausted",
) -> bool:
    """Escalate *candidate* to a genuine ``needs_eyes`` notification.
    *escalation_leg* distinguishes WHY in the audit/needs_eyes payload —
    the normal 15-minute exhaustion (default) vs. a forced escalation from
    :func:`_record_candidate_exception` after
    :data:`_MAX_CANDIDATE_EXCEPTIONS` (which passes its own value) — same
    function, same genuine artifact, different recorded cause. Pages
    Sentry (``level="warning"``) gated on the SAME rowcount check as the
    DB write — see module docstring "Sentry activation alert"."""
    result = cast(
        "CursorResult[object]",
        await session.execute(
            _ESCALATE_RETRY_SQL,
            {
                "id": str(candidate.notification_id),
                "attempt": candidate.attempt,
                "new_attempt": new_attempt,
            },
        ),
    )
    if result.rowcount != 1:
        return False

    body_row = (
        (await session.execute(_SELECT_MESSAGE_BODY_SQL, {"message_id": str(candidate.message_id)}))
        .mappings()
        .one_or_none()
    )
    raw_text = body_row["body"] if body_row is not None else None

    needs_eyes_payload = {
        "message_id": str(candidate.message_id),
        "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
        "reasons": ["classification_failed"],
        "leg": escalation_leg,
        "raw_text": raw_text,
    }
    await session.execute(
        _INSERT_NEEDS_EYES_SQL,
        {
            "landlord_id": str(candidate.landlord_id),
            "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
            "payload": json.dumps(needs_eyes_payload),
        },
    )
    await session.execute(
        _INSERT_DEGRADED_MODE_AUDIT_SQL,
        {
            "landlord_id": str(candidate.landlord_id),
            "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
            "payload": json.dumps(
                {
                    "message_id": str(candidate.message_id),
                    "case_id": str(candidate.case_id) if candidate.case_id is not None else None,
                    "reasons": ["classification_failed"],
                    "leg": escalation_leg,
                }
            ),
        },
    )
    # Sentry is not transactional -- fired only after the writes above
    # actually executed on this session, gated on the same rowcount check
    # (`result.rowcount != 1` returned False above) as everything else in
    # this function.
    _alert_degraded_mode_escalation(
        message_id=candidate.message_id, case_id=candidate.case_id, leg=escalation_leg
    )
    return True


async def _record_candidate_exception(
    candidate: DegradedRetryCandidate, exc: Exception
) -> str | None:
    """A single candidate's processing (:func:`_process_candidate`)
    raised — see module docstring "Exception handling never silently
    loops forever". ALWAYS logs + pages Sentry (never a silent tick).
    Returns an outcome string if this call force-escalated the row
    (:data:`_MAX_CANDIDATE_EXCEPTIONS` reached), else ``None`` (logged +
    paged, not yet escalated — the row is untouched so it correctly
    re-selects next tick).

    Wrapped in its OWN try/except: a failure in the exception-handling
    path itself (e.g. the counter UPDATE erroring) must never propagate
    into the sweep loop and abort processing of OTHER due candidates.
    """
    log.error(
        "degraded_mode_sweep_candidate_failed",
        notification_id=str(candidate.notification_id),
        message_id=str(candidate.message_id),
        exc_type=type(exc).__name__,
    )
    sentry_sdk.capture_message(
        "degraded_mode_sweep: candidate processing raised",
        level="error",
        extras={
            "notification_id": str(candidate.notification_id),
            "message_id": str(candidate.message_id),
            "exc_type": type(exc).__name__,
        },
    )
    try:
        async with asynccontextmanager(get_admin_session)() as session:
            count_row = (
                (
                    await session.execute(
                        _INCREMENT_EXCEPTION_COUNT_SQL, {"id": str(candidate.notification_id)}
                    )
                )
                .mappings()
                .one_or_none()
            )
            if count_row is None:
                # Lost race / already resolved by something else this tick
                # -- nothing more to do for this candidate.
                return None
            exception_count = cast("int", count_row["exception_count"])
            if exception_count < _MAX_CANDIDATE_EXCEPTIONS:
                return None

            applied = await _escalate(
                session,
                candidate,
                new_attempt=candidate.attempt + 1,
                escalation_leg="retry_exhausted_exception",
            )
            return _OUTCOME_ESCALATED_BY_EXCEPTION if applied else _OUTCOME_LOST_RACE
    except Exception as inner_exc:
        log.error(
            "degraded_mode_sweep_exception_handling_failed",
            notification_id=str(candidate.notification_id),
            exc_type=type(inner_exc).__name__,
        )
        return None


async def _process_candidate(candidate: DegradedRetryCandidate) -> str:
    """Re-attempt classification for *candidate* — but FIRST, the
    re-animation guard (see module docstring): if the case has moved on,
    supersede and return without ever calling ``run_graph``. Otherwise,
    re-attempt classification (outside any held DB session — never hold a
    pooled connection across the external ``run_graph`` call, same
    convention as ``load_context.py``/``draft_response.py``), then apply
    exactly one of resolve/reschedule/escalate via its own short-lived
    session."""
    async with asynccontextmanager(get_admin_session)() as session:
        moved_on = await _case_has_moved_on(session, candidate)
        if moved_on:
            applied = await _supersede(session, candidate)
            return _OUTCOME_SUPERSEDED if applied else _OUTCOME_LOST_RACE

    final_state = await run_graph(candidate.message_id)
    still_failing = bool(final_state.get("classification_failed"))

    async with asynccontextmanager(get_admin_session)() as session:
        if not still_failing:
            applied = await _resolve(session, candidate)
            return _OUTCOME_RESOLVED if applied else _OUTCOME_LOST_RACE

        new_attempt = candidate.attempt + 1
        next_at = next_retry_at(attempt=new_attempt, failed_at=candidate.failed_at)
        if next_at is not None:
            applied = await _reschedule(
                session, candidate, new_attempt=new_attempt, next_at=next_at
            )
            return _OUTCOME_RESCHEDULED if applied else _OUTCOME_LOST_RACE

        applied = await _escalate(session, candidate, new_attempt=new_attempt)
        return _OUTCOME_ESCALATED if applied else _OUTCOME_LOST_RACE


async def sweep_degraded_mode_retries(*, now: datetime | None = None) -> list[SweepOutcome]:
    """DB entrypoint for one sweep tick. See module docstring "The
    scheduler seam" (nothing calls this today; a future cron/scheduled job
    should — same seam as ``case_lifecycle.sweep_cases`` — and see the
    module docstring's DEPLOYMENT-GATING FACT for why that matters).

    Returns one :class:`SweepOutcome` per due candidate this tick actually
    touched (including a ``lost_race`` outcome for a guard miss — unlike
    ``case_lifecycle.sweep_cases``, which drops lost-race candidates from
    its return value, this function reports them explicitly since a
    caller here may want to observe/log every attempted candidate).

    One candidate's exception (``run_graph`` failure, or any other raise
    inside ``_process_candidate``) is ALWAYS logged AND paged via Sentry,
    and bounded via :func:`_record_candidate_exception` — it can never
    silently abort the tick for every OTHER due message, and a
    persistently-failing candidate force-escalates rather than looping
    forever (see module docstring "Exception handling never silently
    loops forever").
    """
    effective_now = now or datetime.now(UTC)
    outcomes: list[SweepOutcome] = []

    async with asynccontextmanager(get_admin_session)() as session:
        rows = (
            (await session.execute(_SELECT_DUE_CANDIDATES_SQL, {"now": effective_now}))
            .mappings()
            .all()
        )
        candidates = [_candidate_from_row(dict(row)) for row in rows]

    for candidate in candidates:
        try:
            outcome = await _process_candidate(candidate)
        except Exception as exc:
            outcome_or_none = await _record_candidate_exception(candidate, exc)
            if outcome_or_none is None:
                continue
            outcome = outcome_or_none
        log.info(
            "degraded_mode_sweep_candidate_processed",
            notification_id=str(candidate.notification_id),
            message_id=str(candidate.message_id),
            outcome=outcome,
        )
        outcomes.append(
            SweepOutcome(
                notification_id=candidate.notification_id,
                message_id=candidate.message_id,
                outcome=outcome,
            )
        )

    log.info("degraded_mode_sweep_complete", candidates_processed=len(outcomes))
    return outcomes


__all__: list[str] = [
    "DegradedRetryCandidate",
    "SweepOutcome",
    "next_retry_at",
    "sweep_degraded_mode_retries",
]
