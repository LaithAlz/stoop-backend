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

Safety review, 2026-07-12 (finding N1, BLOCKING) — CORRECTED account, born
enriched, not enriched-after-the-fact
--------------------------------------------------------------------------
An earlier revision fixed this by having :func:`handle_emergency_trigger`
FIRST durably enrich the already-created ``emergency_call`` row (ack token
+ ``next_attempt_at = now()``) in its OWN short transaction, THEN attempt
the real T+0 sends. That left a real "pre-enrich silence window": a crash
between the webhook's ``emergency_call`` INSERT and THIS module's
enrichment transaction committing left the row at ``next_attempt_at IS
NULL`` forever — durable, but invisible to the sweep (which required
``next_attempt_at IS NOT NULL``), with nothing left to retry it (Twilio
already got its 200 for the SMS webhook).

Fixed properly by moving the enrichment ONE LEVEL UP: the webhook's own
``emergency_call`` INSERT
(``app/routers/webhooks/twilio.py::_INSERT_EMERGENCY_NOTIFICATION_SQL``)
now sets ``next_attempt_at = now()`` AND writes a fresh ``ack_token``
into ``payload``, in the SAME transaction/statement that creates the row
— "born enriched". The row is sweep-recoverable the INSTANT it is
durable, with ZERO dependency on this module ever running at all, let
alone completing. Belt 2 (defense in depth for a legacy/edge row that
somehow still lacks this): :data:`_SELECT_DUE_EMERGENCY_CALLS_SQL` also
treats a ``next_attempt_at IS NULL`` row as due (ordered first via
``NULLS FIRST``), and :data:`_CLAIM_STEP_SQL` supplies a fresh
``ack_token`` at claim time for any row that still lacks one.

:func:`handle_emergency_trigger` is therefore now a purely BEST-EFFORT
immediate attempt at processing step 0 — it does no durable write of its
own (see its own docstring): it reads back ``landlord_id``/``created_at``
and delegates to :func:`_run_candidate_safely`, the EXACT SAME claim-
guarded code path (:func:`_process_due_row`) the periodic 60-second
ticker (``app/scheduler.py``) uses for every later step (and now ALSO for
step 0, on a crash-recovered or healed row). ``_process_due_row`` creates
the durable ``emergency_sms`` row (the tenant safety SMS's send-intent —
schema-v1.md's "already-anticipated-but-unused" row, finally drained
here) atomically with ITS OWN claim of step 0, so whichever caller ends
up processing step 0 — the T+0 immediate attempt, or a later sweep tick
— creates it exactly once, with the same guarantee. If the process
crashes at ANY point — before, during, or after ``handle_emergency_
trigger`` runs, or even if it's never invoked at all — the row's due
state is already durable, so the very next sweep tick (within 60s of
restart) picks it up and performs whatever never happened. There is no
in-process timer anywhere in this module; the ticker only wakes and reads
due rows (never-break-adjacent design constraint from the issue:
"retries/chain state = data ... never in-process timers for the
SCHEDULE").

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

Category template priority — SETTLED 2026-07-12 (copy-guardian + safety
sign-off)
--------------------------------------------------------------------------
A single inbound message can trip more than one Tier-0 HARD category at
once (e.g. "fire" and "gas_co" together). Plain-language-rules.md caps a
tenant safety SMS at 3 numbered lines total, so this module must pick ONE
template rather than concatenating every matched category's steps.
:data:`_CATEGORY_PRIORITY` orders ``person`` (immediate threat to a human
life) above ``security`` (an in-progress break-in) above ``fire`` above
``gas_co`` above ``water``. Originally flagged here as a defensible-but-
unconfirmed product decision; the 2026-07-12 copy-guardian + safety-
reviewer round signed off on this exact ordering — no longer an open
question, changing it now is a genuine copy/safety decision, not a typo
fix.

911-first wording vs. physical-safety-first step order (founder ruling,
2026-07-12 — copy finding C2)
--------------------------------------------------------------------------
The rubric's "fire / medical / crime → 911 first" judgment call (severity-
rubric-v1.md) governs WHEN Stoop tells the tenant to call 911 relative to
Stoop's OWN handling (immediately, never held back pending landlord
approval or further triage) — it does not mandate that "call 911" be the
literal first WORD of every safety text. The ``fire`` and ``security``
templates below deliberately order their numbered steps by PHYSICAL SAFETY
first (get out of the unit / get somewhere safe and lock the door), THEN
"call 911" — telling someone mid-fire to dial a phone before moving their
feet is worse guidance, not more faithful to the rubric. (The ``person``
template puts "call 911" as its own first step — appropriate there since
there is no "get out" action to take first.) Kept AS BUILT per this
ruling; see severity-rubric-v1.md's own judgment-calls section for the
authoritative one-line clarification.

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

Cost metering (#111, schema-v1.md v1.12)
------------------------------------------
Every SMS-type action (never a voice call) that actually sends carries its
segment count + estimated Twilio cost in its own ``ActionOutcome``, via the
pure ``app/integrations/sms_segments.py`` helper — see
:func:`_sms_sent_outcome`. These ride along in the SAME ``'emergency_call_
attempt'`` audit row every attempt already writes (no new audit action, no
schema change) — ``app/cost_reporting.py`` is the read side.

DB access
---------
Admin engine (pre-identity/background context — no landlord JWT exists for
either the webhook-triggered T+0 path or the scheduled sweep). Allowlisted
in ``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``. All Twilio
sends go through ``app/integrations/twilio_send.py::get_twilio_sender()`` —
this module and that one are the ONLY two files allowed to reference it,
machine-enforced by ``tests/test_twilio_send_allowlist.py``. The ONE
exception is :func:`revoke_backup_ack_tokens` (below) — see its own
docstring for why it deliberately runs on the CALLER's landlord-scoped
session instead.

