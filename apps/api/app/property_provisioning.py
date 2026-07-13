"""Property Twilio-number provisioning + deprovisioning orchestration (#53).

This module sits between ``app/routers/properties.py`` (HTTP/DB wiring) and
``app/integrations/twilio_provision.py`` (thin Twilio REST wrappers) —
same layering as ``app/agent/emergency_chain.py`` sitting between the
webhook router and ``app/integrations/twilio_send.py``. Two jobs:

1. **Provisioning** (:func:`provision_number`, called from
   ``POST /v1/properties``): search → purchase → configure webhooks →
   best-effort A2P campaign association. A number that was actually
   PURCHASED but can't be fully provisioned (webhook config fails) is
   released as compensation before the error surfaces — never a
   purchased-but-orphaned number, and (because the caller's own DB INSERT
   only happens after this returns successfully, inside the SAME
   request-scoped transaction ``require_landlord`` hands out — see
   ``app/deps.py``) never a half-provisioned ``properties`` row either:
   any failure here means the row is never inserted at all, and any
   failure in the router's OWN insert step (extremely unlikely — see
   :func:`release_number_best_effort`'s caller) releases the number too.

2. **Deprovisioning** (:func:`schedule_number_release` +
   :func:`sweep_pending_number_releases`, called from ``DELETE
   /v1/properties/{id}`` and ``app/scheduler.py`` respectively): the
   ``properties`` row is hard-deleted immediately (unchanged contract),
   but the Twilio number itself is released only after a 24h grace period
   — "windows are data, not sleeps" (``apps/api/CLAUDE.md``), the same
   doctrine the approve-flow's ``scheduled_send_at`` undo window already
   uses. The "window" here is a durable ``notifications`` row
   (``type='number_release'``, schema-v1.md v1.10 amendment), not an
   in-process timer — ``app/scheduler.py``'s own docstring is explicit
   that its ticker "carries NO schedule state ... never in-process timers
   for the SCHEDULE," and this module follows that same rule.

Webhook URLs
------------
Both the inbound SMS webhook (``app/routers/webhooks/twilio.py``'s
``POST /sms``, #40) and the voice webhook (``POST /voice``, #108) are
configured on every purchased number, both derived from
``settings.public_base_url`` plus :data:`SMS_WEBHOOK_PATH`/
:data:`VOICE_WEBHOOK_PATH` — imported from that router module, where they
are themselves derived from the ACTUAL registered route table (never a
hand-duplicated literal here; safety review finding L3) so a future rename
of either endpoint can never silently orphan a newly-provisioned number's
webhook config. #108's OWN outbound calls fetch their TwiML from a
per-call, dynamically-parameterized url (``emergency_chain.py``'s
``render_voice_action_url``), not from the number's account-level Voice
URL setting — so configuring ``/voice`` here is purely a defensive default
for the case where someone dials the property's number directly (Twilio
would otherwise have no instructions at all for that call); ``/voice``
already answers gracefully with no ``notification_id`` query param (a
logged apology TwiML, see that router's own docstring), which is exactly
what a direct dial hits. ``public_base_url`` is required here (unlike its
optional-in-dev role for INBOUND signature verification) — there is no
proxy-header fallback that makes sense for constructing an OUTBOUND
"here's where to send future webhooks" URL to hand Twilio; if unset,
:func:`provision_number` raises :class:`PublicBaseUrlUnconfiguredError`
before attempting any Twilio call at all (a deployment-configuration
error, never a per-request one — see api-contracts.md's v1.12 amendment).

Deprovisioning safety (safety review, 2026-07-13)
----------------------------------------------------
Three additions beyond the original design, all in
:func:`sweep_pending_number_releases`:

- **M1/L1 — claim before calling Twilio.** Every due row is claimed with a
  single optimistic-concurrency ``UPDATE ... WHERE status='pending' AND
  attempt=:old_attempt`` (mirrors ``app/agent/emergency_chain.py``'s own
  claim discipline EXACTLY) before ``release_number`` is ever called —
  two scheduler ticks (on two machines, or two overlapping ticks on one)
  racing the same due row can never both call Twilio's release endpoint
  for it; the loser's claim UPDATE simply returns no row and it does
  nothing further this tick. The claim also advances ``attempt``/
  ``next_attempt_at`` BEFORE the call, so a crash mid-release leaves the
  row retryable on schedule rather than stuck.
- **M2 — a 404 from Twilio (already released) is treated as SUCCESS**, not
  a retryable failure: for a release goal, "the number doesn't exist
  anymore" already satisfies the goal. Marked ``sent`` immediately, no
  retry, no Sentry page — keeps the eventual exhaustion page trustworthy
  (it means "we kept trying and Twilio kept refusing," never "the thing we
  wanted was already true").
- **L2 — never release a SID a LIVE ``properties`` row still references.**
  Structurally this should never happen today (a released Twilio SID can
  never be re-issued to a later purchase), but this is exactly the kind of
  invariant a FUTURE number-reuse feature could silently violate — one
  indexed ``SELECT`` before every release call closes the catastrophic
  "release a number a tenant can still text" failure mode outright rather
  than trusting that invariant to hold forever. A hit here marks the row
  ``exhausted`` with a loud Sentry page (retrying would never help; the
  schedule itself is wrong).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager as _acm
from datetime import UTC, datetime, timedelta
from typing import Any

import sentry_sdk
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_admin_session
from app.integrations.twilio_provision import (
    TwilioNumberNotFoundError,
    TwilioProvisioner,
    get_twilio_provisioner,
)
from app.routers.webhooks.twilio import SMS_WEBHOOK_PATH, VOICE_WEBHOOK_PATH

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tunables — hardcoded constants, never settings/feature-flag driven (this
# module is not agent/prefilter/notifications, but grace periods and retry
# policy are procedural facts, not rollout/pricing knobs either way).
# ---------------------------------------------------------------------------

NUMBER_RELEASE_GRACE_PERIOD_SECONDS: float = 24 * 60 * 60
"""24 hours between a confirmed property deletion and the actual Twilio
release — a same-day window for a landlord/ops to notice an accidental
deletion before the number is gone for good (api-contracts.md v1.12)."""

_NUMBER_RELEASE_RETRY_INTERVAL_SECONDS: float = 15 * 60
"""On a release-call failure, retry in 15 minutes — short enough to
recover quickly from a transient Twilio blip, long enough not to hammer
Twilio's API every scheduler tick."""

