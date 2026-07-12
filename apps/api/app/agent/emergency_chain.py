"""The emergency escalation chain (#108) — real execution behind the
``fire_emergency_protocol`` seam (``app/agent/emergency.py``).

Implements ``docs/02-product/emergency-prefilter.md``'s "The escalation
chain" verbatim:

    T+0     voice call to landlord (Twilio, spoken summary + "press 1 to
            acknowledge") + safety SMS to tenant (category template)
    T+2m    if unacknowledged: SMS to landlord with an ack link
    T+5m    second voice call to landlord
    T+10m   backup contact (if configured): voice call + SMS
    T+15m   third call to landlord; tenant gets an honest status update
    T+20m+  repeat the landlord+backup cycle every 15 min until acknowledged

Every timing above is a module-level constant
(:data:`ESCALATION_FIXED_OFFSETS_MINUTES` / :data:`ESCALATION_REPEAT_INTERVAL_MINUTES`)
— "configurable" (issue #108's AC) means "change these constants in one
place", never a runtime setting/env var/feature flag: CLAUDE.md rule 7
forbids feature-flag reads anywhere near the emergency path, and this
module lives in ``agent/`` where flag reads are disallowed outright.

The state machine lives entirely in the ``notifications`` table (schema-v1.md
v1.3 — the ``emergency_call`` row the webhook already creates), driven by
``status``/``attempt``/``next_attempt_at``/``acknowledged_at``. NO NEW
COLUMN OR TABLE was needed for this issue — every piece of chain state
(the ack token, which step is next, the rendered tenant-safety-SMS body)
fits inside the existing ``payload`` jsonb, following the exact pattern
schema-v1.md's v1.8 amendments already established for ``tenant_ack``/
``degraded_retry``.

The "instant + durable sweep" hybrid (crash-safety design)
--------------------------------------------------------------------------
T+0 must be genuinely instant (issue #108 AC: "sends instantly, no approval
gate") — but this module is invoked SYNCHRONOUSLY inside the Twilio SMS
webhook handler (``app/routers/webhooks/twilio.py``, which the campaign
explicitly says not to re-plumb), and that handler's crash-recovery
(redelivery) story only re-invokes ``fire_emergency_protocol`` when the
`emergency_call` notification INSERT itself is the one that created a new
row — a crash exactly inside this module would NOT be retried by Twilio.

Fixed by never depending on that retry for correctness:
:func:`handle_emergency_trigger` FIRST durably enriches the already-created
``emergency_call`` row with an ack token and ``next_attempt_at = now()``
(making attempt 0 immediately "due") and creates the ``emergency_sms``
row (the tenant safety SMS's durable send-intent — schema-v1.md's
"already-anticipated-but-unused" row, finally drained here), in one short
transaction. ONLY AFTER that commits does it attempt the real T+0 Twilio
calls, via the EXACT SAME code path
(:func:`run_emergency_chain_sweep` / :func:`_run_candidate_safely`) the
periodic 60-second ticker (``app/scheduler.py``) uses for every later step.
If the process crashes at ANY point — before, during, or after the actual
sends — the row's due state is already durable, so the very next sweep
tick (within 60s of restart) picks it up and performs whatever the crash
interrupted. There is no in-process timer anywhere in this module; the
ticker only wakes and reads due rows (never-break-adjacent design
constraint from the issue: "retries/chain state = data ... never in-process
timers for the SCHEDULE").

Idempotency — every attempt exactly-once per claim
--------------------------------------------------------------------------
Each step is claimed via a single self-guarded ``UPDATE ... WHERE id = :id
AND status = 'pending' AND attempt = :old_attempt AND acknowledged_at IS
NULL RETURNING id`` (:data:`_CLAIM_STEP_SQL`) BEFORE any Twilio call is
placed — the same TOCTOU discipline
``app/agent/degraded_mode_sweep.py``/``app/agent/case_lifecycle.py``
already use. A lost race (two concurrent claims for the same row — the
T+0 immediate call racing the periodic sweep's very first tick, or two
overlapping sweep ticks) is a silent no-op for the loser: ``"lost_race"``,
no duplicate call/SMS, matching this codebase's established precedent.

Accepted trade-off (documented, not papered over — mirrors the #44/#45
draft-sender design menu's own accepted trade-off for its ``sending``
claim): advancing ``attempt``/``next_attempt_at`` happens BEFORE the actual
Twilio calls for that step. A crash in the narrow window between a
successful claim and the Twilio call(s) completing means THAT SPECIFIC
step's call/SMS may not go out, while the NEXT scheduled step still fires
on schedule (the row's due-ness is independent of whether this step's send
succeeded). Given the chain repeats every 15 minutes indefinitely across
TWO redundant contacts (landlord + backup) until acknowledged, a single
skipped attempt at an exquisitely unlucky crash instant is a survivable
degradation, not a missed emergency — accepting this now avoids a second
``status`` value (e.g. an in-flight ``'sending'``-equivalent) that
schema-v1.md's CHECK constraint does not carry and that this issue's
scope explicitly says to avoid inventing without a STOP-and-report.

Every Twilio call/SMS failure (not just a crash) is caught, logged, and
paged to Sentry (level=error, metadata only — rule #5) by
:func:`_execute_action` and never re-raised — a bad phone number or a
Twilio outage degrades that one action to ``"failed"`` in the attempt's
audit row, but the chain keeps advancing on schedule regardless.

Category template priority (product decision made here — flag for
copy-guardian/founder confirmation)
--------------------------------------------------------------------------
A single inbound message can trip more than one Tier-0 HARD category at
once (e.g. "fire" and "gas_co" together). Plain-language-rules.md caps a
tenant safety SMS at 3 numbered lines total, so this module must pick ONE
template rather than concatenating every matched category's steps.
:data:`_CATEGORY_PRIORITY` orders ``person`` (immediate threat to a human
life) above ``security`` (an in-progress break-in) above ``fire`` above
``gas_co`` above ``water`` — a defensible but NOT rubric-dictated ordering;
flagged here explicitly for copy-guardian/founder sign-off rather than
treated as settled doctrine.

Known limitation (discovered building this, not solved here — flagged, not
silently patched over)
--------------------------------------------------------------------------
The tenant safety SMS needs a phone number to send to. This module only
ever has ``tenants.phone`` (via ``messages.tenant_id``), never a raw
"From" number lifted off the original inbound webhook request — no column
for that exists on ``messages`` (schema-v1.md), and adding one is exactly
the kind of new-column decision this issue's scope requires a STOP for
rather than inventing unilaterally. In the overwhelming common case
(a registered tenant) this is a non-issue. The edge case — a Tier-0 HARD
hit arriving from a phone number that matches no ``tenants`` row for that
property — has NO stored channel back to that specific sender: the
landlord/backup escalation chain still runs in full (nothing here
depends on tenant_id), but the tenant-facing safety SMS action is recorded
as ``"skipped"`` (``reason="no_tenant_phone"``), never silently dropped
with no trace. Left as a discovered gap for the spec to record, not solved
unilaterally here.

DB access
---------
Admin engine (pre-identity/background context — no landlord JWT exists for
either the webhook-triggered T+0 path or the scheduled sweep). Allowlisted
in ``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``. All Twilio
sends go through ``app/integrations/twilio_send.py::get_twilio_sender()`` —
this module and that one are the ONLY two files allowed to reference it,
machine-enforced by ``tests/test_twilio_send_allowlist.py``.
"""

