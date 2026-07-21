"""Expo Push API OUTBOUND send client (#210 M3).

Mirrors ``app/integrations/twilio_send.py``'s client pattern exactly —
Protocol seam, lazy singleton, injectable for tests — but this is a
SEPARATE, unrelated egress module: it is never subject to
``tests/test_twilio_send_allowlist.py`` (that allowlist governs Twilio
sending only, per CLAUDE.md's "only the draft flow and the emergency
safety path may call twilio send" rule), and it never carries the
emergency path — push is for approvals/status only
(``docs/03-engineering/schema-v1.md``'s v1.13 amendments; the escalation
chain in ``app/agent/emergency_chain.py`` stays voice+SMS only, untouched
by this module).

No Expo SDK dependency — raw ``httpx`` POST to
``https://exp.host/--/api/v2/push/send``, per issue instructions. Expo's
API accepts (and this module could batch) an array of messages per
request, but per this issue's explicit design this module sends ONE
Expo API call per outbox row (never batched) — simpler 1:1 outcome
mapping for the sweep's per-row CAS-claim loop
(``app/push_outbox.py::run_push_outbox_sweep``); batching many rows into
one Expo request is a plausible follow-up, not done here.

Never-break rule #5
--------------------
An Expo push token is credential-adjacent (identifies a specific device
install) — never logged. Every log line in this module and its caller
carries only uuids (``push_outbox.id``/``device_token_id``) and Expo's
own status/error-code strings, never the raw token.

Injectable / fakeable (never a live send in tests)
--------------------------------------------------
:class:`ExpoPushSender` is a ``Protocol`` — ``ExpoHttpPushSender`` is the
ONLY real implementation (constructed lazily, never at import time).
Tests call :func:`set_expo_push_sender_for_tests` to inject a fake, or use
``respx`` directly against the real sender for HTTP-shape-level coverage
— mirrors ``app/integrations/twilio_send.py``'s
``set_twilio_sender_for_tests``/``app/integrations/anthropic.py``'s
``reset_client_for_tests`` pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

_HTTP_TIMEOUT_SECONDS: float = 5.0
"""Tighter than ``app/integrations/twilio_send.py``'s own 10s outbound
timeout — defense-in-depth (safety review HIGH-1) alongside
``app/push_outbox.py``'s own wall-clock tick deadline: a hung Expo
request must never stall the push sweep's whole tick, and a shorter
per-call timeout means more of the shared batch gets a real attempt
before that deadline is reached. Halving it costs nothing here (unlike
the emergency chain, this sender is never on a critical single-attempt
path — a timed-out attempt just retries on the next tick)."""

DEVICE_NOT_REGISTERED_ERROR_CODE: str = "DeviceNotRegistered"
"""Expo's per-receipt error code for a permanently dead token (app
uninstalled, or the OS revoked it) — see
``app/push_outbox.py``'s DeviceNotRegistered handling."""


@dataclass(frozen=True)
class ExpoPushMessage:
    """One Expo push message — ``to`` is the raw Expo push token
    (``ExponentPushToken[...]``), never logged (see module docstring).
    ``title``/``body`` are FIXED, generic, landlord-facing copy (never
    derived from a tenant message/name — schema-v1.md's v1.13 amendments);
    ``data`` carries uuids only (``case_id``/``draft_id``), never PII."""

    to: str
    title: str
    body: str
    data: dict[str, str]


@dataclass(frozen=True)
class ExpoPushTicket:
    """One Expo push receipt — ``status`` is ``"ok"`` or ``"error"``.
    ``error_code`` is populated only when ``status == "error"`` and Expo's
    response carried a ``details.error`` value (e.g.
    :data:`DEVICE_NOT_REGISTERED_ERROR_CODE`); ``None`` for a successful
    send or an error Expo didn't classify."""

    status: str
    error_code: str | None = None
    message: str | None = None


class ExpoPushSender(Protocol):
    """Injectable seam for outbound Expo push sends — see module
    docstring."""

    async def send_push(self, message: ExpoPushMessage) -> ExpoPushTicket:
        """Send one push message. Never raises for an Expo-reported
        per-receipt error (returns an ``ExpoPushTicket`` with
        ``status="error"`` instead) — only a genuine transport failure
        (timeout, connection error, malformed response) raises, which the
        caller (``app/push_outbox.py``) treats as a transient send
        failure, same as an error ticket with no recognized error code."""
        ...


class ExpoHttpPushSender:
    """The ONLY class in this codebase that ever calls Expo's real push
    API. Raw ``httpx``, no Expo SDK — see module docstring."""

    async def send_push(self, message: ExpoPushMessage) -> ExpoPushTicket:
        body: dict[str, Any] = {
            "to": message.to,
            "title": message.title,
            "body": message.body,
            "data": message.data,
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(_EXPO_PUSH_URL, json=body)
            response.raise_for_status()
            parsed: dict[str, Any] = response.json()

        # Expo's response shape for a single-message request is
        # {"data": {"status": "ok"|"error", "id"?: str, "message"?: str,
        # "details"?: {"error": "DeviceNotRegistered", ...}}} -- a dict,
        # not a list, when exactly one message was posted (Expo's array
        # form is only for a batched multi-message request, which this
        # module never sends -- see module docstring).
        ticket_data = parsed.get("data")
        if not isinstance(ticket_data, dict):
            raise ExpoPushResponseError("Expo push response missing a usable 'data' object")

        status = ticket_data.get("status")
        if not isinstance(status, str):
            raise ExpoPushResponseError("Expo push response missing a usable 'status' field")

        details = ticket_data.get("details")
        error_code = None
        if isinstance(details, dict):
            raw_error_code = details.get("error")
            error_code = raw_error_code if isinstance(raw_error_code, str) else None

        raw_message = ticket_data.get("message")
        return ExpoPushTicket(
            status=status,
            error_code=error_code,
            message=raw_message if isinstance(raw_message, str) else None,
        )


class ExpoPushResponseError(Exception):
    """Raised when Expo's HTTP response is malformed/unexpected — treated
    as a transient send failure by ``app/push_outbox.py`` (same as an
    ``httpx`` timeout/connection error), never a crash."""


_sender: ExpoPushSender | None = None


def get_expo_push_sender() -> ExpoPushSender:
    """Return the process-wide :class:`ExpoPushSender`, created lazily —
    mirrors ``app/integrations/twilio_send.py::get_twilio_sender``."""
    global _sender
    if _sender is None:
        _sender = ExpoHttpPushSender()
    return _sender


def set_expo_push_sender_for_tests(sender: ExpoPushSender | None) -> None:
    """Test-only seam: inject a fake sender, or pass ``None`` to drop the
    cached instance — mirrors
    ``app/integrations/twilio_send.py::set_twilio_sender_for_tests``."""
    global _sender
    _sender = sender


__all__: list[str] = [
    "DEVICE_NOT_REGISTERED_ERROR_CODE",
    "ExpoHttpPushSender",
    "ExpoPushMessage",
    "ExpoPushResponseError",
    "ExpoPushSender",
    "ExpoPushTicket",
    "get_expo_push_sender",
    "set_expo_push_sender_for_tests",
]
