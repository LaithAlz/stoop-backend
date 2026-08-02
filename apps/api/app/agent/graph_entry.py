"""Background graph-invocation seam (#40 scope boundary → #34 fills the body).

``app/routers/webhooks/twilio.py`` schedules ``enqueue_classification`` as a
``BackgroundTasks`` callback for every TENANT-party inbound message, to run
AFTER the 200 TwiML response has already been sent (issue #40 AC:
"Background task invokes the graph ... after response"). #34 wires the
actual ``StateGraph`` (``app/agent/graph.py::run_graph``) this function
invokes below.

Session note
------------
A ``BackgroundTasks`` callback runs AFTER FastAPI has already exited the
request's dependency stack — the request-scoped session the webhook
handler used (``get_admin_session``) is already committed/closed by the
time this function runs. So this function opens its OWN admin session
rather than receiving one from the caller. Admin engine, not RLS-scoped,
for the same reason the webhook router uses it: there is no HTTP
request/JWT here to resolve a ``landlord_id`` GUC from (see
``app/db/session.py``'s module docstring, "the pre-identity / service-path
escape hatch"). Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST`` alongside the
webhook router, with the same justification.

Gating on COMPLETION, not RECEIPT (safety review MEDIUM, #34 fix round)
------------------------------------------------------------------------
An earlier revision gated re-running the graph on a ``message_received``
audit row's mere EXISTENCE — but that row only ever meant "a background
task started processing this message," never "the pipeline actually
finished." A crash partway through ``run_graph`` (after
``message_received`` was already written) combined with a Twilio
redelivery of the SAME message would then skip re-running the graph
FOREVER — the exact silent-loss shape never-break rule #2/#40's own
contract exist to prevent, just moved one layer down. Fixed in two rounds:
completion is keyed on markers carrying THIS ``message_id`` in the jsonb
payload (``draft_response.py`` and ``degraded_mode.py`` both stamp it for
exactly this reason). A ``'degraded_mode'`` row alone is complete
(:data:`_DEGRADED_MODE_COMPLETED_SQL`); a ``'drafted'`` row is NECESSARY
but not sufficient — the thread's own checkpoint must ALSO show the run
reached a terminal or legitimately-paused state
(:func:`_thread_reached_terminal_or_paused_state`), because the case's
ambient status can be non-``'open'`` for reasons unrelated to this
message's run (see "Round 2 fix" below — a reproduced stuck-draft window
on multi-message cases).
Redelivery after a mid-graph crash now correctly RE-RUNS the graph; the
downstream idempotent writes (``uq_notifications_message_dedupe``,
migration 0006; ``uq_drafts_one_pending`` + ``draft_response.py``'s own
stale-then-insert retry; ``message_cases``' ``ON CONFLICT DO NOTHING``)
absorb a re-run safely rather than double-creating anything.

The unknown-sender gap above was CLOSED by #184 item 3: a message with no
case runs the case graph on the checkpointed per-message fallback thread
(``f"message:{message_id}"``), and the completion gate now inspects that
thread via :func:`_fallback_thread_ran_to_completion` when no drafted row
exists — reusing the existing checkpoint-inspection mechanism, no new
marker vocabulary. The helper deliberately differs from the drafted-row
one: it requires ``snapshot.values`` non-empty as the "actually ran"
witness, because a NEVER-RUN thread also has empty ``next`` and treating
that as complete would skip the first delivery outright (see its own
docstring).

Crash-window coherence with #43's ``mark_awaiting_approval`` (safety
review MEDIUM, #43 fix round; REPRODUCED as MAJOR and fixed again, same
issue's second review round — see "Round 2" below)
------------------------------------------------------------------------
``draft_response`` and ``mark_awaiting_approval`` are TWO SEPARATE nodes
(``app/agent/graph.py``'s case-scoped graph) — LangGraph commits
``draft_response``'s own output (the draft row + the ``'drafted'`` audit
row this module's completion check was already reading) as soon as THAT
node completes, independently of whether the NEXT node
(``mark_awaiting_approval``, which flips ``cases.status`` to
``'awaiting_approval'``) ever runs at all. A crash in that exact window
(the durable marker written, the case-status transition not yet reached)
combined with the ORIGINAL completion check (``'drafted'`` alone,
regardless of case status) would have skipped every future redelivery of
this message FOREVER.

Round 1 fix (INSUFFICIENT, reproduced live in round 2): required the case
(joined via ``audit_log.case_id``) to have a status OTHER than ``'open'``
— reasoning: "if it's not 'open', ``mark_awaiting_approval`` must have
run." **FALSE in general** — a case can be non-``'open'`` for a reason
completely UNRELATED to THIS message's own run: a SECOND message landing
on a case that is ALREADY ``'awaiting_approval'`` (from an EARLIER
message's successful completion) inherits that same non-``'open'`` status
regardless of whether ITS OWN ``mark_awaiting_approval`` ever executes.
Reproduced directly (``tests/test_agent_shadow_interrupt.py::
test_second_message_crash_on_already_awaiting_approval_case_heals_on_redelivery``):
M1 completes fully (case ``awaiting_approval``, draft D1 pending). M2
arrives, its OWN ``draft_response`` commits (D1 staled, D2 pending,
``'drafted'`` marker written for M2), then a crash before M2's OWN
``mark_awaiting_approval``. The round-1 check saw ``'drafted'`` for M2 AND
``status <> 'open'`` (true, but leftover from M1) → wrongly "complete".
Every redelivery of M2 was then a silent no-op FOREVER: the thread sat at
``next=('mark_awaiting_approval',)`` with NO live interrupt, so
``resume_case_thread`` for D2 would raise ``CaseNotAwaitingApprovalError``
too — D2 was permanently unapprovable unless some THIRD, unrelated tenant
message happened to arrive later. Exactly the forbidden silent-dead-end
class never-break rule #2 exists to prevent.

Round 2 fix — key completion to THIS message's OWN run reaching a
paused/terminal state, never the case's ambient historical status
------------------------------------------------------------------------
Two schema-free design options were on the table (a migration-backed
dedicated marker was the other — rejected: ``audit_log.action`` is a fixed
``CHECK`` list, schema-v1.md doc-first + migration + round-trip for one
value not otherwise needed is disproportionate when a schema-free fix is
sound). Chosen: when a ``'drafted'`` marker exists for this message (and
no ``'degraded_mode'`` marker — that branch is unconditionally complete,
unchanged), ALSO inspect the case's OWN checkpointed thread directly
(:func:`_thread_reached_terminal_or_paused_state` — compiles the case
graph and calls ``aget_state(config)``, exactly the same introspection
``app/agent/graph.py::resume_case_thread`` already relies on):

- ``snapshot.next`` EMPTY → the thread ran all the way to ``END`` (the
  degraded/emergency exit, OR ``await_approval``'s own documented
  case_id/draft_id-``None`` skip-the-pause branches) — genuinely done.
- ``snapshot.interrupts`` NON-EMPTY → legitimately paused at a LIVE
  ``await_approval`` interrupt — genuinely done (this is the normal,
  overwhelmingly common case: no extra work needed to reach this
  conclusion beyond the state read).
- ``snapshot.next`` NON-EMPTY and ``snapshot.interrupts`` EMPTY → a task
  was SCHEDULED but never executed (the crash window) — NOT complete.
  Redelivery correctly re-runs: a plain ``ainvoke`` restarts the run from
  ``START`` (verified semantics, module docstring in ``app/agent/graph.py``
  "Stale-draft re-run"), ``draft_response``'s stale-then-insert absorbs
  the orphaned pending draft from the crashed attempt exactly as it does
  for any other stale draft, and the case reliably reaches
  ``awaiting_approval`` this time.

This costs one extra ``aget_state`` round trip ONLY on the (rare)
redelivery path where a ``'drafted'`` marker already exists without a
``'degraded_mode'`` one — never on the common "no marker yet" fast path,
and never on the ``'degraded_mode'`` fast path either.

``message_received`` itself is now PURELY an observability/audit-trail
line, not a gate — appended idempotently (see below) the first time this
process sees the message, regardless of whether the graph goes on to
succeed.

Idempotent INSERT — index-enforced since migration 0016 (#184 item 4)
------------------------------------------------------------------------
:data:`_INSERT_RECEIVED_IF_NOT_EXISTS_SQL` (name kept for grep history)
is now a plain ``INSERT ... ON CONFLICT ... DO NOTHING`` against the
partial unique expression index ``uq_audit_message_received_dedupe``
(migration 0016, schema-v1.md v1.20) — Postgres's own conflict detection,
safe across arbitrarily many concurrent connections. This replaced the
earlier ``INSERT ... SELECT ... WHERE NOT EXISTS`` form, which this
docstring previously (honestly) flagged as not cross-process-safe: two
genuinely concurrent transactions could each evaluate ``NOT EXISTS`` as
true before either committed, producing duplicate rows. Duplicates were
only ever cosmetic (``message_received`` stopped gating anything in the
round-2 fix above), but the audit trail is a product surface (the LTB
artifact), and the index is the same 0006 pattern every other idempotent
write here already uses.

Never raises outward
---------------------
A ``BackgroundTasks`` callback has no caller left to handle an exception
(the response already went out) — the idempotency guard, the graph
invocation, and the failure-path Sentry/last-resort-notification calls
below are ALL wrapped so a failure anywhere is logged and swallowed, never
propagated.

Total-failure visibility (safety review MEDIUM, #34 fix round)
------------------------------------------------------------------------
``log.error`` alone never reaches Sentry (this process's
``LoggingIntegration`` is configured with ``event_level=None`` —
structlog/stdlib log records are breadcrumbs only, never auto-promoted to
Sentry events; see ``app/observability.py``). That meant a ``run_graph``
failure — the ONE thing that means a tenant message might be stuck with
NEITHER a draft NOR a notification — paged nobody. Fixed: on a
``run_graph`` exception, this function now ALSO calls
``sentry_sdk.capture_message`` (metadata only — ``message_id``/
``landlord_id`` uuids and the exception type NAME, never a message body,
phone number, or JWT — rule #5) AND attempts ONE last-resort ``needs_eyes``
notification INSERT (:func:`_attempt_last_resort_needs_eyes`) so the
message has the best remaining chance of surfacing to a person even when
``degraded_mode`` itself never got to run (e.g. the failure happened
inside the PRE-ROUTING half, before ``degraded_mode`` even exists as a
reachable node). That helper is idempotent (same
``uq_notifications_message_dedupe`` pattern as everywhere else) and is
ITSELF wrapped so a failure inside it (e.g. no real ``landlord_id`` to
satisfy the table's FK) is logged and swallowed, never raised — there is
nothing further downstream to catch it.

A transient failure gets a RETRY, not just a page (safety review, #186
follow-up round, NEW-2)
------------------------------------------------------------------------
The generic handling above treats every ``run_graph`` failure identically
— a single immediate ``needs_eyes`` page, with no automatic follow-up.
That is the right floor for a genuine LOGIC failure (a bug, a malformed
row), but :class:`app.agent.graph.CaseLockAcquisitionTimeoutError`
(#186's bounded try-lock retry loop, ``app/agent/graph.py``, exhausting
its own bounded wait under genuine same-case contention) is a TRANSIENT
RESOURCE condition, not a logic one — the SAME message retried a few
minutes later, once the contention/burst has drained, will very likely
classify normally. Discovered gap: before this fix, a message shed by
this exception landed in the SAME one-shot ``needs_eyes`` push as every
other failure, with nothing ever retrying it — an LLM-only emergency
buried in message #5 of a burst could be silently dropped for good (the
landlord's push notification is easy to miss, and nothing else ever
re-attempts classification for that message).

Fixed: this exception type is now caught SPECIFICALLY (before the generic
``except Exception`` below) and, instead of the one-shot last-resort
``needs_eyes``, calls :func:`app.agent.nodes.degraded_mode.
queue_degraded_retry` — the SAME durable ``degraded_retry`` marker
(``notifications`` row, schema-v1.md v1.8) the classification-failed
"no keywords" leg already writes, reusing its EXACT payload shape/dedupe
index (never a new one — see that function's own docstring). This means
``app/agent/degraded_mode_sweep.py``'s existing, already-scheduled
1/5/15-minute retry-then-escalate ladder becomes the terminal path: it
re-attempts ``run_graph`` (by which point the contention/burst has almost
certainly cleared) and, only if genuinely still failing after all three
attempts, escalates to a real ``needs_eyes`` — the SAME sweep this
codebase already relies on for the classification-failure case, now also
catching this resource-failure case. The Sentry page (metadata only,
same shape as every other ``run_graph`` failure) is UNCHANGED — ops still
learns immediately; only the LANDLORD-facing path changes, from "paged
once, nothing else ever happens" to "durably retried, escalated only if
still failing."

:attr:`app.agent.graph.CaseLockAcquisitionTimeoutError.case_id` (set at
raise time inside ``app/agent/graph.py::_case_lock``) is used directly —
no extra lookup needed; ``run_graph`` never reaches ``_case_lock`` at all
unless ``identify_case`` already attached the message to a real case, so
this is always the correct, already-resolved case for this message.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from uuid import UUID

import sentry_sdk
import structlog
from langchain_core.runnables import RunnableConfig
from sqlalchemy import text

from app.agent.graph import CaseLockAcquisitionTimeoutError, compile_case_graph, run_graph
from app.agent.nodes.degraded_mode import REASON_CASE_LOCK_ACQUISITION_TIMEOUT, queue_degraded_retry
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

# Completion markers — see module docstring "Gating on COMPLETION, not
# RECEIPT" and "Crash-window coherence with #43's mark_awaiting_approval".
# Deliberately NOT keyed on 'message_received'. 'degraded_mode' alone is
# unconditionally complete (that exit never touches cases.status at all).
# 'drafted' alone is NOT sufficient -- see _thread_reached_terminal_or_
# paused_state below, which decides the 'drafted'-only case.
_DEGRADED_MODE_COMPLETED_SQL = text(
    "SELECT EXISTS ("
    "  SELECT 1 FROM audit_log"
    "  WHERE action = 'degraded_mode' AND payload ->> 'message_id' = :message_id"
    ")"
)

_SELECT_DRAFTED_THREAD_SQL = text(
    "SELECT c.langgraph_thread_id"
    "  FROM audit_log al JOIN cases c ON c.id = al.case_id"
    " WHERE al.action = 'drafted' AND al.payload ->> 'message_id' = :message_id"
    " LIMIT 1"
)


async def _thread_reached_terminal_or_paused_state(thread_id: str) -> bool:
    """``True`` iff the case's checkpointed thread has either run all the
    way to ``END`` (``snapshot.next`` empty) or is legitimately paused at a
    LIVE ``await_approval`` interrupt (``snapshot.interrupts`` non-empty).
    ``False`` when a task is SCHEDULED but was never executed (``next``
    non-empty, no interrupt recorded) — the crash window between
    ``draft_response`` and ``mark_awaiting_approval`` (or any later node) —
    see module docstring "Round 2 fix". Same introspection
    ``app/agent/graph.py::resume_case_thread`` already relies on; cheap
    (one ``aget_state`` round trip), only ever called on the rare
    redelivery path where a ``'drafted'`` marker already exists.
    """
    case_graph = compile_case_graph()
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await case_graph.aget_state(config)
    if not snapshot.next:
        return True
    return bool(snapshot.interrupts)


async def _fallback_thread_ran_to_completion(thread_id: str) -> bool:
    """``True`` iff the per-message FALLBACK thread (unknown-sender path,
    #184 item 3) genuinely RAN and reached a terminal/paused state.
    Differs from :func:`_thread_reached_terminal_or_paused_state` in one
    load-bearing way: that helper is only ever called on a thread a
    ``'drafted'`` marker PROVES has run, so it may read "empty ``next``"
    as "terminal". A never-run thread ALSO has empty ``next`` (and empty
    ``values``) — reading that as complete would skip the FIRST delivery
    outright. Here ``snapshot.values`` non-empty is the "it actually ran"
    witness; fail direction on any ambiguity is ``False`` → re-run (safe
    and idempotent, merely paid)."""
    case_graph = compile_case_graph()
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await case_graph.aget_state(config)
    if not snapshot.values:
        return False  # never ran at all — first delivery must proceed
    if not snapshot.next:
        return True
    return bool(snapshot.interrupts)


# Single-statement idempotent insert — see module docstring "Idempotent
# INSERT, single statement" for the honest limits of this pattern.
# #184 item 4: the WHERE NOT EXISTS form this replaced was honestly
# documented as not cross-process-safe (two concurrent transactions can
# both pass the existence check). Migration 0016's partial unique
# expression index (`uq_audit_message_received_dedupe`) + this ON CONFLICT
# (expression + predicate matching the index verbatim, required for
# Postgres's unique-index inference — the house 0006 pattern) closes it at
# the database. Purely-cosmetic-duplicate era is over; the audit trail is
# a product surface (the LTB artifact).
_INSERT_RECEIVED_IF_NOT_EXISTS_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, NULL, 'system', 'message_received', "
    "        jsonb_build_object('message_id', CAST(:message_id AS text))) "
    "ON CONFLICT ((payload ->> 'message_id')) WHERE action = 'message_received' "
    "DO NOTHING"
)

# Same uq_notifications_message_dedupe idempotency pattern used everywhere
# else in this codebase (app/routers/webhooks/twilio.py,
# app/agent/nodes/identify_property.py, app/agent/nodes/degraded_mode.py).
_INSERT_LAST_RESORT_NEEDS_EYES_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'needs_eyes', 'push', 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'message_id'), type) WHERE type IN ('emergency_call', 'needs_eyes')
    DO NOTHING
    RETURNING id
    """
)


