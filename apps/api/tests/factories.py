"""Shared DB-seeding factory helpers for integration tests.

Senior review (asked across PRs #173/#175 and again on #34): "test-factory
duplication now spans 6+ files — extract tests/factories.py as a standalone
follow-up before the suite doubles." This module is that extraction.

Each function takes an already-open ``AsyncSession``, inserts one row
using the same minimal-columns-plus-DB-defaults shape every existing test
module already relied on, commits, and returns the new row's ``id`` as a
``str`` (the convention every existing caller already expects — keeping
that shape means callers don't need to change beyond the import).

Scope: used by this issue's (#34) new test modules
(``test_agent_graph.py``, ``test_agent_degraded_mode.py``,
``test_agent_graph_entry.py``). Migrating the older test modules that
still duplicate these helpers locally (``test_agent_nodes.py``,
``test_agent_classify_intent.py``, ``test_agent_classify_severity.py``,
``test_agent_draft_response.py``, ``test_webhooks_twilio_sms.py``, and
others) is explicitly OUT OF SCOPE for this extraction — a follow-up, not
done unilaterally here.

``insert_vendor``/``insert_case``/``insert_audit_log``/``insert_draft``
were added for #54/#55/#57's new router test modules
(``test_properties_router.py``, ``test_tenants_router.py``,
``test_vendors_router.py``, ``test_cases_router.py``) — same shape
convention as every helper above. ``insert_case``/``insert_draft`` gained
further kwargs (``vendor_id``, ``recipient``, ``scheduled_send_at``,
``edited``, ``final_body``) for #44/#45's new test modules
(``test_agent_finalize_draft_decision.py``, ``test_drafts_router.py``,
``test_agent_draft_sender.py``) — these bypass the graph entirely (no
Anthropic mocking needed) to seed a case/draft directly at whatever
status a given approve/reject/edit-and-send/sender test needs.

``insert_notification`` was added for #213's ``GET /v1/queue``
``notification_id`` correlation tests (``test_queue_router.py``) — seeds a
``notifications`` row directly at whatever ``type``/``case_id``/
``acknowledged_at``/``created_at`` a given correlation/latest-wins/
cross-tenant test needs, bypassing the webhook + escalation-chain
machinery that would normally create one.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def fresh_phone() -> str:
    """A syntactically-plausible, collision-free phone number for tests —
    never a real number. Matches every existing test module's own
    duplicated ``_fresh_phone`` helper exactly."""
    return f"+1416{uuid.uuid4().int % 10_000_000:07d}"


async def insert_landlord(
    session: AsyncSession,
    *,
    voice_profile: dict[str, Any] | None = None,
    full_name: str | None = None,
    phone: str | None = None,
) -> str:
    """``phone`` added for #108 (the emergency escalation chain calls/texts
    ``landlords.phone``) — defaults ``None``, matching the schema's own
    nullable column and every EXISTING caller's prior behavior unchanged."""
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO landlords (id, auth_user_id, email, voice_profile, full_name, phone) "
            "VALUES (:id, :auth_id, :email, CAST(:voice_profile AS jsonb), :full_name, :phone)"
        ),
        {
            "id": landlord_id,
            "auth_id": str(uuid.uuid4()),
            "email": f"{landlord_id}@example.com",
            "voice_profile": json.dumps(voice_profile) if voice_profile is not None else None,
            "full_name": full_name,
            "phone": phone,
        },
    )
    await session.commit()
    return landlord_id