from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager as _acm
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import sentry_sdk
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.twiml.voice_response import Gather, VoiceResponse

from app.config import settings
from app.db.session import get_admin_session
from app.integrations.twilio_send import TwilioSender, get_twilio_sender

log = structlog.get_logger(__name__)
# ``_acm(get_admin_session)()`` turns the generator function into an async
# context manager -- same idiom as ``app/agent/degraded_mode_sweep.py``.

# ---------------------------------------------------------------------------
# Escalation timing — the configurable schedule (issue #108 AC)
# ---------------------------------------------------------------------------

ESCALATION_FIXED_OFFSETS_MINUTES: tuple[int, ...] = (0, 2, 5, 10, 15, 20)
"""Minutes from the chain's start (``notifications.created_at`` of the
``emergency_call`` row, i.e. T+0) that attempts 0..5 are due —
emergency-prefilter.md's T+0/2/5/10/15/20 table. Configurable: edit this
tuple (and/or :data:`ESCALATION_REPEAT_INTERVAL_MINUTES`) in one place —
never sourced from settings/env/a feature flag."""

ESCALATION_REPEAT_INTERVAL_MINUTES: int = 15
"""Once :data:`ESCALATION_FIXED_OFFSETS_MINUTES` is exhausted (attempt
index >= 6), the landlord+backup cycle repeats every this many minutes,
forever, until acknowledged — "T+20m+ repeat ... every 15 min"."""


def next_offset_minutes(attempt: int) -> int:
    """Pure: minutes from T+0 the attempt numbered *attempt* (0-indexed —
    how many attempts have ALREADY been made) is due. Mirrors
    ``app/agent/degraded_mode_sweep.py::next_retry_at``'s "absolute offset
    from the start, never relative to now" convention."""
    if attempt < len(ESCALATION_FIXED_OFFSETS_MINUTES):
        return ESCALATION_FIXED_OFFSETS_MINUTES[attempt]
    cycles_past_fixed = attempt - len(ESCALATION_FIXED_OFFSETS_MINUTES) + 1
    return (
        ESCALATION_FIXED_OFFSETS_MINUTES[-1]
        + cycles_past_fixed * ESCALATION_REPEAT_INTERVAL_MINUTES
    )


# Action tags — see module docstring's escalation-chain table.
_ACTION_LANDLORD_CALL = "landlord_call"
_ACTION_LANDLORD_SMS = "landlord_sms"
_ACTION_BACKUP_CALL = "backup_call"
_ACTION_BACKUP_SMS = "backup_sms"
_ACTION_TENANT_SAFETY_SMS = "tenant_safety_sms"
_ACTION_TENANT_STATUS_SMS = "tenant_status_sms"


def actions_for_step(step: int) -> tuple[str, ...]:
    """Pure: which action(s) the chain performs at step *step* (0-indexed —
    step 0 is T+0, step 1 is T+2m, ... step 5+ is the T+20m+ repeating
    cycle). Encodes emergency-prefilter.md's escalation table exactly."""
    if step == 0:
        return (_ACTION_LANDLORD_CALL, _ACTION_TENANT_SAFETY_SMS)
    if step == 1:
        return (_ACTION_LANDLORD_SMS,)
    if step == 2:
        return (_ACTION_LANDLORD_CALL,)
    if step == 3:
        return (_ACTION_BACKUP_CALL, _ACTION_BACKUP_SMS)
    if step == 4:
        return (_ACTION_LANDLORD_CALL, _ACTION_TENANT_STATUS_SMS)
    return (_ACTION_LANDLORD_CALL, _ACTION_BACKUP_CALL, _ACTION_BACKUP_SMS)


