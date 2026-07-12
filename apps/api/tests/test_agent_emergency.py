"""Unit tests for app.agent.emergency — the #40→#108 execution seam.

``fire_emergency_protocol`` now delegates the real work to
``app.agent.emergency_chain.handle_emergency_trigger`` (#108); these tests
pin the SEAM's own contract: it invokes that delegate with exactly the
right arguments, it logs identifiers/category names only (never a phone
number or message body, rule #5), and — critically — a failure in the
delegate is caught, logged, and paged to Sentry, NEVER propagated (the
Twilio SMS webhook this is awaited from must always still return its 200
promptly). ``app/agent/emergency_chain.py``'s own test file
(``tests/test_agent_emergency_chain.py``) covers the real DB/Twilio-facing
logic; this file is deliberately narrow, mirroring its original scope.

Mocks ``app.agent.emergency.log``/``handle_emergency_trigger`` directly
rather than reconfiguring structlog's global state (unlike
``tests/test_observability.py``'s ``capsys``-based approach) — see the
original version of this file's docstring for the cross-test-pollution
rationale this avoids.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.emergency import fire_emergency_protocol


@pytest.mark.unit
async def test_fire_emergency_protocol_does_not_raise() -> None:
    with patch("app.agent.emergency.handle_emergency_trigger", new=AsyncMock()):
        await fire_emergency_protocol(
            notification_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            property_id=uuid.uuid4(),
            categories=["fire", "gas_co"],
        )


@pytest.mark.unit
async def test_fire_emergency_protocol_logs_identifiers_and_categories_only() -> None:
    notification_id = uuid.uuid4()
    message_id = uuid.uuid4()
    property_id = uuid.uuid4()

    with (
        patch("app.agent.emergency.handle_emergency_trigger", new=AsyncMock()),
        patch("app.agent.emergency.log") as mock_log,
    ):
        await fire_emergency_protocol(
            notification_id=notification_id,
            message_id=message_id,
            property_id=property_id,
            categories=["fire"],
        )

    mock_log.info.assert_called_once_with(
        "emergency_protocol_seam_invoked",
        notification_id=str(notification_id),
        message_id=str(message_id),
        property_id=str(property_id),
        categories=["fire"],
    )


@pytest.mark.unit
async def test_fire_emergency_protocol_delegates_with_exact_arguments() -> None:
    notification_id = uuid.uuid4()
    message_id = uuid.uuid4()
    property_id = uuid.uuid4()
    categories = ["fire", "gas_co"]

    fake_delegate = AsyncMock()
    with patch("app.agent.emergency.handle_emergency_trigger", new=fake_delegate):
        await fire_emergency_protocol(
            notification_id=notification_id,
            message_id=message_id,
            property_id=property_id,
            categories=categories,
        )

    fake_delegate.assert_awaited_once_with(
        notification_id=notification_id,
        message_id=message_id,
        property_id=property_id,
        categories=categories,
    )


@pytest.mark.unit
async def test_fire_emergency_protocol_never_raises_when_delegate_fails() -> None:
    """The single most important contract of this seam post-#108: a
    downstream Twilio/DB hiccup inside the escalation chain must NEVER
    surface as an exception here — the webhook this is awaited from would
    otherwise 500 on a transient failure it doesn't need to. The chain
    recovers via the next scheduler tick regardless (see
    ``app/agent/emergency_chain.py``'s own docstring)."""
    failing_delegate = AsyncMock(side_effect=RuntimeError("twilio boom"))

    with (
        patch("app.agent.emergency.handle_emergency_trigger", new=failing_delegate),
        patch("app.agent.emergency.sentry_sdk") as mock_sentry,
        patch("app.agent.emergency.log") as mock_log,
    ):
        await fire_emergency_protocol(
            notification_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            property_id=uuid.uuid4(),
            categories=["fire"],
        )

    mock_log.error.assert_called_once()
    assert mock_log.error.call_args.args[0] == "emergency_protocol_seam_failed"
    mock_sentry.capture_message.assert_called_once()
    assert mock_sentry.capture_message.call_args.kwargs["level"] == "error"


@pytest.mark.unit
async def test_fire_emergency_protocol_never_raises_when_delegate_fails_logs_no_pii() -> None:
    """Rule #5: only uuids/exception type names ever reach the failure log
    line or the Sentry event — never a phone number or message body (there
    is none in this seam's own scope, but the assertion pins it anyway)."""
    failing_delegate = AsyncMock(side_effect=RuntimeError("twilio boom, +15551234567"))

    with (
        patch("app.agent.emergency.handle_emergency_trigger", new=failing_delegate),
        patch("app.agent.emergency.sentry_sdk") as mock_sentry,
    ):
        await fire_emergency_protocol(
            notification_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            property_id=uuid.uuid4(),
            categories=["fire"],
        )

    extras = mock_sentry.capture_message.call_args.kwargs["extras"]
    assert extras["exc_type"] == "RuntimeError"
    assert "+1555" not in str(extras)