async def insert_property(
    session: AsyncSession,
    landlord_id: str,
    *,
    house_rules: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    twilio_number: str | None = None,
    twilio_sid: str | None = None,
    backup_contact: dict[str, Any] | None = None,
    address_line1: str | None = None,
    city: str = "Toronto",
    province: str = "ON",
) -> str:
    """``twilio_number``/``backup_contact`` added for #108 (the emergency
    escalation chain needs a per-property outbound caller-id number and an
    optional backup contact) — both default ``None``, matching the schema's
    own nullable columns and every EXISTING caller's prior behavior
    unchanged. ``twilio_sid`` added for #53 (deprovisioning tests need a
    SID to schedule a release for) — also defaults ``None``.

    ``address_line1``/``city``/``province`` added for #203 (migration
    0013's landlord-scoped, normalized-address UNIQUE index on
    ``properties``): ``address_line1`` now defaults to a value DERIVED FROM
    the freshly generated ``property_id`` (globally unique) rather than a
    fixed literal, so every EXISTING caller that inserts more than one
    property for the SAME landlord in one test keeps working unchanged with
    zero call-site changes. Pass an explicit ``address_line1`` (and,
    if needed, ``city``/``province``) only when a test specifically wants
    to exercise dedupe/collision behavior."""
    property_id = str(uuid.uuid4())
    effective_address_line1 = (
        address_line1 if address_line1 is not None else f"{property_id} Test St"
    )
    await session.execute(
        text(
            "INSERT INTO properties (id, landlord_id, label, address_line1, city, province, "
            "house_rules, lat, lon, twilio_number, twilio_sid, backup_contact) "
            "VALUES (:id, :landlord_id, 'Test Property', :address_line1, :city, :province, "
            ":house_rules, :lat, :lon, :twilio_number, :twilio_sid, CAST(:backup_contact AS jsonb))"
        ),
        {
            "id": property_id,
            "landlord_id": landlord_id,
            "address_line1": effective_address_line1,
            "city": city,
            "province": province,
            "house_rules": house_rules,
            "lat": lat,
            "lon": lon,
            "twilio_number": twilio_number,
            "twilio_sid": twilio_sid,
            "backup_contact": json.dumps(backup_contact) if backup_contact is not None else None,
        },
    )
    await session.commit()
    return property_id


async def insert_tenant(
    session: AsyncSession, landlord_id: str, property_id: str, *, name: str | None = "Maria"
) -> str:
    tenant_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO tenants (id, landlord_id, property_id, phone, name) "
            "VALUES (:id, :landlord_id, :property_id, :phone, :name)"
        ),
        {
            "id": tenant_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "phone": fresh_phone(),
            "name": name,
        },
    )
    await session.commit()
    return tenant_id


async def insert_message(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    tenant_id: str | None,
    body: str = "test message",
    party: str = "tenant",
    prefilter: str | None = None,
) -> str:
    """``tenant_id=None`` seeds the "unknown sender" scenario
    (``identify_property``'s own docstring) — ``party`` still defaults to
    ``'tenant'`` since that is the only party for which an unresolved
    ``tenant_id`` means anything (a ``'landlord'`` party message always
    carries ``tenant_id IS NULL`` by schema, regardless of resolution).
    ``prefilter`` is a pre-serialized JSON string (e.g.
    ``PrefilterResult(...).model_dump_json()``); ``None`` leaves the
    column ``NULL``.
    """
    message_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, landlord_id, property_id, tenant_id, direction, party, body, twilio_sid, "
            " prefilter) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, 'inbound', :party, :body, "
            " :twilio_sid, CAST(:prefilter AS jsonb))"
        ),
        {
            "id": message_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "party": party,
            "body": body,
            "twilio_sid": f"SM{uuid.uuid4().hex}",
            "prefilter": prefilter,
        },
    )
    await session.commit()
    return message_id


async def insert_message_case(session: AsyncSession, *, message_id: str, case_id: str) -> None:
    """Link a message to a case via ``message_cases`` — the ONLY durable
    link in production (``messages.case_id`` is always ``NULL``; see
    ``app/agent/nodes/identify_case.py``'s module docstring). Tests that
    need a case-scoped message in their seed data MUST use this instead of
    an ``UPDATE messages SET case_id = ...`` (impossible under ``app_role``
    in production — ``messages`` is append-only, rule #2 — and not
    representative of how a message ever actually gets linked)."""
    await session.execute(
        text("INSERT INTO message_cases (message_id, case_id) VALUES (:message_id, :case_id)"),
        {"message_id": message_id, "case_id": case_id},
    )
    await session.commit()


async def insert_vendor(
    session: AsyncSession,
    landlord_id: str,
    *,
    trade: str = "plumbing",
    active: bool = True,
    name: str = "Test Vendor",
) -> str:
    vendor_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO vendors (id, landlord_id, name, trade, phone, active) "
            "VALUES (:id, :landlord_id, :name, :trade, :phone, :active)"
        ),
        {
            "id": vendor_id,
            "landlord_id": landlord_id,
            "name": name,
            "trade": trade,
            "phone": fresh_phone(),
            "active": active,
        },
    )
    await session.commit()
    return vendor_id


