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
        # Adversarial safety review, 2026-08-04, item 2 \u2014 the class had a
        # hole: it enumerated U+2011/U+2012/U+2013/U+2014 but skipped the
        # character literally named HYPHEN, plus the other look-alike
        # dashes and a bare line break.
        pytest.param("\u2010", id="hyphen-the-one-literally-named-hyphen"),
        pytest.param("\u2015", id="horizontal-bar"),
        pytest.param("\u2212", id="minus-sign"),
        pytest.param("\uff0d", id="fullwidth-hyphen-minus-cjk-ime"),
        pytest.param("\n", id="line-feed"),
        pytest.param("\r", id="carriage-return"),
        # Zero-width / bidi format characters \u2014 invisible on screen, so a
        # landlord has no way to see or remove one. DELIBERATE DECISION:
        # permitted as separators (see the constant's own comment).
        pytest.param("\u00ad", id="soft-hyphen"),
        pytest.param("\u200b", id="zero-width-space"),
        pytest.param("\u2060", id="word-joiner"),
        pytest.param("\ufeff", id="bom-zero-width-no-break-space"),
        pytest.param("\u200e", id="left-to-right-mark-rtl-paste"),
        pytest.param("\u200f", id="right-to-left-mark"),
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
# to_e164 — #277 follow-up: country-code allowlist (adversarial safety
# review, 2026-08-04, BLOCKING). The trunk-zero drop is only correct for
# countries that actually drop the leading 0 internationally; Italy, San
# Marino, Vatican City, and (post-2021) Cote d'Ivoire all retain it, so
# the ungated rule turned a correct, dialable number into an undialable
# one. See app/phone.py's module docstring, "Country-code allowlist".
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_to_e164_uk_on_allowlist_still_has_trunk_zero_dropped() -> None:
    """The UK (44) IS on the allowlist — unchanged behavior, pinned so a
    future edit to the allowlist can't silently regress the exact case
    #277 was opened for."""
    assert to_e164("+44 (0)20 7946 0958") == "+442079460958"


@pytest.mark.unit
def test_to_e164_italy_not_on_allowlist_is_left_alone() -> None:
    """Italy (39) RETAINS its trunk zero internationally — libphonenumber
    carries a dedicated ``italian_leading_zero`` field for exactly this.
    The ungated rule turned this correct, dialable Rome number into
    ``"+39669821234"`` — undialable, on the field the escalation chain
    dials. Left alone, plain punctuation-stripping reproduces the correct
    value on its own."""
    assert to_e164("+39 (0)6 6982 1234") == "+390669821234"


@pytest.mark.unit
def test_to_e164_san_marino_not_on_allowlist_is_left_alone() -> None:
    """San Marino (378) also retains its trunk zero internationally."""
    assert to_e164("+378 (0)549 882345") == "+3780549882345"


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("416555\u03781234", id="unassigned-codepoint-U+0378"),
        pytest.param("4165551234\U00010d40", id="garay-digit-zero-U+10D40-unicode-16"),
    ],
)
def test_to_e164_rejects_a_codepoint_this_python_cannot_rule_out(value: str) -> None:
    """The guard is an ALLOWLIST of categories that are unambiguously not
    digits, not an ``isnumeric()`` denylist, and this is why.

    ``str.isnumeric()`` consults the Unicode table compiled into whichever
    CPython build is running. A genuine digit newer than that table (or a
    codepoint currently unassigned that becomes one) answers ``False``, the
    denylist passes it, and ``\\D`` under ``re.ASCII`` then strips it as
    ordinary punctuation. The remaining digits close up and the value
    stores as a DIFFERENT, still-plausible number, which is exactly the
    silent-drop hazard this guard exists to prevent, on the field the
    emergency chain dials.

    Measured against the denylist version: both of these parsed as
    ``"+14165551234"`` here while both TS mirrors rejected them, so the
    canonicalization AUTHORITY was the permissive one. An API-direct write
    never touches a browser, so this function is the only thing standing
    in front of it.
    """
    assert to_e164(value) is None


@pytest.mark.unit
def test_to_e164_vatican_city_not_on_allowlist_is_left_alone() -> None:
    """Vatican City (379) retains its trunk zero internationally too. Named
    in the allowlist docstring and both docs, but until now asserted
    nowhere (re-verify finding 1), which is how a country quietly gets
    added to the allowlist later by someone reading only the tests."""
    assert to_e164("+379 (0)6 698 12345") == "+3790669812345"


@pytest.mark.unit
def test_to_e164_cote_divoire_not_on_allowlist_is_left_alone() -> None:
    """Cote d'Ivoire (225), under its post-2021 ten-digit numbering plan,
    also retains its trunk zero internationally."""
    assert to_e164("+225 (0)1 23 45 67 89") == "+2250123456789"


