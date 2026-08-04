"""Unit tests for app.phone — the E.164 canonicalization authority (#232,
#260). Pure functions, no I/O, no DB — every case mirrors
``apps/web/src/features/account/profileEdit.ts``'s own ``toE164`` test
matrix (that file's F2/F3/N1/R1/R3/R4 safety-review history) so client and
server are provably testing the SAME policy.
"""

from __future__ import annotations

import pytest

from app.errors import AppError
from app.phone import canonicalize_phone, is_plausible_nanp, to_e164

# ---------------------------------------------------------------------------
# to_e164 — accept cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Bare NANP 10-digit -> assume +1.
        ("4165551234", "+14165551234"),
        # 11-digit, leading 1 -> +1-prefixed.
        ("14165551234", "+14165551234"),
        # Already +-prefixed NANP -> punctuation-stripped passthrough.
        ("+14165551234", "+14165551234"),
        # Punctuated NANP forms all normalize to the same canonical value.
        ("(416) 555-1234", "+14165551234"),
        ("416-555-1234", "+14165551234"),
        ("416.555.1234", "+14165551234"),
        ("  416 555 1234  ", "+14165551234"),
        # +1 with punctuation.
        ("+1 (416) 555-1234", "+14165551234"),
        # Non-NANP international: punctuation-stripped, digit-count only.
        ("+44 20 7946 0958", "+442079460958"),
        ("+442079460958", "+442079460958"),
        # #277: the single most common WRITTEN form of a UK number — a
        # parenthesized trunk zero directly after the country code — is
        # dropped, not carried through into a 13-digit non-number.
        ("+44 (0)20 7946 0958", "+442079460958"),
        # Same rule, no separators / hyphen separators / doubled spaces.
        ("+44(0)2079460958", "+442079460958"),
        ("+44-(0)20-7946-0958", "+442079460958"),
        ("+44  (0)  20 7946 0958", "+442079460958"),
    ],
)
def test_to_e164_accepts(raw: str, expected: str) -> None:
    assert to_e164(raw) == expected


# ---------------------------------------------------------------------------
# to_e164 — #277: parenthesized trunk zero
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_to_e164_parenthesized_trunk_zero_dropped_after_country_code() -> None:
    """The exact case from the issue: the UK's single most common written
    form must normalize to a real, dialable number, not the 13-digit
    non-number Twilio rejects (21211)."""
    assert to_e164("+44 (0)20 7946 0958") == "+442079460958"


@pytest.mark.unit
@pytest.mark.parametrize(
    "separator",
    [
        pytest.param("\u00a0", id="nbsp"),
        pytest.param("\u2009", id="thin-space"),
        pytest.param("\u202f", id="narrow-nbsp"),
        pytest.param("\u3000", id="ideographic-space"),
        pytest.param("\u2013", id="en-dash"),
        pytest.param("\u2011", id="non-breaking-hyphen"),
    ],
)
def test_to_e164_trunk_zero_rule_sees_unicode_separators(separator: str) -> None:
    """The separator class is spelled out character by character precisely
    so this holds. Python's ``\\s`` under ``re.ASCII`` is ASCII-only while
    JavaScript's is Unicode-aware even without the ``u`` flag, so writing
    it as ``[\\s-]`` made the browser drop the trunk zero here and the
    server keep it. A UK number pasted off a web page that wrote it with
    ``&nbsp;`` is the realistic input, not a contrived one, and the same
    Python-vs-JavaScript regex-semantics gap already cost us #232/#260.

    The mirror of this test lives in apps/mobile/src/lib/__tests__/
    phone.test.ts. Both must agree, byte for byte, on every case here.
    """
    assert to_e164(f"+44{separator}(0)20 7946 0958") == "+442079460958"


@pytest.mark.unit
def test_to_e164_parenthesized_area_code_is_not_a_trunk_zero() -> None:
    """`+1 (416) 555 0100` must be untouched: those parentheses hold an
    area code, not a trunk marker — the rule only fires on the LITERAL
    parenthesized "0", never any other parenthesized digit run."""
    assert to_e164("+1 (416) 555 0100") == "+14165550100"


@pytest.mark.unit
def test_to_e164_unparenthesized_leading_zero_is_left_alone() -> None:
    """A genuine leading zero that was NOT parenthesized is out of scope
    for this fix (option 1 only, issue #277) — still passes straight
    through to the international branch's own digit-count check, exactly
    as before this change."""
    assert to_e164("+44 020 7946 0958") == "+4402079460958"


