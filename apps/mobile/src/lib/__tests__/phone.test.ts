/**
 * `toE164`/`phoneLooksValid` normalization matrix — issue #269. Every case
 * mirrors apps/api/tests/test_phone.py's own matrix so the mobile client
 * and the server are provably testing the SAME policy (the F2/F3/N1/R1/R3/
 * R4 safety-review history referenced in ../phone.ts's header, plus the
 * 2026-08-03 non-ASCII-digit finding both sides now share).
 */
import { phoneLooksValid, toE164 } from "../phone";

describe("toE164 — accepts", () => {
  it.each([
    ["4165551234", "+14165551234"],
    ["14165551234", "+14165551234"],
    ["+14165551234", "+14165551234"],
    ["(416) 555-1234", "+14165551234"],
    ["416-555-1234", "+14165551234"],
    ["416.555.1234", "+14165551234"],
    ["  416 555 1234  ", "+14165551234"],
    ["+1 (416) 555-1234", "+14165551234"],
    ["+44 20 7946 0958", "+442079460958"],
    ["+442079460958", "+442079460958"],
  ])("normalizes %s to %s", (raw, expected) => {
    expect(toE164(raw)).toBe(expected);
  });
});

describe("toE164 — rejects", () => {
  it.each([
    "",
    "   ",
    "n/a",
    "same as before",
    "-",
    // A dropped digit — 9-digit NANP, no plus.
    "416555013",
    // A dropped digit WITH a plus (N1: the international escape hatch
    // must not disable the NANP gate for NANP numbers).
    "+1416555013",
    // An extension appended — digit-soup that fails every shape check.
    "416-555-0134 x22",
    // N11 service code as the area code.
    "9115551234",
    // N11 service code as the exchange.
    "4169115234",
    // Too short for the international length bound (no leading "1", so
    // this hits the international branch, not NANP).
    "+2345678",
    // Too long even for the international length bound.
    "+3312345678901234",
    // A bare "+" with nothing else (R1 regression: must never emit a bare
    // "+" onto the emergency-call field).
    "+",
    // Non-ASCII digit (safety review 2026-08-03, finding 1): one
    // Arabic-Indic "١" ahead of an otherwise ordinary Toronto number must
    // never be silently stripped/coerced into "+14165551234".
    "+١4165551234",
    // All-Arabic-Indic-digit rendering of a UK number.
    "+٤٤٢٠٧٩٤٦٠٩٥٨",
    // Extended Arabic-Indic (Persian/Urdu) digit, no leading "+" — the
    // bare-10-digit branch must not silently drop it either.
    "416555۴234",
    // Fullwidth digit rendering.
    "４１６５５５１２３４",
    // CJK ideographic numeral.
    "一4165551234",
  ])("rejects %s", (raw) => {
    expect(toE164(raw)).toBeNull();
  });
});

describe("toE164 — never emits a bare plus (R1 regression)", () => {
  it.each(["+", "416555013422", "416-555-0134 x22", "n/a", ""])(
    "%s is either null or a real dialable value",
    (raw) => {
      const result = toE164(raw);
      if (result !== null) {
        expect(result.startsWith("+")).toBe(true);
        expect(result.length).toBeGreaterThanOrEqual(9);
      }
    },
  );
});

describe("phoneLooksValid", () => {
  it("blank is valid (keep current / leave unset)", () => {
    expect(phoneLooksValid("")).toBe(true);
    expect(phoneLooksValid("   ")).toBe(true);
  });

  it("agrees with toE164 for a dialable value", () => {
    expect(phoneLooksValid("(416) 555-1234")).toBe(true);
    expect(phoneLooksValid("+1 416 555 1234")).toBe(true);
  });

  it("agrees with toE164 for junk / non-ASCII-digit input", () => {
    expect(phoneLooksValid("n/a")).toBe(false);
    expect(phoneLooksValid("416-555-0134 x22")).toBe(false);
    expect(phoneLooksValid("+١4165551234")).toBe(false);
  });
});
