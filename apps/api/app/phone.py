"""E.164 phone-number canonicalization — THE single source of truth for
what counts as a dialable phone number anywhere in this codebase (#232,
#260).

Every phone-bearing column in ``docs/03-engineering/schema-v1.md`` —
``landlords.phone``, ``properties.twilio_number``, the ``phone`` key inside
``properties.backup_contact``, ``tenants.phone``, ``vendors.phone`` — is
documented as E.164. Before this module existed, nothing enforced that at
write time: ``PATCH /v1/me`` accepted ``phone: ""`` (clearing the
emergency-call target as effectively as an explicit ``null``, #260) and no
endpoint validated shape at all, so a malformed value reached Twilio's
``create_call``/``send_sms`` and failed silently
(``app/agent/emergency_chain.py``'s ``_execute_action`` degrades a
bad-number failure to ``status='failed'`` by design — never re-raised), or
a format-drifted stored value simply never matched the Twilio ``/sms``
webhook's exact-string comparison, misrouting (or, since #170,
dead-lettering into ``unrouted_inbound``) a message that should have
reached a real property/tenant (#232).

``to_e164`` below is an ASCII-digits-only behavioral port of
``apps/web/src/features/account/profileEdit.ts``'s ``toE164`` (landed for
issue #234 PR 5) — client and server must never disagree about what is
dialable, for the ASCII input this codebase actually expects. Read that
file's own comments for the safety-review history (F2/F3/N1/R1/R3/R4)
that produced these exact rules; it is not repeated here.

**Non-ASCII "digit" characters are REJECTED, never silently stripped or
matched (safety review, 2026-08-03, finding 1 — BLOCKING).** Python's ``\\d``/
``\\D`` are Unicode-aware by default (``\\d`` matches the full Unicode
``Nd`` category — Arabic-Indic, Devanagari, fullwidth digits, …), unlike
JavaScript's, which are always ASCII-only. Left unguarded, a value like
``"+١4165551234"`` (one Arabic-Indic ``1`` ahead of an otherwise
ordinary Toronto number) would parse differently in each language: this
module's regexes now carry ``re.ASCII`` so ``\\d``/``\\D`` behave exactly
like JavaScript's (a non-ASCII digit is "not a digit" to either regex, so
it is stripped as punctuation would be, same as the TS side) — but
SILENTLY dropping what a human plausibly intended as a real digit is its
own hazard (it can shift the remaining digits into a DIFFERENT,
still-plausible-looking number instead of failing loudly). ``to_e164``
therefore rejects outright, before any other processing, if the input
contains ANY character that is a digit under Python's own (Unicode-aware)
``str.isdigit()`` but is not plain ASCII ``0``-``9`` — never silently
dropped, never coerced.

Canonicalization policy (mirrors the TS implementation exactly)
------------------------------------------------------------------------
- **Empty / whitespace-only** input -> rejected (``None``).
- **Bare NANP 10-digit** (e.g. ``"4165551234"``) -> assumed ``+1``; area
  code and exchange must both plausibly be NANP (see ``is_plausible_nanp``).
- **11-digit, leading ``1``** (e.g. ``"14165551234"``) -> ``+`` prepended,
  same NANP plausibility check on the trailing 10 digits.
- **Already ``+``-prefixed** -> a leading parenthesized trunk zero is
  dropped first (see "Parenthesized trunk zero" below), then punctuation
  is stripped, then:
  - if the remaining digits start with ``1``, treated as NANP: must be
    exactly 11 digits and pass the same plausibility check (a ``+1``
    number gets no less scrutiny than a bare one — a dropped digit like
    ``"+1416555013"`` must not slip through just because it has a plus).
  - otherwise treated as non-NANP international: accepted if the total
    digit count is between 8 and 15 inclusive (E.164's own bound), no
    further shape validation — this codebase has no non-NANP dialing
    rules to check against.
- **Extensions / any other junk** (e.g. ``"416-555-0134 x22"``, ``"n/a"``)
  -> rejected (``None``) — the digit-soup either fails the NANP
  plausibility check or the international length bound.

Parenthesized trunk zero (#277)
------------------------------------------------------------------------
``"+44 (0)20 7946 0958"`` — the single most common *written* form of a UK
number — used to normalize to ``"+4402079460958"``: a 13-digit non-number
Twilio rejects (21211), landing on the field the emergency chain dials
(``app/agent/emergency_chain.py``'s ``_execute_action`` swallows that
failure into ``status="failed"`` without re-raising, so the failure is
silent). The ``(0)`` is a trunk prefix, dialed only for calls placed
*inside* the country and never part of the number in E.164 form.

This module now drops a leading trunk ``0`` when it appears
**parenthesized, directly after the country code** — narrow and
well-understood, not a general trunk-prefix rule for every country. It
must run on the RAW string, before punctuation is stripped: once digits
are collapsed a real ``(0)`` is indistinguishable from an ordinary digit
``0`` that happens to sit there. The match is anchored to the very start
of the ``+``-prefixed string (immediately after the country code) and
requires the literal parenthesized ``(0)`` (spaces/hyphens allowed
around it, per the anchor) — ``"+1 (416) 555 0100"`` is untouched: those
parentheses hold an area code, not a trunk marker, so the literal
``"(0)"`` never matches ``"(416)"``. A genuine, un-parenthesized leading
``0`` (e.g. ``"+44 020 7946 0958"``) is left exactly as before — this is
option 1 only (see the issue); it does not attempt to detect or repair
that case.

``is_plausible_nanp``: area code and exchange code both start ``2``-``9``
and neither is an N11 service code (``211``/``411``/``911``/… — never
assignable to a real subscriber) — the same cheap regex as the TS version,
deliberately permissive about everything else (a false REJECT here is its
own failure mode, since this is the field a landlord/tenant/vendor is
reached on).
"""

