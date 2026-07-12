"""Twilio OUTBOUND send client — voice calls + SMS (#108).

This is the SECOND sanctioned egress module in the codebase, and the
FIRST one actually built. Per ``apps/api/CLAUDE.md`` ("Send to tenant/vendor
happens only through the draft flow or the emergency safety path. There is
no other code path that calls ``twilio.send``. Keep it that way.") there are
exactly TWO code paths ever allowed to place a real outbound call or send a
real outbound SMS:

1. **The emergency safety path** (this module + ``app/agent/emergency_chain.py``,
   #108) — landlord/backup-contact voice calls and the tenant safety SMS/
   escalation texts. Built here.
2. **The approve-flow draft sender** (#44, NOT YET BUILT as of this module) —
   will get its own call site into whatever send module it introduces (SMS
   only; #44 does not need voice calls). It must never reach into THIS
   module's internals — if it ever needs SMS sending, it gets its own
   ``get_twilio_sender()``-shaped seam, reviewed on its own merits.

``app/integrations/twilio.py`` (verify-only: inbound webhook signature
checking) is a SEPARATE module on purpose — inbound verification has no
overlap with outbound sending, and keeping them apart means a future reader
grepping for "who can send" never has to first mentally filter out
signature-verification code.

Call-site discipline (grep-able, mirrors the admin-session allowlist
pattern)
--------------------------------------------------------------------------
``get_twilio_sender()`` is the ONLY way to obtain something that can place a
real call or send a real SMS. ``tests/test_twilio_send_allowlist.py``
machine-enforces an allowlist of exactly which files may reference it —
today just this module's own definition and its sole caller,
``app/agent/emergency_chain.py``. Extend that allowlist deliberately (never
loosen the grep) the day #44 needs its own SMS-sending seam.

Injectable / fakeable (never a live send in tests)
--------------------------------------------------------------------------
:class:`TwilioSender` is a ``Protocol`` — ``TwilioRestSender`` is the ONLY
real implementation (constructed lazily, never at import time, so merely
importing this module never touches the network or requires real Twilio
credentials). Tests call :func:`set_twilio_sender_for_tests` to inject a
fake that records calls and returns canned SIDs — mirrors
``app/integrations/anthropic.py``'s ``get_client()``/``reset_client_for_tests()``
pattern exactly. There is no code path in this repository's test suite that
constructs a real ``TwilioRestSender``.

Async, not blocking
--------------------
Both methods use the Twilio Python SDK's ``*_async`` resource methods with
an :class:`~twilio.http.async_http_client.AsyncTwilioHttpClient` — genuine
``aiohttp``-backed async I/O, never a blocking call on the event loop (no
``asyncio.to_thread`` shim needed).

Never-break rule #5
--------------------
Both methods return only the Twilio-assigned SID — an opaque identifier,
safe to log or store. Callers must never log ``to``/``from_``/``body`` —
see ``app/agent/emergency_chain.py``'s own logging discipline.
"""

from __future__ import annotations

from typing import Protocol

from twilio.http.async_http_client import AsyncTwilioHttpClient
from twilio.rest import Client

from app.config import settings


class TwilioSender(Protocol):
    """Injectable seam for outbound Twilio actions — see module docstring.

    Both methods return the Twilio-assigned SID (safe to log/store — an
    opaque identifier, never a phone number or message body).
    """

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        """Send one SMS. Returns the Twilio ``MessageSid``."""
        ...

    async def create_call(self, *, to: str, from_: str, twiml_url: str) -> str:
        """Place one voice call, fetching TwiML instructions from
        *twiml_url* (our own ``POST /webhooks/twilio/voice`` endpoint,
        parameterized with the notification id — see
        ``app/agent/emergency_chain.py::render_voice_action_url``). Returns
        the Twilio ``CallSid``."""
        ...


_HTTP_TIMEOUT_SECONDS: float = 10.0
"""Safety review, 2026-07-12 (finding 5, MEDIUM): the Twilio SDK's default
``AsyncTwilioHttpClient`` has NO timeout at all — one hung carrier/network
request would block indefinitely. The escalation-chain sweep processes
candidates SEQUENTIALLY (never-break: never fire concurrent external calls
for the same tick's bookkeeping), so an unbounded hang on ONE building's
call/SMS would stall EVERY other building's due escalation behind it for
as long as the hang lasts — this bound (10s, generous for a REST call) is
the gate that prevents that. Bounded-gather parallelism across candidates
was considered and left for later (optional per this finding) — the
timeout alone closes the "stalls forever" failure mode, which is the one
that actually matters for the zero-missed-emergency invariant."""


class TwilioRestSender:
    """The ONLY class in this codebase that ever calls Twilio's real
    send/call REST API. See module docstring "Call-site discipline"."""

    def __init__(self) -> None:
        self._client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            http_client=AsyncTwilioHttpClient(timeout=_HTTP_TIMEOUT_SECONDS),
        )

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        message = await self._client.messages.create_async(to=to, from_=from_, body=body)
        return str(message.sid)

    async def create_call(self, *, to: str, from_: str, twiml_url: str) -> str:
        call = await self._client.calls.create_async(to=to, from_=from_, url=twiml_url)
        return str(call.sid)


_sender: TwilioSender | None = None


def get_twilio_sender() -> TwilioSender:
    """Return the process-wide :class:`TwilioSender`, created lazily.

    A single sender is reused across calls (connection pooling via the
    underlying ``aiohttp`` session); tests call
    :func:`set_twilio_sender_for_tests` to substitute a fake rather than
    mutating this module-level singleton in place — mirrors
    ``app/integrations/anthropic.py::get_client``.
    """
    global _sender
    if _sender is None:
        _sender = TwilioRestSender()
    return _sender


def set_twilio_sender_for_tests(sender: TwilioSender | None) -> None:
    """Test-only seam: inject a fake sender, or pass ``None`` to drop the
    cached instance (forces lazy re-construction of a real
    ``TwilioRestSender`` on the next :func:`get_twilio_sender` call) —
    mirrors ``app/integrations/anthropic.py::reset_client_for_tests``."""
    global _sender
    _sender = sender


__all__: list[str] = [
    "TwilioRestSender",
    "TwilioSender",
    "get_twilio_sender",
    "set_twilio_sender_for_tests",
]