_NUMBER_RELEASE_MAX_ATTEMPTS: int = 5
"""After this many failed release attempts, the row moves to `exhausted`
(the notifications table's existing terminal-vs-transient convention,
schema-v1.md v1.8 amendments) rather than retrying forever."""


# ---------------------------------------------------------------------------
# Exceptions — mapped to the standard error envelope by the router.
# ---------------------------------------------------------------------------


class PublicBaseUrlUnconfiguredError(Exception):
    """``settings.public_base_url`` is unset — there is no URL to hand
    Twilio for the inbound webhook. A deployment-configuration error, never
    a per-request one (production boot already requires this setting)."""


class NoNumbersAvailableError(Exception):
    """Every step of the search cascade (area code → province → any CA
    number) came back with zero candidates. No purchase was attempted, so
    there is nothing to release."""


class ProvisioningFailedError(Exception):
    """A genuine Twilio-side (or DB) failure during search/purchase
    /webhook-configuration. ``stage`` names which step failed — logged,
    never included in any response message (rule #5-adjacent: never
    interpolate exception internals into a client-facing message)."""

    def __init__(self, stage: str) -> None:
        super().__init__(f"provisioning failed at stage={stage}")
        self.stage = stage


class ProvisionResult:
    """The outcome of a successful :func:`provision_number` call."""

    __slots__ = ("a2p_status", "phone_number", "twilio_sid")

    def __init__(self, *, phone_number: str, twilio_sid: str, a2p_status: str) -> None:
        self.phone_number = phone_number
        self.twilio_sid = twilio_sid
        self.a2p_status = a2p_status