# ---------------------------------------------------------------------------
# Copy — tenant safety SMS (category-templated), landlord/backup alerts,
# the T+15m honest tenant status update. Every string here is what
# copy-guardian reviews verbatim (see module docstring).
# ---------------------------------------------------------------------------

_CATEGORY_PRIORITY: tuple[str, ...] = ("person", "security", "fire", "gas_co", "water")
"""See module docstring "Category template priority" — a product decision
made here, flagged for copy-guardian/founder confirmation, not settled
doctrine."""

_CATEGORY_SHORT_LABELS: dict[str, str] = {
    "fire": "a fire",
    "gas_co": "a gas or CO leak",
    "water": "a serious water leak",
    "security": "a break-in",
    "person": "a medical emergency",
}

# plain-language-rules.md: grade-5, <=15-word sentences, max 3 numbered
# steps, concrete over relative, no jargon. Each line below is well under
# 15 words. Copy-guardian reviews these verbatim — see module docstring.
_TENANT_SAFETY_SMS_TEMPLATES: dict[str, str] = {
    "fire": ("1. Get out of the unit now.\n2. Call 911 once you're outside.\n3. Don't go back in."),
    "gas_co": (
        "1. Leave the unit right now.\n"
        "2. Don't flip switches or light anything.\n"
        "3. Call 911 from outside."
    ),
    "water": (
        "1. Stay away from the water.\n"
        "2. Don't touch outlets or switches near it.\n"
        "3. Call 911 if you're not sure it's safe."
    ),
    "security": (
        "1. Get somewhere safe and lock the door.\n"
        "2. Call 911 now.\n"
        "3. Stay on the line until help arrives."
    ),
    "person": (
        "1. Call 911 right now.\n"
        "2. Stay with them if it's safe to.\n"
        "3. Unlock the door for paramedics if you can."
    ),
}

_TENANT_SAFETY_SMS_FALLBACK: str = (
    "1. If you're in danger, get to safety.\n"
    "2. Call 911.\n"
    "3. Stay somewhere safe until help arrives."
)
"""Defensive-only: reached iff Tier-0 ever fires a HARD category outside
the five ``PrefilterResult.categories`` values documented in
``docs/02-product/emergency-prefilter.md`` — should never happen in
practice, kept so a genuinely-unexpected category still gets a safe,
plain-language instruction rather than no message at all."""

TENANT_STATUS_TEMPLATE: str = (
    "Still reaching {landlord_label} — if the situation is getting dangerous, call 911."
)
"""Verbatim from ``docs/02-product/emergency-prefilter.md``'s T+15m "honest
tenant status" line."""

_FALLBACK_LANDLORD_LABEL: str = "your landlord"
_FALLBACK_TENANT_LABEL: str = "the tenant"


def choose_primary_category(categories: list[str]) -> str:
    """Pure: pick the ONE category to template against when more than one
    Tier-0 HARD category fired — see module docstring "Category template
    priority"."""
    for category in _CATEGORY_PRIORITY:
        if category in categories:
            return category
    return categories[0] if categories else "unknown"


def category_short_label(category: str) -> str:
    """Pure, public accessor for :data:`_CATEGORY_SHORT_LABELS` — used by
    ``app/routers/webhooks/twilio.py``'s ``/voice`` handler when rendering
    the initial TwiML fetch (never reaches into this module's private
    dict directly)."""
    return _CATEGORY_SHORT_LABELS.get(category, "an emergency")


def render_tenant_safety_sms(categories: list[str]) -> tuple[str, str]:
    """Pure: ``(chosen_category, body)`` for the T+0 tenant safety SMS."""
    category = choose_primary_category(categories)
    return category, _TENANT_SAFETY_SMS_TEMPLATES.get(category, _TENANT_SAFETY_SMS_FALLBACK)


def render_tenant_status_sms(landlord_label: str) -> str:
    """Pure: the T+15m honest status update sent to the tenant."""
    return TENANT_STATUS_TEMPLATE.format(landlord_label=landlord_label)


def render_landlord_alert_sms(
    *, property_label: str, category_label: str, tenant_label: str, ack_url: str
) -> str:
    """Pure: the T+2m (and every repeat-cycle) SMS to the landlord —
    emergency-prefilter.md's "🚨 EMERGENCY at ⟨property⟩: ⟨summary⟩. Call
    ⟨tenant⟩ or press link to acknowledge." template."""
    return (
        f"\U0001f6a8 EMERGENCY at {property_label}: {category_label}. "
        f"Call {tenant_label} or tap to acknowledge: {ack_url}"
    )


def render_backup_alert_sms(
    *,
    property_label: str,
    category_label: str,
    landlord_label: str,
    tenant_label: str,
    ack_url: str,
) -> str:
    """Pure: the T+10m/repeat-cycle SMS to the backup contact — same
    template as :func:`render_landlord_alert_sms`, plus a line noting the
    landlord hasn't answered."""
    return (
        f"\U0001f6a8 EMERGENCY at {property_label}: {category_label}. "
        f"{landlord_label} hasn't answered. Call {tenant_label} or tap to acknowledge: {ack_url}"
    )


