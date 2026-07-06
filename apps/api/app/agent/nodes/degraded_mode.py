"""``degraded_mode`` node (#34, G1) — the durable landlord-notification seam
for a double-failure inside the classify/draft pipeline.

Routed to by ``app/agent/graph.py``'s conditional edges, NEVER called
directly by any other node:

- after ``classify_severity`` when ``state["classification_failed"]`` is
  ``True`` (the Anthropic call failed twice — see that node's own
  "degraded-mode seam" docstring, which explicitly leaves the durable
  record + notification to "a future graph (#34)"); ``draft_response`` is
  SKIPPED entirely in that case (there is no severity to draft against).
- after ``draft_response`` when ``state["draft_guard_failed"]`` is
  ``True`` (the model's own acknowledgment failed the hard safety guards
  twice — a draft WAS still inserted, using the generic safe fallback
  text; this node's job is only the notification, not the draft itself).

These two flags are mutually exclusive within a single graph run today
(``draft_response`` never runs when ``classification_failed`` is set, and
never sets ``draft_guard_failed`` unless it actually ran) — this node
still checks ``classification_failed`` first, defensively, so a future
change that makes them coexist fails toward "notify once, for the
classification failure" rather than notifying twice or crashing.

Merge-blocking gate (#34 G1, PR #173/#175 senior review): "classification
-failed AND draft_guard_failed must each route to an explicit degraded
-mode edge ... at minimum a clearly-named function that writes a
needs_eyes notification (idempotent via the 0006 partial unique index
pattern) and appends a landlord-readable reasoning_log line. NO SILENT
DEAD-ENDS." This module is that function's home.

What this node does NOT do (scope, per the same gate + emergency
-prefilter.md's degraded-mode table): it does NOT send the tenant-facing
holding ack (that SMS send belongs to #109's actual protocol, once the
safety-path sender exists — #108/Phase 4) and it does NOT run the
escalation chain (#108). Today it durably records "a person needs to look
at this one" so the failure is never silent — the tenant-facing send and
the timed escalation are explicitly future work, tracked separately.

Idempotency
-----------
The ``notifications`` INSERT targets ``uq_notifications_message_dedupe``
(migration 0006, schema-v1.md v1.3) directly via
``ON CONFLICT ((payload ->> 'message_id'), type) WHERE type IN
('emergency_call', 'needs_eyes') DO NOTHING`` — the SAME pattern
``app/routers/webhooks/twilio.py`` and
``app/agent/nodes/identify_property.py`` already use. At most one
``needs_eyes``/``emergency_call`` notification ever exists per
``message_id`` — if a redelivered/retried graph run reaches this node for
a message that already has one (from an earlier attempt, OR from
``identify_property``'s own unknown-sender case), the INSERT is a silent
no-op. The ``degraded_mode`` ``audit_log`` row is gated on the
notification INSERT having actually created a new row, for the same
reason ``app/routers/webhooks/twilio.py``'s
``_ensure_tenant_emergency_artifacts`` gates its own audit row: a
redelivery that finds the artifact already created must not also
duplicate the append-only audit trail.

Never-break rule #5: only uuids/booleans/a short reason string ever reach
``log.*`` calls or the notification/audit payloads here — never a
message body or phone number.

DB access
---------
Admin engine (background/graph context), same pattern as every other #30/
#110/#32/#33 node. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import structlog
from sqlalchemy import text

from app.agent.schemas import CaseContext
from app.agent.state import AgentState
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

REASON_CLASSIFICATION_FAILED = "classification_failed"
REASON_DRAFT_GUARD_FAILED = "draft_guard_failed"

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


def _resolve_reason(state: AgentState) -> str:
    """Which of the two G1 flags triggered this run — see module
    docstring for why ``classification_failed`` is checked first."""
    if state.get("classification_failed"):
        return REASON_CLASSIFICATION_FAILED
    return REASON_DRAFT_GUARD_FAILED


async def degraded_mode(state: AgentState) -> dict[str, Any]:
    """Durably notify the landlord that this message needs a person's
    attention, and record it in the append-only audit trail. Returns a
    partial state update (``reasoning_log`` only — this node never touches
    ``severity``/``draft``, whatever the upstream node already set stays
    as-is)."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    reason = _resolve_reason(state)

    landlord_id = case_context.landlord_id
    if landlord_id is None:  # pragma: no cover — invariant: identify_property always sets this
        log.error("degraded_mode_missing_landlord_id", message_id=str(message_id))
        reasoning_log.append(
            "I couldn't finish this one on my own, and I couldn't reach you either."
        )
        return {"reasoning_log": reasoning_log}

    case_id = case_context.case_id
    payload = {
        "message_id": str(message_id),
        "case_id": str(case_id) if case_id is not None else None,
        "reason": reason,
    }

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
        message_id=str(message_id),
        case_id=str(case_id) if case_id is not None else None,
        reason=reason,
        notification_created=created,
    )
    reasoning_log.append(
        "I couldn't finish this one on my own, so I've sent you a notification to take a look."
    )
    return {"reasoning_log": reasoning_log}


__all__: list[str] = ["REASON_CLASSIFICATION_FAILED", "REASON_DRAFT_GUARD_FAILED", "degraded_mode"]