async def insert_case(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    tenant_id: str,
    vendor_id: str | None = None,
    status: str = "open",
    severity: str | None = None,
    title: str | None = None,
) -> str:
    """Seeds a ``cases`` row directly (bypassing the graph) — #44/#45's new
    test modules exercise ``resolve_draft_decision``/the drafts router
    against a known status, not against whatever ``run_graph`` would
    naturally produce. Also used by #54/#55/#57's router test modules
    (``test_properties_router.py``, ``test_cases_router.py``).

    Defaults are NEUTRAL — ``status='open'``/``severity=None``/
    ``title=None`` — matching ``schema-v1.md``'s own column defaults (a
    freshly-created, never-classified case) rather than any one caller's
    preferred fixture shape. A #198 revision briefly flipped these to
    ``status='awaiting_approval'``/``severity='urgent'``/``title='Test
    case'`` to save a few callers a kwarg; PR #198's senior review flagged
    the wide implicit blast radius that created (every OTHER caller of this
    shared factory silently inherited a non-neutral case shape it never
    asked for), and #199 reverted it: every call site that actually needs
    a non-open/classified/titled case now passes ``status``/``severity``/
    ``title`` EXPLICITLY — see each call site for why. Explicit beats
    implicit for a shared test helper this widely used."""
    case_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO cases (id, landlord_id, property_id, tenant_id, vendor_id, status, "
            "severity, title, langgraph_thread_id) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, :vendor_id, :status, "
            ":severity, :title, :thread_id)"
        ),
        {
            "id": case_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "vendor_id": vendor_id,
            "status": status,
            "severity": severity,
            "title": title,
            "thread_id": str(uuid.uuid4()),
        },
    )
    await session.commit()
    return case_id


async def insert_audit_log(
    session: AsyncSession,
    *,
    landlord_id: str,
    case_id: str | None = None,
    actor: str = "agent",
    action: str = "classified",
    payload: dict[str, Any] | None = None,
) -> int:
    result = await session.execute(
        text(
            "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
            "VALUES (:landlord_id, :case_id, :actor, :action, CAST(:payload AS jsonb)) "
            "RETURNING id"
        ),
        {
            "landlord_id": landlord_id,
            "case_id": case_id,
            "actor": actor,
            "action": action,
            "payload": json.dumps(payload or {}),
        },
    )
    await session.commit()
    return int(result.scalar_one())


async def insert_draft(
    session: AsyncSession,
    *,
    landlord_id: str,
    case_id: str,
    recipient: str = "tenant",
    body: str = "Thanks for letting me know, I'll look into it.",
    status: str = "pending",
    scheduled_send_at: Any = None,
    edited: bool = False,
    final_body: str | None = None,
    auto_send: bool = False,
) -> str:
    """``auto_send`` added for #60's trust-ladder tests (default ``False``,
    matching the schema's own default and every existing caller's prior
    behavior unchanged)."""
    draft_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO drafts (id, landlord_id, case_id, recipient, body, prompt_version, "
            "status, scheduled_send_at, edited, final_body, auto_send) "
            "VALUES (:id, :landlord_id, :case_id, :recipient, :body, 'v1', :status, "
            ":scheduled_send_at, :edited, :final_body, :auto_send)"
        ),
        {
            "id": draft_id,
            "landlord_id": landlord_id,
            "case_id": case_id,
            "recipient": recipient,
            "body": body,
            "status": status,
            "scheduled_send_at": scheduled_send_at,
            "edited": edited,
            "final_body": final_body,
            "auto_send": auto_send,
        },
    )
    await session.commit()
    return draft_id


async def insert_notification(
    session: AsyncSession,
    *,
    landlord_id: str,
    case_id: str | None = None,
    type_: str = "emergency_call",
    channel: str = "voice",
    status: str = "pending",
    payload: dict[str, Any] | None = None,
    acknowledged_at: Any = None,
    created_at: Any = None,
) -> str:
    """Seeds a ``notifications`` row directly (bypassing the webhook/
    escalation-chain machinery) — #213's queue-card ``notification_id``
    tests exercise the read side against a KNOWN notification state
    (type/ack/case-linkage), not against a full emergency-chain run.
    ``created_at`` defaults to the column's own DB default (``now()``) but
    can be overridden to make "latest wins" ordering deterministic in
    tests without relying on real-clock granularity between two inserts."""
    notification_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO notifications "
            "(id, landlord_id, case_id, type, channel, status, payload, acknowledged_at, "
            " created_at) "
            "VALUES (:id, :landlord_id, :case_id, :type, :channel, :status, "
            "CAST(:payload AS jsonb), :acknowledged_at, COALESCE(:created_at, now()))"
        ),
        {
            "id": notification_id,
            "landlord_id": landlord_id,
            "case_id": case_id,
            "type": type_,
            "channel": channel,
            "status": status,
            "payload": json.dumps(payload or {}),
            "acknowledged_at": acknowledged_at,
            "created_at": created_at,
        },
    )
    await session.commit()
    return notification_id


