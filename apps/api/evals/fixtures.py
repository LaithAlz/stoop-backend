"""Shared, non-scenario-specific fixtures for the eval harness.

Not app code, not a DB row, not subject to any freeze -- this is eval
-infrastructure data, editable in place.
"""

from __future__ import annotations

DEFAULT_VOICE_PROFILE: dict[str, object] = {
    "tone": "warm, direct, a little informal -- signs off with first name only",
    "samples": [
        "Hey! Thanks for flagging this - I'll get someone out this week.",
        "No worries, I'll take care of it. Let me know if it gets worse in the meantime.",
    ],
}
"""One representative landlord voice profile, reused for every scenario's
draft_response call (issue #35's "(voice profile from a fixture)"). Deliberately
a single shared fixture, not one per scenario -- the draft assertions test
whether the CONTENT is correct for the severity/refusal topic, not whether
voice-profile injection itself works (that is already covered by
``tests/test_agent_draft_response.py``)."""

__all__: list[str] = ["DEFAULT_VOICE_PROFILE"]
