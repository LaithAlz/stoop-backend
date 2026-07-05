"""Background graph-invocation seam (#40 scope boundary → #30-series fills the body).

``app/routers/webhooks/twilio.py`` schedules ``enqueue_classification`` as a
``BackgroundTasks`` callback for every TENANT-party inbound message, to run
AFTER the 200 TwiML response has already been sent (issue #40 AC:
"Background task invokes the graph ... after response"). The LangGraph
agent itself does not exist yet (#30-series issues: ``identify_property``,
``load_context``, ``classify_intent``, ``classify_severity``,
``draft_response``, ...); this module is the honest stub that stands in for
it until then.

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

Idempotency
-----------
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
load-bearing, not decorative.

Known, accepted race (consolidated review item 8): the guard is a plain
check-then-insert (``SELECT EXISTS`` followed by a separate ``INSERT``),
not a single atomic statement — two genuinely CONCURRENT invocations for
the same ``message_id`` could both pass the existence check before either
inserts, producing two ``message_received`` rows. Accepted for v1: nothing
today schedules two truly concurrent background tasks for the same
message (Twilio redeliveries are sequential in practice, and the webhook
handler's own recovery path only runs when THIS request's INSERT
conflicted, i.e. after the row is already durably committed). Real
concurrency here becomes a live concern once #30 introduces retries/
parallel graph invocations — fix it there (e.g. the same atomic ``WHERE
NOT EXISTS`` pattern ``app/routers/webhooks/twilio.py`` already uses for
its own idempotent notification inserts) rather than here, speculatively.

#30 replaces this body with the actual LangGraph invocation, keyed on
``cases.langgraph_thread_id``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import structlog
from sqlalchemy import text

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
    """Background-task stub standing in for the LangGraph agent (#30-series).

    Logs the invocation (uuids only — never a phone number or message
    body) and appends a ``message_received`` ``audit_log`` row if one
    doesn't already exist for this message. Never raises outward: a
    ``BackgroundTasks`` callback that raises has no caller left to handle
    it (the response already went out), so any failure here is logged and
    swallowed rather than crashing the worker.
    """
    log.info("graph_entry_stub_invoked", message_id=str(message_id))

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
    except Exception as exc:
        log.error("graph_entry_stub_failed", exc_type=type(exc).__name__)