def build_voice_twiml(*, property_label: str, category_label: str, action_url: str) -> str:
    """Pure: the TwiML for a landlord/backup voice call — spoken summary +
    "press 1 to acknowledge" (issue #108 AC), single TwiML app. If no digit
    arrives within the ``Gather`` timeout, Twilio falls through to the
    trailing ``<Say>`` and ends the call WITHOUT a second request — the
    chain's next scheduled attempt (not this call) is what tries again."""
    response = VoiceResponse()
    gather = Gather(num_digits=1, action=action_url, method="POST", timeout=10)
    gather.say(
        f"This is Stoop with an emergency at {property_label}. "
        f"A tenant reported {category_label}. Press 1 to acknowledge."
    )
    response.append(gather)
    response.say("No response received. Stoop will try again shortly. Goodbye.")
    return str(response)


def build_ack_confirmation_twiml() -> str:
    """Pure: TwiML spoken after a genuine ``Digits=1`` acknowledgment."""
    response = VoiceResponse()
    response.say("Thanks, got it.")
    return str(response)


def build_error_twiml() -> str:
    """Pure: TwiML fallback for a missing/malformed/unknown notification id
    on the voice webhook — never a raw 500 to Twilio (see
    ``app/routers/webhooks/twilio.py``'s voice handler)."""
    response = VoiceResponse()
    response.say("Sorry, something went wrong. Goodbye.")
    return str(response)


def _first_name(full_name: str | None) -> str | None:
    """Same convention as ``app/agent/nodes/degraded_mode.py::_first_name``
    — duplicated rather than imported, per this codebase's established
    "small, stable helper, not worth a cross-module private import"
    pattern."""
    if not full_name:
        return None
    stripped = full_name.strip()
    return stripped.split()[0] if stripped else None


def _landlord_label(full_name: str | None) -> str:
    return _first_name(full_name) or _FALLBACK_LANDLORD_LABEL


def render_voice_action_url(notification_id: UUID) -> str:
    """The URL Twilio fetches TwiML from / POSTs gathered digits to for
    *notification_id*'s voice calls — ``POST /webhooks/twilio/voice``,
    parameterized so the SAME handler can identify which chain a given
    call/digit-gather belongs to (never a phone number or message body in
    the query string — just an opaque notification id, rule #5).

    Falls back to a local-dev placeholder when ``settings.public_base_url``
    is unset (never raises) — a REAL Twilio call still can't reach
    ``localhost`` from Twilio's network, but this keeps the function total
    so local/test callers never need to special-case an unset
    ``public_base_url``; production already REQUIRES it be set
    (``app/config.py::_require_public_base_url_in_production``).
    """
    base = (settings.public_base_url or "http://localhost:8000").rstrip("/")
    return f"{base}/webhooks/twilio/voice?notification_id={notification_id}"


def render_ack_url(notification_id: UUID, ack_token: str) -> str:
    """The tokenized ``GET /ack/{token}`` link embedded in landlord/backup
    SMS alerts — api-contracts.md's "also reachable via tokenized GET link
    from SMS: /ack/{token}". *notification_id* is accepted for a consistent
    call-site shape with :func:`render_voice_action_url` but is not part of
    the URL itself (the token alone is the lookup key — see
    :func:`acknowledge_by_token`)."""
    del notification_id  # unused — see docstring
    base = (settings.public_base_url or "http://localhost:8000").rstrip("/")
    return f"{base}/ack/{ack_token}"


# ---------------------------------------------------------------------------
# Context — everything needed to actually place a call / send an SMS,
# re-derived FRESH at each attempt (never snapshotted into payload) so a
# landlord/tenant contact-info edit between attempts is always honored.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmergencyContext:
    property_label: str
    twilio_number: str | None
    backup_contact: dict[str, Any] | None
    landlord_phone: str | None
    landlord_full_name: str | None
    tenant_name: str | None
    tenant_phone: str | None


_SELECT_CONTEXT_SQL = text(
    """
    SELECT
      p.label AS property_label,
      p.twilio_number AS twilio_number,
      p.backup_contact AS backup_contact,
      l.phone AS landlord_phone,
      l.full_name AS landlord_full_name,
      t.name AS tenant_name,
      t.phone AS tenant_phone
    FROM messages m
    JOIN properties p ON p.id = m.property_id
    JOIN landlords l ON l.id = m.landlord_id
    LEFT JOIN tenants t ON t.id = m.tenant_id
    WHERE m.id = :message_id
    """
)


async def _load_context(session: AsyncSession, message_id: UUID) -> EmergencyContext | None:
    row = (
        (await session.execute(_SELECT_CONTEXT_SQL, {"message_id": str(message_id)}))
        .mappings()
        .one_or_none()
    )
    if row is None:  # pragma: no cover — invariant: messages are never deleted
        return None
    return EmergencyContext(
        property_label=row["property_label"],
        twilio_number=row["twilio_number"],
        backup_contact=row["backup_contact"],
        landlord_phone=row["landlord_phone"],
        landlord_full_name=row["landlord_full_name"],
        tenant_name=row["tenant_name"],
        tenant_phone=row["tenant_phone"],
    )


def _backup_phone(backup_contact: dict[str, Any] | None) -> str | None:
    if not backup_contact:
        return None
    phone = backup_contact.get("phone")
    return phone if isinstance(phone, str) and phone else None


