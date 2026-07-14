"""SMS segment counting + Twilio per-segment cost estimate (#111).

Pure functions, no I/O — the Twilio-side counterpart to
``app/integrations/anthropic.py``'s "Cost accounting" section (issue #111's
AC: "Twilio per-segment cost recorded on outbound sends"). Never imports
``app.config`` or constructs a network client; every sanctioned SMS-sending
call site (``app/agent/draft_sender.py``, ``app/agent/emergency_chain.py``)
calls :func:`count_segments` on the body it is ABOUT to send / just sent,
then :func:`estimate_sms_cost_cents` on the resulting segment count, and
records both in its own ``audit_log`` payload — this module itself never
writes to the database.

Segment counting (GSM 03.38 / Twilio's own documented rules)
--------------------------------------------------------------------------
A carrier segments an SMS body by the encoding it can be represented in:

- **GSM-7** (the default, 7-bit alphabet): a single segment holds
  :data:`GSM7_SINGLE_SEGMENT_LIMIT` (160) characters; once a body needs
  MORE than one segment, each segment only holds
  :data:`GSM7_MULTIPART_SEGMENT_LIMIT` (153) — the multi-part User Data
  Header eats the other 7 septets of every segment once concatenation is
  in play.
- **GSM-7 extended table** — ``^{}\\[~]|€`` plus a form-feed control
  character are not in the 7-bit BASIC repertoire; each one is sent as a
  2-septet ESCAPE sequence, so it counts DOUBLE against the same 160/153
  limits above (:data:`_GSM7_EXTENDED_CHARS`).
- **UCS-2** (the fallback): the WHOLE message drops to UCS-2 the moment
  even ONE character (anywhere in the body — emoji, most accented Latin
  outside the small GSM set, non-Latin scripts) is not representable in
  GSM-7 basic+extended. A single UCS-2 segment holds
  :data:`UCS2_SINGLE_SEGMENT_LIMIT` (70) UTF-16 code UNITS (not Python
  codepoints — an astral character like an emoji outside the Basic
  Multilingual Plane is a UTF-16 surrogate PAIR, two code units, exactly
  like a real carrier counts it); once multi-part, each segment holds
  :data:`UCS2_MULTIPART_SEGMENT_LIMIT` (67).

Pricing — FOUNDER-PROVISIONAL, same "conservative placeholder" pattern as
``app/integrations/anthropic.py``
--------------------------------------------------------------------------
No confirmed Twilio Canada per-segment outbound-SMS invoice exists in this
environment (mirrors ``anthropic.py``'s own "Cost accounting" precedent: no
billing-dashboard access, no pricing API — a small, hardcoded, CONSERVATIVE
constant instead). Twilio's long-standing published outbound SMS rate for
Canadian long codes is $0.0075 USD/segment; :data:`SEGMENT_PRICE_USD_CENTS`
(0.75 cents) uses that figure. Treat this as a placeholder to reconcile with
the real invoiced rate once real billing data exists — erring high rather
than guessing low, same bias-rule spirit the rubric applies elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# GSM 03.38 default alphabet (0x00-0x7F) -- reproduced verbatim, not a
# Stoop-specific rule. The 0x1B ESCAPE control character is deliberately
# EXCLUDED from the basic set below: it is a shift code that only ever
# appears as part of an extended-table escape sequence, never as literal
# text content in a real message body.
# ---------------------------------------------------------------------------
_GSM7_BASIC_CHARS: str = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

# GSM 03.38 extension table -- the escape-sequence characters that cost TWO
# GSM-7 septets each. Form feed (page break) is included for completeness
# even though it practically never appears in a real tenant/landlord SMS.
_GSM7_EXTENDED_CHARS: str = "\x0c^{}\\[~]|€"

_GSM7_CHARSET: frozenset[str] = frozenset(_GSM7_BASIC_CHARS) | frozenset(_GSM7_EXTENDED_CHARS)
_GSM7_EXTENDED_SET: frozenset[str] = frozenset(_GSM7_EXTENDED_CHARS)

GSM7_SINGLE_SEGMENT_LIMIT: int = 160
GSM7_MULTIPART_SEGMENT_LIMIT: int = 153
UCS2_SINGLE_SEGMENT_LIMIT: int = 70
UCS2_MULTIPART_SEGMENT_LIMIT: int = 67

SEGMENT_PRICE_USD_CENTS: float = 0.75
"""FOUNDER-PROVISIONAL -- see module docstring "Pricing". $0.0075 USD per
segment (Twilio's long-standing published Canada outbound SMS rate)."""

_PRICING_SOURCE_NOTE: str = (
    "Twilio's long-standing published Canada outbound SMS rate "
    "($0.0075 USD/segment) -- no independently-confirmed invoiced rate was "
    "available in this environment. Conservative placeholder; reconcile "
    "with real billing data once available."
)

Encoding = Literal["gsm7", "ucs2"]


@dataclass(frozen=True)
class SmsSegments:
    """Result of :func:`count_segments` for one message body."""

    encoding: Encoding
    units: int
    """GSM-7 septets (extended chars counted double) when ``encoding ==
    "gsm7"``, or UTF-16 code units (surrogate pairs counted as 2) when
    ``encoding == "ucs2"``."""
    segments: int
    """Number of SMS segments Twilio would bill this body as. ``0`` only
    for an empty body (never a real send)."""


def _is_gsm7(body: str) -> bool:
    """``True`` iff every character in *body* is in the GSM-7 basic OR
    extended repertoire. A single non-GSM character (emoji, an accented
    character outside the small GSM set, non-Latin scripts, ...) drops the
    WHOLE message to UCS-2 -- there is no per-character mixing in a real
    SMS PDU."""
    return all(ch in _GSM7_CHARSET for ch in body)


def count_segments(body: str) -> SmsSegments:
    """Count the SMS segments *body* would be billed as, choosing GSM-7 vs
    UCS-2 per :func:`_is_gsm7` and applying the correct single-segment vs
    multi-part-per-segment limit for whichever encoding applies (module
    docstring "Segment counting"). Pure -- no I/O, no settings/config read.
    """
    if not body:
        return SmsSegments(encoding="gsm7", units=0, segments=0)

    encoding: Encoding
    if _is_gsm7(body):
        encoding = "gsm7"
        units = sum(2 if ch in _GSM7_EXTENDED_SET else 1 for ch in body)
        single_limit, multipart_limit = GSM7_SINGLE_SEGMENT_LIMIT, GSM7_MULTIPART_SEGMENT_LIMIT
    else:
        encoding = "ucs2"
        # UTF-16 code units, not Python codepoints -- an astral character
        # (e.g. an emoji outside the Basic Multilingual Plane) is a
        # surrogate PAIR (2 code units), exactly how a real carrier counts
        # it against the 70/67 UCS-2 limits.
        units = len(body.encode("utf-16-le")) // 2
        single_limit, multipart_limit = UCS2_SINGLE_SEGMENT_LIMIT, UCS2_MULTIPART_SEGMENT_LIMIT

    if units <= single_limit:
        segments = 1
    else:
        segments = -(-units // multipart_limit)  # ceil division, no float rounding
    return SmsSegments(encoding=encoding, units=units, segments=segments)


def estimate_sms_cost_cents(segments: int) -> float:
    """Estimate the USD-cents cost of sending a message that costs
    *segments* SMS segments -- see module docstring "Pricing". Rounded to 4
    decimal places, matching ``anthropic.estimate_cost_cents``'s precision
    (and ``messages.sms_cost_cents numeric(10,4)``'s column shape, per
    schema-v1.md's v1.12 amendment note on that still-unwritten column).
    """
    return round(segments * SEGMENT_PRICE_USD_CENTS, 4)


__all__: list[str] = [
    "GSM7_MULTIPART_SEGMENT_LIMIT",
    "GSM7_SINGLE_SEGMENT_LIMIT",
    "SEGMENT_PRICE_USD_CENTS",
    "UCS2_MULTIPART_SEGMENT_LIMIT",
    "UCS2_SINGLE_SEGMENT_LIMIT",
    "SmsSegments",
    "count_segments",
    "estimate_sms_cost_cents",
]
