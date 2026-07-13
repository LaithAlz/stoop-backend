"""Twilio phone-number PROVISIONING client — search / purchase / configure
webhooks / associate an A2P campaign / release (#53).

This is NOT the outbound send seam. ``app/integrations/twilio_send.py``'s
module docstring is explicit that exactly two code paths may ever place a
real call or send a real SMS (the emergency chain, and — once built — the
approve-flow draft sender); provisioning a NUMBER is a distinct capability
(account-level Twilio REST resources: ``AvailablePhoneNumbers``,
``IncomingPhoneNumbers``, ``Messaging/Services/.../PhoneNumbers``), never a
message/call send, so it gets its OWN seam here rather than extending that
module's sender-getter allowlist. ``tests/test_twilio_send_allowlist.py``
is therefore untouched by this module (note: that allowlist test also
greps every file's raw text for the SDK-client import string this section
describes banning, so this docstring deliberately never spells it out
verbatim either — see the next section).

Raw HTTP, never the Twilio SDK's REST client
----------------------------------------------
``tests/test_twilio_send_allowlist.py::
test_no_direct_twilio_rest_client_construction_outside_send_module`` bans
importing the Twilio Python SDK's REST-client module anywhere except
``twilio_send.py`` — this module honors that by talking to Twilio's REST
API directly over ``httpx`` (the same library ``app/integrations/
weather.py`` and ``app/integrations/supabase_auth.py`` already use for
their own outbound HTTP), never importing that SDK client at all.

Injectable / fakeable (never a live call in tests)
----------------------------------------------------
:class:`TwilioProvisioner` is a ``Protocol``; :class:`TwilioRestProvisioner`
is the only real implementation, constructed lazily (never at import time,
so importing this module never touches the network or requires real
credentials) — mirrors ``twilio_send.py``'s own sender-getter/test
-injection pattern exactly. There is no code path in this repository's
test suite that constructs a real ``TwilioRestProvisioner`` — every test
injects a fake via :func:`set_twilio_provisioner_for_tests`.

Never-break rule #5
--------------------
Every method here returns/logs only Twilio SIDs (opaque, safe to log/store)
or a caller-supplied phone number it was explicitly told to act on — no
method here ever LOGS a phone number itself; callers (``app/
property_provisioning.py``) are responsible for keeping their own logging
to uuids/SIDs only.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from app.config import settings

_HTTP_TIMEOUT_SECONDS: float = 10.0
"""Same bound as ``twilio_send.py``'s ``_HTTP_TIMEOUT_SECONDS`` — a real
Twilio REST call must never hang the request indefinitely."""

_API_BASE = "https://api.twilio.com/2010-04-01"
_MESSAGING_API_BASE = "https://messaging.twilio.com/v1"

_COUNTRY_CODE = "CA"
"""Stoop is a Canadian product (architecture.md: A2P 10DLC / CASL) —
every provisioned number is a Canadian local number. Not configurable via
settings/flags; this is a product fact, not a rollout knob."""


class TwilioProvisioner(Protocol):
    """Injectable seam for Twilio phone-number provisioning — see module
    docstring. Every method is a thin wrapper over ONE Twilio REST call;
    the search/purchase/release-on-failure ORCHESTRATION lives in
    ``app/property_provisioning.py``, not here."""

    async def search_available_numbers(
        self, *, area_code: str | None = None, region: str | None = None
    ) -> list[str]:
        """Return candidate E.164 Canadian local numbers matching the given
        filter (at most one of ``area_code``/``region`` is meaningfully set
        by a caller at a time, though nothing here enforces that). An empty
        list means "no inventory for this filter" — not an error; the
        caller decides whether/how to broaden the search."""
        ...

    async def purchase_number(self, *, phone_number: str) -> str:
        """Buy *phone_number* (as returned by ``search_available_numbers``).
        Returns the Twilio ``PhoneNumberSid`` (``PN...``)."""
        ...

    async def configure_webhooks(self, *, twilio_sid: str, sms_url: str, voice_url: str) -> None:
        """Point the purchased number's inbound SMS/voice webhooks at this
        deployment. Both set in the SAME Twilio call (Twilio's
        ``IncomingPhoneNumbers`` update resource accepts both at once)."""
        ...

    async def associate_messaging_service(
        self, *, twilio_sid: str, messaging_service_sid: str
    ) -> None:
        """Associate the number with an existing Twilio Messaging Service
        (the A2P 10DLC/CASL campaign association) — only ever called when
        ``settings.twilio_messaging_service_sid`` is configured; see
        ``app/property_provisioning.py``."""
        ...

    async def release_number(self, *, twilio_sid: str) -> None:
        """Release (delete) the number back to Twilio's pool — used both
        as post-failure compensation (a purchase that can't be fully
        provisioned) and as the deprovisioning grace-period's eventual
        action."""
        ...


class TwilioRestProvisioner:
    """The ONLY class in this codebase that calls Twilio's real number
    -provisioning REST API. See module docstring."""

    def __init__(self) -> None:
        self._account_sid = settings.twilio_account_sid
        self._auth = (settings.twilio_account_sid, settings.twilio_auth_token)

    def _client(self) -> httpx.AsyncClient:
        # Fresh client per call (mirrors weather.py/supabase_auth.py's own
        # per-fetch client, not twilio_send.py's persistent one) — this
        # module's calls are infrequent (property create/delete, not
        # per-message), so pooling overhead isn't worth the extra
        # lifecycle-management complexity.
        return httpx.AsyncClient(auth=self._auth, timeout=_HTTP_TIMEOUT_SECONDS)

    async def search_available_numbers(
        self, *, area_code: str | None = None, region: str | None = None
    ) -> list[str]:
        params: dict[str, str] = {"SmsEnabled": "true", "VoiceEnabled": "true", "PageSize": "5"}
        if area_code:
            params["AreaCode"] = area_code
        if region:
            params["InRegion"] = region

        url = (
            f"{_API_BASE}/Accounts/{self._account_sid}/AvailablePhoneNumbers/"
            f"{_COUNTRY_CODE}/Local.json"
        )
        async with self._client() as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        candidates = data.get("available_phone_numbers") or []
        return [
            c["phone_number"] for c in candidates if isinstance(c, dict) and c.get("phone_number")
        ]

    async def purchase_number(self, *, phone_number: str) -> str:
        url = f"{_API_BASE}/Accounts/{self._account_sid}/IncomingPhoneNumbers.json"
        async with self._client() as client:
            response = await client.post(url, data={"PhoneNumber": phone_number})
            response.raise_for_status()
            data = response.json()
        return str(data["sid"])

    async def configure_webhooks(self, *, twilio_sid: str, sms_url: str, voice_url: str) -> None:
        url = f"{_API_BASE}/Accounts/{self._account_sid}/IncomingPhoneNumbers/{twilio_sid}.json"
        body = {
            "SmsUrl": sms_url,
            "SmsMethod": "POST",
            "VoiceUrl": voice_url,
            "VoiceMethod": "POST",
        }
        async with self._client() as client:
            response = await client.post(url, data=body)
            response.raise_for_status()

    async def associate_messaging_service(
        self, *, twilio_sid: str, messaging_service_sid: str
    ) -> None:
        url = f"{_MESSAGING_API_BASE}/Services/{messaging_service_sid}/PhoneNumbers"
        async with self._client() as client:
            response = await client.post(url, data={"PhoneNumberSid": twilio_sid})
            response.raise_for_status()

    async def release_number(self, *, twilio_sid: str) -> None:
        url = f"{_API_BASE}/Accounts/{self._account_sid}/IncomingPhoneNumbers/{twilio_sid}.json"
        async with self._client() as client:
            response = await client.delete(url)
            response.raise_for_status()


_provisioner: TwilioProvisioner | None = None


def get_twilio_provisioner() -> TwilioProvisioner:
    """Return the process-wide :class:`TwilioProvisioner`, created lazily —
    mirrors ``twilio_send.py``'s own analogous sender-getter."""
    global _provisioner
    if _provisioner is None:
        _provisioner = TwilioRestProvisioner()
    return _provisioner


def set_twilio_provisioner_for_tests(provisioner: TwilioProvisioner | None) -> None:
    """Test-only seam: inject a fake, or pass ``None`` to drop the cached
    instance — mirrors ``set_twilio_sender_for_tests``."""
    global _provisioner
    _provisioner = provisioner


__all__: list[str] = [
    "TwilioProvisioner",
    "TwilioRestProvisioner",
    "get_twilio_provisioner",
    "set_twilio_provisioner_for_tests",
]
