"""Unit tests for ``app/integrations/sms_segments.py`` (#111) — pure
functions, no I/O, no database. Covers the GSM-7/UCS-2 segment-counting
matrix (single-segment boundary, multi-part math, extended-table double
counting, UCS-2 fallback including astral-plane emoji) and the per-segment
cost estimate.
"""

from __future__ import annotations

import pytest

from app.integrations.sms_segments import (
    GSM7_MULTIPART_SEGMENT_LIMIT,
    GSM7_SINGLE_SEGMENT_LIMIT,
    SEGMENT_PRICE_USD_CENTS,
    UCS2_MULTIPART_SEGMENT_LIMIT,
    UCS2_SINGLE_SEGMENT_LIMIT,
    count_segments,
    estimate_sms_cost_cents,
)


class TestGsm7Boundary:
    def test_exactly_160_gsm7_chars_is_one_segment(self) -> None:
        result = count_segments("a" * GSM7_SINGLE_SEGMENT_LIMIT)
        assert result.encoding == "gsm7"
        assert result.units == 160
        assert result.segments == 1

    def test_161_gsm7_chars_rolls_to_two_segments(self) -> None:
        result = count_segments("a" * (GSM7_SINGLE_SEGMENT_LIMIT + 1))
        assert result.encoding == "gsm7"
        assert result.units == 161
        assert result.segments == 2

    def test_exactly_two_multipart_segments_worth(self) -> None:
        result = count_segments("a" * (GSM7_MULTIPART_SEGMENT_LIMIT * 2))
        assert result.segments == 2

    def test_one_over_two_multipart_segments_rolls_to_three(self) -> None:
        result = count_segments("a" * (GSM7_MULTIPART_SEGMENT_LIMIT * 2 + 1))
        assert result.segments == 3

    def test_empty_body_is_zero_segments(self) -> None:
        result = count_segments("")
        assert result.segments == 0
        assert result.units == 0


class TestGsm7Extended:
    """Extended-table characters (``^{}\\[]|€`` + form feed) cost DOUBLE
    against the same 160/153 GSM-7 limits — an ESCAPE + the character."""

    def test_extended_chars_count_double(self) -> None:
        result = count_segments("~")
        assert result.encoding == "gsm7"
        assert result.units == 2

    def test_80_extended_chars_is_exactly_one_segment_boundary(self) -> None:
        # 80 extended chars * 2 units each == 160 units == the single-segment
        # limit exactly.
        result = count_segments("~" * 80)
        assert result.units == 160
        assert result.segments == 1

    def test_81_extended_chars_rolls_to_two_segments(self) -> None:
        result = count_segments("~" * 81)
        assert result.units == 162
        assert result.segments == 2

    @pytest.mark.parametrize("char", list("^{}\\[~]|€"))
    def test_every_documented_extended_char_stays_gsm7(self, char: str) -> None:
        result = count_segments(f"hello {char}")
        assert result.encoding == "gsm7"


class TestUcs2Fallback:
    def test_single_emoji_forces_ucs2(self) -> None:
        result = count_segments("😀")
        assert result.encoding == "ucs2"

    def test_astral_emoji_counts_as_two_utf16_code_units(self) -> None:
        # U+1F600 is outside the Basic Multilingual Plane -- a real UCS-2
        # SMS PDU spends a surrogate PAIR (2 code units) on it, not 1.
        result = count_segments("😀")
        assert result.units == 2

    def test_accented_char_outside_gsm_repertoire_forces_ucs2(self) -> None:
        # 'ê' (circumflex e) is not in the small GSM-7 accented set (only
        # è/é/ì/ò/etc. from the specific default alphabet are).
        result = count_segments("ê")
        assert result.encoding == "ucs2"

    def test_exactly_70_ucs2_units_is_one_segment(self) -> None:
        result = count_segments("😀" * (UCS2_SINGLE_SEGMENT_LIMIT // 2))
        assert result.units == UCS2_SINGLE_SEGMENT_LIMIT
        assert result.segments == 1

    def test_71_ucs2_units_rolls_to_two_segments(self) -> None:
        body = "😀" * (UCS2_SINGLE_SEGMENT_LIMIT // 2) + "x"
        result = count_segments(body)
        assert result.units == UCS2_SINGLE_SEGMENT_LIMIT + 1
        assert result.segments == 2

    def test_multipart_ucs2_math(self) -> None:
        result = count_segments("x" * (UCS2_MULTIPART_SEGMENT_LIMIT * 3))
        assert result.encoding == "gsm7"  # plain ASCII 'x' stays GSM-7
        # Force UCS-2 by adding one non-GSM char, keeping the same length
        # class to exercise the UCS-2 multipart boundary specifically.
        forced = "x" * (UCS2_MULTIPART_SEGMENT_LIMIT * 3 - 1) + "ê"
        forced_result = count_segments(forced)
        assert forced_result.encoding == "ucs2"
        assert forced_result.units == UCS2_MULTIPART_SEGMENT_LIMIT * 3
        assert forced_result.segments == 3


class TestSingleNonGsmCharDropsWholeMessage:
    def test_one_bad_char_among_many_good_ones_is_still_ucs2(self) -> None:
        result = count_segments(("a" * 200) + "😀")
        assert result.encoding == "ucs2"


class TestCostEstimate:
    def test_one_segment(self) -> None:
        assert estimate_sms_cost_cents(1) == SEGMENT_PRICE_USD_CENTS

    def test_three_segments_is_linear(self) -> None:
        assert estimate_sms_cost_cents(3) == round(SEGMENT_PRICE_USD_CENTS * 3, 4)

    def test_zero_segments_is_zero_cost(self) -> None:
        assert estimate_sms_cost_cents(0) == 0.0