async def insert_trust_metrics(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    severity: str = "routine",
    clean_approvals: int = 0,
    edited_approvals: int = 0,
    rejections: int = 0,
    consecutive_clean: int = 0,
    autonomy_unlocked: bool = False,
    unlocked_at: Any = None,
    revoked_at: Any = None,
) -> str:
    """Seeds a ``trust_metrics`` row directly (bypassing the sender/
    rejection upserts) — #60's own test modules exercise the auto-send
    eligibility check/revoke endpoints against a KNOWN trust state, not
    against whatever a real send/reject sequence would naturally
    accumulate."""
    trust_metrics_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO trust_metrics "
            "(id, landlord_id, property_id, severity, clean_approvals, edited_approvals, "
            "rejections, consecutive_clean, autonomy_unlocked, unlocked_at, revoked_at) "
            "VALUES (:id, :landlord_id, :property_id, :severity, :clean_approvals, "
            ":edited_approvals, :rejections, :consecutive_clean, :autonomy_unlocked, "
            ":unlocked_at, :revoked_at)"
        ),
        {
            "id": trust_metrics_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "severity": severity,
            "clean_approvals": clean_approvals,
            "edited_approvals": edited_approvals,
            "rejections": rejections,
            "consecutive_clean": consecutive_clean,
            "autonomy_unlocked": autonomy_unlocked,
            "unlocked_at": unlocked_at,
            "revoked_at": revoked_at,
        },
    )
    await session.commit()
    return trust_metrics_id


async def insert_push_token(
    session: AsyncSession,
    *,
    landlord_id: str,
    platform: str = "ios",
    revoked_at: Any = None,
) -> str:
    """Seeds a ``push_tokens`` row (#210 M3) — a fresh, collision-free
    token string every call (mirrors ``fresh_phone``'s "never a real
    value" convention)."""
    push_token_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO push_tokens (id, landlord_id, token, platform, revoked_at) "
            "VALUES (:id, :landlord_id, :token, :platform, :revoked_at)"
        ),
        {
            "id": push_token_id,
            "landlord_id": landlord_id,
            "token": f"ExponentPushToken[{uuid.uuid4()}]",
            "platform": platform,
            "revoked_at": revoked_at,
        },
    )
    await session.commit()
    return push_token_id


async def insert_push_outbox(
    session: AsyncSession,
    *,
    landlord_id: str,
    device_token_id: str,
    kind: str = "draft_awaiting_approval",
    payload: dict[str, Any] | None = None,
    status: str = "pending",
    attempt: int = 0,
    next_attempt_at: Any = None,
) -> str:
    """Seeds a ``push_outbox`` row (#210 M3) directly — #210's own test
    modules exercise the sweep against a KNOWN outbox state, not against
    whatever ``mark_awaiting_approval``'s enqueue seam would naturally
    produce."""
    push_outbox_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO push_outbox "
            "(id, landlord_id, device_token_id, kind, payload, status, attempt, next_attempt_at) "
            "VALUES (:id, :landlord_id, :device_token_id, :kind, CAST(:payload AS jsonb), "
            ":status, :attempt, :next_attempt_at)"
        ),
        {
            "id": push_outbox_id,
            "landlord_id": landlord_id,
            "device_token_id": device_token_id,
            "kind": kind,
            "payload": json.dumps(payload or {}),
            "status": status,
            "attempt": attempt,
            "next_attempt_at": next_attempt_at,
        },
    )
    await session.commit()
    return push_outbox_id


__all__: list[str] = [
    "fresh_phone",
    "insert_audit_log",
    "insert_case",
    "insert_draft",
    "insert_landlord",
    "insert_message",
    "insert_message_case",
    "insert_notification",
    "insert_property",
    "insert_push_outbox",
    "insert_push_token",
    "insert_tenant",
    "insert_trust_metrics",
    "insert_vendor",
]