# ---------------------------------------------------------------------------
# Provisioning — POST /v1/properties
# ---------------------------------------------------------------------------


def _webhook_urls() -> tuple[str, str]:
    """Return ``(sms_url, voice_url)`` for a freshly-purchased number — see
    module docstring "Webhook URLs". Paths come from
    ``app/routers/webhooks/twilio.py``'s own registered-route constants
    (safety review finding L3), never a literal duplicated here."""
    if not settings.public_base_url:
        raise PublicBaseUrlUnconfiguredError()
    base = settings.public_base_url.rstrip("/")
    return f"{base}{SMS_WEBHOOK_PATH}", f"{base}{VOICE_WEBHOOK_PATH}"


async def _find_candidate_numbers(
    provisioner: TwilioProvisioner, *, area_code: str | None, province: str
) -> list[str]:
    """Search cascade: area code → property's province (the "nearest"
    fallback the issue's AC asks for — there is no lat/lon to search by
    proximity at creation time; see api-contracts.md's v1.12 amendment for
    why) → any available Canadian local number. Returns the first
    non-empty candidate list, or an empty list if every step came back
    empty."""
    if area_code:
        found = await provisioner.search_available_numbers(area_code=area_code)
        if found:
            return found

    found = await provisioner.search_available_numbers(region=province)
    if found:
        return found

    return await provisioner.search_available_numbers()


async def release_number_best_effort(twilio_sid: str) -> None:
    """Best-effort compensating release — never raises. Used both when a
    purchase can't be fully provisioned (webhook config failure) and when
    the caller's OWN post-purchase step (the DB insert/read-back in
    ``app/routers/properties.py``) fails. A failure HERE means a real,
    billed Twilio number is now orphaned (purchased, but no ``properties``
    row will ever reference it) — logged loudly (uuid/SID-level only) so
    ops can release it manually, but must never mask the ORIGINAL failure
    that triggered the compensation attempt."""
    provisioner = get_twilio_provisioner()
    try:
        await provisioner.release_number(twilio_sid=twilio_sid)
    except TwilioNumberNotFoundError:
        # Already gone -- same M2 reasoning as the sweep: for a release
        # goal this IS success, not a failure worth paging on.
        log.info("twilio_provisioning_compensation_release_already_gone", twilio_sid=twilio_sid)
    except Exception as exc:
        log.error(
            "twilio_provisioning_compensation_release_failed",
            twilio_sid=twilio_sid,
            exc_type=type(exc).__name__,
        )
        sentry_sdk.capture_message(
            "Twilio provisioning: post-failure compensation release failed "
            "-- a purchased number may be orphaned",
            level="error",
            extras={"twilio_sid": twilio_sid, "exc_type": type(exc).__name__},
        )


def alert_purchased_but_unrecorded(twilio_sid: str) -> None:
    """Loud, ALWAYS-fires alert for the "purchased a real number, then
    failed to durably record it" path (safety review finding M3) —
    ``app/routers/properties.py`` calls this whenever its post-purchase DB
    write (the INSERT or the read-back that follows it) fails, regardless
    of whether the compensating :func:`release_number_best_effort` call
    itself succeeds. Distinct from that function's own alert (which fires
    only when the RELEASE call itself fails) — this one fires on the
    triggering condition itself, so ops sees "a purchase had to be
    compensated" even on the common case where the compensating release
    succeeds cleanly. uuid/SID-level only (rule #5)."""
    log.error("twilio_provisioning_purchased_but_db_write_failed", twilio_sid=twilio_sid)
    sentry_sdk.capture_message(
        "Twilio provisioning: a number was purchased but never durably "
        "recorded -- compensating release attempted",
        level="error",
        extras={"twilio_sid": twilio_sid},
    )


