"""Machine-enforced allowlist for ``get_twilio_sender`` — the ONLY seam
that can place a real outbound Twilio call or send a real outbound SMS
(``app/integrations/twilio_send.py``, #108).

Mirrors ``tests/test_migrations_0005.py``'s
``test_get_admin_session_referenced_only_by_allowlisted_files`` exactly —
same rationale, different capability. ``apps/api/CLAUDE.md``: "Send to
tenant/vendor happens only through the draft flow or the emergency safety
path. There is no other code path that calls ``twilio.send``." This
allowlist has exactly FOUR entries: the seam's own definition, its first
caller (the emergency escalation chain), its second caller —
``app/integrations/sms_sender.py``, the approve-flow draft sender's real
Twilio binding (#44/#45's integration commit) — and its third and FINAL
sanctioned caller, ``app/agent/landlord_sms.py`` (#122, approve-by-SMS —
the draft-ready SMS + every reply confirmation, sent to the case's own
LANDLORD, never a tenant/vendor; deliberately added below, never a silent
new call site — see that module's own docstring "A NEW sanctioned
Twilio-send call site"). Red-fails the instant a new file references
``get_twilio_sender`` without this allowlist being updated to match,
forcing that update to be a deliberate, reviewed diff — exactly the
protection this repo's send-call-site discipline depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Files allowed to reference get_twilio_sender: its definition
# (app/integrations/twilio_send.py), its first caller
# (app/agent/emergency_chain.py, #108), its second, sanctioned caller
# (app/integrations/sms_sender.py, #44/#45 — the draft flow's OWN real
# Twilio binding, added deliberately, not a silent new call site: it
# delegates through get_twilio_sender() rather than constructing a second
# twilio.rest.Client stack), and its THIRD, sanctioned caller
# (app/agent/landlord_sms.py, #122 approve-by-SMS — sends ONLY to the
# case's own landlord, never a tenant/vendor; added deliberately for the
# exact same reason as the other two). EXTEND THIS DELIBERATELY, not by
# loosening the grep, if a FOURTH sanctioned sender ever needs to
# reference it.
_TWILIO_SEND_ALLOWLIST: frozenset[str] = frozenset(
    {
        "app/integrations/twilio_send.py",
        "app/agent/emergency_chain.py",
        # Sanctioned draft flow (#44/#45): the ONLY other place allowed to
        # obtain a real Twilio sender, alongside the emergency chain above.
        "app/integrations/sms_sender.py",
        # Sanctioned landlord-facing SMS outbox (#122, approve-by-SMS): the
        # draft-ready notice + every reply confirmation. Sends ONLY to the
        # case's own landlord (never a tenant/vendor) — see that module's
        # own docstring for why this is a NEW call site rather than a reuse
        # of sms_sender.py above (a different recipient class, a different
        # notification-driven outbox, its own drain sweep).
        "app/agent/landlord_sms.py",
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