# ---------------------------------------------------------------------------
# Per-action execution — never raises (Twilio failures are caught and
# recorded as a "failed" outcome; see module docstring).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionOutcome:
    action: str
    status: str  # "sent" | "failed" | "skipped"
    sid: str | None = None
    exc_type: str | None = None
    reason: str | None = None  # set only when status == "skipped"


async def _execute_action(
    sender: TwilioSender,
    action: str,
    ctx: EmergencyContext,
    *,
    categories: list[str],
    notification_id: UUID,
    ack_token: str,
) -> ActionOutcome:
    if not ctx.twilio_number:
        return ActionOutcome(action=action, status="skipped", reason="no_twilio_number")
    from_number = ctx.twilio_number

    ack_url = render_ack_url(notification_id, ack_token)
    action_url = render_voice_action_url(notification_id)
    landlord_label = _landlord_label(ctx.landlord_full_name)
    tenant_label = ctx.tenant_name or _FALLBACK_TENANT_LABEL
    category = choose_primary_category(categories)
    category_label = _CATEGORY_SHORT_LABELS.get(category, "an emergency")

    try:
        if action == _ACTION_LANDLORD_CALL:
            if not ctx.landlord_phone:
                return ActionOutcome(action=action, status="skipped", reason="no_landlord_phone")
            sid = await sender.create_call(
                to=ctx.landlord_phone, from_=from_number, twiml_url=action_url
            )
            return ActionOutcome(action=action, status="sent", sid=sid)

        if action == _ACTION_LANDLORD_SMS:
            if not ctx.landlord_phone:
                return ActionOutcome(action=action, status="skipped", reason="no_landlord_phone")
            body = render_landlord_alert_sms(
                property_label=ctx.property_label,
                category_label=category_label,
                tenant_label=tenant_label,
                ack_url=ack_url,
            )
            sid = await sender.send_sms(to=ctx.landlord_phone, from_=from_number, body=body)
            return ActionOutcome(action=action, status="sent", sid=sid)

        if action in (_ACTION_BACKUP_CALL, _ACTION_BACKUP_SMS):
            backup_phone = _backup_phone(ctx.backup_contact)
            if not backup_phone:
                return ActionOutcome(action=action, status="skipped", reason="no_backup_contact")
            if action == _ACTION_BACKUP_CALL:
                sid = await sender.create_call(
                    to=backup_phone, from_=from_number, twiml_url=action_url
                )
            else:
                body = render_backup_alert_sms(
                    property_label=ctx.property_label,
                    category_label=category_label,
                    landlord_label=landlord_label,
                    tenant_label=tenant_label,
                    ack_url=ack_url,
                )
                sid = await sender.send_sms(to=backup_phone, from_=from_number, body=body)
            return ActionOutcome(action=action, status="sent", sid=sid)

        if action == _ACTION_TENANT_SAFETY_SMS:
            if not ctx.tenant_phone:
                return ActionOutcome(action=action, status="skipped", reason="no_tenant_phone")
            _, body = render_tenant_safety_sms(categories)
            sid = await sender.send_sms(to=ctx.tenant_phone, from_=from_number, body=body)
            return ActionOutcome(action=action, status="sent", sid=sid)

        if action == _ACTION_TENANT_STATUS_SMS:
            if not ctx.tenant_phone:
                return ActionOutcome(action=action, status="skipped", reason="no_tenant_phone")
            body = render_tenant_status_sms(landlord_label)
            sid = await sender.send_sms(to=ctx.tenant_phone, from_=from_number, body=body)
            return ActionOutcome(action=action, status="sent", sid=sid)

        return ActionOutcome(  # pragma: no cover
            action=action, status="skipped", reason="unknown_action"
        )
    except Exception as exc:
        log.error("emergency_chain_action_failed", action=action, exc_type=type(exc).__name__)
        sentry_sdk.capture_message(
            "emergency_chain: action failed",
            level="error",
            extras={
                "notification_id": str(notification_id),
                "action": action,
                "exc_type": type(exc).__name__,
            },
        )
        return ActionOutcome(action=action, status="failed", exc_type=type(exc).__name__)


# ---------------------------------------------------------------------------
# Candidate loading, claiming, and step processing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmergencyCallCandidate:
    notification_id: UUID
    landlord_id: UUID
    attempt: int
    message_id: UUID
    property_id: UUID
    categories: list[str]
    ack_token: str | None
    chain_started_at: datetime
    """The chain's T+0 instant — ``notifications.created_at`` of the
    ``emergency_call`` row (set once, at the webhook's original INSERT,
    never touched again). EVERY step's ``next_attempt_at`` is computed as
    an offset from THIS value, never from "now" at claim time — see
    :func:`next_offset_minutes`'s own docstring ("absolute offset from the
    chain's start"). Using the per-tick ``now`` instead would silently
    stretch the schedule by however late each tick happened to run."""


def _candidate_from_row(row: dict[str, Any]) -> EmergencyCallCandidate:
    payload = cast("dict[str, Any]", row["payload"])
    return EmergencyCallCandidate(
        notification_id=cast("UUID", row["id"]),
        landlord_id=cast("UUID", row["landlord_id"]),
        attempt=cast("int", row["attempt"]),
        message_id=UUID(str(payload["message_id"])),
        property_id=UUID(str(payload["property_id"])),
        categories=list(payload.get("categories") or []),
        ack_token=cast("str | None", payload.get("ack_token")),
        chain_started_at=cast("datetime", row["created_at"]),
    )


