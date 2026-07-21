"""Push-notification outbox sweep (#210 M3) — drains ``push_outbox`` rows
enqueued by ``app/agent/nodes/await_approval.py::mark_awaiting_approval``
(schema-v1.md's v1.13 amendments; the enqueue seam itself).

**Push never carries the emergency path** (CLAUDE.md rule #1) — this
module has no relationship whatsoever to
``app/agent/emergency_chain.py``/the ``notifications`` table's escalation
chain; it only ever drains ``push_outbox``, a table the emergency chain
never reads or writes. Push failure is INVISIBLE to the approval flow by
construction: nothing in this module (or its caller,
``app/scheduler.py``) can affect ``cases``/``drafts`` status — a landlord
who never registered a device, or whose push genuinely never arrives,
loses nothing; the dashboard queue and approve-by-SMS remain the source
of truth regardless of what this sweep does.

Sweep shape — same house pattern as ``app/property_provisioning.py::
sweep_pending_number_releases`` (bounded batch, CAS claim BEFORE the
external call, bounded-attempt-count backoff) and
``app/agent/emergency_chain.py``'s ``_CLAIM_STEP_SQL`` (optimistic
concurrency via ``WHERE status = 'pending' AND attempt = :old_attempt``).

Wall-clock tick deadline (safety review HIGH-1)
------------------------------------------------
This sweep shares the SAME single scheduler ticker task
(``app/scheduler.py``) as the emergency chain sweep, which MUST run
promptly every tick. Up to :data:`_PUSH_OUTBOX_SWEEP_BATCH_LIMIT` (50)
candidates, each risking a real network round-trip to Expo, could
otherwise stretch one tick far past 60s — exactly the failure mode
``app/agent/draft_sender.py::sender_tick`` already solved for the draft
sender (its own ``DEFAULT_TICK_DEADLINE_SECONDS``). :func:`run_push_outbox_sweep`
mirrors that fix exactly: a wall-clock budget
(:data:`DEFAULT_TICK_DEADLINE_SECONDS`, computed from an injectable
*time_source* — the real event loop clock in production, a fake,
advanceable one in tests) checked BEFORE claiming each candidate. Once
exceeded, this sweep stops CLAIMING new rows for the rest of the tick — a
row already claimed and mid-send always finishes (never abandoned
mid-flight); any leftover due rows simply stay ``'pending'`` and due,
claimed whole by the very next tick. Nothing is lost, and — the actual
point of this fix — the emergency chain sweep's NEXT run is never
meaningfully delayed by a slow/hanging Expo backend. Push, like the draft
sender, is therefore wall-clock-bounded specifically so it can never
starve the emergency sweep.

Expo error-handling matrix
--------------------------
Per claimed row, exactly one of:

- **Device already revoked** (``push_tokens.revoked_at`` was set — by an
  EARLIER sweep tick, since enqueue — before this row was ever sent):
  skip the Expo call entirely, mark this row ``'failed'`` (terminal —
  nothing will ever make a dead token live again on its own).
- **Device reassigned to a different landlord** (safety review MEDIUM-1
  — see "Ownership-transfer safety" below): skip the Expo call entirely,
  mark this row ``'failed'`` (terminal — the row's own landlord no longer
  owns this device; nothing to deliver, ever).
- **Expo reports ``DeviceNotRegistered``** (the per-receipt error code for
  a permanently dead token): mark ``push_tokens.revoked_at = now()`` +
  this row ``'failed'`` (TERMINAL for this cause — deliberately UNLIKE
  ``app/agent/emergency_chain.py``'s ``tenant_ack``/``emergency_sms``
  convention, where ``'failed'`` is transient-and-retried; here
  ``'failed'`` specifically means "we now know this token is dead,
  retrying is pointless").
- **Any other failure** (a transport-level exception — timeout,
  connection error, malformed Expo response — or an Expo-reported ticket
  error with a different/no error code): TRANSIENT. Reschedule via the
  bounded backoff (:data:`_PUSH_MAX_ATTEMPTS` attempts,
  :data:`_PUSH_RETRY_INTERVAL_SECONDS` apart, mirroring
  ``sweep_pending_number_releases``'s own shape) until the bound is
  reached, at which point the row is marked ``'exhausted'``.
- **Success** (``status == "ok"``): mark ``'sent'``.

**Deliberate divergence from every other sweep in this codebase: no
Sentry page on exhaustion.** ``app/property_provisioning.py``'s own
release-exhaustion and ``app/agent/degraded_mode_sweep.py``'s escalation
both page Sentry — those are landlord-facing failures with no other
recovery path. Push exhaustion is NOT: it is best-effort by design (see
module docstring's first paragraph), and the landlord already has the
dashboard queue / approve-by-SMS regardless of whether their phone ever
buzzed. A single ``log.info`` line is the whole signal; paging on-call
for a missed best-effort nudge would be noise, not a real incident.

Ownership-transfer safety (safety review MEDIUM-1)
----------------------------------------------------
A shared device (landlord A signs out, landlord B signs into the SAME
physical device/app install) moves a ``push_tokens`` row from A to B via
``POST /v1/devices``'s upsert (``app/routers/devices.py``'s "Token
ownership model"). A ``push_outbox`` row enqueued for A BEFORE that
transfer still carries ``landlord_id = A`` and still references the SAME
``device_token_id`` — which now belongs to B. Two independent guards,
never relying on the ``device_token_id`` join alone (the same
explicit-predicate discipline ``app/routers/devices.py`` already
documents for its own cross-tenant queries):

1. :data:`_SELECT_DUE_PUSH_OUTBOX_SQL` joins ``push_tokens`` with
   ``AND pt.landlord_id = po.landlord_id`` — an orphaned row can never
   even be SELECTED for an actual send attempt, regardless of anything
   else in this module.
2. :func:`run_push_outbox_sweep` ALSO runs
   :data:`_MARK_ORPHANED_ROWS_SQL` once per tick, a bulk ``UPDATE`` that
   terminally fails (``'failed'``, ``payload.terminal_reason =
   'device_reassigned'``) every ``'pending'`` row whose device's CURRENT
   owner no longer matches the row's own ``landlord_id`` — otherwise an
   orphaned row would simply never be selected by guard 1 above and would
   sit ``'pending'`` forever, invisible and unresolved. This is a plain,
   non-CAS bulk statement (safe: it only ever transitions
   ``'pending'`` -> ``'failed'``, so a race with the per-row claim path
   just means whichever gets there first wins, never a double-write).

Payload safety (rule #5-adjacent)
----------------------------------
:func:`_build_message` reads ONLY the ``case_id``/``draft_id`` keys out of
``push_outbox.payload`` — never the whole payload blindly — and the push
notification's own ``title``/``body`` are FIXED, generic strings, never
derived from a tenant message/name. This is what makes it structurally
impossible for a future ``payload`` shape change to leak PII into a push
notification that transits Apple/Google's servers.

DB access
---------
Admin engine (background/scheduled-job context, no request/landlord JWT —
same rationale as ``app/property_provisioning.py``/
``app/agent/degraded_mode_sweep.py``). Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager as _acm
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_admin_session
from app.integrations.expo_push import (
    DEVICE_NOT_REGISTERED_ERROR_CODE,
    ExpoPushMessage,
    get_expo_push_sender,
)

log = structlog.get_logger(__name__)

_PUSH_OUTBOX_SWEEP_BATCH_LIMIT: int = 50
"""Per-tick cap on how many due rows one sweep call processes — mirrors
``app/property_provisioning.py``'s own ``_NUMBER_RELEASE_SWEEP_BATCH_
LIMIT`` (same "bounded work per tick" discipline); anything left over is
still due (or becomes more overdue) and is picked up on the NEXT tick."""

_PUSH_MAX_ATTEMPTS: int = 5
"""After this many failed attempts, a row moves to 'exhausted' — same
bounded-retry shape as ``app/property_provisioning.py``'s
``_NUMBER_RELEASE_MAX_ATTEMPTS``."""

_PUSH_RETRY_INTERVAL_SECONDS: float = 5 * 60
"""Fixed retry interval between attempts — shorter than
``_NUMBER_RELEASE_RETRY_INTERVAL_SECONDS`` (15 min) because a push nudge
is only useful while it's still fresh; the queue/SMS surfaces are the
durable source of truth regardless (see module docstring)."""

DEFAULT_TICK_DEADLINE_SECONDS: float = 25.0
"""Wall-clock budget for one :func:`run_push_outbox_sweep` call (safety
review HIGH-1 — see module docstring "Wall-clock tick deadline"). Same
value as ``app/agent/draft_sender.py``'s own ``DEFAULT_TICK_DEADLINE_
SECONDS``, for the identical reason: this sweep shares the single
scheduler ticker task with the emergency chain sweep, which must run
promptly every tick. Up to :data:`_PUSH_OUTBOX_SWEEP_BATCH_LIMIT` (50)
candidates, each risking Expo's own timeout
(``app/integrations/expo_push.py``'s ``_HTTP_TIMEOUT_SECONDS``, 5s),
could otherwise push a single tick to ~250s. Once exceeded,
:func:`run_push_outbox_sweep` stops CLAIMING new rows for the rest of
that tick — a row already claimed and mid-send always finishes; any
leftover due rows simply remain ``'pending'`` and due, picked up whole by
the very next tick. Nothing is lost."""


def _default_time_source() -> float:
    """The real, monotonic clock this sweep budgets its wall-clock
    deadline against — mirrors ``app/agent/draft_sender.py``'s own
    ``_default_time_source`` exactly (``asyncio.get_running_loop().time()``).
    Injectable so tests can advance a fake clock deterministically instead
    of sleeping for real seconds."""
    return asyncio.get_running_loop().time()


_PUSH_TITLE: str = "Stoop"
_PUSH_BODY_DRAFT_AWAITING_APPROVAL: str = "A reply is waiting for your approval."
"""FIXED, generic, landlord-facing copy — NEVER derived from a tenant
message/name (schema-v1.md's v1.13 amendments; see module docstring
"Payload safety"). Plain English, no jargon (CLAUDE.md rule #8)."""

_OUTCOME_LOST_RACE = "lost_race"
_OUTCOME_FAILED_REVOKED_DEVICE = "failed_revoked_device"
_OUTCOME_SENT = "sent"
_OUTCOME_FAILED_DEVICE_NOT_REGISTERED = "failed_device_not_registered"
_OUTCOME_FAILED_DEVICE_REASSIGNED = "failed_device_reassigned"
_OUTCOME_RESCHEDULED = "rescheduled"
_OUTCOME_EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class PushOutboxOutcome:
    """One claimed row's outcome from a sweep tick — test/observability
    seam, mirrors ``app/agent/emergency_chain.py``'s
    ``EmergencyChainOutcome``."""

    outbox_id: UUID
    outcome: str


# Safety review MEDIUM-1 -- see module docstring "Ownership-transfer
# safety" guard 1: `AND pt.landlord_id = po.landlord_id` means an orphaned
# row (its device reassigned to a different landlord since enqueue) can
# NEVER be selected here, regardless of anything else in this module.
_SELECT_DUE_PUSH_OUTBOX_SQL = text(
    """
    SELECT po.id, po.landlord_id, po.device_token_id, po.kind, po.payload, po.attempt,
           pt.token AS device_token, pt.revoked_at AS device_revoked_at
    FROM push_outbox po
    JOIN push_tokens pt ON pt.id = po.device_token_id AND pt.landlord_id = po.landlord_id
    WHERE po.status = 'pending' AND po.next_attempt_at <= :now
    ORDER BY po.next_attempt_at ASC
    LIMIT :limit
    """
)

# Safety review MEDIUM-1, guard 2 -- see module docstring "Ownership
# -transfer safety". A plain bulk UPDATE, not CAS-guarded: it only ever
# transitions 'pending' -> 'failed', so a race with the per-row claim
# path (_CLAIM_PUSH_OUTBOX_SQL) just means whichever gets there first
# wins -- never a double-write, never needs its own attempt-based guard.
_MARK_ORPHANED_ROWS_SQL = text(
    """
    UPDATE push_outbox po
    SET status = 'failed', updated_at = now(),
        payload = payload || '{"terminal_reason": "device_reassigned"}'::jsonb
    WHERE po.status = 'pending'
      AND EXISTS (
        SELECT 1 FROM push_tokens pt
        WHERE pt.id = po.device_token_id AND pt.landlord_id != po.landlord_id
      )
    RETURNING po.id
    """
)

# Optimistic-concurrency claim -- mirrors app/property_provisioning.py's
# _CLAIM_NUMBER_RELEASE_SQL / app/agent/emergency_chain.py's
# _CLAIM_STEP_SQL exactly: advances attempt/next_attempt_at BEFORE the
# Expo call, guarded by a WHERE clause that only succeeds for the FIRST
# caller to claim this exact (id, attempt) pair.
_CLAIM_PUSH_OUTBOX_SQL = text(
    """
    UPDATE push_outbox
    SET attempt = :new_attempt, next_attempt_at = :next_attempt_at, updated_at = now()
    WHERE id = :id AND status = 'pending' AND attempt = :old_attempt
    RETURNING id
    """
)

_MARK_SENT_SQL = text("UPDATE push_outbox SET status = 'sent', updated_at = now() WHERE id = :id")

_MARK_FAILED_SQL = text(
    "UPDATE push_outbox SET status = 'failed', updated_at = now() WHERE id = :id"
)

_MARK_EXHAUSTED_SQL = text(
    "UPDATE push_outbox SET status = 'exhausted', updated_at = now() WHERE id = :id"
)

_REVOKE_DEVICE_TOKEN_SQL = text(
    "UPDATE push_tokens SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"
)


def _build_message(*, kind: str, payload: dict[str, Any], token: str) -> ExpoPushMessage:
    """Pure: the Expo message for one claimed row. Reads ONLY the
    ``case_id``/``draft_id`` keys out of *payload* — never the whole
    payload blindly — and ``title``/``body`` are fixed, generic copy. See
    module docstring "Payload safety"."""
    data: dict[str, str] = {"kind": kind}
    case_id = payload.get("case_id")
    if case_id:
        data["case_id"] = str(case_id)
    draft_id = payload.get("draft_id")
    if draft_id:
        data["draft_id"] = str(draft_id)
    return ExpoPushMessage(
        to=token,
        title=_PUSH_TITLE,
        body=_PUSH_BODY_DRAFT_AWAITING_APPROVAL,
        data=data,
    )


async def _mark_orphaned_rows() -> list[PushOutboxOutcome]:
    """Safety review MEDIUM-1, guard 2 — see module docstring "Ownership
    -transfer safety". Runs once at the start of every sweep tick, before
    the main due-rows SELECT; a plain bulk UPDATE, never CAS-guarded (see
    :data:`_MARK_ORPHANED_ROWS_SQL`'s own comment)."""
    async with _acm(get_admin_session)() as session:
        rows = (await session.execute(_MARK_ORPHANED_ROWS_SQL)).mappings().all()
    outcomes = [
        PushOutboxOutcome(
            outbox_id=cast("UUID", row["id"]), outcome=_OUTCOME_FAILED_DEVICE_REASSIGNED
        )
        for row in rows
    ]
    for outcome in outcomes:
        log.info("push_outbox_device_reassigned", outbox_id=str(outcome.outbox_id))
    return outcomes


async def _claim(
    session: AsyncSession, *, outbox_id: UUID, old_attempt: int, next_attempt_at: datetime
) -> bool:
    claim_row = (
        (
            await session.execute(
                _CLAIM_PUSH_OUTBOX_SQL,
                {
                    "id": str(outbox_id),
                    "old_attempt": old_attempt,
                    "new_attempt": old_attempt + 1,
                    "next_attempt_at": next_attempt_at,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return claim_row is not None


async def _process_row(row: dict[str, Any], *, effective_now: datetime) -> PushOutboxOutcome:
    outbox_id = cast("UUID", row["id"])
    device_token_id = cast("UUID", row["device_token_id"])
    old_attempt = cast("int", row["attempt"])
    is_last_attempt = old_attempt + 1 >= _PUSH_MAX_ATTEMPTS
    next_attempt_at = effective_now + timedelta(seconds=_PUSH_RETRY_INTERVAL_SECONDS)

    async with _acm(get_admin_session)() as session:
        claimed = await _claim(
            session, outbox_id=outbox_id, old_attempt=old_attempt, next_attempt_at=next_attempt_at
        )
    if not claimed:
        return PushOutboxOutcome(outbox_id=outbox_id, outcome=_OUTCOME_LOST_RACE)

    if row["device_revoked_at"] is not None:
        # The device was revoked (by an earlier tick) since this row was
        # enqueued -- nothing to send to; terminal, not retryable.
        async with _acm(get_admin_session)() as session:
            await session.execute(_MARK_FAILED_SQL, {"id": str(outbox_id)})
        log.info("push_outbox_skipped_revoked_device", outbox_id=str(outbox_id))
        return PushOutboxOutcome(outbox_id=outbox_id, outcome=_OUTCOME_FAILED_REVOKED_DEVICE)

    message = _build_message(
        kind=cast("str", row["kind"]),
        payload=cast("dict[str, Any]", row["payload"]) or {},
        token=cast("str", row["device_token"]),
    )
    sender = get_expo_push_sender()

    try:
        ticket = await sender.send_push(message)
    except Exception as exc:
        return await _handle_transient_failure(
            outbox_id, exc_type=type(exc).__name__, is_last_attempt=is_last_attempt
        )

    if ticket.status == "ok":
        async with _acm(get_admin_session)() as session:
            await session.execute(_MARK_SENT_SQL, {"id": str(outbox_id)})
        log.info("push_outbox_sent", outbox_id=str(outbox_id))
        return PushOutboxOutcome(outbox_id=outbox_id, outcome=_OUTCOME_SENT)

    if ticket.error_code == DEVICE_NOT_REGISTERED_ERROR_CODE:
        async with _acm(get_admin_session)() as session:
            await session.execute(_REVOKE_DEVICE_TOKEN_SQL, {"id": str(device_token_id)})
            await session.execute(_MARK_FAILED_SQL, {"id": str(outbox_id)})
        log.info(
            "push_outbox_device_not_registered",
            outbox_id=str(outbox_id),
            device_token_id=str(device_token_id),
        )
        return PushOutboxOutcome(outbox_id=outbox_id, outcome=_OUTCOME_FAILED_DEVICE_NOT_REGISTERED)

    return await _handle_transient_failure(
        outbox_id,
        exc_type=ticket.error_code or "unknown_expo_error",
        is_last_attempt=is_last_attempt,
    )


async def _handle_transient_failure(
    outbox_id: UUID, *, exc_type: str, is_last_attempt: bool
) -> PushOutboxOutcome:
    """A transport-level exception, or an Expo ticket error with no
    recognized permanent cause -- see module docstring "Expo error
    -handling matrix". The CAS claim already advanced attempt/
    next_attempt_at (or marked this the last try); only the terminal
    'exhausted' transition needs its own write here."""
    if is_last_attempt:
        async with _acm(get_admin_session)() as session:
            await session.execute(_MARK_EXHAUSTED_SQL, {"id": str(outbox_id)})
        # Deliberately NO Sentry page -- see module docstring "Deliberate
        # divergence".
        log.info("push_outbox_exhausted", outbox_id=str(outbox_id), reason=exc_type)
        return PushOutboxOutcome(outbox_id=outbox_id, outcome=_OUTCOME_EXHAUSTED)

    log.info("push_outbox_send_failed_will_retry", outbox_id=str(outbox_id), reason=exc_type)
    return PushOutboxOutcome(outbox_id=outbox_id, outcome=_OUTCOME_RESCHEDULED)


async def run_push_outbox_sweep(
    *,
    now: datetime | None = None,
    deadline_seconds: float = DEFAULT_TICK_DEADLINE_SECONDS,
    time_source: Callable[[], float] = _default_time_source,
) -> list[PushOutboxOutcome]:
    """DB entrypoint for one sweep tick — called by ``app/scheduler.py``'s
    60s ticker, LAST (after ``number_release``). See module docstring for
    the full error-handling matrix, the "push never carries the emergency
    path" invariant, and the wall-clock deadline (safety review HIGH-1)
    this function bounds its own worst-case duration against.

    ``now`` is an injectable override purely for tests (mirrors
    ``run_emergency_chain_sweep(*, now=...)``/
    ``sweep_pending_number_releases(*, now=...)``) — the DB-side "what's
    due" clock, unrelated to *time_source* below.

    *time_source* defaults to the real event loop clock
    (:func:`_default_time_source`, mirroring
    ``app/agent/draft_sender.py::sender_tick``'s own seam) — tests inject
    a fake, monotonically-advanceable callable instead of sleeping for
    real seconds. *deadline_seconds* is the wall-clock budget checked
    against it before claiming each candidate (see
    :data:`DEFAULT_TICK_DEADLINE_SECONDS`).
    """
    effective_now = now if now is not None else datetime.now(UTC)

    outcomes: list[PushOutboxOutcome] = await _mark_orphaned_rows()

    async with _acm(get_admin_session)() as session:
        rows = (
            (
                await session.execute(
                    _SELECT_DUE_PUSH_OUTBOX_SQL,
                    {"now": effective_now, "limit": _PUSH_OUTBOX_SWEEP_BATCH_LIMIT},
                )
            )
            .mappings()
            .all()
        )

    start = time_source()
    claimed_this_tick = 0
    for index, row in enumerate(rows):
        if time_source() - start >= deadline_seconds:
            # Wall-clock budget exceeded (safety review HIGH-1) -- stop
            # CLAIMING new rows for the rest of this tick. See module
            # docstring "Wall-clock tick deadline".
            log.info(
                "push_outbox_sweep_tick_deadline_reached",
                claimed_this_tick=claimed_this_tick,
                remaining_candidates=len(rows) - index,
            )
            break
        outcome = await _process_row(dict(row), effective_now=effective_now)
        outcomes.append(outcome)
        if outcome.outcome != _OUTCOME_LOST_RACE:
            claimed_this_tick += 1

    log.info("push_outbox_sweep_complete", candidates_processed=len(outcomes))
    return outcomes


__all__: list[str] = ["DEFAULT_TICK_DEADLINE_SECONDS", "PushOutboxOutcome", "run_push_outbox_sweep"]