async def _attempt_last_resort_needs_eyes(*, message_id: UUID, landlord_id: UUID) -> None:
    """Best-effort fallback once ``run_graph`` has already failed (caught
    by the caller) — see module docstring "Total-failure visibility".
    Idempotent and itself fully guarded: a failure here (e.g. ``landlord_id``
    doesn't satisfy ``notifications``' FK in some test/edge scenario) is
    logged and swallowed, never raised — there is nothing left downstream
    to catch it.
    """
    try:
        async with asynccontextmanager(get_admin_session)() as session:
            await session.execute(
                _INSERT_LAST_RESORT_NEEDS_EYES_SQL,
                {
                    "landlord_id": str(landlord_id),
                    "payload": json.dumps(
                        # #184 item 5: plural "reasons" list, matching degraded_mode.py's
                        # own needs_eyes payloads. NOT claimed as global
                        # unification: identify_property.py's unknown
                        # -sender needs_eyes still writes a singular
                        # `reason` key (pre-existing; no consumer reads
                        # either key today).
                        {"message_id": str(message_id), "reasons": ["run_graph_failed"]}
                    ),
                },
            )
    except Exception as exc:
        # #184 safety review A5: guarded — a raising logger must not
        # escape a fallback helper (nothing downstream can catch it).
        try:
            log.error(
                "graph_entry_last_resort_needs_eyes_failed",
                message_id=str(message_id),
                exc_type=type(exc).__name__,
            )
        except Exception:  # noqa: BLE001, S110
            pass


