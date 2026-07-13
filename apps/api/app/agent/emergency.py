"""Emergency-protocol EXECUTION seam (#40 scope boundary → #108 fills the body).

A Tier-0 HARD hit (``app/agent/prefilter.py``) on a TENANT message must
leave a durable, queryable trail even before the real voice call / safety
SMS / escalation chain runs. The durable artifacts themselves are created
by the CALLER (``app/routers/webhooks/twilio.py``), in the same request,
before it ever reaches this module:

1. an ``audit_log`` row (``actor='prefilter'``, ``action='emergency_
   triggered'``, payload = rules fired — never the message body), and
2. a ``notifications`` row (``type='emergency_call'``, ``status=
   'pending'``) — the durable record of intent this module now acts on.

This module exists so there is exactly ONE call site
(``app/routers/webhooks/twilio.py``) needs to touch to wire in real
execution, instead of that logic being inlined into the webhook handler
itself — unchanged from before #108. ``fire_emergency_protocol``'s
signature is DELIBERATELY UNCHANGED (the campaign's "do not re-plumb the
webhook" instruction): every argument remains an identifier or a short list
of Tier-0 category names (e.g. ``["fire"]``), never a phone number or
message body (never-break rule #5). No feature flags are read here either
(project rule: flags never gate safety behavior, and this module lives in
``agent/`` where flag reads are disallowed outright).

#108 — real execution (this revision)
--------------------------------------
The real work — the T+0 tenant safety SMS, the T+0 landlord voice call, and
the T+2m/+5m/+10m/+15m/+20m... escalation chain — lives in
``app/agent/emergency_chain.py`` (a sibling module, not inlined here, so
the chain's substantial state-machine/template/Twilio-client logic doesn't
bloat this seam's job of "exactly one call site"). This function's only
job is to invoke that module's :func:`~app.agent.emergency_chain.handle_emergency_trigger`
and guarantee the invariant this seam has always had: a failure HERE must
never surface as a 5xx on the Twilio SMS webhook (the durable
``notifications``/``audit_log`` rows the caller already wrote are
untouched by this failing) and must never be silent (logged AND paged to
Sentry, metadata only — rule #5). A crash or exception here is fully
recovered by ``app/scheduler.py``'s 60-second sweep tick, since
``emergency_chain.handle_emergency_trigger`` durably marks the chain "due
now" BEFORE attempting anything — see that module's own docstring "The
instant + durable sweep hybrid".

No DB access happens directly in THIS function (it delegates entirely to
``emergency_chain``, which does its own DB access on the admin engine —
allowlisted in ``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``
under that module's own name, not this one).
"""

from __future__ import annotations

from uuid import UUID

import sentry_sdk
import structlog

from app.agent.emergency_chain import handle_emergency_trigger

log = structlog.get_logger(__name__)


async def fire_emergency_protocol(
    *,
    notification_id: UUID,
    message_id: UUID,
    property_id: UUID,
    categories: list[str],
) -> None:
    """Emergency-protocol execution seam — see module docstring.

    Parameters are identifiers/category names only:

    - ``notification_id`` — the ``notifications`` row the caller already
      inserted with ``status='pending'``; this IS the durable artifact the
      escalation chain is driven from.
    - ``message_id`` / ``property_id`` — correlation ids used to re-derive
      the rest of the chain's context (property/landlord/tenant contact
      info) fresh at every attempt — see ``emergency_chain.py``.
    - ``categories`` — the Tier-0 HARD trigger categories that fired
      (e.g. ``["fire", "gas_co"]``); safe to log, they carry no PII.

    Never raises: any failure is logged and paged to Sentry (metadata
    only, rule #5) so the Twilio SMS webhook this is called from always
    still returns its 200 promptly, regardless of a downstream Twilio/DB
    hiccup — the chain recovers on the next sweep tick regardless (see
    ``emergency_chain.py``).
    """
    log.info(
        "emergency_protocol_seam_invoked",
        notification_id=str(notification_id),
        message_id=str(message_id),
        property_id=str(property_id),
        categories=categories,
    )
    try:
        await handle_emergency_trigger(
            notification_id=notification_id,
            message_id=message_id,
            property_id=property_id,
            categories=categories,
        )
    except Exception as exc:
        log.error(
            "emergency_protocol_seam_failed",
            notification_id=str(notification_id),
            message_id=str(message_id),
            exc_type=type(exc).__name__,
        )
        sentry_sdk.capture_message(
            "fire_emergency_protocol: handle_emergency_trigger raised",
            level="error",
            extras={
                "notification_id": str(notification_id),
                "message_id": str(message_id),
                "exc_type": type(exc).__name__,
            },
        )


__all__: list[str] = ["fire_emergency_protocol"]
