"""Unit tests for app/integrations/expo_push.py (#210 M3).

Markers: ``@pytest.mark.unit`` — mocked httpx via ``respx``; NO real
network access, ever (Expo's push API is mocked in every test here).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.integrations import expo_push as expo_push_mod
from app.integrations.expo_push import (
    DEVICE_NOT_REGISTERED_ERROR_CODE,
    ExpoHttpPushSender,
    ExpoPushMessage,
    ExpoPushResponseError,
    get_expo_push_sender,
    set_expo_push_sender_for_tests,
)

_URL = expo_push_mod._EXPO_PUSH_URL

_MESSAGE = ExpoPushMessage(
    to="ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]",
    title="Stoop",
    body="A reply is waiting for your approval.",
    data={"kind": "draft_awaiting_approval", "case_id": "c1", "draft_id": "d1"},
)


@pytest.mark.unit
async def test_success_returns_ok_ticket() -> None:
    sender = ExpoHttpPushSender()
    with respx.mock() as respx_mock:
        respx_mock.post(_URL).mock(
            return_value=httpx.Response(200, json={"data": {"status": "ok", "id": "receipt-1"}})
        )
        ticket = await sender.send_push(_MESSAGE)

    assert ticket.status == "ok"
    assert ticket.error_code is None


@pytest.mark.unit
async def test_device_not_registered_error_code_surfaced() -> None:
    sender = ExpoHttpPushSender()
    with respx.mock() as respx_mock:
        respx_mock.post(_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "status": "error",
                        "message": "The recipient device is not registered.",
                        "details": {"error": DEVICE_NOT_REGISTERED_ERROR_CODE},
                    }
                },
            )
        )
        ticket = await sender.send_push(_MESSAGE)

    assert ticket.status == "error"
    assert ticket.error_code == DEVICE_NOT_REGISTERED_ERROR_CODE
    assert ticket.message == "The recipient device is not registered."


@pytest.mark.unit
async def test_other_error_code_surfaced_not_confused_with_device_not_registered() -> None:
    sender = ExpoHttpPushSender()
    with respx.mock() as respx_mock:
        respx_mock.post(_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "status": "error",
                        "message": "Message too big.",
                        "details": {"error": "MessageTooBig"},
                    }
                },
            )
        )
        ticket = await sender.send_push(_MESSAGE)

    assert ticket.status == "error"
    assert ticket.error_code == "MessageTooBig"
    assert ticket.error_code != DEVICE_NOT_REGISTERED_ERROR_CODE


@pytest.mark.unit
async def test_error_with_no_details_has_none_error_code() -> None:
    sender = ExpoHttpPushSender()
    with respx.mock() as respx_mock:
        respx_mock.post(_URL).mock(
            return_value=httpx.Response(200, json={"data": {"status": "error"}})
        )
        ticket = await sender.send_push(_MESSAGE)

    assert ticket.status == "error"
    assert ticket.error_code is None


@pytest.mark.unit
async def test_malformed_response_missing_data_raises_response_error() -> None:
    sender = ExpoHttpPushSender()
    with respx.mock() as respx_mock:
        respx_mock.post(_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
        with pytest.raises(ExpoPushResponseError):
            await sender.send_push(_MESSAGE)


@pytest.mark.unit
async def test_malformed_response_missing_status_raises_response_error() -> None:
    sender = ExpoHttpPushSender()
    with respx.mock() as respx_mock:
        respx_mock.post(_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        with pytest.raises(ExpoPushResponseError):
            await sender.send_push(_MESSAGE)


@pytest.mark.unit
async def test_non_2xx_response_raises_http_error() -> None:
    sender = ExpoHttpPushSender()
    with respx.mock() as respx_mock:
        respx_mock.post(_URL).mock(return_value=httpx.Response(500, json={"errors": ["boom"]}))
        with pytest.raises(httpx.HTTPStatusError):
            await sender.send_push(_MESSAGE)


@pytest.mark.unit
async def test_request_body_shape_sent_to_expo() -> None:
    """Never batched (one Expo call per row, per issue instructions) —
    the request body is a single object, not an array, and carries exactly
    ``to``/``title``/``body``/``data``."""
    sender = ExpoHttpPushSender()
    with respx.mock() as respx_mock:
        route = respx_mock.post(_URL).mock(
            return_value=httpx.Response(200, json={"data": {"status": "ok"}})
        )
        await sender.send_push(_MESSAGE)

    assert route.call_count == 1
    sent_json = route.calls.last.request.content
    body = json.loads(sent_json)
    assert body == {
        "to": _MESSAGE.to,
        "title": _MESSAGE.title,
        "body": _MESSAGE.body,
        "data": _MESSAGE.data,
    }


# ---------------------------------------------------------------------------
# Injectable singleton seam
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_expo_push_sender_returns_singleton() -> None:
    set_expo_push_sender_for_tests(None)
    try:
        first = get_expo_push_sender()
        second = get_expo_push_sender()
        assert first is second
    finally:
        set_expo_push_sender_for_tests(None)


@pytest.mark.unit
def test_set_expo_push_sender_for_tests_injects_fake() -> None:
    class _FakeSender:
        async def send_push(self, message: ExpoPushMessage) -> None:  # pragma: no cover
            raise NotImplementedError

    fake = _FakeSender()
    try:
        set_expo_push_sender_for_tests(fake)  # type: ignore[arg-type]
        assert get_expo_push_sender() is fake
    finally:
        set_expo_push_sender_for_tests(None)