async def provision_number(*, area_code: str | None, province: str) -> ProvisionResult:
    """Search, purchase, configure webhooks, and (best-effort) associate an
    A2P campaign for a brand-new property's Twilio number.

    Raises
    ------
    PublicBaseUrlUnconfiguredError
        Before any Twilio call — see module docstring.
    NoNumbersAvailableError
        The search cascade found nothing at all.
    ProvisioningFailedError
        A genuine Twilio-side failure during search/purchase/webhook
        configuration. ``stage`` distinguishes which step failed. A number
        that was actually purchased before the failure is released as
        compensation before this raises.
    """
    sms_url, voice_url = _webhook_urls()
    provisioner = get_twilio_provisioner()

    try:
        candidates = await _find_candidate_numbers(
            provisioner, area_code=area_code, province=province
        )
    except Exception as exc:
        raise ProvisioningFailedError("search") from exc

    if not candidates:
        raise NoNumbersAvailableError()

    phone_number = candidates[0]

    try:
        twilio_sid = await provisioner.purchase_number(phone_number=phone_number)
    except Exception as exc:
        raise ProvisioningFailedError("purchase") from exc

    try:
        await provisioner.configure_webhooks(
            twilio_sid=twilio_sid, sms_url=sms_url, voice_url=voice_url
        )
    except Exception as exc:
        await release_number_best_effort(twilio_sid)
        raise ProvisioningFailedError("configure_webhooks") from exc

    a2p_status = "skipped_unconfigured"
    messaging_service_sid = settings.twilio_messaging_service_sid
    if messaging_service_sid:
        try:
            await provisioner.associate_messaging_service(
                twilio_sid=twilio_sid, messaging_service_sid=messaging_service_sid
            )
            a2p_status = "associated"
        except Exception as exc:
            # Never fails provisioning on this -- see module docstring and
            # api-contracts.md's v1.12 amendment. A working, webhook
            # -configured number without a campaign association is a
            # strictly better outcome than none at all.
            log.warning(
                "twilio_a2p_association_failed",
                twilio_sid=twilio_sid,
                exc_type=type(exc).__name__,
            )
            a2p_status = "failed"
    else:
        log.info("twilio_a2p_association_skipped_unconfigured", twilio_sid=twilio_sid)

    log.info(
        "twilio_number_provisioned",
        twilio_sid=twilio_sid,
        a2p_status=a2p_status,
    )
    return ProvisionResult(phone_number=phone_number, twilio_sid=twilio_sid, a2p_status=a2p_status)


# ---------------------------------------------------------------------------
# Deprovisioning — DELETE /v1/properties/{id} schedules; the scheduler
# sweeps.
# ---------------------------------------------------------------------------

_INSERT_NUMBER_RELEASE_SQL = text(
    """
    INSERT INTO notifications (
        landlord_id, case_id, type, channel, status, payload, next_attempt_at
    )
    VALUES (
        :landlord_id, NULL, 'number_release', 'push', 'pending', CAST(:payload AS jsonb),
        :next_attempt_at
    )
    ON CONFLICT ((payload ->> 'twilio_sid')) WHERE type = 'number_release'
    DO NOTHING
    """
)


async def schedule_number_release(
    session: AsyncSession, *, landlord_id: str, property_id: str, twilio_sid: str
) -> None:
    """Write the durable, sweep-visible "release this number" record —
    called by ``DELETE /v1/properties/{id}`` on the SAME request-scoped
    session as the property delete itself (both commit together at
    ``require_landlord``'s teardown), only when the deleted property had a
    live ``twilio_number``. Idempotent via
    ``uq_notifications_number_release_dedupe`` (schema-v1.md v1.10) —
    guards the narrow race of two concurrent confirmed deletes for the
    same property.

    Payload carries `twilio_sid`/`property_id`/`landlord_id` only — uuids
    and a Twilio SID, never a phone number (rule #5). The property row
    itself is already gone by the time the sweep runs, so this row is the
    ONLY remaining record of which number needs releasing.
    """
    payload: dict[str, Any] = {
        "twilio_sid": twilio_sid,
        "property_id": property_id,
        "landlord_id": landlord_id,
    }
    next_attempt_at = datetime.now(UTC) + timedelta(seconds=NUMBER_RELEASE_GRACE_PERIOD_SECONDS)
    await session.execute(
        _INSERT_NUMBER_RELEASE_SQL,
        {
            "landlord_id": landlord_id,
            "payload": json.dumps(payload),
            "next_attempt_at": next_attempt_at,
        },
    )
    log.info(
        "twilio_number_release_scheduled",
        landlord_id=landlord_id,
        property_id=property_id,
        twilio_sid=twilio_sid,
    )


