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

What one sweep tick does, per due row
---------------------------------------
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
   ``audit_log`` row records the escalation.

Self-guarding WHERE + rowcount-gated side effects (same TOCTOU discipline
as ``app/agent/case_lifecycle.py::_apply_sweep_action``)
------------------------------------------------------------------------
Each of the three outcomes above is applied via an ``UPDATE ... WHERE id
= :id AND status = 'pending' AND attempt = :attempt`` — re-asserting, at
write time, that the row is STILL in the exact state this sweep tick read
it in. No true concurrent sweeper exists yet (nothing schedules this
function at all — see above), so this guard is belt-and-braces today,
not a proven-necessary fix for a reproduced race; it costs nothing and
follows the estabished house style. A lost race (``rowcount != 1``) is a
silent no-op for that candidate — no audit row, no notification — exactly
``case_lifecycle``'s own documented "a lost race is a deliberate silent
no-op" precedent.

Never-break rule #5: only uuids/booleans/short reason or outcome
strings/exception type names ever reach ``log.*`` calls here. The raw
tenant text on the ESCALATED ``needs_eyes`` row's payload is a DB write,
never a log line or Sentry event (same precedent as every other
raw-text-in-a-payload row in this codebase).

Never raises outward from a single candidate's failure
--------------------------------------------------------
``run_graph`` can raise (network errors beyond its own retry budget,
programming-invariant violations). One candidate's exception must never
abort the whole sweep tick for every OTHER due message — each candidate is
processed inside its own try/except, logged and skipped forward on
failure (mirrors ``app/agent/graph_entry.py``'s own "never raise outward"
philosophy, applied per-candidate here instead of per-request).

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
_OUTCOME_LOST_RACE = "lost_race"


@dataclass(frozen=True)
class DegradedRetryCandidate:
    """A read-only, DB-row-shaped snapshot of one due ``degraded_retry``
    notification — decoupled from the DB row type so :func:`next_retry_at`
    stays pure and trivially testable, mirroring
    ``app/agent/case_lifecycle.py``'s ``CaseSnapshot`` convention."""

    notification_id: UUID
    message_id: UUID
    case_id: UUID | None
    landlord_id: UUID
    attempt: int
    failed_at: datetime


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
    return DegradedRetryCandidate(
        notification_id=cast("UUID", row["id"]),
        message_id=message_id,
        case_id=UUID(str(case_id_raw)) if case_id_raw else None,
        landlord_id=cast("UUID", row["landlord_id"]),
        attempt=cast("int", row["attempt"]),
        failed_at=datetime.fromisoformat(str(failed_at_raw)),
    )


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
    session: AsyncSession, candidate: DegradedRetryCandidate, *, new_attempt: int
) -> bool:
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
        "leg": "retry_exhausted",
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
                    "leg": "retry_exhausted",
                }
            ),
        },
    )
    return True


async def _process_candidate(candidate: DegradedRetryCandidate) -> str:
    """Re-attempt classification for *candidate* (outside any held DB
    session — never hold a pooled connection across the external
    ``run_graph`` call, same convention as ``load_context.py``/
    ``draft_response.py``), then apply exactly one of resolve/reschedule/
    escalate via its own short-lived session."""
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
    should — same seam as ``case_lifecycle.sweep_cases``).

    Returns one :class:`SweepOutcome` per due candidate this tick actually
    touched (including a ``lost_race`` outcome for a guard miss — unlike
    ``case_lifecycle.sweep_cases``, which drops lost-race candidates from
    its return value, this function reports them explicitly since a
    caller here may want to observe/log every attempted candidate).

    One candidate's exception (``run_graph`` failure) is logged and
    skipped — it can never abort the tick for every OTHER due message (see
    module docstring "Never raises outward...").
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
            log.error(
                "degraded_mode_sweep_candidate_failed",
                notification_id=str(candidate.notification_id),
                message_id=str(candidate.message_id),
                exc_type=type(exc).__name__,
            )
            continue
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
