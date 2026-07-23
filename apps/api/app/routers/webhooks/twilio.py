"""Twilio inbound webhooks — ``/webhooks/twilio/sms`` (#40) and
``/webhooks/twilio/status`` (#152).

No ``Authorization`` header — every request is verified via Twilio's
HMAC-SHA1 request signature instead (``app/integrations/twilio.py``; see
``docs/03-engineering/api-contracts.md``, "Webhooks"). Both endpoints run
on the ADMIN engine (``get_admin_session``), never ``get_session``/
``require_landlord`` — see ``app/db/session.py``'s module docstring
("Twilio webhook ingestion (#40, forward note)"): there is no landlord JWT
here to resolve a ``landlord_id`` GUC from, and an RLS-scoped session would
silently reject or misfile an inbound tenant message instead of storing it
— exactly the catastrophic direction never-break rule #1 (the emergency
line is never gated) forbids. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``, with the
justification documented there.

Transaction design (TWO consolidated safety-review rounds — round 1 found
a silent message-loss bug, round 2 then found a cross-process duplicate
-escalation bug in round 1's own interim fix)
--------------------------------------------------------------------------
ROUND 1 — silent message loss. An earlier revision ran the ENTIRE ``/sms``
handler (property lookup, Tier-0, the message INSERT, and every
post-persist side effect) on ONE shared ``AsyncSession``/transaction, with
per-side-effect failures merely caught-and-logged (never re-raised). That
is catastrophic: catching an exception does not un-poison the transaction
it happened in — a failed statement leaves the session's transaction
unusable, so the FINAL ``await session.commit()`` that ``get_admin_
session`` performs at request teardown itself raises, which its own
``except -> rollback`` handler converts into a silent ROLLBACK of the
*entire* transaction, including the message INSERT. Net effect: Twilio
gets a 200 (the response was already built), thinks the message was
delivered, and never retries — the tenant's message (possibly a "there is
a fire!") is gone forever, silently. Both independent reviewers
reproduced this by injecting a failure into a single post-persist side
effect and observing the message row vanish.

ROUND 2 — cross-process duplicate escalation. Round 1's fix made each
post-persist side effect idempotent via an APPLICATION-LEVEL
``INSERT ... WHERE NOT EXISTS`` keyed on ``message_id``. That is NOT safe
across processes/connections: two genuinely CONCURRENT webhook
redeliveries of the same ``MessageSid`` can each evaluate the
``NOT EXISTS`` condition as true before either commits its own ``INSERT``
— so BOTH insert an ``emergency_call``/``needs_eyes`` notification
(duplicate escalations, unbounded under a replay storm). Reproduced 3/3
with genuinely overlapping transactions. Fixed with a REAL Postgres
unique constraint the database itself enforces across every connection —
see ``docs/03-engineering/schema-v1.md``'s v1.3 amendments and migration
0006 (``uq_notifications_message_dedupe``).

The current design, in four parts:

1. **The message INSERT commits in its own transaction immediately** —
   ``await session.commit()`` runs right after the INSERT attempt
   (whether it inserted a new row or no-op'd on ``ON CONFLICT``), before
   any side effect is even attempted. By the time a 200 is possible, the
   row is durably on disk, full stop — nothing that happens afterward can
   roll it back, because nothing afterward shares that transaction.

2. **Every post-persist side effect runs in its OWN independent,
   isolated session** (``_isolated_session`` — a fresh admin session per
   side effect, not the request's own ``session``). A failure in one
   (caught by ``_safe_step``, logged, never re-raised) can only ever roll
   back that one side effect's own attempted work — it cannot touch the
   message row, and it cannot poison any other side effect's session.

   **EXCEPTION (safety review, 2026-07-12, finding 2 — BLOCKING):** the
   tenant emergency-artifact step (``_ensure_tenant_emergency_artifacts``)
   is NOT wrapped in ``_safe_step`` — a failure there is logged + paged
   (same shape as ``_safe_step``'s ``alert_on_failure``) and then
   RE-RAISED as an ``AppError(500, ...)``, mirroring the conflict-path
   recovery failure below (module docstring point 4). Rationale: for
   every OTHER side effect, "the artifact never got created and nobody
   finds out until #108's sweeper" was an acceptable tide-over (see "Ops
   visibility tide-over" below, now closed). For the tenant HARD-hit
   escalation specifically, silently 200-ing a failed artifact creation
   means Twilio never retries and the landlord may simply never be
   called — exactly the class of bug the message-row redesign above
   already fixed for message loss; the same fix now applies to this one
   artifact. This does NOT apply to the landlord/``needs_eyes`` side
   effect (still ``_safe_step``, still fail-open) — only the tenant
   emergency-artifact step carries this exception.
   Chosen over SAVEPOINTs on the shared session: independent sessions are
   simpler to reason about correctly (no reliance on autobegin/SAVEPOINT
   interaction with ``get_admin_session``'s commit-on-exit contract) and
   reuse ``get_admin_session`` itself rather than introducing a second
   session-construction pattern — see ``_isolated_session``.

3. **Post-persist side effects are idempotent, keyed on ``message_id``,
   via a REAL Postgres unique index** — ``notifications.
   uq_notifications_message_dedupe`` (migration 0006), a partial unique
   expression index on ``(payload ->> 'message_id', type)`` for
   ``type IN ('emergency_call', 'needs_eyes')``. The INSERT uses
   ``ON CONFLICT (...) WHERE ... DO NOTHING RETURNING id`` targeting this
   index directly (Postgres's own unique-index inference) — safe across
   arbitrarily many concurrent connections, unlike an application-level
   existence check. This closes the crash-recovery hole: if the process
   dies AFTER step 1's commit but BEFORE a side effect completes, the
   message is safely stored but its artifacts are missing. Twilio's
   at-least-once redelivery retries the same ``MessageSid`` — hits
   ``ON CONFLICT DO NOTHING`` on the MESSAGE INSERT (no new row) — and the
   handler looks up the EXISTING row's persisted ``party``/``prefilter``
   and re-runs the SAME idempotent post-persist function. A genuine
   happy-path duplicate finds its artifacts already exist (the
   notification's own ``ON CONFLICT`` returns no row) and no-ops; a
   crash-recovery duplicate finds them missing and creates them — exactly
   once either way, enforced by Postgres regardless of how many
   redeliveries race concurrently.

   Ops-visibility alerts (``_alert_tenant_hard_fire``, consolidated
   review item 3) fire ONLY when the notification INSERT actually
   returned a new row — i.e. THIS delivery is the one that created the
   escalation — never on a redelivery that found it already created. This
   both matches "alert once per real escalation" and naturally bounds
   alert volume under a replay storm.

4. **The conflict-path RECOVERY lookup fails closed to a 5xx, not a 200**
   (consolidated review item 2) — if the SELECT that recovers an existing
   row's authoritative data fails, or (structurally unexpected) finds no
   row, or the stored ``prefilter`` snapshot fails to parse (item 5), THIS
   request cannot complete the recovery — but the message row IS already
   durably stored (from an earlier delivery), so a 5xx here is SAFE: it
   tells Twilio to retry, which is exactly the recovery mechanism.
   Returning 200 here (an earlier revision did) would foreclose that
   retry and could leave a HARD-hit message's artifacts permanently
   missing.

``/status`` never touches ``messages`` — it only ever appends to
``message_status_events`` (also append-only, no UNIQUE, no upsert — every
callback, including duplicates and out-of-order arrivals, is a fact) and
always answers 200 once the signature is valid; its ENTIRE post-signature
body is wrapped in one try/except (consolidated review item 2 — an
unwrapped DB blip on the ``twilio_sid`` lookup was a real path to an
unintended 500, which is itself a contract violation: Twilio retry-storms
on any non-2xx).

Ops visibility (consolidated review items 3/4, #108 closed 2026-07-12): a
``notifications`` row sitting at ``status='pending'`` is now actively
worked by ``app/scheduler.py``'s 60s ticker (the escalation chain sweep
AND the SMS-drain sweep — see ``app/agent/emergency_chain.py``).
``_alert_unknown_to`` and ``_alert_tenant_hard_fire`` below both
``log.error`` AND ``sentry_sdk.capture_message`` (uuids/category names/an
HMAC-keyed digest of the unrecognized ``To`` number only — NEVER the raw
phone number or message body, rule #5) so a human actually sees these
immediately too, independent of the sweeper's own cadence.

Neither ``/sms`` nor ``/status`` calls Twilio's REST API (no outbound send
anywhere in either handler) — both are inbound-only receivers.

``POST /webhooks/twilio/voice`` (#108, added below) is different: it is the
TwiML callback for calls the emergency escalation chain itself places (via
``app/agent/emergency_chain.py``, using
``app/integrations/twilio_send.py``) — this router still never calls
Twilio's REST API directly, it only ANSWERS Twilio's request for what to
say/gather next. Handles two shapes on the SAME endpoint/URL (Twilio
distinguishes them by whether ``Digits`` is present in the form body, not
the path — see ``render_voice_action_url`` in ``emergency_chain.py``):

1. **Initial TwiML fetch** (no ``Digits`` yet) — returns a ``<Gather
   numDigits="1">`` wrapping a spoken summary, falling through to a
   closing ``<Say>`` if nothing is pressed within the timeout (no second
   request in that case — Twilio just ends the call; the chain's NEXT
   scheduled attempt, not a second leg of this same call, is what tries
   again).
2. **Gather completion** (``Digits`` present) — ``Digits == "1"`` calls
   ``emergency_chain.acknowledge_notification`` (idempotent — stops the
   chain) and speaks a short confirmation; any other digit (or none
   matching) just speaks a closing line. Either way, always valid TwiML,
   never a bare error status — Twilio has no useful retry story for a
   voice callback failure the way it does for ``/sms``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Coroutine
from contextlib import AbstractAsyncContextManager
from contextlib import asynccontextmanager as _acm
from typing import Annotated, Any
from uuid import UUID

import sentry_sdk
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import approve_by_sms, emergency_chain, prefilter
from app.agent.emergency import fire_emergency_protocol
from app.agent.graph_entry import enqueue_classification
from app.agent.schemas import PrefilterResult
from app.config import settings
from app.db.session import get_admin_session
from app.errors import AppError
from app.integrations.twilio import reconstruct_signing_url, verify_signature

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks/twilio", tags=["webhooks"])

# ---------------------------------------------------------------------------
# Shared: TwiML-empty response, signature verification, isolated sessions
# ---------------------------------------------------------------------------


def _twiml_empty() -> Response:
    """The uniform 200 response for ``/sms``/``/status`` — an empty TwiML
    ``<Response/>`` telling Twilio "received, no auto-reply". A fresh
    ``Response`` instance every call (never share one across requests)."""
    return Response(content="<Response/>", media_type="text/xml", status_code=200)


def _twiml_response(xml: str) -> Response:
    """A TwiML response carrying real markup (``/voice``, #108) — a fresh
    ``Response`` instance every call, same convention as ``_twiml_empty``."""
    return Response(content=xml, media_type="text/xml", status_code=200)


async def _extract_and_verify(request: Request) -> dict[str, str]:
    """Read the form body and verify Twilio's signature.

    Returns the flattened form params (string values only — Twilio never
    uploads files) on success.

    Raises
    ------
    AppError
        403 ``invalid_signature`` if the signature is missing or does not
        match — BEFORE any DB access, in every caller. Never logs the
        signature, the auth token, the body, or any phone number (rule
        #5) — only the request path, which carries no PII.
    """
    form = await request.form()
    params: dict[str, str] = {k: v for k, v in form.multi_items() if isinstance(v, str)}

    signature = request.headers.get("X-Twilio-Signature")
    url = reconstruct_signing_url(request, public_base_url=settings.public_base_url)

    if not verify_signature(url, params, signature, settings.twilio_auth_token):
        log.warning("twilio_webhook_signature_rejected", request_path=request.url.path)
        raise AppError(
            status_code=403,
            code="invalid_signature",
            message="Request could not be verified.",
        )

    return params


def _isolated_session() -> AbstractAsyncContextManager[AsyncSession]:
    """A short-lived, independent admin session for a single post-persist
    side effect.

    Commits on clean exit, rolls back on any exception, always closes —
    identical lifecycle to ``get_admin_session`` (this wraps that SAME
    generator function via ``contextlib.asynccontextmanager`` rather than
    duplicating its commit/rollback/close logic) — but a FRESH session
    per call, fully isolated from the request's own ``session`` and from
    every other side effect's session. This is the isolation that makes a
    failure in one side effect unable to roll back the message row or any
    OTHER side effect (see module docstring, "Transaction design" point
    2). Already allowlisted in ``tests/test_migrations_0005.py::
    _ADMIN_SESSION_ALLOWLIST`` (same file as the router).
    """
    return _acm(get_admin_session)()


async def _safe_step(
    stage: str,
    awaitable: Coroutine[Any, Any, bool],
    *,
    alert_on_failure: bool = False,
) -> bool:
    """Run *awaitable*, catching and logging (never re-raising) any
    exception — the shared "post-persistence, must still 200" guard
    (module docstring). ``stage`` is a short, static label (never
    request data) identifying which step failed, so structured logs
    stay debuggable without ever containing a body/phone/signature.

    Returns the awaitable's own ``bool`` result on success (e.g. "did
    this idempotent INSERT actually create a new row?", consolidated
    review item 3), or ``False`` on any caught failure — a failed attempt
    created nothing.

    ``alert_on_failure`` additionally pages via Sentry (consolidated
    review item 4) — used for the tenant emergency-artifact path, where a
    failure here means a HARD-hit message's escalation artifacts may not
    exist at all, which nothing else will notice before #108's sweeper
    ships.
    """
    try:
        return await awaitable
    except Exception as exc:
        log.error("twilio_sms_post_persist_stage_failed", stage=stage, exc_type=type(exc).__name__)
        if alert_on_failure:
            sentry_sdk.capture_message(
                "Twilio inbound webhook: post-persist side effect failed",
                level="error",
                extras={"stage": stage, "exc_type": type(exc).__name__},
            )
        return False


def _digest(value: str) -> str:
    """A short, KEYED, one-way reference to *value* — HMAC-SHA256 with
    ``settings.twilio_auth_token`` as the key, truncated AFTER keying.
    Lets ops correlate repeated occurrences of the SAME unrecognized
    number across log lines/Sentry events without ever exposing the
    number itself.

    Consolidated review item 4: an earlier revision used a plain,
    UNKEYED ``sha256(value)[:16]`` — brute-forceable, because E.164 phone
    numbers are a small, enumerable keyspace (a few billion combinations
    at most); anyone who knows the (public) hash algorithm could simply
    hash every possible number and match the digest back to a real one.
    Keying the HMAC with a secret this process already holds (the Twilio
    auth token) makes that infeasible without the key — truncating a
    KEYED MAC is safe (unlike truncating an unkeyed hash, which just
    narrows the brute-force space further). NEVER reuse the OLD, unkeyed
    pattern for a tenant's own ``From`` number or any other real phone
    number — this keyed version is the only one that should ever be used
    for that.
    """
    return hmac.new(
        settings.twilio_auth_token.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]


def _alert_unknown_to(*, to_digest: str, twilio_sid: str) -> None:
    """Consolidated review item 3: an unrecognized ``To`` number now goes
    LOUD (log.error + Sentry) instead of a quiet info log, so ops notices
    a number that is eating inbound messages. Metadata only — a digest of
    ``To``, never the raw number, never ``From``, never the body."""
    log.error("twilio_sms_unknown_to_number", to_digest=to_digest, twilio_sid=twilio_sid)
    sentry_sdk.capture_message(
        "Twilio inbound SMS addressed to an unrecognized `To` number",
        level="error",
        extras={"to_digest": to_digest, "twilio_sid": twilio_sid},
    )


def _alert_tenant_hard_fire(*, message_id: UUID, property_id: UUID, categories: list[str]) -> None:
    """Consolidated review item 4: a tide-over until #108's escalation
    sweeper exists — a ``notifications`` row sitting at ``status=
    'pending'`` pages nobody today. Fires on EVERY Tier-0 HARD hit on a
    tenant message (including redeliveries of the same message, which is
    deliberate: continued alerting on a still-unhandled emergency is the
    correct failure mode here, not noise-suppression). uuids/category
    names only — never a phone number or message body."""
    log.error(
        "twilio_sms_tenant_hard_fire",
        message_id=str(message_id),
        property_id=str(property_id),
        categories=categories,
    )
    sentry_sdk.capture_message(
        "Tier-0 HARD hit on a tenant message",
        level="error",
        extras={
            "message_id": str(message_id),
            "property_id": str(property_id),
            "categories": categories,
        },
    )


# ---------------------------------------------------------------------------
# /sms — routing helpers
# ---------------------------------------------------------------------------

_SELECT_PROPERTY_BY_TO_SQL = text(
    "SELECT id, landlord_id FROM properties WHERE twilio_number = :to_number"
)

_SELECT_LANDLORD_PHONE_SQL = text("SELECT phone FROM landlords WHERE id = :landlord_id")

_SELECT_ACTIVE_TENANT_SQL = text(
    "SELECT id FROM tenants WHERE property_id = :property_id AND phone = :phone AND active = true"
)


async def _lookup_active_tenant(
    session: AsyncSession, *, property_id: UUID, phone: str
) -> UUID | None:
    row = (
        (
            await session.execute(
                _SELECT_ACTIVE_TENANT_SQL,
                {"property_id": str(property_id), "phone": phone},
            )
        )
        .mappings()
        .one_or_none()
    )
    return row["id"] if row is not None else None


async def _is_landlord_command_channel(
    session: AsyncSession,
    *,
    landlord_id: UUID,
    property_id: UUID,
    from_number: str,
) -> bool:
    """api-contracts.md routing predicate: ``From`` == the landlord's phone
    for the property owning ``To``, AND ``From`` does not match an active
    tenant of that property. On collision (a self-managing landlord who is
    also an active tenant in-unit) this returns ``False`` — the TENANT
    pipeline wins, so an emergency can never be routed away from the
    tenant path."""
    landlord_row = (
        (await session.execute(_SELECT_LANDLORD_PHONE_SQL, {"landlord_id": str(landlord_id)}))
        .mappings()
        .one_or_none()
    )
    landlord_phone = landlord_row["phone"] if landlord_row is not None else None

    if not landlord_phone or landlord_phone != from_number:
        return False

    active_tenant_id = await _lookup_active_tenant(
        session, property_id=property_id, phone=from_number
    )
    return active_tenant_id is None


# ---------------------------------------------------------------------------
# /sms — persistence + idempotent side effects
# ---------------------------------------------------------------------------

_INSERT_MESSAGE_SQL = text(
    """
    INSERT INTO messages (
        landlord_id, property_id, tenant_id, case_id, direction, party,
        body, twilio_sid, prefilter
    )
    VALUES (
        :landlord_id, :property_id, :tenant_id, :case_id, 'inbound', :party,
        :body, :twilio_sid, CAST(:prefilter AS jsonb)
    )
    ON CONFLICT (twilio_sid) DO NOTHING
    RETURNING id
    """
)
# `:case_id` (#122) is NULL for every tenant message (case identity isn't
# known at insert time — #110 owns case attach, unchanged) and for a
# landlord message with nothing to correlate against; it is the referenced
# draft's case ONLY for a recognized approve-by-SMS token that correlates
# to a real draft-ready notice (schema-v1.md v1.1's own "case_id = the
# referenced draft's case" comment on `messages.party`). Set once, at
# INSERT time, because `messages` is append-only and can never be
# backfilled afterward — see `app/agent/approve_by_sms.py`'s own module
# docstring "Two-phase design".

# Used on the conflict path ONLY (no row came back from the INSERT above) to
# recover the persisted row's authoritative routing/prefilter data — see
# module docstring "Transaction design" point 3. `case_id` (#122) is
# recovered too, so a REDELIVERED landlord reply's post-persist dispatch
# uses the SAME case this exact message was originally correlated to,
# never a freshly re-resolved (and potentially different) one — see
# `app/agent/approve_by_sms.py::resolve_reply_for_recovered_case`.
_SELECT_MESSAGE_FOR_RECOVERY_SQL = text(
    "SELECT id, landlord_id, property_id, party, tenant_id, case_id, prefilter "
    "FROM messages WHERE twilio_sid = :sid"
)

# Idempotent (atomic, single-statement) creation via a REAL Postgres
# partial unique expression index (schema-v1.md v1.3, migration 0006:
# uq_notifications_message_dedupe) -- ON CONFLICT targets that index
# directly via Postgres's own unique-index inference. Safe across
# CONCURRENT processes/connections, unlike an application-level
# "WHERE NOT EXISTS" (an earlier revision used that, and a safety review
# reproduced 3/3 that two genuinely concurrent redeliveries can each pass
# the existence check before either commits -- see module docstring point
# 3). The WHERE clause here MUST reproduce the index's partial predicate
# VERBATIM -- Postgres's unique-index inference for a partial index
# requires a textually-equivalent predicate to identify the right index.
_INSERT_NEEDS_EYES_SQL = text(
    """
    INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload)
    VALUES (:landlord_id, NULL, 'needs_eyes', 'push', 'pending', CAST(:payload AS jsonb))
    ON CONFLICT ((payload ->> 'message_id'), type) WHERE type IN ('emergency_call', 'needs_eyes')
    DO NOTHING
    RETURNING id
    """
)

_INSERT_EMERGENCY_NOTIFICATION_SQL = text(
    """
    INSERT INTO notifications (
        landlord_id, case_id, type, channel, status, payload, next_attempt_at
    )
    VALUES (
        :landlord_id, NULL, 'emergency_call', 'voice', 'pending', CAST(:payload AS jsonb), now()
    )
    ON CONFLICT ((payload ->> 'message_id'), type) WHERE type IN ('emergency_call', 'needs_eyes')
    DO NOTHING
    RETURNING id
    """
)
# Safety review, 2026-07-12 (finding N1, BLOCKING): the row is now BORN
# ENRICHED -- ``next_attempt_at = now()`` (sweep-visible from the instant
# this INSERT commits, no separate "enrich" step/transaction/module
# required) AND the ack token lives in ``payload`` from the very first
# write (see ``notification_payload`` below). This closes the "pre-enrich
# window": previously, a crash/failure in ``app/agent/emergency_chain.py``'s
# OWN separate enrich transaction (which used to run AFTER this INSERT,
# in a DIFFERENT module/transaction/request phase) could strand a row at
# ``next_attempt_at IS NULL`` forever -- durably persisted, but invisible
# to the sweep, with no redelivery to save it (Twilio already got its 200,
# or the artifact-creation failure already 5xx'd and a retry would just
# hit ON CONFLICT and skip re-enriching). See
# ``app/agent/emergency_chain.py``'s module docstring "The instant +
# durable sweep hybrid" for the full before/after account, and its sweep
# SELECT's own ``next_attempt_at IS NULL`` clause for belt 2 (healing any
# row that somehow still lacks it).

_INSERT_EMERGENCY_AUDIT_SQL = text(
    """
    INSERT INTO audit_log (landlord_id, case_id, actor, action, payload)
    VALUES (:landlord_id, NULL, 'prefilter', 'emergency_triggered', CAST(:payload AS jsonb))
    """
)

# Used ONLY by /voice's initial-TwiML-fetch leg (#108) — the property label
# for the spoken summary plus the payload's stored categories, keyed on the
# emergency_call notification id embedded in the call's own action URL
# (emergency_chain.render_voice_action_url). No phone number/message body
# ever enters this query or its result (rule #5).
_SELECT_NOTIFICATION_FOR_VOICE_SQL = text(
    """
    SELECT n.payload AS payload, p.label AS property_label
    FROM notifications n
    LEFT JOIN properties p ON p.id = (n.payload ->> 'property_id')::uuid
    WHERE n.id = :id
    """
)


async def _ensure_needs_eyes_notification(
    *,
    landlord_id: UUID,
    property_id: UUID,
    message_id: UUID,
    prefilter_result: PrefilterResult,
) -> bool:
    """Landlord command-channel messages that are NOT a recognized,
    correlatable approve-by-SMS reply (#122) get a ``needs_eyes``
    notification here instead — "anything else replied → logged +
    surfaced" (issue #122 AC), same fallback every landlord-authored
    message got before #122 existed (api-contracts.md, "Webhooks": "never
    silently dropped"). A RECOGNIZED token ("1"/"2"/"UNDO") that
    correlates to a real draft-ready notice is dispatched to
    ``app.agent.approve_by_sms.handle_reply`` instead (see the caller,
    ``_run_post_persist_side_effects``) and never reaches this function at
    all. This applies whether or not Tier-0 fired: a Tier-0 HARD hit
    on a landlord-authored message does NOT invoke the tenant emergency
    protocol (there is no tenant/case to act on) — it is recorded (the
    prefilter snapshot already lives on the ``messages`` row itself) and
    surfaced here instead. Payload carries only identifiers + prefilter
    category names — never the message body (rule #5).

    Idempotent via ``uq_notifications_message_dedupe`` (schema-v1.md v1.3,
    migration 0006) and runs on its OWN isolated session — see module
    docstring. Returns ``True`` if this call created a new notification,
    ``False`` if one already existed (idempotent no-op)."""
    payload = {
        "message_id": str(message_id),
        "property_id": str(property_id),
        "prefilter_hard_hit": prefilter_result.hard_hit,
        "categories": prefilter_result.categories,
    }
    async with _isolated_session() as session:
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


_MAX_ACK_TOKEN_INSERT_ATTEMPTS = 3
"""Safety review, 2026-07-12 (finding 4, LOW): ``uq_notifications_ack_token``
(schema-v1.md v1.9, migration 0010) is a genuine UNIQUE index over a
random ``secrets.token_urlsafe(24)`` value (~144 bits of entropy) — a
collision is astronomically unlikely, but "unlikely" is not "impossible",
and the unique index means Postgres WILL raise on one. Regenerating and
retrying a bounded few times, inline, makes the index truly fail-safe
instead of merely fail-loud (a real collision would otherwise 500 the
whole webhook request over something a fresh random token trivially
fixes)."""


def _is_ack_token_collision(exc: IntegrityError) -> bool:
    """``True`` iff *exc* is a UNIQUE VIOLATION on
    ``uq_notifications_ack_token`` specifically — never swallows any OTHER
    integrity error (e.g. a genuine schema/FK problem), which must still
    propagate and 5xx normally."""
    orig = getattr(exc, "orig", None)
    constraint_name = getattr(orig, "constraint_name", None)
    if constraint_name == "uq_notifications_ack_token":
        return True
    # Defensive fallback across driver/version differences in whether
    # constraint_name is populated -- never the primary detection path.
    return "uq_notifications_ack_token" in str(exc)


async def _ensure_tenant_emergency_artifacts(
    *,
    landlord_id: UUID,
    property_id: UUID,
    message_id: UUID,
    prefilter_result: PrefilterResult,
) -> bool:
    """Tier-0 HARD hit on a TENANT message. Leaves three durable, queryable
    artifacts, in order (the actual voice call / safety SMS / escalation
    chain is #108 — see ``app/agent/emergency.py``):

    1. ``notifications`` (``type='emergency_call'``, ``status='pending'``,
       ``next_attempt_at=now()`` — BORN ENRICHED, safety review 2026-07-12
       finding N1: see ``_INSERT_EMERGENCY_NOTIFICATION_SQL``'s own
       comment) — inserted idempotently via ``uq_notifications_message_dedupe``
       (schema-v1.md v1.3, migration 0006); this INSERT is the single
       idempotency GATE for the whole group, enforced by Postgres itself
       (safe across concurrent processes — module docstring point 3): if
       it returns no row (already existed — a genuine duplicate delivery,
       artifacts already created), nothing below runs either, so a retry
       can never double-log the audit entry or double-invoke the seam
       call. The ``payload`` also carries a fresh ``ack_token`` — see
       :data:`_MAX_ACK_TOKEN_INSERT_ATTEMPTS` for what happens on the
       (astronomically unlikely) event that token collides with an
       existing row's.
    2. ``audit_log`` ``emergency_triggered`` (``actor='prefilter'``,
       payload = rules fired — never the message body, rule #5) — only
       written when (1) actually created a new row.
    3. The ``fire_emergency_protocol`` seam call — only invoked when (1)
       actually created a new row.

    ``case_id`` is NULL throughout: this runs pre-routing (#110 owns case
    attach; conversation-model.md). Runs on its OWN isolated session PER
    ATTEMPT — see module docstring "Transaction design". Returns ``True``
    if this call created the artifacts (a genuine new escalation),
    ``False`` if they already existed (idempotent no-op) — the caller uses
    this to decide whether to alert (consolidated review item 3: alert
    only on genuine creation, never on a redelivery of an
    already-escalated message)."""
    notification_id: UUID | None = None

    for attempt in range(_MAX_ACK_TOKEN_INSERT_ATTEMPTS):
        notification_payload = {
            "message_id": str(message_id),
            "property_id": str(property_id),
            "categories": prefilter_result.categories,
            "ack_token": secrets.token_urlsafe(24),
        }
        try:
            async with _isolated_session() as session:
                notification_row = (
                    (
                        await session.execute(
                            _INSERT_EMERGENCY_NOTIFICATION_SQL,
                            {
                                "landlord_id": str(landlord_id),
                                "payload": json.dumps(notification_payload),
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )

                if notification_row is None:
                    # Idempotent no-op: an earlier attempt already created
                    # this message's emergency artifacts (enforced by
                    # Postgres's own unique index, safe even under
                    # genuinely concurrent retries).
                    return False

                notification_id = notification_row["id"]

                audit_payload = {
                    "rules_fired": prefilter_result.categories,
                    "message_id": str(message_id),
                }
                await session.execute(
                    _INSERT_EMERGENCY_AUDIT_SQL,
                    {"landlord_id": str(landlord_id), "payload": json.dumps(audit_payload)},
                )
            break  # committed cleanly -- stop retrying
        except IntegrityError as exc:
            is_last_attempt = attempt == _MAX_ACK_TOKEN_INSERT_ATTEMPTS - 1
            if _is_ack_token_collision(exc) and not is_last_attempt:
                log.warning("emergency_ack_token_collision_retrying", attempt=attempt)
                continue
            raise

    if notification_id is None:  # pragma: no cover — invariant: break only reached after a set id
        raise RuntimeError("emergency artifact insert loop exited without a notification id")

    # Outside the DB transaction on purpose: emergency.py does no DB access
    # of its own (see its module docstring) -- calling it after the
    # artifacts are durably committed means the seam is only ever invoked
    # once the durable record it announces already exists.
    await fire_emergency_protocol(
        notification_id=notification_id,
        message_id=message_id,
        property_id=property_id,
        categories=prefilter_result.categories,
    )
    return True


async def _run_post_persist_side_effects(
    background_tasks: BackgroundTasks,
    *,
    landlord_id: UUID,
    property_id: UUID,
    message_id: UUID,
    party: str,
    prefilter_result: PrefilterResult,
    parsed_reply: approve_by_sms.ParsedReply | None,
) -> None:
    """The single shared post-persist path — called identically whether
    *this* request's INSERT just created the row (fresh delivery) or hit
    ``ON CONFLICT`` and recovered an existing row's data (duplicate or
    crash-recovery delivery; see module docstring "Transaction design"
    point 3). Every side effect here is individually idempotent (enforced
    by Postgres, safe even under concurrent retries) and isolated, so
    calling this twice (or a hundred times, concurrently) for the same
    ``message_id`` is always safe.

    *parsed_reply* (#122) is ``None`` for every tenant message (unused);
    for a landlord message it is whatever ``approve_by_sms.resolve_reply``/
    ``resolve_reply_for_recovered_case`` already resolved BEFORE the
    INSERT (module docstring "Tier-0 BEFORE the routing split" sibling —
    see ``app.agent.approve_by_sms``'s own "Two-phase design"). A fully
    -resolved reply (a recognized token that correlates to a real
    draft-ready notice) dispatches to ``approve_by_sms.handle_reply``,
    fail-open via ``_safe_step`` exactly like the needs_eyes side effect it
    replaces for this one message — approve-by-SMS is a convenience
    channel, never a path that can turn a webhook 200 into a 500. Anything
    else (an unrecognized token, or nothing to correlate against) falls
    back to the EXISTING ``_ensure_needs_eyes_notification`` side effect,
    unchanged from before #122.

    Raises
    ------
    AppError
        500 ``tenant_emergency_artifact_failed`` if
        ``_ensure_tenant_emergency_artifacts`` itself raises (safety
        review, 2026-07-12, finding 2) — see module docstring "Transaction
        design" point 2's exception. Never raised for the landlord/
        ``needs_eyes``/approve-by-SMS side effects, which stay fail-open
        via ``_safe_step``.
    """
    if party == "landlord":
        if (
            parsed_reply is not None
            and parsed_reply.command is not None
            and parsed_reply.case_id is not None
            and parsed_reply.draft_id is not None
        ):
            await _safe_step(
                "landlord_approve_by_sms",
                approve_by_sms.handle_reply(landlord_id=landlord_id, parsed=parsed_reply),
            )
            return

        await _safe_step(
            "landlord_needs_eyes_notification",
            _ensure_needs_eyes_notification(
                landlord_id=landlord_id,
                property_id=property_id,
                message_id=message_id,
                prefilter_result=prefilter_result,
            ),
        )
        return

    if prefilter_result.hard_hit:
        try:
            created = await _ensure_tenant_emergency_artifacts(
                landlord_id=landlord_id,
                property_id=property_id,
                message_id=message_id,
                prefilter_result=prefilter_result,
            )
        except Exception as exc:
            # Safety review, 2026-07-12 (finding 2, BLOCKING): a fresh
            # Tier-0 HARD delivery whose artifact creation fails must NOT
            # 200 -- that forecloses Twilio's own retry, which is the ONLY
            # recovery mechanism for a message whose escalation artifacts
            # don't durably exist yet. Mirrors the conflict-path recovery
            # failure below (module docstring point 4) -- same log event
            # name and Sentry message _safe_step would have used, so
            # existing ops alerting on those strings keeps working; the
            # only change is that this one no longer swallows.
            log.error(
                "twilio_sms_post_persist_stage_failed",
                stage="tenant_emergency_artifacts",
                exc_type=type(exc).__name__,
            )
            sentry_sdk.capture_message(
                "Twilio inbound webhook: post-persist side effect failed",
                level="error",
                extras={"stage": "tenant_emergency_artifacts", "exc_type": type(exc).__name__},
            )
            raise AppError(
                status_code=500,
                code="tenant_emergency_artifact_failed",
                message="Temporary delivery failure -- please retry.",
            ) from exc

        # Consolidated review item 3: alert ONLY when this call actually
        # created the escalation -- never on a redelivery that found it
        # already created (bounds alert volume under a replay storm).
        if created:
            _alert_tenant_hard_fire(
                message_id=message_id,
                property_id=property_id,
                categories=prefilter_result.categories,
            )

    # Background graph invocation (AC #4) — scheduled to run AFTER this
    # response is sent. enqueue_classification is itself idempotent (#30's
    # own message_received dedupe check), so scheduling it again on a
    # recovered/duplicate delivery is always safe.
    background_tasks.add_task(enqueue_classification, message_id, landlord_id)


# ---------------------------------------------------------------------------
# POST /webhooks/twilio/sms (#40)
# ---------------------------------------------------------------------------


@router.post("/sms")
async def twilio_sms_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_admin_session)],
) -> Response:
    """POST /webhooks/twilio/sms — the front door (#40).

    See the module docstring for the full transaction design. Order:

    1. Verify signature (403 if invalid/missing) — before any DB access.
    2. Extract + validate the minimal required Twilio fields (400 if
       missing) — still before any DB access. Both (1) and (2) are safe
       for Twilio to retry: nothing has been stored yet.
    3. Resolve the property owning the ``To`` number. No match → 200,
       loud metadata-only alert (contract addition — see module docstring
       and the PR description for why this can't be a 4xx/5xx: there's no
       ``landlord_id``/``property_id`` to satisfy ``messages``' NOT NULL
       columns, and nothing actionable follows from a number we don't
       recognize).
    4. Tier-0 (``app.agent.prefilter.check``) on the raw body BEFORE the
       routing split (landlord-channel vs tenant-channel) — contract
       fidelity to "Tier-0 runs on every inbound SMS before any routing
       split".
    5. Resolve routing, then ``INSERT ... ON CONFLICT (twilio_sid) DO
       NOTHING RETURNING id`` and COMMIT IMMEDIATELY — the row is durably
       on disk before anything else runs (module docstring point 1).
    6. No row back → duplicate/crash-recovery delivery: look up the
       EXISTING row's authoritative data instead of trusting this
       request's own (possibly redundant) computation. Any failure
       recovering that data → 5xx, NOT 200 (module docstring point 4) —
       the message is already safely stored, so Twilio's retry is the
       recovery mechanism, not something to foreclose.
    7. Run the shared, idempotent, isolated post-persist side effects
       (module docstring points 2/3) — safe to run exactly once or many
       times, even concurrently, for the same message.
    """
    params = await _extract_and_verify(request)

    message_sid = params.get("MessageSid")
    from_number = params.get("From")
    to_number = params.get("To")
    body = params.get("Body")

    if not message_sid or not from_number or not to_number or body is None:
        raise AppError(
            status_code=400,
            code="malformed_webhook",
            message="Missing required Twilio fields.",
        )

    property_row = (
        (await session.execute(_SELECT_PROPERTY_BY_TO_SQL, {"to_number": to_number}))
        .mappings()
        .one_or_none()
    )

    if property_row is None:
        _alert_unknown_to(to_digest=_digest(to_number), twilio_sid=message_sid)
        return _twiml_empty()

    property_id: UUID = property_row["id"]
    landlord_id: UUID = property_row["landlord_id"]

    # Tier-0 BEFORE the routing split (contract fidelity, consolidated
    # review item 6): a pure, sub-millisecond function on the raw body,
    # independent of who the routing predicate decides the sender is.
    prefilter_result: PrefilterResult = prefilter.check(body)

    is_landlord_channel = await _is_landlord_command_channel(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        from_number=from_number,
    )

    party: str
    tenant_id: UUID | None
    case_id: UUID | None = None
    parsed_reply: approve_by_sms.ParsedReply | None = None
    if is_landlord_channel:
        party = "landlord"
        tenant_id = None
        # #122 — resolve BEFORE the INSERT: a landlord row's case_id (if
        # any) must be set at insert time (messages is append-only, never
        # backfillable) — see app.agent.approve_by_sms's own module
        # docstring "Two-phase design".
        parsed_reply = await approve_by_sms.resolve_reply(
            session, landlord_id=landlord_id, property_id=property_id, body=body
        )
        if parsed_reply.command is not None and parsed_reply.case_id is not None:
            case_id = parsed_reply.case_id
    else:
        party = "tenant"
        tenant_id = await _lookup_active_tenant(session, property_id=property_id, phone=from_number)

    inserted = (
        (
            await session.execute(
                _INSERT_MESSAGE_SQL,
                {
                    "landlord_id": str(landlord_id),
                    "property_id": str(property_id),
                    "tenant_id": str(tenant_id) if tenant_id is not None else None,
                    "case_id": str(case_id) if case_id is not None else None,
                    "party": party,
                    "body": body,
                    "twilio_sid": message_sid,
                    "prefilter": prefilter_result.model_dump_json(),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    # Transaction design point 1: commit THIS transaction immediately,
    # whether the row was newly inserted or the INSERT no-op'd on
    # conflict. Nothing that runs after this line can ever roll back the
    # message row, because nothing after this line shares this
    # transaction/session for it.
    await session.commit()

    if inserted is not None:
        message_id: UUID = inserted["id"]
    else:
        # Conflict: this MessageSid is already persisted (a genuine
        # duplicate delivery, OR a crash-recovery retry after an earlier
        # delivery committed the row but died before its side effects ran
        # — see module docstring point 3). Recover the AUTHORITATIVE
        # persisted data rather than trusting this request's own
        # (possibly redundant) computation above.
        #
        # Consolidated review item 2 (BLOCKING): any failure recovering
        # that data -- a DB error, a structurally-unexpected missing row
        # (``.one()`` raises ``NoResultFound``), or a malformed/
        # unparseable stored ``prefilter`` snapshot (item 5, folded into
        # the SAME try/except rather than validated afterward) -- must
        # NOT be swallowed into a 200. The message row is ALREADY durably
        # stored (from an earlier delivery); THIS request merely failed to
        # complete the recovery, and a 5xx tells Twilio to retry, which is
        # exactly the recovery mechanism. Returning 200 here (an earlier
        # revision did) would foreclose that retry and could leave a
        # HARD-hit message's artifacts permanently missing. Rule #5 is
        # kept in this error path too: AppError's message is a static
        # string, never interpolated with DB/exception content.
        try:
            existing = (
                (await session.execute(_SELECT_MESSAGE_FOR_RECOVERY_SQL, {"sid": message_sid}))
                .mappings()
                .one()
            )
            await session.commit()

            message_id = existing["id"]
            landlord_id = existing["landlord_id"]
            property_id = existing["property_id"]
            party = existing["party"]
            case_id = existing["case_id"]
            # tenant_id itself is not needed past this point (not a
            # parameter of _run_post_persist_side_effects) -- the recovery
            # SELECT still fetches it for completeness/debuggability,
            # deliberately unused.
            prefilter_result = PrefilterResult.model_validate(existing["prefilter"])
            # #122 — re-derive the referenced draft_id, scoped to the
            # ALREADY-DURABLY-STORED case_id (never re-resolved from
            # scratch) — see app.agent.approve_by_sms.
            # resolve_reply_for_recovered_case's own docstring.
            parsed_reply = (
                await approve_by_sms.resolve_reply_for_recovered_case(
                    session, case_id=case_id, body=body
                )
                if party == "landlord"
                else None
            )
        except Exception as exc:
            log.error("twilio_sms_conflict_recovery_failed", exc_type=type(exc).__name__)
            raise AppError(
                status_code=500,
                code="recovery_failed",
                message="Temporary delivery failure -- please retry.",
            ) from exc

    await _run_post_persist_side_effects(
        background_tasks,
        landlord_id=landlord_id,
        property_id=property_id,
        message_id=message_id,
        party=party,
        prefilter_result=prefilter_result,
        parsed_reply=parsed_reply,
    )

    return _twiml_empty()


# ---------------------------------------------------------------------------
# POST /webhooks/twilio/status (#152)
# ---------------------------------------------------------------------------

_VALID_STATUS_EVENTS = frozenset(
    {"accepted", "queued", "sending", "sent", "delivered", "undelivered", "failed"}
)

# Consolidated review item 7: bound storage under a replay storm while
# still respecting "every callback is a fact" for legitimate delivery
# flows (a message realistically sees at most a handful of status
# transitions; 100 is generous headroom, not a realistic legitimate
# count). Documented in api-contracts.md alongside the endpoint.
_MAX_STATUS_EVENTS_PER_MESSAGE = 100

_SELECT_MESSAGE_BY_SID_SQL = text("SELECT id FROM messages WHERE twilio_sid = :sid")

_COUNT_STATUS_EVENTS_SQL = text(
    "SELECT COUNT(*) FROM message_status_events WHERE message_id = :message_id"
)

_INSERT_STATUS_EVENT_SQL = text(
    "INSERT INTO message_status_events (message_id, status, error_code, payload) "
    "VALUES (:message_id, :status, :error_code, CAST(:payload AS jsonb))"
)

# Fields safe to persist verbatim in message_status_events.payload — never
# From/To/Body (rule #5: no phone numbers/message bodies), even though this
# is a DB write rather than a log line; keeping the allowlist narrow means
# nobody has to re-audit this call site later if Twilio adds new fields.
_STATUS_CALLBACK_PAYLOAD_KEYS = frozenset(
    {"ErrorCode", "ErrorMessage", "MessageStatus", "SmsStatus", "ApiVersion"}
)


@router.post("/status")
async def twilio_status_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_admin_session)],
) -> Response:
    """POST /webhooks/twilio/status — delivery-status callback (#152).

    Signature-verified exactly like ``/sms`` (403 if invalid/missing,
    before any DB access). From there on, ALWAYS 200 fast
    (api-contracts.md): the ENTIRE body below is wrapped in one
    try/except (consolidated review item 2 — an earlier revision left the
    ``twilio_sid`` lookup unwrapped, so a transient DB blip surfaced as an
    unintended 500, itself a contract violation since a non-2xx makes
    Twilio retry-storm a callback that will never resolve differently). A
    missing/out-of-vocabulary status or an unknown ``twilio_sid``
    (including the outbound-insert race — the row genuinely may not exist
    yet) all drop with a metadata-only log line, never a 4xx/5xx — unlike
    ``/sms``'s malformed-field case, retrying a delivery-status callback
    that we can't make sense of would never succeed, so there is nothing
    to gain from letting Twilio retry it.

    Every recognized callback is appended as a FACT to
    ``message_status_events`` — duplicates and out-of-order arrivals are
    both expected and are never de-duplicated or upserted (the table is
    append-only and deliberately has no UNIQUE constraint), UP TO a
    per-message cap (``_MAX_STATUS_EVENTS_PER_MESSAGE`` — consolidated
    review item 7) that bounds storage under a replay storm without
    affecting any legitimate delivery flow. Deriving delivery state by
    status precedence (terminal wins) is a read-side concern for future
    queue/case reads, not this endpoint's job.
    """
    params = await _extract_and_verify(request)

    message_sid = params.get("MessageSid") or params.get("SmsSid")
    status = params.get("MessageStatus") or params.get("SmsStatus")

    if not message_sid or not status:
        log.info("twilio_status_malformed_callback")
        return _twiml_empty()

    if status not in _VALID_STATUS_EVENTS:
        log.info("twilio_status_out_of_vocabulary", status=status)
        return _twiml_empty()

    try:
        message_row = (
            (await session.execute(_SELECT_MESSAGE_BY_SID_SQL, {"sid": message_sid}))
            .mappings()
            .one_or_none()
        )

        if message_row is None:
            # Unknown twilio_sid (including the outbound-insert race) —
            # 200 + drop with a metadata-only log. twilio_sid is an opaque
            # id, safe to log (rule #5 only forbids bodies/phone
            # numbers/signatures).
            log.info("twilio_status_unknown_sid", twilio_sid=message_sid)
            return _twiml_empty()

        message_id = message_row["id"]

        event_count = (
            await session.execute(_COUNT_STATUS_EVENTS_SQL, {"message_id": str(message_id)})
        ).scalar_one()
        if event_count >= _MAX_STATUS_EVENTS_PER_MESSAGE:
            log.warning(
                "twilio_status_replay_cap_exceeded",
                message_id=str(message_id),
                count=event_count,
            )
            return _twiml_empty()

        error_code = params.get("ErrorCode")
        event_payload = {k: v for k, v in params.items() if k in _STATUS_CALLBACK_PAYLOAD_KEYS}

        await session.execute(
            _INSERT_STATUS_EVENT_SQL,
            {
                "message_id": str(message_id),
                "status": status,
                "error_code": error_code,
                "payload": json.dumps(event_payload),
            },
        )
        await session.commit()
    except Exception as exc:
        log.error("twilio_status_processing_failed", exc_type=type(exc).__name__)

    return _twiml_empty()


# ---------------------------------------------------------------------------
# POST /webhooks/twilio/voice (#108) — TwiML callback for the emergency call
# ---------------------------------------------------------------------------


@router.post("/voice")
async def twilio_voice_webhook(request: Request) -> Response:
    """POST /webhooks/twilio/voice — TwiML callback for the emergency call
    (``Digits=1`` → acknowledge). See module docstring for the two request
    shapes this single endpoint answers.

    Deliberately takes no ``session`` dependency of its own: every DB
    access this handler needs goes through
    ``app.agent.emergency_chain``'s own admin-session helpers
    (``acknowledge_notification`` / the context lookup baked into
    rendering the initial TwiML) — this file stays allowlisted for
    ``get_admin_session`` via its OWN direct dependency on ``/sms``/
    ``/status`` above, not because of anything this handler does directly.

    Always 200 with valid TwiML — never a bare error status. A missing or
    malformed ``notification_id`` query parameter (should never happen: it
    is generated by our own ``emergency_chain.render_voice_action_url``,
    never client-supplied) is a loud, metadata-only log line plus a
    generic spoken apology, never a 4xx/5xx (Twilio has no useful retry
    story for a voice callback — unlike ``/sms``, retrying would just
    replay the exact same malformed URL).
    """
    params = await _extract_and_verify(request)

    notification_id_raw = request.query_params.get("notification_id")
    if not notification_id_raw:
        log.error("twilio_voice_missing_notification_id")
        return _twiml_response(emergency_chain.build_error_twiml())

    try:
        notification_id = UUID(notification_id_raw)
    except ValueError:
        log.error("twilio_voice_malformed_notification_id")
        return _twiml_response(emergency_chain.build_error_twiml())

    digits = params.get("Digits")
    if digits == "1":
        await emergency_chain.acknowledge_notification(
            notification_id, actor="system", channel="voice_keypress"
        )
        return _twiml_response(emergency_chain.build_ack_confirmation_twiml())

    if digits is not None:
        # A digit was gathered but it wasn't "1" — no acknowledgment; the
        # chain's own schedule (not a retry within this same call) tries
        # again later.
        return _twiml_response(emergency_chain.build_error_twiml())

    # Initial TwiML fetch (no Digits yet) — render the spoken summary +
    # Gather. Context comes straight off the emergency_call row's own
    # durable state (message_id/property_id/categories), never re-derived
    # from this request.
    async with _isolated_session() as session:
        row = (
            (
                await session.execute(
                    _SELECT_NOTIFICATION_FOR_VOICE_SQL, {"id": str(notification_id)}
                )
            )
            .mappings()
            .one_or_none()
        )

    if row is None:
        log.error("twilio_voice_unknown_notification_id")
        return _twiml_response(emergency_chain.build_error_twiml())

    payload = row["payload"] or {}
    categories = list(payload.get("categories") or [])
    property_label = row["property_label"] or "the property"
    primary_category = emergency_chain.choose_primary_category(categories)

    twiml = emergency_chain.build_voice_twiml(
        property_label=property_label,
        category_label=emergency_chain.category_short_label(primary_category),
        action_url=emergency_chain.render_voice_action_url(notification_id),
    )
    return _twiml_response(twiml)


# ---------------------------------------------------------------------------
# Registered-path constants — read by app/property_provisioning.py (#53
# safety review, finding L3) to configure a newly-purchased number's inbound
# webhooks. Derived from the ACTUAL registered route table below (never a
# hand-duplicated literal), so a future rename of either endpoint above is
# structurally impossible to silently drift out of sync with what a
# freshly-provisioned number gets pointed at — this module is executed
# top-to-bottom exactly once at import, so every route above is already
# registered on ``router.routes`` by the time this runs.
# ---------------------------------------------------------------------------


def _registered_path(endpoint_name: str) -> str:
    """Return the full path (this router's own ``/webhooks/twilio`` prefix
    already baked in by FastAPI's ``add_api_route``) for the endpoint
    function named *endpoint_name*."""
    for route in router.routes:
        if isinstance(route, APIRoute) and route.name == endpoint_name:
            return route.path
    raise RuntimeError(f"no registered route named {endpoint_name!r}")  # pragma: no cover


SMS_WEBHOOK_PATH = _registered_path("twilio_sms_webhook")
VOICE_WEBHOOK_PATH = _registered_path("twilio_voice_webhook")