Per-recipient ack tokens (#289)
--------------------------------
Before this revision, ``_execute_action`` computed exactly ONE ``ack_url``
per step and handed the identical token to ``landlord_sms`` and
``backup_sms`` — one shared secret held by two different people. Three
consequences: (1) removing a backup contact could not revoke a link they
already held, (2) ``acknowledge_by_token`` could not record WHICH
recipient acknowledged, and (3) the two recipients could never be given
different semantics (an explicitly out-of-scope product question — this
issue builds only the foundation, not a softer backup-ack behavior).

Fixed by minting a genuinely separate token PER RECIPIENT, both living in
the same ``payload`` jsonb the single token used to occupy:

- ``payload.ack_token`` — the landlord's own token. Same key name as
  before this revision (minimizes churn; every pre-existing reader/writer
  of this key keeps working unchanged).
- ``payload.ack_token_backup`` — the backup contact's own token, new in
  this revision (migration 0018, v1.24 amendment — a sibling partial
  UNIQUE expression index, same shape as ``uq_notifications_ack_token``).

Both are minted the SAME way: lazily, by :data:`_CLAIM_STEP_SQL`'s own
healing CASE branches, the first time ANY step of a chain is claimed
(step 0's claim, on the common path — well before a backup action could
ever need one at step 3). This is deliberately NOT done at the webhook's
own ``INSERT`` (unlike ``ack_token`` historically was, for the "born
enriched" crash-safety reasons its own module docstring above explains at
length) — that reasoning does not apply here: even if ``ack_token_backup``
is briefly absent, the row is still due (``next_attempt_at`` is already
set independently), so the very first claim heals it in, with no window
where the chain could be stuck or a step could silently lack a token to
send. :func:`_execute_action` now takes both tokens and picks the right
one per action: ``landlord_sms`` gets ``ack_token``, ``backup_sms`` gets
``ack_token_backup`` — the voice-call actions (``landlord_call``/
``backup_call``) never used a token at all (they carry only
``notification_id`` in their TwiML ``action_url``) and are unchanged;
splitting THAT surface per-recipient is a separate, larger change (no way
to tell which physical phone call is "the landlord's" vs. "the backup's"
without capturing Twilio's ``From`` — which would mean storing a phone
number, forbidden by never-break rule #5) and is explicitly out of this
issue's scope.

Attribution: :func:`acknowledge_by_token` now resolves EITHER token
(:data:`_SELECT_NOTIFICATION_BY_TOKEN_SQL` matches ``ack_token`` OR
``ack_token_backup``) and records which one matched as
``audit_log.payload.recipient_role`` — ``"landlord"`` or ``"backup"``, a
role, never a phone number (rule #5). The dashboard surface
(``POST /v1/notifications/{id}/ack``) passes ``recipient_role="landlord"``
explicitly (it is authenticated, so there is no ambiguity). The voice
press-1 surface has no way to know which physical call is ringing (see
above) and passes no role at all — ``recipient_role`` stays ``None``
(``null`` in the audit payload) for that surface, an honest "unknown",
never a guess.

Revocation: :func:`revoke_backup_ack_tokens` strips ``ack_token_backup``
from every in-flight (``status='pending'``, not yet acknowledged)
``emergency_call`` notification for a property — called by
``app/routers/properties.py`` the moment a ``PATCH`` genuinely CLEARS
``backup_contact`` (a non-null value transitioning to an explicit
``null`` — the same "clears it" definition api-contracts.md's v1.25
amendment (#268) already established), in the SAME transaction as the
property update. The very next time that chain's next step is claimed,
:data:`_CLAIM_STEP_SQL`'s healing mints a FRESH ``ack_token_backup`` —
distinct from the revoked one, and not yet disclosed to anyone — so a
removed backup contact's already-held link stops matching immediately,
while the landlord's own ``ack_token`` (a different key, never touched by
this revocation) keeps working exactly as before. Editing
``backup_contact`` to a DIFFERENT phone number (not a clear) does not
revoke anything today — flagged, not solved, see that function's own
docstring.

Existing tokens at deploy time: a chain already mid-flight when this
ships keeps its pre-existing, pre-split ``ack_token`` — which still
authenticates BOTH the landlord and anyone who already holds a copy of
that link, including a backup contact who received it before this
deploy — this migration cannot retroactively split a secret already
disclosed to two people over SMS. Revoking ``backup_contact`` on such a
chain removes only ``ack_token_backup`` (freshly minted post-deploy, not
yet sent to anyone); the pre-existing shared value under ``ack_token``
keeps working for whoever already has it. Full closure of the shared-
token gap applies to every chain CREATED after this ships, all of which
mint a genuinely distinct ``ack_token_backup`` well before any backup
action ever fires.

Fail-safe: a lookup/validation error on either token surface
(:func:`resolve_ack_token`, :func:`acknowledge_by_token`) never mutates
anything — the ``SELECT`` (or the claim-guarded ``UPDATE`` for a genuine
ack) either succeeds and commits, or raises and commits nothing; there is
no partial state where a chain's schedule stops advancing because a token
lookup failed. The schedule is driven entirely by ``next_attempt_at``,
independent of whether any particular ack attempt succeeded — same
invariant the rest of this module's crash-safety design already rests on.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable
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
from app.integrations.sms_segments import count_segments, estimate_sms_cost_cents
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
"""See module docstring "Category template priority" — SETTLED per the
2026-07-12 copy-guardian + safety-reviewer joint ruling (was previously
flagged here as an unconfirmed product decision; both are now signed off
on this exact ordering)."""

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
        "3. Call 911 now."
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
    # `.strip()`, not bare truthiness (safety review, 2026-08-03, finding 3
    # residual): a whitespace-only value survived the old check and was
    # handed to `create_call(to="   ")`, which Twilio rejects (21211) —
    # `_execute_action` then swallows that into `status="failed"`, so the
    # T+10m backup leg silently never fires. The writers treat
    # whitespace-only as "no backup configured"; this must agree with them.
    return phone.strip() if isinstance(phone, str) and phone.strip() else None


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
    segments: int | None = None
    """#111 cost metering (schema-v1.md v1.12) -- SMS segment count for a
    ``"sent"`` SMS-type action ONLY (``landlord_sms``/``backup_sms``/
    ``tenant_safety_sms``/``tenant_status_sms``). ``None`` for voice-call
    actions (priced per-minute, out of this issue's scope) and for
    ``skipped``/``failed`` outcomes (no SMS actually went out)."""
    sms_cost_cents: float | None = None
    """Estimated Twilio cost for :attr:`segments` -- see
    ``app/integrations/sms_segments.py::estimate_sms_cost_cents``. Same
    ``None``-for-non-SMS/non-sent scope as :attr:`segments`."""


_SELECT_EMERGENCY_SMS_ROW_SQL = text(
    """
    SELECT id, attempt FROM notifications
    WHERE type = 'emergency_sms' AND payload ->> 'message_id' = :message_id
      AND status IN ('pending', 'failed')
    """
)


async def _claim_emergency_sms_for_send(message_id: UUID) -> bool:
    """Atomically claim the ``emergency_sms`` row for *message_id* — SELECT
    its current ``attempt``, then compare-and-swap it via the SAME
    ``_CLAIM_SMS_DRAIN_SQL`` the SMS-drain sweep uses (:func:`
    _process_sms_drain_candidate`) — BEFORE the T+0 inline send below is
    allowed to proceed (safety review, 2026-07-12, finding 3, MINOR).

    Without this, the inline send (triggered by
    ``app/agent/emergency_chain.py::handle_emergency_trigger``'s
    immediate call, OR by a later ``emergency_call`` sweep tick reaching
    step 0) and the INDEPENDENT ``run_sms_drain_sweep`` tick had no shared
    gate: both could see the SAME ``emergency_sms`` row as "not yet sent"
    at the same instant and each send the tenant safety text — a
    sub-second-window double-text. Claiming here, via the identical
    attempt-based compare-and-swap the drain sweep itself uses, means
    whichever of the two gets there first wins; the other finds the
    attempt already bumped and skips sending, mirroring the
    ``emergency_call`` row's own claim discipline (:data:`_CLAIM_STEP_SQL`)
    exactly, just for the ``emergency_sms`` row instead.

    Returns ``False`` when there is nothing left to claim (already sent/
    exhausted by the other path, or genuinely missing) — the caller must
    NOT send in that case.
    """
    async with _acm(get_admin_session)() as session:
        row = (
            (await session.execute(_SELECT_EMERGENCY_SMS_ROW_SQL, {"message_id": str(message_id)}))
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        claimed = (
            (
                await session.execute(
                    _CLAIM_SMS_DRAIN_SQL,
                    {
                        "id": str(row["id"]),
                        "old_attempt": row["attempt"],
                        "new_attempt": row["attempt"] + 1,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        return claimed is not None


def _sms_sent_outcome(action: str, sid: str, body: str) -> ActionOutcome:
    """#111 cost metering (schema-v1.md v1.12): every SMS-type action that
    actually sent (never a voice call, never ``skipped``/``failed``) gets
    its segment count + estimated cost recorded, computed from the SAME
    *body* just sent (pure, no I/O — ``app/integrations/sms_segments.py``)."""
    segment_info = count_segments(body)
    return ActionOutcome(
        action=action,
        status="sent",
        sid=sid,
        segments=segment_info.segments,
        sms_cost_cents=estimate_sms_cost_cents(segment_info.segments),
    )


async def _execute_action(
    sender: TwilioSender,
    action: str,
    ctx: EmergencyContext,
    *,
    categories: list[str],
    notification_id: UUID,
    ack_token: str,
    ack_token_backup: str,
    message_id: UUID,
) -> ActionOutcome:
    """#289: *ack_token* and *ack_token_backup* are two genuinely distinct
    per-recipient tokens (module docstring "Per-recipient ack tokens") —
    ``landlord_sms`` is built with *ack_token*, ``backup_sms`` with
    *ack_token_backup*. Neither voice-call action uses either token (their
    TwiML ``action_url`` carries only *notification_id*)."""
    if not ctx.twilio_number:
        return ActionOutcome(action=action, status="skipped", reason="no_twilio_number")
    from_number = ctx.twilio_number

    landlord_ack_url = render_ack_url(notification_id, ack_token)
    backup_ack_url = render_ack_url(notification_id, ack_token_backup)
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
                ack_url=landlord_ack_url,
            )
            sid = await sender.send_sms(to=ctx.landlord_phone, from_=from_number, body=body)
            return _sms_sent_outcome(action, sid, body)

        if action in (_ACTION_BACKUP_CALL, _ACTION_BACKUP_SMS):
            backup_phone = _backup_phone(ctx.backup_contact)
            if not backup_phone:
                return ActionOutcome(action=action, status="skipped", reason="no_backup_contact")
            if action == _ACTION_BACKUP_CALL:
                sid = await sender.create_call(
                    to=backup_phone, from_=from_number, twiml_url=action_url
                )
                return ActionOutcome(action=action, status="sent", sid=sid)
            body = render_backup_alert_sms(
                property_label=ctx.property_label,
                category_label=category_label,
                landlord_label=landlord_label,
                tenant_label=tenant_label,
                ack_url=backup_ack_url,
            )
            sid = await sender.send_sms(to=backup_phone, from_=from_number, body=body)
            return _sms_sent_outcome(action, sid, body)

        if action == _ACTION_TENANT_SAFETY_SMS:
            if not ctx.tenant_phone:
                return ActionOutcome(action=action, status="skipped", reason="no_tenant_phone")
            if not await _claim_emergency_sms_for_send(message_id):
                # Lost the race to a concurrent run_sms_drain_sweep tick
                # (or this step somehow ran twice) -- see
                # _claim_emergency_sms_for_send's own docstring. NEVER
                # send twice.
                return ActionOutcome(
                    action=action, status="skipped", reason="already_claimed_elsewhere"
                )
            _, body = render_tenant_safety_sms(categories)
            sid = await sender.send_sms(to=ctx.tenant_phone, from_=from_number, body=body)
            return _sms_sent_outcome(action, sid, body)

        if action == _ACTION_TENANT_STATUS_SMS:
            if not ctx.tenant_phone:
                return ActionOutcome(action=action, status="skipped", reason="no_tenant_phone")
            body = render_tenant_status_sms(landlord_label)
            sid = await sender.send_sms(to=ctx.tenant_phone, from_=from_number, body=body)
            return _sms_sent_outcome(action, sid, body)

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
    SET attempt = :new_attempt,
        next_attempt_at = :next_attempt_at,
        updated_at = now(),
        payload = payload
                    || (CASE
                          WHEN payload ->> 'ack_token' IS NULL
                          THEN jsonb_build_object('ack_token', CAST(:fallback_ack_token AS text))
                          ELSE '{}'::jsonb
                        END)
                    || (CASE
                          WHEN payload ->> 'ack_token_backup' IS NULL
                          THEN jsonb_build_object(
                                 'ack_token_backup', CAST(:fallback_ack_token_backup AS text)
                               )
                          ELSE '{}'::jsonb
                        END)
    WHERE id = :id AND status = 'pending' AND attempt = :old_attempt AND acknowledged_at IS NULL
    RETURNING id, payload ->> 'ack_token' AS ack_token,
              payload ->> 'ack_token_backup' AS ack_token_backup
    """
)
# The explicit ``CAST(... AS text)`` on each fallback token above is
# required, not cosmetic: asyncpg must know the wire type of every bind
# parameter before it can send the (binary) protocol message, and
# ``jsonb_build_object``'s variadic ``"any"`` signature gives the planner
# nothing to infer a plain string literal's type FROM inside a ``CASE``
# branch that may not even execute -- Postgres raises
# ``IndeterminateDatatypeError`` on the token parameter without it.
# Discovered running this round's own tests (every claim was raising and
# silently degrading to ``"processing_error"``).
# Safety review, 2026-07-12 (finding N1, belt 2 -- healing): every row born
# via app/routers/webhooks/twilio.py's INSERT already carries an ack_token
# from the start, so the first CASE above is normally a no-op (ELSE
# branch). It only fires for a healed legacy/edge row the sweep picked up
# via its own ``next_attempt_at IS NULL`` clause -- claiming such a row
# now ALSO guarantees it leaves with a real ack_token, atomically, in the
# SAME statement that claims it, so every OTHER step (the T+2m ack-link
# SMS, the voice call's own action_url, etc.) always has one to use.
# #289 (module docstring "Per-recipient ack tokens"): the SECOND CASE
# branch mints ``ack_token_backup`` the SAME way, but is NOT normally a
# no-op -- unlike ``ack_token``, nothing sets it at INSERT time, so it
# heals in on literally the FIRST claim of every chain (step 0, on the
# common path), well before any backup action could need one at step 3.
# Both CASE expressions are combined with ``||`` (jsonb concatenation of
# two DISTINCT keys, or a no-op ``'{}'::jsonb`` on either side) rather
# than nested, so either key can independently be healed in the same
# statement regardless of whether the other one already existed. The
# caller always passes freshly generated fallback tokens for BOTH keys
# even though one or both are usually discarded (the ELSE/no-op branch) --
# cheaper than a round trip to check first, same trade-off the original
# (landlord-only) version of this statement already made.

_INSERT_ATTEMPT_AUDIT_SQL = text(
    """
    INSERT INTO audit_log (landlord_id, case_id, actor, action, payload)
    VALUES (:landlord_id, NULL, 'system', 'emergency_call_attempt', CAST(:payload AS jsonb))
    """
)

_MARK_EMERGENCY_SMS_SQL = text(
    """
    UPDATE notifications SET status = :status, updated_at = now()
    WHERE type = 'emergency_sms' AND payload ->> 'message_id' = :message_id
      AND status IN ('pending', 'failed')
    """
)
# Bug fix (issue #186 triage, cross-path duplicate-send): this predicate
# must match every status :func:`_claim_emergency_sms_for_send` was willing
# to claim (its own SELECT/CAS, ``_SELECT_EMERGENCY_SMS_ROW_SQL`` /
# ``_CLAIM_SMS_DRAIN_SQL``, both ``status IN ('pending', 'failed')``) --
# NOT just ``'pending'``. Before this fix: a row that a PRIOR
# ``run_sms_drain_sweep`` tick had already marked ``'failed'`` (via
# ``_MARK_SMS_DRAIN_FAILED_SQL``) could still be claimed and successfully
# sent by THIS (inline T+0, or a later step-0-processing sweep tick) path
# -- the claim's own CAS treats ``'failed'`` as fair game, by design, so a
# transient failure can heal on a later attempt. But this write only ever
# matched ``status = 'pending'``, so a successful send that healed a
# ``'failed'`` row silently no-opped here: the row stayed ``'failed'``
# forever, and the NEXT ``run_sms_drain_sweep`` tick saw a ``'failed'`` row
# with nothing recorded and resent the tenant safety SMS again --
# indefinitely, once per tick, since ``'sent'`` was never reached.
#
# Exactly-once reasoning for the widened predicate (traced every caller):
# 1. This statement can only ever move a row OUT of {'pending', 'failed'}
#    (into 'sent' or 'failed') -- it can never match, and therefore never
#    touch, a row already 'sent' or 'exhausted' (both excluded from the
#    ``IN (...)`` set). Widening the predicate cannot make it regress an
#    already-terminal row, however many times it is (re-)called.
# 2. :func:`_mark_emergency_sms_status` (the only caller of this SQL) is
#    itself invoked from exactly ONE call site -- inside
#    :func:`_process_due_row`'s ``step == 0`` handling -- which only ever
#    runs to completion for whichever single caller wins
#    ``_CLAIM_STEP_SQL``'s own CAS (``attempt = :old_attempt``) on the
#    UNRELATED ``emergency_call`` row's ``attempt`` column: that CAS can
#    transition a given ``emergency_call`` row's step 0 (attempt 0 -> 1)
#    AT MOST ONCE in the row's entire lifetime, so this mark statement is
#    reachable AT MOST ONCE, full stop, independent of the ``emergency_sms``
#    row's own status/attempt at the time it runs. There is no scenario
#    where two different successful sends both reach this write for the
#    same ``emergency_sms`` row -- so no attempt/identity check is needed
#    on THIS statement for it to stay exactly-once.
# 3. The SEND this one guaranteed write is recording is itself gated,
#    separately, by :func:`_claim_emergency_sms_for_send`'s own attempt-CAS
#    (the SAME ``_CLAIM_SMS_DRAIN_SQL`` ``_process_sms_drain_candidate``
#    uses) immediately before the Twilio call -- that is what stops THIS
#    path and a concurrent ``run_sms_drain_sweep`` tick from both sending
#    the tenant safety SMS for the same attempt slot. This write only needs
#    to persist that already-exclusive claim's outcome into whatever
#    transient status the row currently holds. See
#    :func:`_claim_emergency_sms_for_send`'s docstring for the claim side.


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
    docstring).

    The AUTHORITATIVE ``ack_token``/``ack_token_backup`` used for this
    step's actions are whatever :data:`_CLAIM_STEP_SQL` returns (never
    ``candidate.ack_token`` from the original SELECT) — safety review,
    2026-07-12, finding N1 belt 2: this is what makes a healed
    (previously ``next_attempt_at IS NULL``) legacy row usable the moment
    it's claimed, even though its ack_token didn't exist at SELECT time.
    Same reasoning now covers ``ack_token_backup`` (#289) — see module
    docstring "Per-recipient ack tokens".
    """
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
                        "fallback_ack_token": secrets.token_urlsafe(24),
                        "fallback_ack_token_backup": secrets.token_urlsafe(24),
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if claim_row is None:
            return "lost_race"

        ack_token = cast("str | None", claim_row["ack_token"])
        ack_token_backup = cast("str | None", claim_row["ack_token_backup"])

        if step == 0:
            # Create the durable ``emergency_sms`` send-intent row for the
            # tenant safety text -- atomically, in the SAME transaction as
            # THIS claim (safety review, 2026-07-12, finding N1 belt 1
            # follow-through). ``_CLAIM_STEP_SQL``'s own optimistic-
            # concurrency check (``attempt = :old_attempt``) already
            # guarantees step 0 is claimed by exactly one caller ever --
            # the T+0 immediate call (:func:`handle_emergency_trigger`) on
            # the common path, or a later sweep tick recovering from a
            # crash/healed row -- so piggybacking this INSERT on the same
            # claim gives it the identical exactly-once guarantee for
            # free, with no separate idempotency mechanism (an ``ON
            # CONFLICT`` dedupe index, a payload marker, ...) needed. This
            # is also what makes recovery complete regardless of WHEN a
            # crash happens: before, during, or after
            # ``handle_emergency_trigger``'s own attempt, or even if that
            # function is never invoked at all -- see module docstring
            # "The instant + durable sweep hybrid".
            category, body = render_tenant_safety_sms(candidate.categories)
            await session.execute(
                _INSERT_EMERGENCY_SMS_SQL,
                {
                    "landlord_id": str(candidate.landlord_id),
                    "payload": json.dumps(
                        {
                            "message_id": str(candidate.message_id),
                            "property_id": str(candidate.property_id),
                            "category": category,
                            "body": body,
                        }
                    ),
                },
            )

        ctx = await _load_context(session, candidate.message_id)

    if ctx is None:  # pragma: no cover — invariant: messages are never deleted
        log.error("emergency_chain_context_missing", notification_id=str(candidate.notification_id))
        sentry_sdk.capture_message(
            "emergency_chain: context missing for a claimed attempt",
            level="error",
            extras={"notification_id": str(candidate.notification_id)},
        )
        return "context_missing"

    if (
        ack_token is None or ack_token_backup is None
    ):  # pragma: no cover — invariant: _CLAIM_STEP_SQL always sets both
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
                ack_token=ack_token,
                ack_token_backup=ack_token_backup,
                message_id=candidate.message_id,
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
                        # #111 cost metering (schema-v1.md v1.12): lets a
                        # cost rollup group emergency-chain SMS cost by
                        # door without a second join through notifications.
                        "property_id": str(candidate.property_id),
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

_INSERT_EMERGENCY_SMS_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'emergency_sms', 'sms', 'pending', CAST(:payload AS jsonb))
    RETURNING id
    """
)

_SELECT_EMERGENCY_CALL_HEADER_SQL = text(
    "SELECT landlord_id, created_at FROM notifications WHERE id = :id"
)


async def _load_trigger_header(notification_id: UUID) -> tuple[UUID, datetime] | None:
    """Read-only: ``(landlord_id, created_at)`` off the ALREADY-CREATED
    ``emergency_call`` row — see :func:`handle_emergency_trigger`'s
    docstring for why this reads rather than writes. Split out to its own
    (tiny, DB-touching) function purely so tests can force this SPECIFIC
    step to fail/raise without reaching for private SQL internals — see
    ``tests/test_agent_emergency_chain.py``'s "residual enrich path"
    coverage."""
    async with _acm(get_admin_session)() as session:
        row = (
            (await session.execute(_SELECT_EMERGENCY_CALL_HEADER_SQL, {"id": str(notification_id)}))
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    return cast("UUID", row["landlord_id"]), cast("datetime", row["created_at"])


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

    Safety review, 2026-07-12 (finding N1, BLOCKING) — no separate durable
    write happens in THIS function any more
    --------------------------------------------------------------------
    Before this revision, this function did its OWN enrichment (ack token
    + ``next_attempt_at = now()``) in a short transaction BEFORE the real
    T+0 sends — the row only became sweep-visible once THAT committed. A
    crash between the webhook's own INSERT and this function's enrichment
    commit left the row at ``next_attempt_at IS NULL`` forever: durable,
    but invisible to the sweep (the "pre-enrich silence window").

    That enrichment now happens ONE LEVEL UP, in the SAME transaction as
    the webhook's own ``emergency_call`` INSERT
    (``app/routers/webhooks/twilio.py::_INSERT_EMERGENCY_NOTIFICATION_SQL``
    sets ``next_attempt_at = now()`` and a fresh ``ack_token`` at INSERT
    time) — by the time ANYTHING calls this function, the row is ALREADY
    durably due, with NO dependency on this function ever running at all.
    This function is now a purely BEST-EFFORT immediate attempt at
    processing step 0: it (1) reads back ``landlord_id``/``created_at``
    (deliberately NOT re-plumbed as a parameter — see below) and (2)
    delegates to :func:`_run_candidate_safely`, the SAME claim-guarded
    path (:func:`_process_due_row`) the periodic sweep uses for every
    later step. If ANYTHING here fails or raises — the read, the claim,
    a Twilio call, a crash of the whole process — the row is already due,
    so the very next sweep tick (within 60s) picks it up, independently
    re-derives everything it needs, and performs whatever this attempt
    didn't (:func:`_process_due_row` also creates the durable
    ``emergency_sms`` row for step 0, atomically with ITS claim, so even
    a crash before this function is ever invoked still gets the tenant
    safety text sent — see that function's own docstring). Idempotency
    against a genuine double-invocation of this function needs no
    separate guard either: :data:`_CLAIM_STEP_SQL`'s own optimistic
    concurrency check (``attempt = :old_attempt``) means a second call
    always loses the race once the first has claimed step 0.

    Deliberately does NOT take ``landlord_id`` as a parameter (unlike the
    other fields, which match ``fire_emergency_protocol``'s existing
    signature exactly, left UNCHANGED per the campaign's "do not re-plumb
    the webhook" instruction): the webhook already wrote it onto the
    ``emergency_call`` row it created, so :func:`_load_trigger_header`
    reads it straight back instead of requiring a new parameter/call-site
    edit.
    """
    header = await _load_trigger_header(notification_id)
    if header is None:  # pragma: no cover — invariant: webhook always creates this row first
        log.error("emergency_chain_notification_missing", notification_id=str(notification_id))
        return
    landlord_id, chain_started_at = header

    candidate = EmergencyCallCandidate(
        notification_id=notification_id,
        landlord_id=landlord_id,
        attempt=0,
        message_id=message_id,
        property_id=property_id,
        categories=categories,
        ack_token=None,  # _process_due_row always uses whatever _CLAIM_STEP_SQL returns
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
      AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
    ORDER BY next_attempt_at NULLS FIRST
    """
)
# Safety review, 2026-07-12 (finding N1, BLOCKING, belt 2 -- "the sweep ALSO
# treats next_attempt_at IS NULL emergency_call rows as due-and-needing-
# enrichment"): every row THIS codebase creates today is already born
# enriched (webhook's own INSERT sets next_attempt_at=now(), see
# app/routers/webhooks/twilio.py's _INSERT_EMERGENCY_NOTIFICATION_SQL) --
# the ``IS NULL`` branch exists purely to HEAL any row that somehow still
# lacks it (a legacy row from before this fix, or a future bug elsewhere
# that inserts one without setting it), rather than leaving it invisible
# to the sweep forever. ``NULLS FIRST`` treats an un-enriched row as the
# most urgent candidate, not an accident of NULL sort order.


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
# SMS drain sweep — tenant_ack (#109 holding ack) + emergency_sms (#108
# tenant safety text). Safety review, 2026-07-12 (spec finding S1, MAJOR;
# safety finding 3, MEDIUM).
# ---------------------------------------------------------------------------
#
# ``tenant_ack`` (degraded_mode.py's holding-ack send-intent) and
# ``emergency_sms`` (this module's T+0 tenant safety SMS) are both
# ONE-SHOT, at-most-once sends today: each gets exactly ONE attempt (via
# ``app/agent/nodes/degraded_mode.py``'s own audit trail for tenant_ack —
# it never sends anything itself, only queues the row — and via THIS
# module's step-0 processing for emergency_sms), and if that ONE attempt
# fails, NOTHING ever retries it. For emergency_sms specifically — the
# only non-redundant tenant-facing message in the whole chain (the
# landlord/backup escalation has multiple independent contacts and
# repeats every 15 minutes; the tenant's category-templated safety
# instructions do not) — "at most once" is not an acceptable durability
# guarantee. schema-v1.md's own v1.8 note said as much for tenant_ack:
# "status stays 'pending' until #108's sender exists and drains it" — this
# sweep is that drain, closing BOTH types' durability gap the same way:
# resend on every tick until genuinely delivered, never capped.
#
# Idempotency mirrors the emergency_call chain's own claim discipline:
# ``attempt`` (already present on every ``notifications`` row) is reused
# purely as a concurrency-safe claim guard here, NOT a schedule -- there
# is no next_attempt_at gating; a row is simply "due" whenever its status
# is 'pending' or 'failed', so the sweep retries it every tick until it
# reaches 'sent'.
#
# 'failed' vs. 'exhausted' (safety review, 2026-07-12, finding N2): 'failed'
# is TRANSIENT-ONLY -- it stays in the retry set above, resent every tick.
# A genuinely TERMINAL outcome (today: 'no_tenant_phone' -- no amount of
# retrying supplies a phone number this message never had) is marked
# 'exhausted' instead (schema-v1.md's CHECK already allows it, the same
# terminal value ``degraded_retry`` uses) so a second tick is a true
# no-op for that row, never an infinite, silently-repeating no-op send
# attempt.
#
# Wall-clock tick deadline (issue #229, PR #228 senior-review advisory 1) --
# TWO PASSES, not one shared queue (safety re-review, blocking finding 1,
# 2026-08-01)
# --------------------------------------------------------------------------
# This sweep shares the SAME single scheduler ticker task
# (``app/scheduler.py``) as ``run_emergency_chain_sweep`` (which runs
# BEFORE it, unchanged/out of scope for #229) and every other sweep that
# tick -- an unbounded run here, one ``send_sms`` per due row with no
# per-tick cap, could otherwise delay the NEXT tick's emergency chain
# sweep. Bounded the identical way ``app/agent/draft_sender.py::sender_tick``
# / ``app/push_outbox.py::run_push_outbox_sweep`` already are: a wall-clock
# budget (:data:`DEFAULT_TICK_DEADLINE_SECONDS`, 25s by default, computed
# from an injectable *time_source*).
#
# An EARLIER revision of this deadline drained BOTH ``tenant_ack`` and
# ``emergency_sms`` from ONE shared ``ORDER BY created_at`` queue under the
# SAME budget -- a genuine, reproduced starvation bug: a handful of older,
# permanently-failing ``tenant_ack`` rows (each risking the full 10s Twilio
# timeout, ``app/integrations/twilio_send.py``'s ``AsyncTwilioHttpClient``)
# could consume the ENTIRE 25s budget before the loop ever reached a
# NEWER, genuinely-due ``emergency_sms`` row later in the same
# ``created_at`` ordering -- the one non-redundant tenant-facing message in
# the whole chain (see "Idempotency" above) silently never attempted, tick
# after tick, for as long as the poisoned ``tenant_ack`` rows kept sorting
# first. Fixed by giving ``emergency_sms`` its OWN, UNBOUNDED pass, run
# FIRST and to completion every tick, entirely independent of the deadline:
# ``emergency_sms`` rows are rare (tens at most, per the T+0 emergency
# chain's own cadence -- never a bulk-insert path), so draining ALL of them
# every tick, however long that takes, is the correct trade -- exactly
# mirroring why this row type's retry-forever ('failed' never 'exhausted'
# except for the structural ``no_tenant_phone`` case) semantics were never
# capped in the first place (see "Idempotency" above). ``tenant_ack`` gets
# its OWN, SEPARATE 25s-bounded pass, run second, using the exact
# stop-claiming-not-abandoning discipline the (now-removed) single-queue
# version used. Both passes still order ``ORDER BY created_at`` within
# themselves; ONLY cross-type ordering (a stale ``tenant_ack`` blocking a
# fresh ``emergency_sms``) was ever the problem.

DEFAULT_TICK_DEADLINE_SECONDS: float = 25.0
"""Wall-clock budget for the ``tenant_ack`` pass of one
:func:`run_sms_drain_sweep` call (issue #229, PR #228 senior-review
advisory 1) -- same value and rationale as
``app/agent/draft_sender.py::DEFAULT_TICK_DEADLINE_SECONDS`` /
``app/push_outbox.py::DEFAULT_TICK_DEADLINE_SECONDS``: this sweep shares
the single scheduler ticker task with ``run_emergency_chain_sweep``, which
must run promptly every tick. Once exceeded, the ``tenant_ack`` pass stops
CLAIMING new candidates for that tick -- a candidate already claimed and
mid-send always finishes; any leftover due rows simply remain due, picked
up whole by the very next tick. Does NOT bound the ``emergency_sms``
pass, which runs first, unbounded, to completion every tick -- see "TWO
PASSES" above."""

_SMS_DRAIN_SELECT_BATCH_LIMIT: int = 100
"""Per-tick cap on how many due ``tenant_ack`` candidates one sweep call
reads (issue #229, PR #228 senior-review advisory 2) -- mirrors
``app/push_outbox.py``'s own ``_PUSH_OUTBOX_SWEEP_BATCH_LIMIT`` (same
"bounded work per tick" discipline, same chosen value); anything left over
is still due and is picked up on the NEXT tick. Deliberately NOT applied
to the ``emergency_sms`` SELECT (see "TWO PASSES" above) -- capping that
one would silently reintroduce the exact starvation bug this fix closes,
just past the 100th row instead of past the 25s budget."""

# Safety re-review round 2, blocking finding, 2026-08-01 -- SYMMETRY with
# app/agent/landlord_sms.py's own fix for the identical A1+A2-shaped risk:
# a permanently-failing ``tenant_ack`` row never exhausts (no exhaustion
# cap exists for this type at all -- only the structural
# ``no_tenant_phone`` terminal case, see module docstring "Idempotency")
# and, with the batch LIMIT above + `ORDER BY created_at` + no backoff, a
# cluster of stuck ``tenant_ack`` rows could permanently occupy the entire
# LIMIT 100 window's head, hiding a newer, genuinely-due ``tenant_ack`` row
# from ever being selected at all -- the SAME shape as the landlord_sms.py
# finding, just for holding-ack SMS instead of draft-ready notices. Fixed
# the SAME way: exponential backoff on ``next_attempt_at`` on every
# genuine send failure, so a stuck row falls OUT of the window's head
# instead of permanently occupying a slot.
#
# Backoff basis is ``attempt`` here, NOT a dedicated ``send_failures``
# payload counter (deliberately, a narrower fix than landlord_sms.py's own
# blocking-finding-2 remediation): this module's ``attempt`` column was
# never the subject of an attempt-burn finding the way landlord_sms.py's
# was, and adding a whole new payload-counter mechanism to LEGACY-BEHAVIOR
# rows this issue never asked to attempt-burn-harden is real scope creep
# for a backoff-only fix. The practical consequence is bounded and benign:
# a crash/CancelledError between claim and send would advance the backoff
# exponent slightly further than a "genuine failures only" count would --
# but since ``tenant_ack`` retries forever regardless (no exhaustion cap
# to prematurely trip), an occasionally-larger-than-warranted backoff
# delay is a minor LATENCY effect, never a lost notification or an
# incorrect terminal state. NOT applied to ``emergency_sms`` at all (see
# "TWO PASSES" above and :data:`_SELECT_DUE_EMERGENCY_SMS_DRAIN_SQL`'s own
# comment) -- the tenant safety SMS keeps its unconditional every-tick
# retry; a future "cleanup" adding a backoff there would silently reopen
# blocking finding 1's own starvation direction (a stuck emergency_sms
# candidate backing off instead of being retried every tick).
_TENANT_ACK_BACKOFF_BASE_SECONDS: float = 60.0
"""Base interval for :func:`_tenant_ack_backoff_seconds` -- same value and
rationale as ``app/agent/landlord_sms.py``'s own
``_LANDLORD_SMS_BACKOFF_BASE_SECONDS`` (one scheduler tick, so the FIRST
genuine failure's backoff is indistinguishable from "just try again next
tick")."""

_TENANT_ACK_BACKOFF_CAP_SECONDS: float = 3600.0
"""Backoff ceiling (1 hour) -- same value and rationale as
``app/agent/landlord_sms.py``'s own ``_LANDLORD_SMS_BACKOFF_CAP_SECONDS``:
a stuck ``tenant_ack`` row (which never exhausts) must still be retried at
some bounded worst-case cadence forever."""


def _tenant_ack_backoff_seconds(attempt: int) -> float:
    """Pure: exponential backoff for the Nth claim/send attempt of a
    ``tenant_ack`` candidate (1-indexed — ``attempt=1`` is the FIRST
    attempt). ``60s * 2**(attempt - 1)``, capped at 3600s -- same
    progression as ``app/agent/landlord_sms.py``'s own
    ``_landlord_sms_backoff_seconds``, over ``attempt`` rather than a
    dedicated failure counter (see the module comment above
    :data:`_TENANT_ACK_BACKOFF_BASE_SECONDS` for why)."""
    uncapped = _TENANT_ACK_BACKOFF_BASE_SECONDS * float(2 ** (attempt - 1))
    return min(uncapped, _TENANT_ACK_BACKOFF_CAP_SECONDS)


def _default_time_source() -> float:
    """The real, monotonic clock :func:`run_sms_drain_sweep` budgets its
    wall-clock deadline against -- mirrors
    ``app/agent/draft_sender.py::_default_time_source`` /
    ``app/push_outbox.py::_default_time_source`` exactly
    (``asyncio.get_running_loop().time()``). Injectable so tests can
    advance a fake clock deterministically instead of sleeping for real
    seconds."""
    return asyncio.get_running_loop().time()


_SELECT_DUE_EMERGENCY_SMS_DRAIN_SQL = text(
    """
    SELECT id, landlord_id, type, attempt, payload
    FROM notifications
    WHERE type = 'emergency_sms' AND status IN ('pending', 'failed')
    ORDER BY created_at
    """
)
# Deliberately NO LIMIT and NO next_attempt_at/backoff filter -- see "TWO
# PASSES" above and the backoff comment above _TENANT_ACK_BACKOFF_BASE_
# SECONDS: this pass must drain every due emergency_sms row every tick,
# unconditionally and unbounded, or BOTH fixes regress (the LIMIT
# regression reintroduces blocking finding 1's starvation; a backoff
# filter here would silently reintroduce it in a subtler shape -- a stuck
# emergency_sms row LOOKS handled because it keeps "retrying", but a
# backed-off one would stop being attempted every tick, which is exactly
# what this row type must never do). Pinned by
# test_emergency_sms_pass_has_no_backoff_filter.

_SELECT_DUE_TENANT_ACK_DRAIN_SQL = text(
    """
    SELECT id, landlord_id, type, attempt, payload
    FROM notifications
    WHERE type = 'tenant_ack' AND status IN ('pending', 'failed')
      AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
    ORDER BY created_at
    LIMIT :limit
    """
)

_CLAIM_SMS_DRAIN_SQL = text(
    """
    UPDATE notifications SET attempt = :new_attempt, updated_at = now()
    WHERE id = :id AND status IN ('pending', 'failed') AND attempt = :old_attempt
    RETURNING id
    """
)

_MARK_SMS_DRAIN_SENT_SQL = text(
    "UPDATE notifications SET status = 'sent', updated_at = now() WHERE id = :id"
)

_MARK_SMS_DRAIN_FAILED_SQL = text(
    "UPDATE notifications SET status = 'failed', updated_at = now() WHERE id = :id"
)
# ``'failed'`` is TRANSIENT-ONLY -- it stays inside both
# _SELECT_DUE_EMERGENCY_SMS_DRAIN_SQL's and
# _SELECT_DUE_TENANT_ACK_DRAIN_SQL's own ``status IN ('pending', 'failed')``
# retry set, so the next tick tries again. Never use it for an outcome that
# retrying can never fix. Used for BOTH row types' failure path EXCEPT
# ``tenant_ack``'s own backoff write below -- see
# :data:`_MARK_TENANT_ACK_FAILED_WITH_BACKOFF_SQL`'s own comment.

# Safety re-review round 2, blocking finding, 2026-08-01 -- tenant_ack-ONLY
# variant that ALSO sets `next_attempt_at` (exponential backoff, see the
# module comment above _TENANT_ACK_BACKOFF_BASE_SECONDS). NEVER used for
# emergency_sms (that pass keeps _MARK_SMS_DRAIN_FAILED_SQL, unconditional
# every-tick retry -- see _SELECT_DUE_EMERGENCY_SMS_DRAIN_SQL's own
# comment).
_MARK_TENANT_ACK_FAILED_WITH_BACKOFF_SQL = text(
    "UPDATE notifications SET status = 'failed', updated_at = now(), "
    "next_attempt_at = :next_attempt_at WHERE id = :id"
)

_MARK_SMS_DRAIN_EXHAUSTED_SQL = text(
    "UPDATE notifications SET status = 'exhausted', updated_at = now() WHERE id = :id"
)
# Safety review, 2026-07-12 (finding N2): ``no_tenant_phone`` is TERMINAL,
# not transient -- no number of retries changes the fact that this
# message's row has no stored channel back to a tenant (see module
# docstring "Known limitation"). Marking it ``'failed'`` (the previous
# behavior) was a genuine bug dressed up as "terminal" in a comment only:
# ``'failed'`` rows ARE re-selected by both drain-pass SELECTs above every
# tick forever, so the sweep would silently re-attempt (and re-fail) this
# exact row on every single tick, indefinitely. ``'exhausted'`` (schema-
# v1.md's CHECK already allows it -- the SAME terminal value
# ``degraded_retry`` uses once ITS chain concludes) is excluded from that
# ``IN (...)`` clause, so a genuinely unfixable row is swept ONCE, marked,
# and never touched again -- a second tick is a true no-op for it.


@dataclass(frozen=True)
class SmsDrainCandidate:
    notification_id: UUID
    landlord_id: UUID
    notification_type: str  # 'tenant_ack' | 'emergency_sms'
    attempt: int
    message_id: UUID
    body: str


@dataclass(frozen=True)
class SmsDrainOutcome:
    notification_id: UUID
    notification_type: str
    outcome: str  # "sent" | "failed" | "lost_race" | "context_missing" | "no_tenant_phone"


def _sms_drain_candidate_from_row(row: dict[str, Any]) -> SmsDrainCandidate | None:
    payload = cast("dict[str, Any]", row["payload"])
    message_id_raw = payload.get("message_id")
    body = payload.get("body")
    if message_id_raw is None or body is None:
        # pragma: no cover -- invariant: always set at creation time
        return None
    return SmsDrainCandidate(
        notification_id=cast("UUID", row["id"]),
        landlord_id=cast("UUID", row["landlord_id"]),
        notification_type=cast("str", row["type"]),
        attempt=cast("int", row["attempt"]),
        message_id=UUID(str(message_id_raw)),
        body=str(body),
    )


async def _process_sms_drain_candidate(
    candidate: SmsDrainCandidate, *, effective_now: datetime
) -> str:
    """Claim + send exactly ONE attempt for *candidate* (may raise —
    callers must never let one candidate's exception silently stall the
    whole tick; see :func:`run_sms_drain_sweep`). *effective_now* is used
    ONLY for ``tenant_ack``'s own backoff write on a genuine send failure
    (safety re-review round 2) -- a no-op for ``emergency_sms`` candidates,
    which never back off (see :data:`_SELECT_DUE_EMERGENCY_SMS_DRAIN_SQL`'s
    own comment)."""
    new_attempt = candidate.attempt + 1

    async with _acm(get_admin_session)() as session:
        claim_row = (
            (
                await session.execute(
                    _CLAIM_SMS_DRAIN_SQL,
                    {
                        "id": str(candidate.notification_id),
                        "old_attempt": candidate.attempt,
                        "new_attempt": new_attempt,
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
        log.error("sms_drain_context_missing", notification_id=str(candidate.notification_id))
        sentry_sdk.capture_message(
            "emergency_chain: sms drain context missing for a claimed attempt",
            level="error",
            extras={"notification_id": str(candidate.notification_id)},
        )
        return "context_missing"

    if not ctx.tenant_phone or not ctx.twilio_number:
        # No stored channel back to this message's sender (see module
        # docstring "Known limitation" on emergency_chain.py) -- TERMINAL,
        # never retried again since retrying can't change this fact.
        # Marked 'exhausted' (not silently left pending forever, and not
        # 'failed' -- see _MARK_SMS_DRAIN_EXHAUSTED_SQL's own comment for
        # why 'failed' would keep this row in the retry set forever).
        async with _acm(get_admin_session)() as session:
            await session.execute(
                _MARK_SMS_DRAIN_EXHAUSTED_SQL, {"id": str(candidate.notification_id)}
            )
        log.warning(
            "sms_drain_no_tenant_phone",
            notification_id=str(candidate.notification_id),
            notification_type=candidate.notification_type,
        )
        return "no_tenant_phone"

    sender = get_twilio_sender()
    try:
        await sender.send_sms(to=ctx.tenant_phone, from_=ctx.twilio_number, body=candidate.body)
    except Exception as exc:
        log.error(
            "sms_drain_send_failed",
            notification_id=str(candidate.notification_id),
            notification_type=candidate.notification_type,
            exc_type=type(exc).__name__,
        )
        sentry_sdk.capture_message(
            "emergency_chain: sms drain send failed",
            level="error",
            extras={
                "notification_id": str(candidate.notification_id),
                "notification_type": candidate.notification_type,
                "exc_type": type(exc).__name__,
            },
        )
        async with _acm(get_admin_session)() as session:
            if candidate.notification_type == "tenant_ack":
                # Safety re-review round 2 -- exponential backoff, see the
                # module comment above _TENANT_ACK_BACKOFF_BASE_SECONDS.
                # NEVER for emergency_sms (see _SELECT_DUE_EMERGENCY_SMS_
                # DRAIN_SQL's own comment) -- that branch below is
                # unaffected, still _MARK_SMS_DRAIN_FAILED_SQL.
                next_attempt_at = effective_now + timedelta(
                    seconds=_tenant_ack_backoff_seconds(new_attempt)
                )
                await session.execute(
                    _MARK_TENANT_ACK_FAILED_WITH_BACKOFF_SQL,
                    {"id": str(candidate.notification_id), "next_attempt_at": next_attempt_at},
                )
            else:
                await session.execute(
                    _MARK_SMS_DRAIN_FAILED_SQL, {"id": str(candidate.notification_id)}
                )
        return "failed"

    async with _acm(get_admin_session)() as session:
        await session.execute(_MARK_SMS_DRAIN_SENT_SQL, {"id": str(candidate.notification_id)})

    log.info(
        "sms_drain_sent",
        notification_id=str(candidate.notification_id),
        notification_type=candidate.notification_type,
    )
    return "sent"


async def _run_sms_drain_candidate_safely(
    candidate: SmsDrainCandidate, *, effective_now: datetime
) -> str:
    """Never-raises wrapper — same rationale as
    :func:`_run_candidate_safely`: a row's own claim (or lack thereof) is
    the only durable state this sweep depends on, so there is no
    "stuck forever" risk from one candidate's exception blocking others."""
    try:
        return await _process_sms_drain_candidate(candidate, effective_now=effective_now)
    except Exception as exc:
        log.error(
            "sms_drain_candidate_processing_failed",
            notification_id=str(candidate.notification_id),
            notification_type=candidate.notification_type,
            exc_type=type(exc).__name__,
        )
        sentry_sdk.capture_message(
            "emergency_chain: sms drain candidate processing raised",
            level="error",
            extras={
                "notification_id": str(candidate.notification_id),
                "notification_type": candidate.notification_type,
                "exc_type": type(exc).__name__,
            },
        )
        return "processing_error"


async def _drain_emergency_sms_pass(*, effective_now: datetime) -> list[SmsDrainOutcome]:
    """Pass 1 -- UNBOUNDED. Drains every due ``emergency_sms`` row to
    completion, every tick, with no deadline and no cap (see "TWO PASSES"
    above). Run FIRST, before the ``tenant_ack`` pass, so a poisoned/slow
    ``tenant_ack`` backlog can never sit ahead of the tenant safety text in
    program order either. *effective_now* is threaded through purely for
    :func:`_process_sms_drain_candidate`'s shared signature -- a genuine
    no-op for every candidate here, which are all ``emergency_sms`` (never
    backed off)."""
    async with _acm(get_admin_session)() as session:
        rows = (await session.execute(_SELECT_DUE_EMERGENCY_SMS_DRAIN_SQL)).mappings().all()
        candidates = [
            c for row in rows if (c := _sms_drain_candidate_from_row(dict(row))) is not None
        ]

    outcomes: list[SmsDrainOutcome] = []
    for candidate in candidates:
        outcome = await _run_sms_drain_candidate_safely(candidate, effective_now=effective_now)
        outcomes.append(
            SmsDrainOutcome(
                notification_id=candidate.notification_id,
                notification_type=candidate.notification_type,
                outcome=outcome,
            )
        )
    return outcomes


async def _drain_tenant_ack_pass(
    *, effective_now: datetime, deadline_seconds: float, time_source: Callable[[], float]
) -> list[SmsDrainOutcome]:
    """Pass 2 -- 25s-bounded (issue #229). Drains due ``tenant_ack`` rows,
    up to :data:`_SMS_DRAIN_SELECT_BATCH_LIMIT` candidates, stopping
    CLAIMING new ones once *deadline_seconds* is exceeded (stop-claiming
    -not-abandoning — see "TWO PASSES" above). *start* is taken BEFORE the
    candidate SELECT (issue #229, PR #228 senior-review advisory 2) so the
    budget covers the SELECT's own duration too, not just the claim/send
    loop. *effective_now* is the DB-side "what's due" clock (safety
    re-review round 2's backoff window) -- unrelated to *time_source*,
    which bounds only this call's own wall-clock duration."""
    start = time_source()
    async with _acm(get_admin_session)() as session:
        rows = (
            (
                await session.execute(
                    _SELECT_DUE_TENANT_ACK_DRAIN_SQL,
                    {"limit": _SMS_DRAIN_SELECT_BATCH_LIMIT, "now": effective_now},
                )
            )
            .mappings()
            .all()
        )
        candidates = [
            c for row in rows if (c := _sms_drain_candidate_from_row(dict(row))) is not None
        ]

    outcomes: list[SmsDrainOutcome] = []
    for index, candidate in enumerate(candidates):
        if time_source() - start >= deadline_seconds:
            # Wall-clock budget exceeded (issue #229) -- stop CLAIMING new
            # candidates for the rest of this tick. Every candidate claimed
            # above already finished processing (this loop awaits each one
            # in turn, never abandoning a claimed row mid-send); the
            # remaining candidates stay 'pending'/'failed' and due, claimed
            # whole by the very next tick -- nothing lost, see
            # DEFAULT_TICK_DEADLINE_SECONDS's own docstring.
            log.info(
                "sms_drain_sweep_tenant_ack_deadline_reached",
                claimed_this_tick=len(outcomes),
                remaining_candidates=len(candidates) - index,
            )
            break
        outcome = await _run_sms_drain_candidate_safely(candidate, effective_now=effective_now)
        outcomes.append(
            SmsDrainOutcome(
                notification_id=candidate.notification_id,
                notification_type=candidate.notification_type,
                outcome=outcome,
            )
        )
    return outcomes


async def run_sms_drain_sweep(
    *,
    now: datetime | None = None,
    deadline_seconds: float = DEFAULT_TICK_DEADLINE_SECONDS,
    time_source: Callable[[], float] = _default_time_source,
) -> list[SmsDrainOutcome]:
    """DB entrypoint for one SMS-drain sweep tick — drains every
    ``pending``/``failed`` ``tenant_ack``/``emergency_sms`` row, resending
    until genuinely delivered. Called by ``app/scheduler.py``'s 60-second
    ticker, in the SAME tick as :func:`run_emergency_chain_sweep` and
    ``app/agent/degraded_mode_sweep.py::sweep_degraded_mode_retries``.

    ``now`` is an injectable override purely for tests (mirrors
    ``run_emergency_chain_sweep(*, now=...)``/
    ``run_push_outbox_sweep(*, now=...)``) — the DB-side "what's due" clock
    for ``tenant_ack``'s own backoff window (safety re-review round 2, see
    "TWO PASSES" below); a genuine no-op for ``emergency_sms``, which never
    backs off. Production callers never pass it; the default is genuine
    wall-clock time.

    TWO PASSES (issue #229, PR #228 senior-review, blocking finding 1) —
    see the module-level "TWO PASSES" comment above :data:`
    DEFAULT_TICK_DEADLINE_SECONDS` for the full starvation story this
    fixes: :func:`_drain_emergency_sms_pass` (UNBOUNDED, runs first, drains
    every due ``emergency_sms`` row to completion) then
    :func:`_drain_tenant_ack_pass` (25s-bounded, runs second). A poisoned
    ``tenant_ack`` backlog can therefore never delay, let alone starve, a
    genuinely due ``emergency_sms`` row — the two row types no longer share
    a queue or a budget at all.

    *deadline_seconds*/*time_source* bound ONLY the ``tenant_ack`` pass
    (issue #229 — see "TWO PASSES" above); *time_source* defaults to the
    real event loop clock (:func:`_default_time_source`) — tests inject a
    fake, monotonically-advanceable callable instead of sleeping for real
    seconds.
    """
    effective_now = now if now is not None else datetime.now(UTC)
    outcomes = await _drain_emergency_sms_pass(effective_now=effective_now)
    outcomes.extend(
        await _drain_tenant_ack_pass(
            effective_now=effective_now, deadline_seconds=deadline_seconds, time_source=time_source
        )
    )
    log.info("sms_drain_sweep_complete", candidates_processed=len(outcomes))
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
    """
    SELECT id, acknowledged_at,
           CASE
             WHEN payload ->> 'ack_token' = :token THEN 'landlord'
             WHEN payload ->> 'ack_token_backup' = :token THEN 'backup'
           END AS recipient_role
    FROM notifications
    WHERE payload ->> 'ack_token' = :token OR payload ->> 'ack_token_backup' = :token
    """
)
# #289: matches EITHER per-recipient token (module docstring "Per-recipient
# ack tokens") and reports WHICH one matched as ``recipient_role`` — used by
# :func:`acknowledge_by_token` for attribution, ignored by
# :func:`resolve_ack_token` (read-only rendering has no need for it). Two
# rows can never both match the same token (each key has its own UNIQUE
# partial expression index — migrations 0010/0018), and a single row's two
# token keys are always minted independently, so at most one branch of the
# ``CASE`` ever fires for a given ``:token``.

_INSERT_ACK_AUDIT_SQL = text(
    """
    INSERT INTO audit_log (landlord_id, case_id, actor, action, payload)
    VALUES (:landlord_id, :case_id, :actor, 'acknowledged', CAST(:payload AS jsonb))
    """
)


async def acknowledge_notification(
    notification_id: UUID, *, actor: str, channel: str, recipient_role: str | None = None
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

    ``recipient_role`` (#289) — ``'landlord'`` / ``'backup'`` / ``None``,
    recorded verbatim into the audit payload (never a phone number, rule
    #5). ``None`` means "genuinely unknown" (the voice press-1 surface has
    no way to tell which physical call is ringing — see module docstring
    "Per-recipient ack tokens"), not a default to be read as "landlord".
    :func:`acknowledge_by_token` passes whichever role
    :data:`_SELECT_NOTIFICATION_BY_TOKEN_SQL` resolved; the dashboard
    surface (``app/routers/notifications.py``) passes ``'landlord'``
    explicitly, since that path is authenticated and unambiguous.
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
                            "recipient_role": recipient_role,
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


async def resolve_ack_token(token: str) -> tuple[UUID, datetime | None] | None:
    """READ-ONLY lookup of *token* — ``(notification_id, acknowledged_at)``,
    or ``None`` if *token* matches no notification. NEVER mutates anything.
    Resolves EITHER per-recipient token (module docstring "Per-recipient
    ack tokens", #289) — the caller doesn't need to know or care which one
    was used; the confirmation page renders identically either way.

    Safety review, 2026-07-12 (finding 1, CRITICAL): ``GET /ack/{token}``
    used to call :func:`acknowledge_by_token` directly, so an SMS
    link-preview prefetcher (iMessage/RCS/carrier link scanners routinely
    ``GET`` a URL to generate a preview, with no human involved at all)
    could silently acknowledge a LIVE emergency chain before anyone ever
    saw the text. ``app/routers/notifications.py``'s ``GET`` handler now
    calls ONLY this function to render a confirmation page; the actual
    acknowledgment happens exclusively in ``POST /ack/{token}``, which
    calls :func:`acknowledge_by_token` below.
    """
    async with _acm(get_admin_session)() as session:
        row = (
            (await session.execute(_SELECT_NOTIFICATION_BY_TOKEN_SQL, {"token": token}))
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    return cast("UUID", row["id"]), cast("datetime | None", row["acknowledged_at"])


async def acknowledge_by_token(token: str, *, channel: str) -> tuple[UUID, datetime] | None:
    """Resolve the tokenized SMS-link token to a notification id and
    ACKNOWLEDGE it — ``None`` if *token* matches no notification at all.
    MUTATES (stamps ``acknowledged_at``) — only ever called from
    ``POST /ack/{token}`` (see module docstring "Safety review, 2026-07-12,
    finding 1" and :func:`resolve_ack_token`'s docstring). See
    :func:`acknowledge_notification` for idempotency semantics.

    #289: *token* may be either the landlord's or the backup contact's own
    token — :data:`_SELECT_NOTIFICATION_BY_TOKEN_SQL` resolves whichever
    one matches and reports which recipient it belongs to
    (``recipient_role``), forwarded verbatim to
    :func:`acknowledge_notification` for the audit trail.
    """
    async with _acm(get_admin_session)() as session:
        row = (
            (await session.execute(_SELECT_NOTIFICATION_BY_TOKEN_SQL, {"token": token}))
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        notification_id = cast("UUID", row["id"])
        recipient_role = cast("str | None", row["recipient_role"])

    acknowledged_at = await acknowledge_notification(
        notification_id, actor="system", channel=channel, recipient_role=recipient_role
    )
    if acknowledged_at is None:  # pragma: no cover — invariant: row existed a moment ago
        return None
    return notification_id, acknowledged_at


_REVOKE_BACKUP_ACK_TOKENS_SQL = text(
    """
    UPDATE notifications
    SET payload = payload - 'ack_token_backup', updated_at = now()
    WHERE type = 'emergency_call' AND status = 'pending' AND acknowledged_at IS NULL
      AND landlord_id = :landlord_id
      AND payload ->> 'property_id' = :property_id
      AND payload ? 'ack_token_backup'
    """
)


async def revoke_backup_ack_tokens(
    session: AsyncSession, *, landlord_id: UUID, property_id: UUID
) -> None:
    """Strip the backup contact's own ack token (``payload.ack_token_backup``)
    from every in-flight (``status='pending'``, not yet acknowledged)
    ``emergency_call`` notification for *property_id* — see module
    docstring "Per-recipient ack tokens" / "Revocation".

    Called by ``app/routers/properties.py`` the moment a ``PATCH`` genuinely
    CLEARS ``backup_contact`` (a non-null value transitioning to an
    explicit ``null`` — api-contracts.md's v1.25 amendment's "clears it"
    definition, #268), in the SAME transaction as the property update.

    DELIBERATELY DIVERGES from this module's own "admin engine only"
    convention (module docstring "DB access"): every other function here
    runs in a pre-identity/background context with no landlord JWT to
    scope a session to. This one is the opposite — it is only ever called
    from an ALREADY-AUTHENTICATED, RLS-scoped landlord request (``app/
    deps.py``'s ``require_landlord``) — so it takes that CALLER's own
    *session* instead of opening an admin one, for two reasons: (1) it
    then commits/rolls back ATOMICALLY with the property update itself
    (same transaction — either both the clear and the revocation land, or
    neither does), and (2) it inherits that session's RLS scoping for free.
    The explicit ``landlord_id = :landlord_id`` predicate above is
    defense-in-depth on top of that (CLAUDE.md: "RLS arrives in #22, code
    behaves as if it's already on" — this must be correct even if RLS
    were somehow not enforced on this connection), matching ``app/routers/
    notifications.py``'s own ``_SELECT_NOTIFICATION_OWNED_SQL`` precedent.

    A no-op (touches zero rows, no error) when there is no live chain for
    this property, or its ``emergency_call`` row(s) never got far enough
    to have a backup token minted yet, or it was already revoked — the
    ``payload ? 'ack_token_backup'`` guard just avoids a spurious
    ``updated_at`` bump in that case. The revoked row's very next claimed
    step (:data:`_CLAIM_STEP_SQL`) mints a FRESH, distinct
    ``ack_token_backup`` automatically — not yet disclosed to anyone — so
    the removed contact's old link stops matching immediately while the
    chain keeps running exactly as before for the landlord's own side.

    Does NOT revoke the landlord's own ``ack_token`` (a different payload
    key, never touched here) and does NOT revoke anything when
    ``backup_contact`` is merely EDITED to a different phone number rather
    than cleared (out of this issue's scope — flagged, not solved; see
    module docstring "Revocation").
    """
    await session.execute(
        _REVOKE_BACKUP_ACK_TOKENS_SQL,
        {"landlord_id": str(landlord_id), "property_id": str(property_id)},
    )


__all__: list[str] = [
    "DEFAULT_TICK_DEADLINE_SECONDS",
    "ESCALATION_FIXED_OFFSETS_MINUTES",
    "ESCALATION_REPEAT_INTERVAL_MINUTES",
    "TENANT_STATUS_TEMPLATE",
    "ActionOutcome",
    "EmergencyCallCandidate",
    "EmergencyChainOutcome",
    "EmergencyContext",
    "SmsDrainCandidate",
    "SmsDrainOutcome",
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
    "resolve_ack_token",
    "revoke_backup_ack_tokens",
    "run_emergency_chain_sweep",
    "run_sms_drain_sweep",
]
