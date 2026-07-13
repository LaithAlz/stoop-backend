"""``SmsSender`` — the injectable seam ``app/agent/draft_sender.py``'s
ticker depends on. This is the SECOND of the two sanctioned outbound-send
call sites (``apps/api/CLAUDE.md``: "Send to tenant/vendor happens only
through the draft flow or the emergency safety path").

Real binding (#108 integration, landed)
----------------------------------------
Issue #108 (``feat/emergency-executor``, merged as PR #196) built
``app/integrations/twilio_send.py`` — the real Twilio client
(``TwilioSender``/``TwilioRestSender``, an ``AsyncTwilioHttpClient`` with a
10s timeout) and its sole prior caller, the emergency escalation chain.
This module stays a SEPARATE file on purpose (draft_sender.py must never
import ``app.integrations.twilio_send`` directly, and nothing outside
``twilio_send.py`` may construct a real Twilio REST client at all — see
``tests/test_twilio_send_allowlist.py``): :class:`TwilioBackedSmsSender`
below is a thin adapter that delegates every real send to
``twilio_send.py``'s own ``get_twilio_sender()`` singleton — the SAME
client/timeout/credentials the emergency safety path uses, never a second
Twilio client stack. ``app/integrations/sms_sender.py`` is now itself
allowlisted in ``tests/test_twilio_send_allowlist.py`` as the sanctioned
draft-flow call site (the second of exactly two).

Why ``from_e164`` is required
------------------------------
Each ``properties`` row owns its own ``twilio_number`` (nullable until
provisioned, schema-v1.md) — inbound webhook routing keys strictly off the
Twilio "To" number to resolve which property a message belongs to
(``app/routers/webhooks/twilio.py``). A reply must go out from that SAME
number or a tenant's next inbound message would arrive at (or appear to
come from) the wrong property. ``app/agent/draft_sender.py`` resolves the
case's property's ``twilio_number`` and passes it through as
``from_e164`` — mirrors ``app/agent/emergency_chain.py``'s own
``ctx.twilio_number`` convention exactly. A property with no
``twilio_number`` yet provisioned is refused the same way
``emergency_chain.py`` refuses one (``reason="no_twilio_number"``) —
never a send from some other property's number, and never a fabricated
placeholder number.

The deployment-gating pattern this module used to rely on
------------------------------------------------------------------------
Before this integration, :func:`get_default_sms_sender` returned ``None``
and ``app/agent/draft_sender.py``'s ``run_sender_loop`` treated that as
"the worker is disabled." That gate is no longer needed for the real
binding below (``twilio_account_sid``/``twilio_auth_token`` are required,
non-optional settings — see ``app/config.py`` — so constructing a real
sender never depends on an absent credential the way it hypothetically
could have). :class:`NotImplementedSmsSender` is kept, unused, as a
documented, loudly-failing placeholder contract for any FUTURE binding
that bypasses :func:`get_default_sms_sender` by mistake — never
constructed by this module today.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from app.integrations.twilio_send import get_twilio_sender

log = structlog.get_logger(__name__)


class SmsSender(Protocol):
    """The one interface ``app/agent/draft_sender.py`` depends on. Returns
    the provider's message sid (a plain string — never assumed to be a
    Twilio-specific shape) on success; raises on failure (the ticker
    itself decides what "failure" means for its own claim/retry bookkeeping
    — see that module's docstring). ``from_e164`` is the sending
    property's own ``twilio_number`` (see module docstring "Why
    ``from_e164`` is required") — never a landlord- or account-wide
    default number."""

    async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str: ...


class NotImplementedSmsSender:
    """Documented, loudly-failing placeholder — see module docstring "The
    deployment-gating pattern this module used to rely on". Never
    constructed by :func:`get_default_sms_sender`; exists only so a future
    caller that bypasses the sanctioned binding by mistake fails loudly
    instead of pretending to succeed."""

    async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str:
        raise NotImplementedError(
            "No real SmsSender is wired in yet -- get_default_sms_sender() "
            "(app/integrations/sms_sender.py) is the sanctioned way to obtain one."
        )


class TwilioBackedSmsSender:
    """The real production binding for :class:`SmsSender` (#108
    integration commit). Delegates every send to ``app.integrations.
    twilio_send``'s already-reviewed ``get_twilio_sender()`` singleton —
    never constructs its own real Twilio REST client or duplicates any
    HTTP setup (``tests/test_twilio_send_allowlist.py`` machine-enforces
    both the call-site allowlist and the direct-construction import ban).
    Holds no state of its own; cheap to construct per call."""

    async def send_sms(self, *, to_e164: str, from_e164: str, body: str) -> str:
        sender = get_twilio_sender()  # sanctioned draft-flow call site (allowlisted)
        return await sender.send_sms(to=to_e164, from_=from_e164, body=body)


def get_default_sms_sender() -> SmsSender:
    """Returns the real Twilio-backed :class:`SmsSender` binding (#108's
    integration commit) — ``app/scheduler.py``'s 60s ticker calls this
    once per tick before draining due drafts via ``app.agent.draft_sender.
    sender_tick``. Always returns a working instance: ``twilio_account_sid``
    /``twilio_auth_token`` are required (non-optional) settings, so there is
    no "unconfigured" state to gate on here the way there was before this
    binding existed.
    """
    return TwilioBackedSmsSender()


__all__: list[str] = [
    "NotImplementedSmsSender",
    "SmsSender",
    "TwilioBackedSmsSender",
    "get_default_sms_sender",
]