_CLAIM_STEP_SQL = text(
    """
    UPDATE notifications
    SET attempt = :new_attempt, next_attempt_at = :next_attempt_at, updated_at = now()
    WHERE id = :id AND status = 'pending' AND attempt = :old_attempt AND acknowledged_at IS NULL
    RETURNING id
    """
)

_INSERT_ATTEMPT_AUDIT_SQL = text(
    """
    INSERT INTO audit_log (landlord_id, case_id, actor, action, payload)
    VALUES (:landlord_id, NULL, 'system', 'emergency_call_attempt', CAST(:payload AS jsonb))
    """
)

_MARK_EMERGENCY_SMS_SQL = text(
    """
    UPDATE notifications SET status = :status, updated_at = now()
    WHERE type = 'emergency_sms' AND payload ->> 'message_id' = :message_id AND status = 'pending'
    """
)


async def _mark_emergency_sms_status(
    session: AsyncSession, message_id: UUID, outcomes: list[ActionOutcome]
) -> None:
    sms_outcome = next((o for o in outcomes if o.action == _ACTION_TENANT_SAFETY_SMS), None)
    if sms_outcome is None or sms_outcome.status == "skipped":
        return
    status = "sent" if sms_outcome.status == "sent" else "failed"
    await session.execute(
        _MARK_EMERGENCY_SMS_SQL, {"status": status, "message_id": str(message_id)}
    )


