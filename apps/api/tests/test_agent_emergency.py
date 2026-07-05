"""Unit tests for app.agent.emergency — the #40→#108 execution seam.

``fire_emergency_protocol`` is intentionally a log-only stub today (see
its module docstring): the durable artifacts (audit_log/notifications)
are created by its CALLER, not by this function. These tests pin that
contract: no exception, no DB access, and the log call carries only
identifiers/category names — never a phone number or message body.

Mocks ``app.agent.emergency.log`` directly rather than reconfiguring
structlog's global state (unlike ``tests/test_observability.py``'s
``capsys``-based approach) — mutating the process-wide structlog
configuration from within a single test leaks across every other test
file that logs anything afterward (a real, empirically-confirmed hazard:
``structlog.PrintLoggerFactory(file=sys.stdout)`` captures whatever
``sys.stdout`` object is live at configuration time, which under
``capsys`` is a per-test capture stream that pytest closes at that test's
teardown — any later test's log call then raises "I/O operation on
closed file"). Mocking the module-level logger avoids that class of
cross-test pollution entirely.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.agent.emergency import fire_emergency_protocol


@pytest.mark.unit
async def test_fire_emergency_protocol_does_not_raise() -> None:
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

    with patch("app.agent.emergency.log") as mock_log:
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
