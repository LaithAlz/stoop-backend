"""``identify_property`` node (#30) — the graph's first node.

Twilio-number -> property and sender -> tenant resolution ALREADY happened
once, synchronously, in the webhook handler (``app/routers/webhooks/
twilio.py``) before the message row was ever persisted — that is how
``messages.landlord_id``/``property_id``/``tenant_id`` got their values.
This node does NOT repeat that resolution from raw Twilio numbers. Per the
issue's own scope decision, it RE-DERIVES ``case_context`` from the
persisted ``messages`` row (keyed on ``state["message_id"]``), which is the
source of truth by the time the graph runs — trusting anything else the
caller might have set in ``state`` instead would risk drift between the
message actually persisted and what this node believes about it.

Unknown sender (#30 AC: "unknown sender handled gracefully")
--------------------------------------------------------------
The webhook already distinguishes two "we don't recognize this sender"
cases and handles ONE of them itself:

- ``party == 'landlord'`` (the landlord's own number, not matching an
  active tenant) — the webhook's own post-persist step already creates a
  ``needs_eyes`` notification for every such message (#122 doesn't parse
  approve-by-SMS replies yet). Nothing more to do here.
- ``party == 'tenant'`` but ``tenant_id IS NULL`` — a phone number that is
  neither the landlord's own number nor an active tenant on file for the
  property. The webhook does NOT create any notification for this case
  today (see that module's ``_run_post_persist_side_effects`` — only the
  ``party == 'landlord'`` branch calls ``_ensure_needs_eyes_notification``).
  THIS is the gap #30's AC closes: this node logs it and ensures a
  ``needs_eyes`` notification exists (idempotent, keyed on
  ``(payload->>'message_id', type)`` — the SAME partial unique index
  (``uq_notifications_message_dedupe``, migration 0006) and ``ON CONFLICT``
  pattern the webhook itself uses, reproduced here rather than importing
  the webhook module's private helpers). No crash either way — the case
  simply has no tenant to attach to (``identify_case``, #110, will also see
  ``tenant_id IS NULL`` and skip case-routing for the same reason:
  ``cases.tenant_id`` is ``NOT NULL`` in schema-v1.md, so a case can never
  be opened without a known tenant).

DB access
---------
Runs in the background/graph context (no HTTP request, no landlord JWT) —
uses ``get_admin_session``, the same pre-identity/service-path pattern
``app/agent/graph_entry.py`` already uses. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import CaseContext
from app.agent.state import AgentState
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

_SELECT_MESSAGE_SQL = text(
    "SELECT m.id, m.landlord_id, m.property_id, m.tenant_id, m.party, "
    "p.label AS property_label, t.name AS tenant_name "
    "FROM messages m "
    "JOIN properties p ON p.id = m.property_id "
    "LEFT JOIN tenants t ON t.id = m.tenant_id "
    "WHERE m.id = :message_id"
)

# Mirrors app/routers/webhooks/twilio.py's `_INSERT_NEEDS_EYES_SQL` exactly —
# same partial unique index (`uq_notifications_message_dedupe`, migration
# 0006), same ON CONFLICT target. Reproduced locally rather than importing
# that module's private helper (cross-module private imports are a code
# smell; this SQL is small and stable).
_INSERT_NEEDS_EYES_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'needs_eyes', 'push', 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'message_id'), type) WHERE type IN ('emergency_call', 'needs_eyes')
    DO NOTHING
    RETURNING id
    """
)


class MessageNotFoundError(RuntimeError):
    """Raised when ``state["message_id"]`` does not match any persisted
    ``messages`` row. Should never happen in production — the graph is only
    ever invoked (``app/agent/graph_entry.py``) after the webhook has
    already durably committed the message row — so this is a programming-
    invariant violation, not a routine "unknown sender" case (that is
    handled gracefully below; this is not)."""


async def _ensure_unknown_sender_notification(
    session: AsyncSession, *, landlord_id: UUID, property_id: UUID, message_id: UUID
) -> bool:
    payload = {
        "message_id": str(message_id),
        "property_id": str(property_id),
        "reason": "unknown_sender",
    }
    row = (
        (
            await session.execute(
                _INSERT_NEEDS_EYES_SQL,
                {"landlord_id": str(landlord_id), "payload": json.dumps(payload)},
            )
        )
        .mappings()
        .one_or_none()
    )
    return row is not None


async def identify_property(state: AgentState) -> dict[str, Any]:
    """Re-derive ``case_context`` identifiers from the persisted message
    row and handle the unknown-tenant-sender case gracefully.

    Returns a partial state update: ``case_context`` and the FULL
    (accumulated) ``reasoning_log`` — see ``app/agent/state.py``'s
    "Accumulation note".
    """
    message_id = state["message_id"]
    reasoning_log = list(state.get("reasoning_log") or [])

    async with asynccontextmanager(get_admin_session)() as session:
        row = (
            (await session.execute(_SELECT_MESSAGE_SQL, {"message_id": str(message_id)}))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise MessageNotFoundError(
                f"identify_property: no messages row for message_id={message_id}"
            )

        landlord_id: UUID = row["landlord_id"]
        property_id: UUID = row["property_id"]
        tenant_id: UUID | None = row["tenant_id"]
        party: str = row["party"]
        property_label: str = row["property_label"]
        tenant_name: str | None = row["tenant_name"]

        case_context = CaseContext(
            landlord_id=landlord_id,
            property_id=property_id,
            tenant_id=tenant_id,
        )

        log.info(
            "identify_property_matched",
            message_id=str(message_id),
            property_id=str(property_id),
            tenant_id=str(tenant_id) if tenant_id is not None else None,
        )

        if party == "tenant" and tenant_id is None:
            # Unknown sender — plain, warm, landlord-facing copy (rule #8 /
            # CLAUDE.md's "shown on the approval card"), never a raw id.
            reasoning_log.append(
                f"New sender — I don't recognize this number for {property_label}, "
                "so I've flagged it for you."
            )
            created = await _ensure_unknown_sender_notification(
                session,
                landlord_id=landlord_id,
                property_id=property_id,
                message_id=message_id,
            )
            log.info(
                "identify_property_unknown_sender",
                message_id=str(message_id),
                created_notification=created,
            )
        elif party == "landlord":
            reasoning_log.append(
                f"This is one of your own messages on the line for {property_label}."
            )
        elif tenant_name:
            reasoning_log.append(f"This message came in from {tenant_name} at {property_label}.")
        else:
            reasoning_log.append(f"This message came in from a tenant at {property_label}.")

    return {"case_context": case_context, "reasoning_log": reasoning_log}


__all__: list[str] = ["MessageNotFoundError", "identify_property"]