from __future__ import annotations

import re

from app.errors import AppError

# re.ASCII (safety-review finding, #232/#260 follow-up): without it, \d/\D
# are Unicode-aware (\d matches the full Nd category — Arabic-Indic,
# Devanagari, fullwidth digits, …), unlike JavaScript's, which are always
# ASCII-only — see module docstring, "Non-ASCII 'digit' characters".
_NON_DIGIT_RE = re.compile(r"\D+", re.ASCII)
_NANP_RE = re.compile(r"^(?!\d11)[2-9]\d{2}(?!\d11)[2-9]\d{6}$", re.ASCII)
# #277: anchored to the start of the string — "the country code" means
# whatever digit run sits between "+" and the parenthesized "(0)", nothing
# more. 1-3 digits mirrors E.164's own country-code length bound. Requires
# the LITERAL "(0)" (not just any parenthesized digit), which is what keeps
# "+1 (416) 555 0100" (an area code, not a trunk marker) untouched.
_PARENTHESIZED_TRUNK_ZERO_RE = re.compile(r"^(\+\d{1,3})[\s-]*\(0\)[\s-]*", re.ASCII)


def is_plausible_nanp(digits: str) -> bool:
    """``True`` iff *digits* (exactly 10 digits, no country code) is a
    plausible NANP subscriber number — see module docstring."""
    return _NANP_RE.match(digits) is not None


def _contains_non_ascii_digit(value: str) -> bool:
    """``True`` iff *value* contains a character Python considers numeric
    but which is not plain ASCII ``0``-``9`` — e.g. ``"١"`` (Arabic-Indic
    one), ``"٤"`` (Arabic-Indic four), ``"４"`` (fullwidth four).

    Uses ``str.isnumeric()`` rather than ``str.isdigit()`` (safety review,
    2026-08-03, finding 1 residual): ``isdigit()`` misses CJK numerals —
    ``"一"`` and ``"〇"`` are literally one and zero, i.e. exactly "a
    character a human plausibly meant as a digit", which is this guard's
    whole rationale. Without this they were silently STRIPPED and the
    remainder accepted, so ``"+一4165551234"`` became ``"+4165551234"`` —
    a different, still-plausible number stored on the field the escalation
    chain dials. The ``not ch.isascii()`` conjunct keeps ``0``-``9`` out of
    it, and no legitimate phone character (``+ - ( ) . space x``) is
    numeric, so this rejects nothing real."""
    return any(ch.isnumeric() and not ch.isascii() for ch in value)


def _drop_parenthesized_trunk_zero(value: str) -> str:
    """Drop a leading trunk ``0`` written parenthesized directly after the
    country code (``"+44 (0)20 7946 0958"`` -> ``"+4420 7946 0958"``) —
    see module docstring, "Parenthesized trunk zero" (#277). Must be
    called on the RAW (punctuation-intact) string, before any digit
    stripping — once digits are collapsed a real ``(0)`` can no longer be
    told apart from an ordinary digit ``0``. A no-op when the pattern
    doesn't match (e.g. ``"+1 (416) 555 0100"``, where the parenthesized
    content is an area code, not a literal ``"0"``)."""
    return _PARENTHESIZED_TRUNK_ZERO_RE.sub(r"\1", value, count=1)


def to_e164(raw: str) -> str | None:
    """Best-effort canonicalization of *raw* to E.164, or ``None`` if it
    cannot be confidently canonicalized — see module docstring for the
    exact policy.

    Pure, no I/O — safe to call from a route handler, a migration, or the
    Twilio webhook's routing-match path alike.
    """
    if _contains_non_ascii_digit(raw):
        # Never silently strip/ignore a non-ASCII digit character —
        # rejecting outright is the only safe response (see module
        # docstring); checked before anything else, including .strip().
        return None

    trimmed = raw.strip()
    plus = trimmed.startswith("+")
    if plus:
        # #277: only the international branch — must run before the
        # punctuation strip below, and only on a "+"-prefixed value (a
        # bare NANP input has no country code for "(0)" to sit after).
        trimmed = _drop_parenthesized_trunk_zero(trimmed)
    digits = _NON_DIGIT_RE.sub("", trimmed)

    if plus:
        if digits.startswith("1"):
            return f"+{digits}" if len(digits) == 11 and is_plausible_nanp(digits[1:]) else None
        return f"+{digits}" if 8 <= len(digits) <= 15 else None

    if len(digits) == 10 and is_plausible_nanp(digits):
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1") and is_plausible_nanp(digits[1:]):
        return f"+{digits}"
    return None


def canonicalize_phone(value: str, *, field: str) -> str:
    """Canonicalize *value* to E.164 or raise 422 ``invalid_field``.

    ``value`` here is already known to be non-null — callers that need to
    treat an explicit empty string as equivalent to ``null`` for a
    not-nullable-by-business-rule field must do so BEFORE calling this
    (see ``app.validation.normalize_blank_to_null``, then
    ``app.validation.reject_explicit_null``). An empty string reaching
    this function directly (e.g. a required-field create body) is simply
    uncanonicalizable, same as any other junk input, and 422s the same
    way — *field* is a fixed, known field name (e.g. ``"phone"`` or
    ``"backup_contact.phone"``), never request data, so this stays within
    ``AppError``'s static-message rule (``app/errors.py``).
    """
    canonical = to_e164(value)
    if canonical is None:
        raise AppError(
            status_code=422,
            code="invalid_field",
            message=f"{field} must be a valid, dialable phone number.",
        )
    return canonical


__all__: list[str] = ["canonicalize_phone", "is_plausible_nanp", "to_e164"]