@pytest.mark.unit
def test_to_e164_parenthesized_trunk_zero_only_applies_to_plus_branch() -> None:
    """A bare (non-"+") NANP input has no country code for "(0)" to sit
    after — the trunk-zero regex is anchored on a leading "+", so this
    stays rejected exactly as before this change, not silently normalized
    to "+14165551234" by treating the "(0)" as a trunk marker."""
    assert to_e164("(0)4165551234") is None


# ---------------------------------------------------------------------------
# to_e164 — reject cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "n/a",
        "same as before",
        "-",
        # A dropped digit — 9-digit NANP, no plus.
        "416555013",
        # A dropped digit WITH a plus (N1: the international escape hatch
        # must not disable the NANP gate for NANP numbers).
        "+1416555013",
        # An extension appended — digit-soup that fails every shape check.
        "416-555-0134 x22",
        # N11 service code as the area code.
        "9115551234",
        # N11 service code as the exchange.
        "4169115234",
        # Too short for the international length bound (does not start
        # with "1", so this hits the international branch, not NANP).
        "+2345678",
        # Too long even for the international length bound (16 digits,
        # does not start with "1").
        "+3312345678901234",
        # A bare "+" with nothing else (R1 regression: must never emit a
        # bare "+" onto the emergency-call field).
        "+",
        # Safety-review finding: Python's \d/\D are Unicode-aware by
        # default (unlike JavaScript's, always ASCII-only) — one
        # Arabic-Indic "١" (digit one) ahead of an otherwise ordinary
        # Toronto number must never be silently stripped/coerced into
        # "+14165551234" (mismatched vs. what a caller actually sent) or
        # accepted with the non-ASCII digit still embedded.
        "+١4165551234",
        # All-Arabic-Indic-digit rendering of "+442079460958" (a UK
        # number) — same class of rejection.
        "+٤٤٢٠٧٩٤٦٠٩٥٨",
    ],
)
def test_to_e164_rejects(raw: str) -> None:
    assert to_e164(raw) is None


@pytest.mark.unit
def test_to_e164_rejects_non_ascii_digit_even_when_otherwise_well_formed() -> None:
    """Same finding, no leading ``+`` — the bare-10-digit branch must not
    silently drop a single non-ASCII digit character either."""
    assert to_e164("416555۴234") is None  # ۴ = extended arabic-indic 4


@pytest.mark.unit
def test_to_e164_never_emits_bare_plus() -> None:
    """R1 (mirrors the TS safety-review regression note): no input should
    ever produce a lone ``"+"`` or otherwise un-dialable value — every
    non-``None`` result starts with ``"+"`` followed by at least 8 digits."""
    for raw in ["+", "416555013422", "416-555-0134 x22", "n/a", ""]:
        result = to_e164(raw)
        if result is not None:
            assert result.startswith("+") and len(result) >= 9


# ---------------------------------------------------------------------------
# is_plausible_nanp
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("digits", "expected"),
    [
        ("4165551234", True),
        ("2125551234", True),
        # Area code starting with 0/1 is never assignable.
        ("0165551234", False),
        ("1165551234", False),
        # Area code is an N11 service code.
        ("9115551234", False),
        # Exchange code is an N11 service code.
        ("4169115234", False),
        # Exchange code starting with 0/1.
        ("4160551234", False),
        ("4161551234", False),
    ],
)
def test_is_plausible_nanp(digits: str, expected: bool) -> None:
    assert is_plausible_nanp(digits) is expected


# ---------------------------------------------------------------------------
# canonicalize_phone — the raising wrapper every write path calls
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_canonicalize_phone_returns_canonical_form() -> None:
    assert canonicalize_phone("(416) 555-1234", field="phone") == "+14165551234"


@pytest.mark.unit
def test_canonicalize_phone_raises_invalid_field_on_uncanonicalizable() -> None:
    with pytest.raises(AppError) as exc_info:
        canonicalize_phone("n/a", field="phone")
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_field"
    assert "phone" in exc_info.value.message


@pytest.mark.unit
def test_canonicalize_phone_error_message_uses_field_name_not_value() -> None:
    """The 422 message must never echo the submitted value back (rule #5
    territory — a malformed phone-shaped string is still PII-adjacent) —
    only the static field name appears."""
    with pytest.raises(AppError) as exc_info:
        canonicalize_phone("garbage-value-xyz", field="backup_contact.phone")
    assert "garbage-value-xyz" not in exc_info.value.message
    assert "backup_contact.phone" in exc_info.value.message
