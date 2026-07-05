"""Emergency-protocol EXECUTION seam (#40 scope boundary → #108 fills the body).

A Tier-0 HARD hit (``app/agent/prefilter.py``) on a TENANT message must
leave a durable, queryable trail even though the real voice call / safety
SMS to the tenant / escalation chain do not exist yet (#108, tracked
separately — see ``docs/02-product/emergency-prefilter.md``, "The
escalation chain"). The durable artifacts themselves are created by the
CALLER (``app/routers/webhooks/twilio.py``), in the same request, before it
ever reaches this module:

1. an ``audit_log`` row (``actor='prefilter'``, ``action='emergency_
   triggered'``, payload = rules fired — never the message body), and
2. a ``notifications`` row (``type='emergency_call'``, ``status=
   'pending'``) — the durable record of intent #108's sweeper will poll
   for and act on.

This module exists so there is exactly ONE call site
(``app/routers/webhooks/twilio.py``) #108 needs to touch to wire in real
execution, instead of that logic being inlined into the webhook handler
itself. TODAY ``fire_emergency_protocol`` does nothing but log that the
protocol was requested — every argument is an identifier or a short list
of Tier-0 category names (e.g. ``["fire"]``), never a phone number or
message body (never-break rule #5). No DB access happens here (the caller
already wrote the durable rows); no feature flags are read here either
(project rule: flags never gate safety behavior, and this module lives in
``agent/`` where flag reads are disallowed outright).
"""

from __future__ import annotations

from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


async def fire_emergency_protocol(
    *,
    notification_id: UUID,
    message_id: UUID,
    property_id: UUID,
    categories: list[str],
) -> None:
    """Emergency-protocol execution seam — records intent only (see module docstring).

    Parameters are identifiers/category names only:

    - ``notification_id`` — the ``notifications`` row the caller already
      inserted with ``status='pending'``; this IS the durable artifact
      #108's sweeper acts on, not something this function needs to create
      or mutate.
    - ``message_id`` / ``property_id`` — correlation ids for the log line
      only.
    - ``categories`` — the Tier-0 HARD trigger categories that fired
      (e.g. ``["fire", "gas_co"]``); safe to log, they carry no PII.

    #108 replaces this body with the actual Twilio voice call to the
    landlord, the safety SMS to the tenant, and the T+2m/+5m/+10m/+15m/...
    escalation chain. Until then this is intentionally a no-op beyond the
    log line below — never silent, and the artifacts that actually matter
    (the ``audit_log``/``notifications`` rows) are already durable by the
    time this is called.
    """
    log.info(
        "emergency_protocol_seam_invoked",
        notification_id=str(notification_id),
        message_id=str(message_id),
        property_id=str(property_id),
        categories=categories,
    )
