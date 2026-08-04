/**
 * `toE164`/`phoneLooksValid` normalization matrix — issue #269. Every case
 * mirrors apps/api/tests/test_phone.py's own matrix so the mobile client
 * and the server are provably testing the SAME policy (the F2/F3/N1/R1/R3/
 * R4 safety-review history referenced in ../phone.ts's header, plus the
 * 2026-08-03 non-ASCII-digit finding both sides now share), and the
 * fail-closed-allowlist shape apps/web/src/lib/phone.ts landed for #273
 * (commit 155118a) — the "unassigned/newer-than-this-engine's-Unicode-
 * table codepoint still rejects" and "realistic paste still accepts"
 * cases below are that shape's own test matrix, ported.
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
    // #273: an unassigned codepoint (Cn, no General_Category letter/space/
    // format/punctuation/symbol) — not a digit under ANY Unicode version,
    // but also not on the allowlist, so it fails closed rather than being
    // silently stripped as "probably punctuation".
    `416555${String.fromCodePoint(0x0378)}1234`,
    // #273: a post-Unicode-15 digit (U+10D40, GARAY DIGIT ZERO — added in
    // Unicode 16.0). The point of the fail-closed allowlist is that this
    // rejects regardless of whether the running engine's Unicode table
    // already recognizes it as Nd or not — it's never Letter/Space/Format/
    // Punctuation/Symbol either way, so it can never silently pass.
    `416555${String.fromCodePoint(0x10d40)}1234`,
  ])("rejects %s", (raw) => {
    expect(toE164(raw)).toBeNull();
  });
});

describe("toE164 — #273: a realistic paste is still accepted (over-rejection is the failure that matters here)", () => {
  it.each([
    // NBSP and narrow NBSP — the copy-off-a-webpage case.
    [`416${String.fromCodePoint(0x00a0)}555${String.fromCodePoint(0x00a0)}1234`, "+14165551234"],
    [`416${String.fromCodePoint(0x202f)}555${String.fromCodePoint(0x202f)}1234`, "+14165551234"],
    // En dash, em dash, non-breaking hyphen — Word autocorrect / pasted
    // formatting.
    [`416${String.fromCodePoint(0x2013)}555${String.fromCodePoint(0x2013)}1234`, "+14165551234"],
    [`416${String.fromCodePoint(0x2014)}555${String.fromCodePoint(0x2014)}1234`, "+14165551234"],
    [`416${String.fromCodePoint(0x2011)}555${String.fromCodePoint(0x2011)}1234`, "+14165551234"],
    // LRM (RTL paste) and BOM (pasted from a file/webpage) — zero-width
    // format controls that legitimately ride along with pasted text.
    [`${String.fromCodePoint(0x200e)}416-555-1234`, "+14165551234"],
    [`${String.fromCodePoint(0xfeff)}416-555-1234`, "+14165551234"],
    // Labels in front of the number — ASCII, accented Latin, CJK, and
    // Cyrillic — must never block saving the number itself.
    ["Tel: 416-555-1234", "+14165551234"],
    ["Célular: 416-555-1234", "+14165551234"],
    ["携帯 416-555-1234", "+14165551234"],
    ["моб. 416 555 1234", "+14165551234"],
  ])("accepts %s", (raw, expected) => {
    expect(toE164(raw)).toBe(expected);
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