_NUMBER_RELEASE_SWEEP_BATCH_LIMIT: int = 50
"""Per-tick cap on how many due releases one sweep call processes (safety
review, finding M1/L1) — bounds a single tick's work under a burst of
deletions; mirrors the general "bounded work per tick" discipline every
other sweep in this codebase already follows. Anything left over is still
due (or becomes more overdue) and is picked up on the NEXT tick — no
schedule state is lost, only deferred, same as every other sweep here."""

_SELECT_DUE_NUMBER_RELEASES_SQL = text(
    """
    SELECT id, payload, attempt FROM notifications
    WHERE type = 'number_release' AND status = 'pending' AND next_attempt_at <= :now
    ORDER BY next_attempt_at ASC
    LIMIT :limit
    """
)

# Optimistic-concurrency claim (safety review, finding M1/L1) — mirrors
# app/agent/emergency_chain.py's own _CLAIM_STEP_SQL exactly: advances
# attempt/next_attempt_at BEFORE the Twilio call, guarded by a WHERE clause
# that only succeeds for the FIRST caller to claim this exact (id, attempt)
# pair. A concurrent second claim attempt (two scheduler ticks racing the
# same due row, on one machine or two) finds attempt already bumped and
# gets no row back -- it does nothing further this tick. This is the
# CRITICAL fix: without it, two machines (or two overlapping ticks) can
# both read the same due row and both call release_number for it.
_CLAIM_NUMBER_RELEASE_SQL = text(
    """
    UPDATE notifications
    SET attempt = :new_attempt, next_attempt_at = :next_attempt_at, updated_at = now()
    WHERE id = :id AND status = 'pending' AND attempt = :old_attempt
    RETURNING id
    """
)

_MARK_RELEASED_SQL = text(
    "UPDATE notifications SET status = 'sent', updated_at = now() WHERE id = :id"
)

_MARK_EXHAUSTED_SQL = text(
    "UPDATE notifications SET status = 'exhausted', updated_at = now() WHERE id = :id"
)

# L2 (safety review): defensive ownership guard -- never release a SID a
# LIVE properties row currently references. See module docstring
# "Deprovisioning safety".
_SELECT_LIVE_PROPERTY_FOR_SID_SQL = text("SELECT 1 FROM properties WHERE twilio_sid = :twilio_sid")