async def _process_due_row(candidate: EmergencyCallCandidate) -> str:
    """Claim + execute exactly ONE step for *candidate* (may raise — see
    :func:`_run_candidate_safely` for the never-raises wrapper both the
    sweep and the T+0 immediate call use). Whether this candidate is DUE
    was already decided by the caller's own SELECT — the NEXT
    ``next_attempt_at`` is computed from ``candidate.chain_started_at``,
    never from "now" at claim time, so a late-running tick never
    stretches the schedule (see :class:`EmergencyCallCandidate`'s own
    docstring)."""
    step = candidate.attempt
    new_attempt = step + 1
    next_at = candidate.chain_started_at + timedelta(minutes=next_offset_minutes(new_attempt))

    async with _acm(get_admin_session)() as session:
        claim_row = (
            (
                await session.execute(
                    _CLAIM_STEP_SQL,
                    {
                        "id": str(candidate.notification_id),
                        "old_attempt": candidate.attempt,
                        "new_attempt": new_attempt,
                        "next_attempt_at": next_at,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if claim_row is None:
            return "lost_race"

        ctx = await _load_context(session, candidate.message_id)

    if ctx is None:  # pragma: no cover — invariant: messages are never deleted
        log.error("emergency_chain_context_missing", notification_id=str(candidate.notification_id))
        sentry_sdk.capture_message(
            "emergency_chain: context missing for a claimed attempt",
            level="error",
            extras={"notification_id": str(candidate.notification_id)},
        )
        return "context_missing"

    if candidate.ack_token is None:  # pragma: no cover — invariant: enriched before first due
        log.error(
            "emergency_chain_missing_ack_token", notification_id=str(candidate.notification_id)
        )
        return "missing_ack_token"

    sender = get_twilio_sender()
    actions = actions_for_step(step)
    outcomes: list[ActionOutcome] = []
    for action in actions:
        outcomes.append(
            await _execute_action(
                sender,
                action,
                ctx,
                categories=candidate.categories,
                notification_id=candidate.notification_id,
                ack_token=candidate.ack_token,
            )
        )

    async with _acm(get_admin_session)() as session:
        await session.execute(
            _INSERT_ATTEMPT_AUDIT_SQL,
            {
                "landlord_id": str(candidate.landlord_id),
                "payload": json.dumps(
                    {
                        "notification_id": str(candidate.notification_id),
                        "message_id": str(candidate.message_id),
                        "step": step,
                        "actions": [asdict(outcome) for outcome in outcomes],
                    }
                ),
            },
        )
        if _ACTION_TENANT_SAFETY_SMS in actions:
            await _mark_emergency_sms_status(session, candidate.message_id, outcomes)

    log.info(
        "emergency_chain_step_processed",
        notification_id=str(candidate.notification_id),
        step=step,
        outcomes=[outcome.status for outcome in outcomes],
    )
    return "processed"


async def _run_candidate_safely(candidate: EmergencyCallCandidate) -> str:
    """Never-raises wrapper around :func:`_process_due_row` — used by BOTH
    the T+0 immediate call (:func:`handle_emergency_trigger`) and the
    periodic sweep (:func:`run_emergency_chain_sweep`). ANY exception is
    logged AND paged to Sentry (metadata only, rule #5) — never silent —
    and never propagated: the row's own schedule already advanced (or
    didn't, on a lost race) independently of whether this call succeeds,
    so there is no "stuck forever" risk requiring a bounded-retry counter
    the way ``app/agent/degraded_mode_sweep.py`` needs one."""
    try:
        return await _process_due_row(candidate)
    except Exception as exc:
        log.error(
            "emergency_chain_step_processing_failed",
            notification_id=str(candidate.notification_id),
            step=candidate.attempt,
            exc_type=type(exc).__name__,
        )
        sentry_sdk.capture_message(
            "emergency_chain: step processing raised",
            level="error",
            extras={
                "notification_id": str(candidate.notification_id),
                "step": candidate.attempt,
                "exc_type": type(exc).__name__,
            },
        )
        return "processing_error"


# ---------------------------------------------------------------------------
# T+0 — the "instant" trigger (called by app/agent/emergency.py)
# ---------------------------------------------------------------------------

_ENRICH_EMERGENCY_CALL_SQL = text(
    """
    UPDATE notifications
    SET payload = payload || CAST(:extra AS jsonb),
        next_attempt_at = :next_attempt_at, updated_at = now()
    WHERE id = :id AND status = 'pending' AND next_attempt_at IS NULL
    RETURNING landlord_id, created_at
    """
)

_INSERT_EMERGENCY_SMS_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'emergency_sms', 'sms', 'pending', CAST(:payload AS jsonb))
    RETURNING id
    """
)


async def handle_emergency_trigger(
    *,
    notification_id: UUID,
    message_id: UUID,
    property_id: UUID,
    categories: list[str],
) -> None:
    """The T+0 orchestration — see module docstring "The instant + durable
    sweep hybrid". Called by ``app/agent/emergency.py::fire_emergency_protocol``
    exactly once per genuinely-new escalation (gated by the webhook's own
    ``uq_notifications_message_dedupe`` INSERT — see that module).

    Deliberately does NOT take ``landlord_id`` as a parameter (unlike the
    other fields, which match ``fire_emergency_protocol``'s existing
    signature exactly, left UNCHANGED per the campaign's "do not re-plumb
    the webhook" instruction): the webhook already wrote it onto the
    ``emergency_call`` row it created, so step 1 below reads it straight
    back via that same row's ``RETURNING`` clause instead of requiring a
    new parameter/call-site edit.

    1. Durably enrich the ALREADY-CREATED ``emergency_call`` row (ack
       token + ``next_attempt_at = now()``, making attempt 0 immediately
       due) and create the ``emergency_sms`` durable send-intent row, in
       ONE short transaction. Idempotent: guarded by
       ``next_attempt_at IS NULL`` — a second call (should never happen
       given the upstream gate, but cheap defense-in-depth) is a no-op.
    2. Attempt the real T+0 sends immediately, via the SAME
       :func:`_run_candidate_safely` the periodic sweep uses — never
       raises; a failure here is fully recovered by the next sweep tick
       (the row is already due).
    """
    now = datetime.now(UTC)
    ack_token = secrets.token_urlsafe(24)
    category, body = render_tenant_safety_sms(categories)

    async with _acm(get_admin_session)() as session:
        enriched = (
            (
                await session.execute(
                    _ENRICH_EMERGENCY_CALL_SQL,
                    {
                        "id": str(notification_id),
                        "extra": json.dumps({"ack_token": ack_token}),
                        "next_attempt_at": now,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if enriched is None:
            # Already enriched by an earlier call -- see docstring above.
            log.info("emergency_chain_already_enriched", notification_id=str(notification_id))
            return

        landlord_id = cast("UUID", enriched["landlord_id"])
        chain_started_at = cast("datetime", enriched["created_at"])

        await session.execute(
            _INSERT_EMERGENCY_SMS_SQL,
            {
                "landlord_id": str(landlord_id),
                "payload": json.dumps(
                    {
                        "message_id": str(message_id),
                        "property_id": str(property_id),
                        "category": category,
                        "body": body,
                    }
                ),
            },
        )

    candidate = EmergencyCallCandidate(
        notification_id=notification_id,
        landlord_id=landlord_id,
        attempt=0,
        message_id=message_id,
        property_id=property_id,
        categories=categories,
        ack_token=ack_token,
        chain_started_at=chain_started_at,
    )
    outcome = await _run_candidate_safely(candidate)
    log.info("emergency_chain_t0_handled", notification_id=str(notification_id), outcome=outcome)


# ---------------------------------------------------------------------------
# The periodic sweep (60s ticker, app/scheduler.py)
# ---------------------------------------------------------------------------

_SELECT_DUE_EMERGENCY_CALLS_SQL = text(
    """
    SELECT id, landlord_id, attempt, payload, created_at
    FROM notifications
    WHERE type = 'emergency_call' AND status = 'pending' AND acknowledged_at IS NULL
      AND next_attempt_at IS NOT NULL AND next_attempt_at <= :now
    ORDER BY next_attempt_at
    """
)


@dataclass(frozen=True)
class EmergencyChainOutcome:
    notification_id: UUID
    outcome: str


async def run_emergency_chain_sweep(*, now: datetime | None = None) -> list[EmergencyChainOutcome]:
    """DB entrypoint for one sweep tick — mirrors
    ``app/agent/degraded_mode_sweep.py::sweep_degraded_mode_retries``.
    Nothing calls this today except ``app/scheduler.py``'s 60-second
    ticker (wired into ``app/main.py``'s lifespan)."""
    effective_now = now or datetime.now(UTC)

    async with _acm(get_admin_session)() as session:
        rows = (
            (await session.execute(_SELECT_DUE_EMERGENCY_CALLS_SQL, {"now": effective_now}))
            .mappings()
            .all()
        )
        candidates = [_candidate_from_row(dict(row)) for row in rows]

    outcomes: list[EmergencyChainOutcome] = []
    for candidate in candidates:
        outcome = await _run_candidate_safely(candidate)
        outcomes.append(
            EmergencyChainOutcome(notification_id=candidate.notification_id, outcome=outcome)
        )

    log.info("emergency_chain_sweep_complete", candidates_processed=len(outcomes))
    return outcomes


# ---------------------------------------------------------------------------
# Acknowledgment — press-1 / SMS-link-tap / dashboard case-open (issue #108 AC)
# ---------------------------------------------------------------------------

_ACK_UPDATE_SQL = text(
    """
    UPDATE notifications
    SET status = 'acknowledged', acknowledged_at = now(), updated_at = now()
    WHERE id = :id AND acknowledged_at IS NULL
    RETURNING acknowledged_at
    """
)

_SELECT_NOTIFICATION_FOR_AUDIT_SQL = text(
    "SELECT landlord_id, case_id, payload, acknowledged_at FROM notifications WHERE id = :id"
)

_SELECT_NOTIFICATION_BY_TOKEN_SQL = text(
    "SELECT id FROM notifications WHERE payload ->> 'ack_token' = :token"
)

_INSERT_ACK_AUDIT_SQL = text(
    """
    INSERT INTO audit_log (landlord_id, case_id, actor, action, payload)
    VALUES (:landlord_id, :case_id, :actor, 'acknowledged', CAST(:payload AS jsonb))
    """
)


async def acknowledge_notification(
    notification_id: UUID, *, actor: str, channel: str
) -> datetime | None:
    """Idempotently acknowledge *notification_id* — stamps
    ``acknowledged_at`` and stops the chain (every future claim's
    ``acknowledged_at IS NULL`` guard then fails, so no further attempt is
    ever scheduled). Safe to call concurrently from any of the three ack
    surfaces (press-1, SMS link tap, dashboard case-open): only the FIRST
    caller's ``UPDATE`` sets the timestamp; every later caller (including a
    genuine race) observes the SAME already-set value and does not write a
    duplicate ``audit_log`` row.

    Returns the acknowledged instant (whether just-set by THIS call or
    already set by an earlier one), or ``None`` if *notification_id*
    does not exist at all.

    ``actor`` — ``audit_log.actor``: ``'landlord'`` for the authenticated
    dashboard path, ``'system'`` for the press-1/SMS-link paths (neither
    can cryptographically verify WHICH human is on the other end of a
    private phone/SMS channel — landlord or their backup contact).
    ``channel`` is a short, static label (``'voice_keypress'``,
    ``'sms_link'``, ``'dashboard'``) recorded in the audit payload.
    """
    async with _acm(get_admin_session)() as session:
        claimed = (
            (await session.execute(_ACK_UPDATE_SQL, {"id": str(notification_id)}))
            .mappings()
            .one_or_none()
        )

        if claimed is not None:
            notif_row = (
                (
                    await session.execute(
                        _SELECT_NOTIFICATION_FOR_AUDIT_SQL, {"id": str(notification_id)}
                    )
                )
                .mappings()
                .one()
            )
            payload = cast("dict[str, Any]", notif_row["payload"] or {})
            await session.execute(
                _INSERT_ACK_AUDIT_SQL,
                {
                    "landlord_id": str(notif_row["landlord_id"]),
                    "case_id": str(notif_row["case_id"]) if notif_row["case_id"] else None,
                    "actor": actor,
                    "payload": json.dumps(
                        {
                            "notification_id": str(notification_id),
                            "channel": channel,
                            "message_id": payload.get("message_id"),
                        }
                    ),
                },
            )
            log.info(
                "emergency_notification_acknowledged",
                notification_id=str(notification_id),
                channel=channel,
            )
            return cast("datetime", claimed["acknowledged_at"])

        existing = (
            (
                await session.execute(
                    _SELECT_NOTIFICATION_FOR_AUDIT_SQL, {"id": str(notification_id)}
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            return None
        return cast("datetime | None", existing["acknowledged_at"])


async def acknowledge_by_token(token: str, *, channel: str) -> tuple[UUID, datetime] | None:
    """Resolve the tokenized ``GET /ack/{token}`` link to a notification id
    and acknowledge it — ``None`` if *token* matches no notification at
    all. See :func:`acknowledge_notification` for idempotency semantics."""
    async with _acm(get_admin_session)() as session:
        row = (
            (await session.execute(_SELECT_NOTIFICATION_BY_TOKEN_SQL, {"token": token}))
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        notification_id = cast("UUID", row["id"])

    acknowledged_at = await acknowledge_notification(
        notification_id, actor="system", channel=channel
    )
    if acknowledged_at is None:  # pragma: no cover — invariant: row existed a moment ago
        return None
    return notification_id, acknowledged_at


__all__: list[str] = [
    "ESCALATION_FIXED_OFFSETS_MINUTES",
    "ESCALATION_REPEAT_INTERVAL_MINUTES",
    "TENANT_STATUS_TEMPLATE",
    "ActionOutcome",
    "EmergencyCallCandidate",
    "EmergencyChainOutcome",
    "EmergencyContext",
    "acknowledge_by_token",
    "acknowledge_notification",
    "actions_for_step",
    "build_ack_confirmation_twiml",
    "build_error_twiml",
    "build_voice_twiml",
    "category_short_label",
    "choose_primary_category",
    "handle_emergency_trigger",
    "next_offset_minutes",
    "render_ack_url",
    "render_backup_alert_sms",
    "render_landlord_alert_sms",
    "render_tenant_safety_sms",
    "render_tenant_status_sms",
    "render_voice_action_url",
    "run_emergency_chain_sweep",
]