def _report_run_graph_failure(*, message_id: UUID, landlord_id: UUID, exc_type: str) -> None:
    """Log + Sentry-page a ``run_graph`` failure, each call individually
    guarded (#184 item 2 / the #231 NEW-2 house pattern): the durable
    fallback has ALREADY been written by the time this runs, and a raising
    logger or Sentry transport must degrade to at-worst a quieter page —
    never to an exception escaping the failure handler. Metadata only
    (uuids + exception type name — rule #5)."""
    try:
        log.error(
            "graph_entry_run_graph_failed",
            message_id=str(message_id),
            exc_type=exc_type,
        )
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        sentry_sdk.capture_message(
            "graph_entry: run_graph failed -- message may be stuck with no draft/notification",
            level="error",
            extras={
                "message_id": str(message_id),
                "landlord_id": str(landlord_id),
                "exc_type": exc_type,
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass


async def _queue_degraded_retry_for_lock_timeout(
    *, message_id: UUID, landlord_id: UUID, case_id: UUID
) -> None:
    """``CaseLockAcquisitionTimeoutError``-specific fallback (safety
    review, #186 follow-up round, NEW-2) — see module docstring "A
    transient failure gets a RETRY, not just a page". Best-effort and
    itself fully guarded, same shape as
    :func:`_attempt_last_resort_needs_eyes`: a failure here is logged and
    swallowed, never raised — there is nothing left downstream to catch
    it.
    """
    try:
        async with asynccontextmanager(get_admin_session)() as session:
            await queue_degraded_retry(
                session,
                message_id=message_id,
                landlord_id=landlord_id,
                case_id=case_id,
                reasons=[REASON_CASE_LOCK_ACQUISITION_TIMEOUT],
            )
    except Exception as exc:
        log.error(
            "graph_entry_degraded_retry_queue_failed",
            message_id=str(message_id),
            exc_type=type(exc).__name__,
        )


async def enqueue_classification(message_id: UUID, landlord_id: UUID) -> None:
    """Background-task entry point (#34) — invokes the real LangGraph
    pipeline (``app/agent/graph.py::run_graph``) for a persisted inbound
    message, skipping only when a completion marker already exists (see
    module docstring "Gating on COMPLETION, not RECEIPT").

    Logs the invocation (uuids only — never a phone number or message
    body). Never raises outward: a ``BackgroundTasks`` callback that
    raises has no caller left to handle it (the response already went
    out), so any failure here — in the idempotency guard OR inside the
    graph itself — is logged, paged via Sentry where it matters (see
    module docstring "Total-failure visibility"), and swallowed rather
    than crashing the worker.
    """
    log.info("graph_entry_invoked", message_id=str(message_id))

    try:
        async with asynccontextmanager(get_admin_session)() as session:
            degraded_mode_done = (
                await session.execute(_DEGRADED_MODE_COMPLETED_SQL, {"message_id": str(message_id)})
            ).scalar_one()
            already_completed = bool(degraded_mode_done)

            if not already_completed:
                # 'drafted' alone is NOT sufficient -- see module docstring
                # "Round 2 fix": the case's CURRENT status can be non-'open'
                # for reasons unrelated to THIS message's own run (e.g. it
                # was already 'awaiting_approval' from an earlier message).
                # Only the thread's OWN checkpoint state can answer "did
                # THIS run reach a paused/terminal state".
                drafted_row = (
                    (
                        await session.execute(
                            _SELECT_DRAFTED_THREAD_SQL, {"message_id": str(message_id)}
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if drafted_row is not None:
                    already_completed = await _thread_reached_terminal_or_paused_state(
                        drafted_row["langgraph_thread_id"]
                    )
                else:
                    # #184 item 3 — unknown-sender completion. A message
                    # with no case (unknown sender) runs the case graph on
                    # the per-message fallback thread (app/agent/graph.py's
                    # `f"message:{message_id}"`), which IS checkpointed —
                    # but it earns neither a 'degraded_mode' marker nor a
                    # drafted-row thread, so every Twilio redelivery
                    # re-ran the PAID classification pipeline for that one
                    # shape (cost-only: the needs_eyes re-notify was always
                    # an idempotent no-op). Checked with
                    # :func:`_fallback_thread_ran_to_completion` — NOT the
                    # drafted-row helper above, whose "empty next =
                    # terminal" reading is only valid for a thread KNOWN
                    # to have run (a never-run thread also has empty
                    # ``next``; treating that as complete would skip the
                    # FIRST delivery of every case-less message — the
                    # silent-dead-end class). LOCALLY GUARDED (#184 safety
                    # review B2, HIGH): this branch runs on the FIRST
                    # delivery of every message and reads the
                    # checkpointer's own separate psycopg pool — a
                    # PoolClosed/failover blip here must NEVER veto the
                    # graph run. Any failure reads as "not completed" →
                    # re-run, which is idempotent and merely paid.
                    try:
                        already_completed = await _fallback_thread_ran_to_completion(
                            f"message:{message_id}"
                        )
                    except Exception as gate_exc:  # noqa: BLE001
                        log.error(
                            "graph_entry_fallback_gate_read_failed",
                            message_id=str(message_id),
                            exc_type=type(gate_exc).__name__,
                        )
                        already_completed = False

            if already_completed:
                return
            await session.execute(
                _INSERT_RECEIVED_IF_NOT_EXISTS_SQL,
                {"landlord_id": str(landlord_id), "message_id": str(message_id)},
            )
        # The session above has already committed (clean exit of
        # get_admin_session) by this point — the message_received row is
        # durable regardless of whatever happens in run_graph() below.
    except Exception as exc:
        # #184 safety review B2: a bare log-and-return here was a silent
        # message drop (log.error never pages — event_level=None), on the
        # one path where Twilio already got its 200 and will never retry.
        # Failure must land toward "tell the landlord": durable last-resort
        # row first, then the guarded page.
        await _attempt_last_resort_needs_eyes(message_id=message_id, landlord_id=landlord_id)
        _report_run_graph_failure(
            message_id=message_id, landlord_id=landlord_id, exc_type=type(exc).__name__
        )
        return

    try:
        await run_graph(message_id)
    except CaseLockAcquisitionTimeoutError as exc:
        # Transient resource failure, not a logic one -- see module
        # docstring "A transient failure gets a RETRY, not just a page"
        # (#186 follow-up round, NEW-2). DURABLE FALLBACK FIRST (#184
        # item 2): the degraded_retry marker is the thing that guarantees
        # a person/retry ever hears about this message -- it must never
        # be skipped because a REPORTING call raised. The helper is fully
        # self-guarded; the log + Sentry page after it are each
        # individually guarded too (the #231 NEW-2 house pattern).
        await _queue_degraded_retry_for_lock_timeout(
            message_id=message_id, landlord_id=landlord_id, case_id=exc.case_id
        )
        _report_run_graph_failure(
            message_id=message_id, landlord_id=landlord_id, exc_type=type(exc).__name__
        )
    except Exception as exc:
        # DURABLE FALLBACK FIRST (#184 item 2) -- same ordering rationale
        # as the lock-timeout branch above.
        await _attempt_last_resort_needs_eyes(message_id=message_id, landlord_id=landlord_id)
        _report_run_graph_failure(
            message_id=message_id, landlord_id=landlord_id, exc_type=type(exc).__name__
        )
