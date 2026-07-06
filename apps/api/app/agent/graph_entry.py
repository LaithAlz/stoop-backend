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

Idempotency — also now gates whether the graph runs at all
------------------------------------------------------------
Appends an ``audit_log`` row (``actor='system'``, ``action=
'message_received'``) keyed on ``payload->>'message_id'`` — but only if no
such row already exists for this message id. ``audit_log`` has no
``message_id`` column of its own (only a nullable ``case_id``, and this
message is pre-routing/case-less — conversation-model.md), so the
correlation key lives in the jsonb payload. The webhook handler's
post-persist side-effect path (``app/routers/webhooks/twilio.py``,
``_run_post_persist_side_effects``) schedules THIS function on every
delivery it processes, including a duplicate/crash-recovery redelivery of
an already-persisted message (consolidated transaction-design review,
issue #40/#152) — not just the very first one — so the guard here is
load-bearing, not decorative. Now that a real graph run happens below,
this guard ALSO decides whether ``run_graph`` is even attempted: when the
row already exists (a redelivery of a message this process — or an
earlier one — has already seen), the function returns immediately WITHOUT
invoking the graph again, since an earlier successful pass already ran it.

Known, accepted race (consolidated review item 8): the guard is a plain
check-then-insert (``SELECT EXISTS`` followed by a separate ``INSERT``),
not a single atomic statement — two genuinely CONCURRENT invocations for
the same ``message_id`` could both pass the existence check before either
inserts, producing two ``message_received`` rows AND two concurrent graph
runs for the same message. Still accepted for v1, same rationale as
before (Twilio redeliveries are sequential in practice, and the webhook
handler's own recovery path only runs when THIS request's INSERT
conflicted, i.e. after the row is already durably committed) — but now
that a real graph run is gated on it, the consequence of losing this race
is larger than it was for the stub (e.g. ``identify_case`` could open two
cases for one message under a genuine concurrent double-run — a
pre-existing, documented risk of that node, not new here). Hardening this
guard to the atomic ``WHERE NOT EXISTS``/unique-index pattern
``app/routers/webhooks/twilio.py`` already uses for its own idempotent
inserts is flagged as follow-up work, not done unilaterally as part of
this issue's explicitly scoped merge-blocking gates.

Never raises outward
---------------------
A ``BackgroundTasks`` callback has no caller left to handle an exception
(the response already went out) — both the idempotency guard AND the
graph invocation below are wrapped so a failure anywhere is logged and
swallowed, never propagated. The ``message_received`` audit row's own
transaction always commits (or fully rolls back) BEFORE the graph is even
attempted, so a failure inside ``run_graph`` can never retroactively lose
that row.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import structlog
from sqlalchemy import text

from app.agent.graph import run_graph
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

_ALREADY_LOGGED_SQL = text(
    "SELECT EXISTS ("
    "  SELECT 1 FROM audit_log"
    "  WHERE action = 'message_received' AND payload ->> 'message_id' = :message_id"
    ")"
)

_INSERT_RECEIVED_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, NULL, 'system', 'message_received', "
    "        jsonb_build_object('message_id', CAST(:message_id AS text)))"
)


async def enqueue_classification(message_id: UUID, landlord_id: UUID) -> None:
    """Background-task entry point (#34) — invokes the real LangGraph
    pipeline (``app/agent/graph.py::run_graph``) for a persisted inbound
    message, exactly once per message (see module docstring
    "Idempotency").

    Logs the invocation (uuids only — never a phone number or message
    body). Never raises outward: a ``BackgroundTasks`` callback that
    raises has no caller left to handle it (the response already went
    out), so any failure here — in the idempotency guard OR inside the
    graph itself — is logged and swallowed rather than crashing the
    worker.
    """
    log.info("graph_entry_invoked", message_id=str(message_id))

    try:
        async with asynccontextmanager(get_admin_session)() as session:
            already_logged = (
                await session.execute(_ALREADY_LOGGED_SQL, {"message_id": str(message_id)})
            ).scalar_one()
            if already_logged:
                return
            await session.execute(
                _INSERT_RECEIVED_SQL,
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
