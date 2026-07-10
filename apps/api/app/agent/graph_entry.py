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
contract exist to prevent, just moved one layer down. Fixed:
:data:`_ALREADY_COMPLETED_SQL` checks for a COMPLETION marker instead — an
``audit_log`` row whose ``action`` is ``'drafted'`` OR ``'degraded_mode'``
carrying THIS ``message_id`` in its jsonb payload (``draft_response.py``
and ``degraded_mode.py`` both now stamp ``message_id`` into that payload
for exactly this reason — see each module's own docstring). EITHER action
existing means "the pipeline reached a durable outcome for this message,"
which is the only thing that should ever justify skipping a re-run.
Redelivery after a mid-graph crash now correctly RE-RUNS the graph; the
downstream idempotent writes (``uq_notifications_message_dedupe``,
migration 0006; ``uq_drafts_one_pending`` + ``draft_response.py``'s own
stale-then-insert retry; ``message_cases``' ``ON CONFLICT DO NOTHING``)
absorb a re-run safely rather than double-creating anything.

Known gap, not fixed here (flagged, not silently accepted): a message from
an unrecognized/unknown-sender (``identify_property``'s own "unknown
sender" branch) never reaches ``draft_response`` far enough to insert a
draft (case_id is ``None``, that node returns early) NOR
``classification_failed``/``draft_guard_failed``/EMERGENCY (classification
still runs and normally succeeds) — so it never gets a completion marker
either, and every redelivery re-runs the full (paid) classification/draft
pipeline for it. Rare in practice (unknown senders should be uncommon)
but a real, discovered cost tradeoff — worth a dedicated completion
signal for that path if it turns out to matter in production.

Crash-window coherence with #43's ``mark_awaiting_approval`` (safety
review MEDIUM, #43 fix round)
------------------------------------------------------------------------
``draft_response`` and ``mark_awaiting_approval`` are TWO SEPARATE nodes
(``app/agent/graph.py``'s case-scoped graph) — LangGraph commits
``draft_response``'s own output (the draft row + the ``'drafted'`` audit
row this module's completion check was already reading) as soon as THAT
node completes, independently of whether the NEXT node
(``mark_awaiting_approval``, which flips ``cases.status`` to
``'awaiting_approval'``) ever runs at all. A crash in that exact window
(the durable marker written, the case-status transition not yet reached)
combined with the OLD completion check (``'drafted'`` alone, regardless of
case status) would have skipped every future redelivery of this message
FOREVER — leaving the case stuck at whatever status it was in BEFORE this
run (``'open'`` for a brand-new case), which is ``AUTO_STALE_ELIGIBLE``
(``app/agent/case_lifecycle.py``): a case could silently auto-resolve
14 days later with an un-acted, never-shown ``pending`` draft still
sitting on it — the audit trail would look "complete" while the actual
approval workflow silently never happened.

Fixed: :data:`_ALREADY_COMPLETED_SQL`'s ``'drafted'`` branch now ALSO
requires the case (joined via ``audit_log.case_id`` — already populated
on every ``'drafted'`` row, see ``draft_response.py``) to have a status
OTHER than ``'open'`` — i.e., that ``mark_awaiting_approval`` (or any
other transition away from ``'open'``) actually ran. The ``'degraded_mode'``
branch is UNCHANGED (unconditional): that exit never touches
``cases.status`` at all (module docstring "Shadow mode (#43)" in
``app/agent/graph.py``), so there is nothing further to wait for there.
If the join condition is false (crash window hit), ``already_completed``
is correctly ``False`` and a redelivery RE-RUNS the graph from scratch —
a second (paid) draft/classification pass, but the SAME stale-then-insert
absorption every other re-run already relies on, and the case reliably
reaches ``awaiting_approval`` this time. A rare, bounded extra cost is
preferable to a case silently stuck forever.

``message_received`` itself is now PURELY an observability/audit-trail
line, not a gate — appended idempotently (see below) the first time this
process sees the message, regardless of whether the graph goes on to
succeed.

Idempotent INSERT, single statement (safety review MEDIUM, same fix round)
------------------------------------------------------------------------
:data:`_INSERT_RECEIVED_IF_NOT_EXISTS_SQL` collapses the old separate
``SELECT EXISTS`` + ``INSERT`` into ONE ``INSERT ... SELECT ... WHERE NOT
EXISTS (...)`` statement — the same shape
``app/routers/webhooks/twilio.py``'s own module docstring names as an
EARLIER, superseded attempt at this exact problem. Full disclosure of that
history's lesson, honestly carried over here: that single-statement form
is NOT airtight against two genuinely CONCURRENT transactions (each can
evaluate its own ``NOT EXISTS`` as true before the other commits,
producing two rows) — true concurrency-proof idempotency needs a real
unique index + ``ON CONFLICT``, which the webhook's OWN
``notifications``/``messages`` writes have (migration 0006) and this
``audit_log`` jsonb-payload correlation does not (adding one would be a
schema/migration change, out of this issue's scope). Collapsing to one
round trip still meaningfully shrinks the race window versus the old
two-statement form, and — critically — a duplicate ``message_received``
row is merely a cosmetic double log line, never a safety issue: it no
longer gates anything (see above), so losing this narrower race only
means an extra observability row, not a duplicate graph run or a lost
message.

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
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from uuid import UUID

import sentry_sdk
import structlog
from sqlalchemy import text

from app.agent.graph import run_graph
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

# Completion marker — see module docstring "Gating on COMPLETION, not
# RECEIPT" and "Crash-window coherence with #43's mark_awaiting_approval".
# Deliberately NOT keyed on 'message_received'. The 'drafted' branch
# requires the case to have moved past 'open' (mark_awaiting_approval, or
# any other transition, actually ran) -- 'degraded_mode' needs no such
# check, that exit never touches cases.status at all.
_ALREADY_COMPLETED_SQL = text(
    "SELECT ("
    "  EXISTS ("
    "    SELECT 1 FROM audit_log"
    "    WHERE action = 'degraded_mode' AND payload ->> 'message_id' = :message_id"
    "  )"
    "  OR EXISTS ("
    "    SELECT 1 FROM audit_log al JOIN cases c ON c.id = al.case_id"
    "    WHERE al.action = 'drafted'"
    "      AND al.payload ->> 'message_id' = :message_id"
    "      AND c.status <> 'open'"
    "  )"
    ")"
)

# Single-statement idempotent insert — see module docstring "Idempotent
# INSERT, single statement" for the honest limits of this pattern.
_INSERT_RECEIVED_IF_NOT_EXISTS_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "SELECT :landlord_id, NULL, 'system', 'message_received', "
    "       jsonb_build_object('message_id', CAST(:message_id AS text)) "
    "WHERE NOT EXISTS ("
    "  SELECT 1 FROM audit_log"
    "  WHERE action = 'message_received' AND payload ->> 'message_id' = :message_id"
    ")"
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
                        {"message_id": str(message_id), "reason": "run_graph_failed"}
                    ),
                },
            )
    except Exception as exc:
        log.error(
            "graph_entry_last_resort_needs_eyes_failed",
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
            already_completed = (
                await session.execute(_ALREADY_COMPLETED_SQL, {"message_id": str(message_id)})
            ).scalar_one()
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
        log.error("graph_entry_message_received_guard_failed", exc_type=type(exc).__name__)
        return

    try:
        await run_graph(message_id)
    except Exception as exc:
        log.error(
            "graph_entry_run_graph_failed",
            message_id=str(message_id),
            exc_type=type(exc).__name__,
        )
        sentry_sdk.capture_message(
            "graph_entry: run_graph failed -- message may be stuck with no draft/notification",
            level="error",
            extras={
                "message_id": str(message_id),
                "landlord_id": str(landlord_id),
                "exc_type": type(exc).__name__,
            },
        )
        await _attempt_last_resort_needs_eyes(message_id=message_id, landlord_id=landlord_id)
