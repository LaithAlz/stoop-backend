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
    ],
)
def test_to_e164_accepts(raw: str, expected: str) -> None:
    assert to_e164(raw) == expected


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
    ],
)
def test_to_e164_rejects(raw: str) -> None:
    assert to_e164(raw) is None


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
