"""``unrouted_inbound`` retention + operator reconciliation digest (#231,
follow-up from #170/PR #230 — safety-reviewer ADVISORY-2 + senior review).

Module home
-----------
A new, standalone module at the ``app/`` root — the SAME tier as
``app/push_outbox.py``/``app/property_provisioning.py`` (scheduler
-invoked, admin-session, periodic-maintenance modules that are neither
graph nodes nor HTTP route handlers). NOT ``app/agent/`` — this has
nothing to do with the LangGraph state machine, and CLAUDE.md's "no
feature-flag reads in agent/, prefilter, or notifications modules" rule
signals ``agent/`` is reserved for classification/drafting/escalation
logic, not table janitor work. NOT added to
``app/routers/webhooks/twilio.py`` (where ``_dead_letter_unrouted_inbound``
lives) either, even though the issue suggested "near the webhook's
dead-letter helpers" as one option: that file is already 1400+ lines of
webhook REQUEST HANDLING (signature verification, message routing,
idempotent side effects) — a scheduled sweep with its own deadline/digest
concerns is a different axis of change entirely, and mixing it in would
make an already-large router file harder to review for its actual job.
This module only ever READS the dead-letter row shape
(``schema-v1.md``'s v1.17 amendment / migration 0015) that the webhook
writes; it has no other coupling to it.

Two jobs, one scheduler entrypoint (:func:`run_unrouted_maintenance_sweep`)
-----------------------------------------------------------------------------
1. **Retention** (:func:`_run_retention_sweep`) — deletes ``unrouted_inbound``
   rows that are BOTH resolved AND old: ``resolved_at IS NOT NULL AND
   resolved_at < :cutoff`` where ``cutoff = now - 30 days``. Bounded to
   :data:`_RETENTION_SWEEP_BATCH_LIMIT` (500) rows per DELETE statement,
   looping (re-checking the wall-clock deadline before each additional
   batch) only if a batch comes back completely full — see "Deadline
   discipline" below.

   **DELIBERATELY NARROWER than the issue's literal wording** ("resolved
   ... and very old unresolved-but-alerted rows"). This module deletes
   ONLY resolved rows. An unresolved ``unrouted_inbound`` row is never
   auto-deleted, regardless of its age — that row IS the durable record of
   a Tier-0-relevant message a human has not yet adjudicated (schema-v1.md
   v1.17: "resolved_at ... NULL = not yet reconciled by an operator").
   Auto-deleting an unresolved row — even a very old one — would silently
   destroy the one artifact this entire feature (#170) exists to make
   durable, exactly the "recoverable only via the Twilio console" failure
   mode #170 closed. If an operator genuinely wants to discard old
   unresolved noise (e.g. a permanently misconfigured `To` from a
   decommissioned test number), that is a deliberate, manual, admin-engine
   operation — never something a scheduled sweep decides on its own. This
   narrowing is intentional, not an oversight; flag it in review if it
   should be revisited.

   **Index finding (checked before writing this module, per the task's own
   instruction to check first and STOP rather than invent one): migration
   0015 created NO index beyond the implicit ones PostgreSQL derives from
   the table's own constraints** — the `id` primary key and the plain
   `twilio_sid UNIQUE` column. There is no index on `resolved_at` (or
   `received_at`). The retention DELETE's `WHERE resolved_at IS NOT NULL
   AND resolved_at < :cutoff` (and the digest's `WHERE resolved_at IS NULL
   AND received_at < :grace_cutoff` below) are therefore sequential scans,
   not index scans — bounded per statement by the `LIMIT`/aggregate itself,
   never by an index. **No new index was added.** Per the task's explicit
   instruction, a new index means schema-doc-first (``docs/03-engineering/
   schema-v1.md``) + its own migration, and that decision belongs to a
   human, not this PR. This is accepted here because `unrouted_inbound` is,
   by construction, a LOW-VOLUME table — it only grows when a validly
   -signed Twilio request arrives for a `To` number that matches no
   `properties.twilio_number` at all (a misconfiguration/format-mismatch
   edge case, not routine traffic; see schema-v1.md's v1.17 amendments,
   point 5, "a format-mismatch drop either way"), and this same retention
   sweep keeps resolved rows from accumulating indefinitely. If real
   production volume ever makes a full scan here measurable, the fix is a
   follow-up migration adding a partial index (e.g. `ON unrouted_inbound
   (resolved_at) WHERE resolved_at IS NOT NULL`, and separately for
   `received_at WHERE resolved_at IS NULL`) — flagged here for the record,
   not applied unilaterally.

2. **Operator digest** (:func:`_maybe_fire_digest`) — a once-per-UTC-day
   Sentry WARNING (`capture_message`, mirroring
   ``app/agent/degraded_mode_sweep.py``'s own `level="warning"` activation
   -alert convention) summarizing UNRESOLVED rows: count + the age (in
   hours) of the oldest one. This is the "operator reconciliation surface"
   half of #231 — today an aging unresolved unrouted emergency is visible
   only via the original loud Sentry error at ingestion time (`app/
   routers/webhooks/twilio.py::_alert_unrouted_possible_emergency`/
   `_alert_unknown_to`) and the durable row itself; nothing periodically
   reminds an operator that a row is STILL sitting unresolved. Fires only
   when:
   - at least one unresolved row exists whose `received_at` is more than
     :data:`_DIGEST_GRACE_SECONDS` (1 hour) old — the grace period exists
     so a message that just arrived (the operator may already be mid
     -reconciliation) doesn't immediately trip the digest; and
   - the digest has not already fired for today's UTC calendar day (see
     "Dedupe stamp" below).
   Durations/counts ONLY, per rule #5 — never `to_number`/`from_number`,
   never `twilio_sid`, never any payload content.

   Dedupe stamp is only advanced when a digest actually fires (count > 0)
   — a zero-count tick does NOT consume the day, so a later same-day tick
   (once a row crosses the 1h grace) can still page.

Deadline discipline (shared with every other sweep in this codebase)
----------------------------------------------------------------------
This sweep shares the SAME single scheduler ticker task
(``app/scheduler.py``) as the emergency chain sweep, which must run
promptly every tick — same rationale as
``app/push_outbox.py::run_push_outbox_sweep``/
``app/agent/draft_sender.py::sender_tick``. Unlike those two, a single
retention DELETE batch here is cheap (one bounded SQL statement, no
per-row network call) — the deadline only matters if the number of
eligible-for-deletion rows in one tick exceeds
:data:`_RETENTION_SWEEP_BATCH_LIMIT` (500), which requires looping for a
second (or further) batch. :func:`_run_retention_sweep` checks
:data:`DEFAULT_TICK_DEADLINE_SECONDS` (25s, the same house value every
other bounded sweep here uses) via an injectable *time_source* BEFORE
claiming each additional batch; once exceeded it simply stops for this
tick — any remaining eligible rows are still resolved-and-old (or become
more so) and are picked up by the very next tick. Nothing is lost, only
deferred — this cleanup work must never delay the emergency sweep's next
run. The digest's own query is a single cheap aggregate (`count(*)`/
`min(...)`), never batched, so it carries no deadline check of its own.

Dedupe consequence, stated honestly (per the task's instruction)
--------------------------------------------------------------------
Deleting a resolved `unrouted_inbound` row reopens its `twilio_sid` for
`ON CONFLICT (twilio_sid) DO NOTHING` purposes (the webhook's own dead
-letter insert, `app/routers/webhooks/twilio.py::
_dead_letter_unrouted_inbound`) — a genuinely-redelivered `MessageSid` for
a message whose original dead-letter row this sweep already deleted would
create a FRESH row (not a silent no-op) and, if that redelivery is a
Tier-0 HARD hit, a fresh page too. Twilio's own redelivery/retry horizon is
approximately 72 hours (per Twilio's documented webhook retry policy) —
essentially impossible against a 30-day retention window. This is
fail-loud (a fresh page beats silent data loss) and accepted, not a bug.

Digest dedupe stamp — in-memory, not durable (accepted, documented)
------------------------------------------------------------------------
:class:`_DigestState` is a bare in-process module singleton (the "house
singleton pattern": mirrors ``app/integrations/weather.py``'s
``_WeatherCacheState``/``app/integrations/supabase_auth.py``'s
``_JwksState`` — one object, one ``reset_for_tests()``, reset by an
autouse ``tests/conftest.py`` fixture). There is deliberately NO durable
home for this stamp: `audit_log` and `notifications` both require a
NOT NULL `landlord_id` (schema-v1.md), and `unrouted_inbound` itself has
none — inventing a landlord-less row in either table, or adding a column
to `unrouted_inbound` just to hold a daily stamp, is exactly the kind of
unilateral schema/scope creep the task explicitly forbids ("do NOT add
columns"). Consequence, accepted: a process restart (a Fly deploy, a crash
-restart) loses the in-memory stamp, so the digest MAY re-page once on the
same UTC day it would otherwise have already fired. That is the same
class of "at most a harmless extra page, never a missed one" tradeoff
`app/scheduler.py`'s own crash-safety doctrine already accepts for every
other sweep's schedule state — the underlying `unrouted_inbound` data
itself is never at risk either way, only whether today's digest fires
once or (rarely) twice.

DB access
---------
Admin engine (``get_admin_session``) throughout — this is background
/scheduled-job context, no request/landlord JWT, exactly the same
rationale ``app/push_outbox.py``/``app/property_provisioning.py`` document
for themselves, and `unrouted_inbound` grants `app_role` NOTHING at all
(schema-v1.md v1.17 amendments, point 2) — only the admin engine can reach
this table at all. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.

No Twilio send anywhere in this module — it only reads/deletes
`unrouted_inbound` rows and pages Sentry; it is not a new outbound-send
call site.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager as _acm
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import sentry_sdk
import structlog
from sqlalchemy import text

from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

_RETENTION_DAYS: int = 30
"""How long a RESOLVED row is kept after `resolved_at` before it becomes
eligible for deletion — see module docstring point 1."""

_RETENTION_SWEEP_BATCH_LIMIT: int = 500
"""Per-DELETE-statement cap, mirroring the house "bounded work per tick"
discipline (``app/push_outbox.py``'s `_PUSH_OUTBOX_SWEEP_BATCH_LIMIT`/
``app/property_provisioning.py``'s `_NUMBER_RELEASE_SWEEP_BATCH_LIMIT`,
both 50 — this one is larger because a DELETE has no per-row network call
to bound against, only statement/lock duration)."""

DEFAULT_TICK_DEADLINE_SECONDS: float = 25.0
"""Wall-clock budget for one :func:`run_unrouted_maintenance_sweep` call —
same house value as every other bounded sweep (`app/push_outbox.py`,
`app/agent/draft_sender.py`). See module docstring "Deadline discipline"."""

# Bounded-batch delete: Postgres has no `DELETE ... LIMIT`, so the LIMIT is
# applied via the inner SELECT and the outer DELETE targets exactly those
# ids. `ORDER BY resolved_at ASC` is not required for correctness (any
# eligible row is equally safe to delete) but makes each tick's behavior
# deterministic (oldest-resolved-first) and easy to test.
#
# `FOR UPDATE SKIP LOCKED` + the repeated predicate on the OUTER delete are
# BOTH load-bearing (#231 safety review, BLOCKING-1/-2 — each empirically
# reproduced against a live second session before the fix):
#
# - Without them, the inner id-set is frozen under READ COMMITTED before
#   any row lock is taken, and EvalPlanQual re-checks only `id = ...` on a
#   concurrently-updated tuple — so an operator's in-flight
#   `UPDATE ... SET resolved_at = NULL` (an un-resolve, run exactly the
#   way schema-v1.md prescribes reconciliation: a one-off psql statement)
#   COMMITTED mid-delete was overwritten and the evidence row deleted
#   anyway, with the operator having seen a successful `UPDATE 1`.
# - `SKIP LOCKED` additionally means the sweep never QUEUES behind a row a
#   human is holding open in an interactive transaction — without it, one
#   idle-in-transaction psql session stalled the DELETE (and with it the
#   whole ticker, emergency sweep included) for as long as the terminal
#   sat open. A skipped row is simply picked up on a later tick once the
#   operator's transaction ends.
# - The outer `resolved_at` re-check is the house self-guarding-write
#   doctrine applied to this module's one mutating statement: the rows are
#   locked by the inner SELECT so it cannot fire today, but it makes the
#   statement safe by construction rather than by planner behavior.
_DELETE_RETENTION_BATCH_SQL = text(
    """
    DELETE FROM unrouted_inbound
    WHERE id IN (
        SELECT id FROM unrouted_inbound
        WHERE resolved_at IS NOT NULL AND resolved_at < :cutoff
        ORDER BY resolved_at ASC
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    AND resolved_at IS NOT NULL AND resolved_at < :cutoff
    RETURNING id
    """
)

# Belt-and-braces bound on lock waits inside the retention DELETE (#231
# safety review, BLOCKING-2): SKIP LOCKED above already steps over
# operator-held rows, but any OTHER lock this statement could ever wait on
# fails fast instead of stalling the shared ticker. Transaction-scoped
# (SET LOCAL), same precedent as app/agent/graph.py's advisory-lock
# session. 2s is generous against sub-second app transactions and
# irrelevant to correctness — a timed-out batch simply retries next tick.
_SET_RETENTION_LOCK_TIMEOUT_SQL = text("SET LOCAL lock_timeout = '2s'")


def _retention_cutoff(now: datetime) -> datetime:
    """Pure boundary function — a resolved row is eligible for deletion
    once `resolved_at` is STRICTLY EARLIER than `now - 30 days` (exclusive
    boundary, matching the issue's own `resolved_at < :now - interval '30
    days'` verbatim). A row resolved EXACTLY 30 days ago (to the
    microsecond) as of `now` is retained for at least one more tick; it
    becomes eligible the instant `resolved_at` is strictly less than this
    cutoff."""
    return now - timedelta(days=_RETENTION_DAYS)


def _default_time_source() -> float:
    """The real, monotonic clock this sweep budgets its wall-clock
    deadline against — mirrors ``app/push_outbox.py``'s/``app/agent/
    draft_sender.py``'s own `_default_time_source` exactly
    (`asyncio.get_running_loop().time()`). Injectable so tests can control
    elapsed time deterministically instead of sleeping for real seconds."""
    return asyncio.get_running_loop().time()


_DELETED_IDS_SAMPLE_CAP: int = 1000
"""Upper bound on how many deleted-row UUIDs one tick's outcome retains
(#231 safety review ADVISORY-5) — comfortably above every test's seeding
volume, comfortably below a megabyte even at backlog scale. The count is
always exact regardless."""


async def _run_retention_sweep(
    *,
    now: datetime,
    deadline_seconds: float,
    time_source: Callable[[], float],
    tick_start: float,
    batch_limit: int = _RETENTION_SWEEP_BATCH_LIMIT,
) -> tuple[int, list[UUID]]:
    """Delete every resolved-and-old row, in bounded batches (see module
    docstring "Deadline discipline"). Returns ``(true total deleted, a
    bounded sample of their ids)`` — the sample is the test/observability
    seam; production callers only log the count.

    NOTE on the retention clock (#231 safety review ADVISORY-1): the
    predicate keys on the ``resolved_at`` VALUE, not on when the resolving
    UPDATE actually ran — an operator who backfills ``resolved_at =
    received_at`` on rows older than 30 days makes them eligible on the
    very next tick, with no grace window. There is no
    "resolution-action timestamp" column to key on (adding one is a
    schema-doc-first decision this module refuses to make unilaterally),
    so the operator guidance is: resolve with ``resolved_at = now()``
    unless immediate cleanup is exactly what you want. Recorded in
    schema-v1.md's retention amendment as well.

    ``batch_limit`` defaults to :data:`_RETENTION_SWEEP_BATCH_LIMIT` (500)
    in production; :func:`run_unrouted_maintenance_sweep` exposes it as an
    injectable test seam (mirrors ``app/agent/draft_sender.py::
    sender_tick``'s own ``batch_size`` parameter) so a test can force the
    multi-batch-plus-deadline path without seeding hundreds of rows.
    """
    cutoff = _retention_cutoff(now)
    deleted_count = 0
    deleted: list[UUID] = []

    while True:
        if time_source() - tick_start >= deadline_seconds:
            log.info(
                "unrouted_retention_sweep_tick_deadline_reached",
                deleted_so_far=deleted_count,
            )
            break

        async with _acm(get_admin_session)() as session:
            await session.execute(_SET_RETENTION_LOCK_TIMEOUT_SQL)
            rows = (
                (
                    await session.execute(
                        _DELETE_RETENTION_BATCH_SQL,
                        {"cutoff": cutoff, "limit": batch_limit},
                    )
                )
                .mappings()
                .all()
            )

        batch_ids = [cast("UUID", row["id"]) for row in rows]
        deleted_count += len(batch_ids)
        if len(deleted) < _DELETED_IDS_SAMPLE_CAP:
            deleted.extend(batch_ids[: _DELETED_IDS_SAMPLE_CAP - len(deleted)])

        if len(batch_ids) < batch_limit:
            # Fewer than a full batch came back -- nothing more is due
            # this tick, no need to loop again (or check the deadline
            # again). NOTE: rows skipped by SKIP LOCKED (an operator
            # holding them open) also present as a short batch — they are
            # deliberately left for a later tick, never waited on.
            break

    return deleted_count, deleted


# ---------------------------------------------------------------------------
# Operator digest
# ---------------------------------------------------------------------------

_DIGEST_GRACE_SECONDS: float = 60 * 60
"""1 hour -- see module docstring point 2, "grace for in-flight operator
action": a row that just arrived is excluded from the digest's count/age
so a human mid-reconciliation doesn't immediately get paged about their
own in-flight row."""

_SELECT_UNRESOLVED_AGED_SQL = text(
    """
    SELECT count(*) AS unresolved_count, min(received_at) AS oldest_received_at
    FROM unrouted_inbound
    WHERE resolved_at IS NULL AND received_at < :grace_cutoff
    """
)


def _digest_day_key(now: datetime) -> str:
    """The UTC calendar-day dedupe key — pure function, mirrors the
    "one thing to key the daily stamp on" shape every other day-scoped
    convention in this codebase uses (ISO date string, unambiguous
    regardless of the caller's own tzinfo)."""
    return now.astimezone(UTC).date().isoformat()


@dataclass
class _DigestState:
    """Mutable module-level dedupe stamp — a single object so there is one
    thing to reset between tests (mirrors ``app/integrations/weather.py``'s
    `_WeatherCacheState`/``app/integrations/supabase_auth.py``'s
    `_JwksState` pattern — see module docstring "Digest dedupe stamp")."""

    last_fired_day: str | None = field(default=None)

    def reset_for_tests(self) -> None:
        self.last_fired_day = None


_digest_state = _DigestState()


def reset_for_tests() -> None:
    """Test-only seam — resets the digest's in-memory day-key stamp.
    Called by an autouse ``tests/conftest.py`` fixture, same convention as
    every other module-singleton reset in this codebase."""
    _digest_state.reset_for_tests()


def _alert_unrouted_digest(*, count: int, oldest_age_hours: float) -> None:
    """The digest page itself — `level="warning"` (mirrors
    ``app/agent/degraded_mode_sweep.py::_alert_degraded_mode_escalation``'s
    own `level="warning"` activation-alert convention; this is a
    "something needs an operator's attention" signal, not the loudest
    `level="error"` this codebase reserves for the original ingestion
    -time emergency alert). Durations/counts ONLY -- never a phone number,
    `twilio_sid`, or payload content (rule #5)."""
    log.warning(
        "unrouted_inbound_unresolved_digest",
        unresolved_count=count,
        oldest_age_hours=round(oldest_age_hours, 1),
    )
    sentry_sdk.capture_message(
        "unrouted_inbound: unresolved rows awaiting operator reconciliation",
        level="warning",
        extras={
            "unresolved_count": count,
            "oldest_age_hours": round(oldest_age_hours, 1),
        },
    )


async def _maybe_fire_digest(*, now: datetime) -> bool:
    """Fire the once-per-UTC-day digest if due — see module docstring
    point 2. Returns whether a digest was actually fired this call (test
    /observability seam)."""
    day_key = _digest_day_key(now)
    if _digest_state.last_fired_day == day_key:
        return False

    grace_cutoff = now - timedelta(seconds=_DIGEST_GRACE_SECONDS)
    async with _acm(get_admin_session)() as session:
        row = (
            (await session.execute(_SELECT_UNRESOLVED_AGED_SQL, {"grace_cutoff": grace_cutoff}))
            .mappings()
            .one()
        )

    count = cast("int", row["unresolved_count"])
    if count == 0:
        # Nothing aged past the grace period yet -- do NOT consume today's
        # stamp, so a later same-day tick (once a row ages past 1h) can
        # still fire. See module docstring "Dedupe stamp".
        return False

    oldest_received_at = cast("datetime | None", row["oldest_received_at"])
    oldest_age_hours = (
        (now - oldest_received_at).total_seconds() / 3600.0
        if oldest_received_at is not None
        else 0.0
    )

    # Stamp AFTER the alert call returns — a raising capture never consumes
    # the day. Caveat (#231 safety review ADVISORY-4): `capture_message`
    # does not raise on client-side rate-limiting/drops, so "once per day"
    # means "attempted once per day", not "guaranteed delivered"; the
    # structlog warning above is the durable-in-logs backstop.
    _alert_unrouted_digest(count=count, oldest_age_hours=oldest_age_hours)
    _digest_state.last_fired_day = day_key
    return True


# ---------------------------------------------------------------------------
# Scheduler entrypoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnroutedMaintenanceOutcome:
    """One sweep tick's outcome — test/observability seam, mirrors
    ``app/push_outbox.py``'s `PushOutboxOutcome`.

    ``deleted_ids`` is a BOUNDED SAMPLE (at most
    :data:`_DELETED_IDS_SAMPLE_CAP` ids — #231 safety review ADVISORY-5: a
    first sweep against a large backlog would otherwise accumulate every
    UUID in memory for a value production only ever counts);
    ``deleted_count`` is always the true total."""

    deleted_count: int
    deleted_ids: list[UUID]
    digest_fired: bool


async def run_unrouted_maintenance_sweep(
    *,
    now: datetime | None = None,
    deadline_seconds: float = DEFAULT_TICK_DEADLINE_SECONDS,
    time_source: Callable[[], float] = _default_time_source,
    retention_batch_limit: int = _RETENTION_SWEEP_BATCH_LIMIT,
) -> UnroutedMaintenanceOutcome:
    """DB entrypoint for one sweep tick — called by ``app/scheduler.py``'s
    60s ticker, LAST (after every other sweep): pure cleanup + a
    reconciliation reminder, never anything the emergency path or the
    approval flow depends on, so it must never sit ahead of anything else
    (see module docstring "Deadline discipline").

    Runs retention FIRST, then the digest -- order is not correctness
    -sensitive (the digest only ever counts UNRESOLVED rows, which
    retention never touches), but cleaning up before reporting reads more
    naturally.

    ``now`` is an injectable override purely for tests (mirrors
    ``app/push_outbox.py::run_push_outbox_sweep(*, now=...)``) -- the
    DB-side "what's due"/"how old" clock, unrelated to *time_source*
    below. *time_source* defaults to the real event loop clock
    (:func:`_default_time_source`) -- tests inject a fake, monotonically
    -advanceable callable instead of sleeping for real seconds.
    *deadline_seconds* is the wall-clock budget the retention loop checks
    against it before claiming each additional batch (see
    :data:`DEFAULT_TICK_DEADLINE_SECONDS`). *retention_batch_limit*
    defaults to the production value (:data:`_RETENTION_SWEEP_BATCH_LIMIT`,
    500) -- an injectable test seam only, so a test can force the
    multi-batch-plus-deadline path without seeding hundreds of rows.
    """
    effective_now = now if now is not None else datetime.now(UTC)
    tick_start = time_source()

    # #231 safety review BLOCKING-3: the two halves are independent jobs
    # sharing one entrypoint — a retention failure (pool blip, lock
    # timeout, deadlock) must NEVER silence the operator digest, which is
    # the only periodic surfacing of an aging unreconciled dead-letter
    # row. Same isolation discipline the scheduler applies BETWEEN its
    # eight jobs, applied WITHIN this one. Metadata-only page (rule #5).
    deleted_count = 0
    deleted_ids: list[UUID] = []
    try:
        deleted_count, deleted_ids = await _run_retention_sweep(
            now=effective_now,
            deadline_seconds=deadline_seconds,
            time_source=time_source,
            tick_start=tick_start,
            batch_limit=retention_batch_limit,
        )
    except Exception as exc:
        log.error("unrouted_retention_sweep_failed", exc_type=type(exc).__name__)
        sentry_sdk.capture_message(
            "unrouted_inbound: retention sweep failed (digest still ran)",
            level="error",
            extras={"exc_type": type(exc).__name__},
        )

    digest_fired = await _maybe_fire_digest(now=effective_now)

    log.info(
        "unrouted_maintenance_sweep_complete",
        deleted_count=deleted_count,
        digest_fired=digest_fired,
    )
    return UnroutedMaintenanceOutcome(
        deleted_count=deleted_count, deleted_ids=deleted_ids, digest_fired=digest_fired
    )


__all__: list[str] = [
    "DEFAULT_TICK_DEADLINE_SECONDS",
    "UnroutedMaintenanceOutcome",
    "reset_for_tests",
    "run_unrouted_maintenance_sweep",
]