async def sweep_pending_number_releases(*, now: datetime | None = None) -> list[str]:
    """Release every due ``number_release`` row — called by
    ``app/scheduler.py``'s 60s ticker, same shape as
    ``run_emergency_chain_sweep``/``run_sms_drain_sweep``
    (``app/agent/emergency_chain.py``): reads what's due from the DB (never
    from in-process memory — ``app/scheduler.py``'s own "Crash-safety"
    doctrine), CLAIMS each row before acting (see module docstring
    "Deprovisioning safety", M1/L1), and durably records the outcome.

    Returns the list of ``twilio_sid``s actually released this tick
    (test seam / observability — callers in production discard it).

    ``now`` is an injectable override purely for tests (mirrors
    ``run_emergency_chain_sweep(*, now=...)``) — production callers never
    pass it, and the default is genuine wall-clock time.
    """
    effective_now = now if now is not None else datetime.now(UTC)

    async with _acm(get_admin_session)() as session:
        rows = (
            (
                await session.execute(
                    _SELECT_DUE_NUMBER_RELEASES_SQL,
                    {"now": effective_now, "limit": _NUMBER_RELEASE_SWEEP_BATCH_LIMIT},
                )
            )
            .mappings()
            .all()
        )

    provisioner = get_twilio_provisioner()
    released: list[str] = []

    for row in rows:
        notification_id = row["id"]
        payload = row["payload"] or {}
        old_attempt = int(row["attempt"])
        twilio_sid = payload.get("twilio_sid")

        if not twilio_sid:
            # Malformed row (should never happen -- schedule_number_release
            # always sets it) -- mark exhausted rather than looping on it
            # forever every tick.
            async with _acm(get_admin_session)() as session:
                await session.execute(_MARK_EXHAUSTED_SQL, {"id": notification_id})
            log.error("number_release_row_missing_twilio_sid", notification_id=str(notification_id))
            continue

        is_last_attempt = old_attempt + 1 >= _NUMBER_RELEASE_MAX_ATTEMPTS
        next_attempt_at = effective_now + timedelta(seconds=_NUMBER_RELEASE_RETRY_INTERVAL_SECONDS)

        # Claim FIRST, before any Twilio call -- see _CLAIM_NUMBER_RELEASE_SQL.
        async with _acm(get_admin_session)() as session:
            claim_row = (
                (
                    await session.execute(
                        _CLAIM_NUMBER_RELEASE_SQL,
                        {
                            "id": notification_id,
                            "old_attempt": old_attempt,
                            "new_attempt": old_attempt + 1,
                            "next_attempt_at": next_attempt_at,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        if claim_row is None:
            # Lost the race -- another process/tick already claimed this
            # row. Do nothing further this tick (test: "lost-claim race ->
            # loser does nothing").
            continue

        # L2: never release a SID a LIVE properties row still references --
        # see module docstring "Deprovisioning safety".
        async with _acm(get_admin_session)() as session:
            live_property = (
                (
                    await session.execute(
                        _SELECT_LIVE_PROPERTY_FOR_SID_SQL, {"twilio_sid": twilio_sid}
                    )
                )
                .mappings()
                .one_or_none()
            )
        if live_property is not None:
            async with _acm(get_admin_session)() as session:
                await session.execute(_MARK_EXHAUSTED_SQL, {"id": notification_id})
            log.error("number_release_blocked_live_property_owns_sid", twilio_sid=twilio_sid)
            sentry_sdk.capture_message(
                "Twilio number release blocked -- a LIVE property still owns this SID",
                level="error",
                extras={"twilio_sid": twilio_sid},
            )
            continue

        try:
            await provisioner.release_number(twilio_sid=twilio_sid)
        except TwilioNumberNotFoundError:
            # M2: already gone -- for a release GOAL this is success, not
            # a retryable failure. No Sentry page (keeps the eventual
            # exhaustion page trustworthy).
            async with _acm(get_admin_session)() as session:
                await session.execute(_MARK_RELEASED_SQL, {"id": notification_id})
            log.info("twilio_number_already_released", twilio_sid=twilio_sid)
            released.append(twilio_sid)
            continue
        except Exception as exc:
            if is_last_attempt:
                async with _acm(get_admin_session)() as session:
                    await session.execute(_MARK_EXHAUSTED_SQL, {"id": notification_id})
                log.error(
                    "number_release_exhausted",
                    twilio_sid=twilio_sid,
                    exc_type=type(exc).__name__,
                )
                sentry_sdk.capture_message(
                    "Twilio number release exhausted its retry budget",
                    level="error",
                    extras={"twilio_sid": twilio_sid, "exc_type": type(exc).__name__},
                )
            # else: the claim above already advanced attempt/next_attempt_at
            # -- nothing further to do; the next due tick retries it.
            continue

        async with _acm(get_admin_session)() as session:
            await session.execute(_MARK_RELEASED_SQL, {"id": notification_id})
        log.info("twilio_number_released", twilio_sid=twilio_sid)
        released.append(twilio_sid)

    return released


__all__: list[str] = [
    "NUMBER_RELEASE_GRACE_PERIOD_SECONDS",
    "NoNumbersAvailableError",
    "ProvisionResult",
    "ProvisioningFailedError",
    "PublicBaseUrlUnconfiguredError",
    "alert_purchased_but_unrecorded",
    "provision_number",
    "release_number_best_effort",
    "schedule_number_release",
    "sweep_pending_number_releases",
]