# ---------------------------------------------------------------------------
# to_e164 - #303: allowlist extension (18 newly-verified countries)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("+82 (0)10 1234 5678", "+821012345678", id="south-korea"),
        pytest.param("+90 (0)532 123 4567", "+905321234567", id="turkey"),
        pytest.param("+380 (0)44 123 4567", "+380441234567", id="ukraine"),
        pytest.param("+385 (0)1 234 5678", "+38512345678", id="croatia"),
        pytest.param("+386 (0)1 234 5678", "+38612345678", id="slovenia"),
        pytest.param("+381 (0)11 234 5678", "+381112345678", id="serbia"),
        pytest.param("+62 (0)812 3456 789", "+628123456789", id="indonesia"),
        pytest.param("+60 (0)12 345 6789", "+60123456789", id="malaysia"),
        pytest.param("+66 (0)81 234 5678", "+66812345678", id="thailand"),
        pytest.param("+63 (0)917 123 4567", "+639171234567", id="philippines"),
        pytest.param("+84 (0)912 345 678", "+84912345678", id="vietnam"),
        pytest.param("+234 (0)803 123 4567", "+2348031234567", id="nigeria"),
        pytest.param("+254 (0)712 345 678", "+254712345678", id="kenya"),
        pytest.param("+92 (0)300 1234567", "+923001234567", id="pakistan"),
        pytest.param("+880 (0)1712 345678", "+8801712345678", id="bangladesh"),
        pytest.param("+212 (0)612 345678", "+212612345678", id="morocco"),
        pytest.param("+233 (0)24 123 4567", "+233241234567", id="ghana"),
        pytest.param("+94 (0)71 234 5678", "+94712345678", id="sri-lanka"),
    ],
)
def test_to_e164_newly_allowlisted_countries_drop_trunk_zero(raw: str, expected: str) -> None:
    assert to_e164(raw) == expected


@pytest.mark.unit
def test_to_e164_brazil_left_off_the_303_allowlist_deliberately() -> None:
    """Brazil (55) was checked and deliberately left out (see the module
    docstring, "Allowlist extension (#303, 2026-08-05)"): its domestic
    long-distance dialing prefix is "0" + a carrier-selection code, not the
    simple trunk zero this rule assumes, so a parenthesized "(0)" right
    after "55" is left exactly as before this issue, same as any other
    unlisted country."""
    assert to_e164("+55 (0)21 91234 5678") == "+55021912345678"


# ---------------------------------------------------------------------------
# to_e164 - #304: a leading zero right after "+" is never valid E.164
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_to_e164_rejects_leading_zero_with_no_country_code() -> None:
    """The exact first example from the issue: no digits between "+" and
    the parenthesized "(0)", so the trunk-zero rule never matches, and the
    result used to pass the international branch's digit-count-only check
    even though a country code can never be empty or "0"."""
    assert to_e164("+ (0)20 7946 0958") is None


@pytest.mark.unit
def test_to_e164_rejects_leading_zero_behind_a_fake_country_code() -> None:
    """The exact second example from the issue: "044" is not a real country
    code and is not on ``_TRUNK_ZERO_COUNTRY_ALLOWLIST``, so the trunk-zero
    rule leaves it alone, and the result used to pass the international
    branch's digit-count-only check (14 digits, 8-15) even though nothing
    can dial a value starting "+0"."""
    assert to_e164("+044 (0)20 7946 0958") is None


@pytest.mark.unit
def test_to_e164_rejects_bare_leading_zero_no_parens() -> None:
    """Not just the parenthesized-trunk-zero shape: any international
    value whose first digit is a plain "0" is undialable."""
    assert to_e164("+0207946 0958") is None


@pytest.mark.unit
def test_to_e164_leading_zero_check_runs_after_an_allowlisted_trunk_zero_drop() -> None:
    """A real, allowlisted country code is never mistaken for a leading
    zero: the UK's own trunk zero is dropped first, leaving digits that
    start with "44", not "0"."""
    assert to_e164("+44 (0)20 7946 0958") == "+442079460958"


@pytest.mark.unit
def test_to_e164_leading_zero_rejection_does_not_touch_retained_zero_countries() -> None:
    """Italy is NOT on the allowlist, so its trunk zero is never dropped,
    so ``digits`` starts with "3" (from "39"), never "0" -- the #304 rule
    never fires for it. Confirms #304 and #303's allowlist compose
    correctly rather than one silently undoing the other."""
    assert to_e164("+39 (0)6 6982 1234") == "+390669821234"


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
