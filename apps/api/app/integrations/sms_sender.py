"""``SmsSender`` — the injectable seam ``app/agent/draft_sender.py``'s
ticker depends on. This is the SECOND of the two sanctioned outbound-send
call sites (``apps/api/CLAUDE.md``: "Send to tenant/vendor happens only
through the draft flow or the emergency safety path").

Scope boundary (2026-07)
------------------------
Issue #108 (parallel branch, ``feat/emergency-executor``) owns
``app/integrations/twilio.py`` and ``app/agent/emergency*.py`` — the real
Twilio client and the emergency-call/safety-SMS execution seam. This
module is deliberately a SEPARATE file: the draft sender must depend only
on this Protocol, never import ``app.integrations.twilio`` directly, so
the two branches cannot collide on the same file. A follow-up, one-commit
integration (after #108 merges) wires a real Twilio-backed implementation
of :class:`SmsSender` into :func:`get_default_sms_sender` — until then this
returns ``None`` on purpose (see that function's own docstring).

The deployment-gating pattern (matches #109's own)
----------------------------------------------------
There are two, deliberately layered defenses against ever silently
"sending" nothing while believing it succeeded:

1. :class:`NotImplementedSmsSender` — the documented placeholder for the
   eventual real binding. If its ``send_sms`` is EVER actually invoked
   (which the current wiring below prevents — see point 2), it raises
   ``NotImplementedError`` loudly rather than silently no-opping or
   fabricating a fake provider sid.
2. :func:`get_default_sms_sender` returns ``None`` (not an instance of
   (1)) until the real binding lands. ``app/agent/draft_sender.py``'s
   ``run_sender_loop`` treats a ``None`` sender as "the worker is
   disabled" and refuses to start its ticking loop AT ALL, logging a loud,
   one-time warning instead — so (1)'s ``NotImplementedError`` is dead
   code in every deployment that exists today, on purpose: the loop simply
   never reaches a call site that could trigger it. Once #108 lands and a
   real adapter is wired in here, the loop starts normally and (1) is
   never constructed at all.
"""

from __future__ import annotations

from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


class SmsSender(Protocol):
    """The one interface ``app/agent/draft_sender.py`` depends on. Returns
    the provider's message sid (a plain string — never assumed to be a
    Twilio-specific shape) on success; raises on failure (the ticker
    itself decides what "failure" means for its own claim/retry bookkeeping
    — see that module's docstring)."""

    async def send_sms(self, *, to_e164: str, body: str) -> str: ...


class NotImplementedSmsSender:
    """The documented placeholder production binding — see module
    docstring "The deployment-gating pattern". Never constructed by
    :func:`get_default_sms_sender` today; exists so the eventual real
    binding has an unambiguous contract to satisfy, and so any FUTURE
    caller that bypasses the loop's own ``None``-gating (a mistake, not a
    sanctioned path) fails loudly instead of pretending to succeed."""

    async def send_sms(self, *, to_e164: str, body: str) -> str:
        raise NotImplementedError(
            "No real SmsSender is wired in yet -- #108's integration commit provides the "
            "Twilio-backed binding (see app/integrations/sms_sender.py's module docstring). "
            "This placeholder must never be reachable in a live deployment; "
            "get_default_sms_sender() returning None is what keeps the sender worker "
            "disabled until that binding exists."
        )


def get_default_sms_sender() -> SmsSender | None:
    """Returns ``None`` until #108's integration commit provides a real
    Twilio-backed binding — see module docstring. Deliberately NOT
    :class:`NotImplementedSmsSender`: ``None`` is what
    ``app/agent/draft_sender.py``'s ``run_sender_loop`` checks to decide
    "the worker is disabled" (logged loudly, once, at start-up) BEFORE ever
    reaching a call site that could invoke ``send_sms`` — the
    ``NotImplementedError`` above is therefore unreachable dead code in
    every deployment today, by construction, not by convention alone.
    """
    return None


__all__: list[str] = ["NotImplementedSmsSender", "SmsSender", "get_default_sms_sender"]
