"""Unit tests for app/integrations/twilio_provision.py (#53).

Markers: ``@pytest.mark.unit`` — mocked httpx via ``respx``; NO real network
access, EVER (never-break: a real number purchase costs real money). Each
test exercises ``TwilioRestProvisioner`` directly against a mocked Twilio
REST endpoint, verifying the exact URL/method/body shape this module sends
— the search/purchase/compensation ORCHESTRATION (cascade fallback,
release-on-failure) is covered separately in
``tests/test_property_provisioning.py`` against a fake, non-HTTP
``TwilioProvisioner``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from app.integrations.twilio_provision import TwilioRestProvisioner

_ACCOUNT_SID = settings.twilio_account_sid
_API_BASE = f"https://api.twilio.com/2010-04-01/Accounts/{_ACCOUNT_SID}"
_MESSAGING_BASE = "https://messaging.twilio.com/v1"


@pytest.mark.unit
async def test_search_available_numbers_returns_phone_numbers() -> None:
    with respx.mock() as mock:
        route = mock.get(f"{_API_BASE}/AvailablePhoneNumbers/CA/Local.json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "available_phone_numbers": [
                        {"phone_number": "+14165551111"},
                        {"phone_number": "+14165552222"},
                    ]
                },
            )
        )
        result = await TwilioRestProvisioner().search_available_numbers(area_code="416")

    assert result == ["+14165551111", "+14165552222"]
    request = route.calls.last.request
    assert request.url.params["AreaCode"] == "416"
    assert request.url.params["SmsEnabled"] == "true"
    assert request.url.params["VoiceEnabled"] == "true"


@pytest.mark.unit
async def test_search_available_numbers_empty_result() -> None:
    with respx.mock() as mock:
        mock.get(f"{_API_BASE}/AvailablePhoneNumbers/CA/Local.json").mock(
            return_value=httpx.Response(200, json={"available_phone_numbers": []})
        )
        result = await TwilioRestProvisioner().search_available_numbers(region="ON")

    assert result == []


@pytest.mark.unit
async def test_search_available_numbers_no_filters_sent_when_none_given() -> None:
    with respx.mock() as mock:
        route = mock.get(f"{_API_BASE}/AvailablePhoneNumbers/CA/Local.json").mock(
            return_value=httpx.Response(200, json={"available_phone_numbers": []})
        )
        await TwilioRestProvisioner().search_available_numbers()

    request = route.calls.last.request
    assert "AreaCode" not in request.url.params
    assert "InRegion" not in request.url.params


@pytest.mark.unit
async def test_purchase_number_returns_sid() -> None:
    with respx.mock() as mock:
        route = mock.post(f"{_API_BASE}/IncomingPhoneNumbers.json").mock(
            return_value=httpx.Response(
                201, json={"sid": "PNabc123", "phone_number": "+14165551111"}
            )
        )
        sid = await TwilioRestProvisioner().purchase_number(phone_number="+14165551111")

    assert sid == "PNabc123"
    request = route.calls.last.request
    assert request.content == b"PhoneNumber=%2B14165551111"


@pytest.mark.unit
async def test_purchase_number_raises_on_http_error() -> None:
    with respx.mock() as mock:
        mock.post(f"{_API_BASE}/IncomingPhoneNumbers.json").mock(
            return_value=httpx.Response(400, json={"message": "invalid number"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await TwilioRestProvisioner().purchase_number(phone_number="+14165551111")


@pytest.mark.unit
async def test_configure_webhooks_sends_sms_and_voice_urls() -> None:
    with respx.mock() as mock:
        route = mock.post(f"{_API_BASE}/IncomingPhoneNumbers/PNabc123.json").mock(
            return_value=httpx.Response(200, json={"sid": "PNabc123"})
        )
        await TwilioRestProvisioner().configure_webhooks(
            twilio_sid="PNabc123",
            sms_url="https://api.stoop.example/webhooks/twilio/sms",
            voice_url="https://api.stoop.example/webhooks/twilio/voice",
        )

    request = route.calls.last.request
    body = request.content.decode()
    assert "SmsUrl=https%3A%2F%2Fapi.stoop.example%2Fwebhooks%2Ftwilio%2Fsms" in body
    assert "VoiceUrl=https%3A%2F%2Fapi.stoop.example%2Fwebhooks%2Ftwilio%2Fvoice" in body
    assert "SmsMethod=POST" in body
    assert "VoiceMethod=POST" in body


@pytest.mark.unit
async def test_associate_messaging_service_posts_to_messaging_api() -> None:
    with respx.mock() as mock:
        route = mock.post(f"{_MESSAGING_BASE}/Services/MGxyz/PhoneNumbers").mock(
            return_value=httpx.Response(201, json={"sid": "PNabc123"})
        )
        await TwilioRestProvisioner().associate_messaging_service(
            twilio_sid="PNabc123", messaging_service_sid="MGxyz"
        )

    request = route.calls.last.request
    assert request.content == b"PhoneNumberSid=PNabc123"


@pytest.mark.unit
async def test_release_number_sends_delete() -> None:
    with respx.mock() as mock:
        route = mock.delete(f"{_API_BASE}/IncomingPhoneNumbers/PNabc123.json").mock(
            return_value=httpx.Response(204)
        )
        await TwilioRestProvisioner().release_number(twilio_sid="PNabc123")

    assert route.calls.last.request.method == "DELETE"


@pytest.mark.unit
async def test_release_number_raises_on_http_error() -> None:
    with respx.mock() as mock:
        mock.delete(f"{_API_BASE}/IncomingPhoneNumbers/PNabc123.json").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await TwilioRestProvisioner().release_number(twilio_sid="PNabc123")
