"""Machine-enforced allowlist for ``get_twilio_sender`` — the ONLY seam
that can place a real outbound Twilio call or send a real outbound SMS
(``app/integrations/twilio_send.py``, #108).

Mirrors ``tests/test_migrations_0005.py``'s
``test_get_admin_session_referenced_only_by_allowlisted_files`` exactly —
same rationale, different capability. ``apps/api/CLAUDE.md``: "Send to
tenant/vendor happens only through the draft flow or the emergency safety
path. There is no other code path that calls ``twilio.send``. Keep it that
way." Today the draft flow's sender doesn't exist yet (#44/#45 unbuilt) —
so this allowlist has exactly TWO entries: the seam's own definition and
its sole caller, the emergency escalation chain. Red-fails the instant a
new file references ``get_twilio_sender`` without this allowlist being
updated to match, forcing that update to be a deliberate, reviewed diff —
exactly the protection this repo's send-call-site discipline depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Files allowed to reference get_twilio_sender: its definition
# (app/integrations/twilio_send.py) and its sole caller
# (app/agent/emergency_chain.py, #108). EXTEND THIS DELIBERATELY, not by
# loosening the grep — e.g. the day #44's draft sender needs its own SMS
# -sending seam, it gets its OWN reviewed addition here (or its own
# separate seam entirely), never a silent new call site.
_TWILIO_SEND_ALLOWLIST: frozenset[str] = frozenset(
    {
        "app/integrations/twilio_send.py",
        "app/agent/emergency_chain.py",
    }
)


@pytest.mark.unit
def test_get_twilio_sender_referenced_only_by_allowlisted_files() -> None:
    referencing: set[str] = set()
    for path in _APP_DIR.rglob("*.py"):
        content = path.read_text()
        if "get_twilio_sender" in content:
            referencing.add(str(path.relative_to(_APP_DIR.parent)))

    assert referencing == set(_TWILIO_SEND_ALLOWLIST), (
        f"files referencing get_twilio_sender changed: {referencing} != "
        f"{set(_TWILIO_SEND_ALLOWLIST)} — update the allowlist deliberately "
        "if this is an intentional new sender call site, with a comment "
        "explaining why (see this test's own module docstring)."
    )


@pytest.mark.unit
def test_no_direct_twilio_rest_client_construction_outside_send_module() -> None:
    """Belt-and-braces: nothing outside ``app/integrations/twilio_send.py``
    may construct a ``twilio.rest.Client`` directly (bypassing the
    ``get_twilio_sender()``/``TwilioSender`` seam entirely would defeat the
    allowlist above — this test catches that even if a future caller never
    references ``get_twilio_sender`` by name)."""
    offending: list[str] = []
    for path in _APP_DIR.rglob("*.py"):
        if path == _APP_DIR / "integrations" / "twilio_send.py":
            continue
        content = path.read_text()
        if "twilio.rest" in content or "from twilio import rest" in content:
            offending.append(str(path.relative_to(_APP_DIR.parent)))

    assert not offending, (
        f"files constructing/importing twilio.rest outside twilio_send.py: {offending} — "
        "route every outbound Twilio call through app.integrations.twilio_send.get_twilio_sender()"
    )
