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
import { phoneErrorMessage, phoneLooksValid, toE164, validatePhone } from "../phone";

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
    // #277: the single most common WRITTEN form of a UK number — a
    // parenthesized trunk zero directly after the country code — is
    // dropped, not carried through into a 13-digit non-number Twilio
    // rejects (21211).
    ["+44 (0)20 7946 0958", "+442079460958"],
    ["+44(0)2079460958", "+442079460958"],
    ["+44-(0)20-7946-0958", "+442079460958"],
    ["+44  (0)  20 7946 0958", "+442079460958"],
  ])("normalizes %s to %s", (raw, expected) => {
    expect(toE164(raw)).toBe(expected);
  });
});

describe("toE164 — #277: parenthesized trunk zero", () => {
  it("drops a leading trunk 0 parenthesized directly after the country code", () => {
    expect(toE164("+44 (0)20 7946 0958")).toBe("+442079460958");
  });

  it("leaves an area code's parentheses alone — not a trunk marker", () => {
    // Those parentheses hold "416" (an area code), not a literal "0" —
    // the rule only fires on the literal parenthesized "0".
    expect(toE164("+1 (416) 555 0100")).toBe("+14165550100");
  });

  // The mirror of apps/api/tests/test_phone.py's
  // test_to_e164_trunk_zero_rule_sees_unicode_separators. JavaScript's `\s`
  // is Unicode-aware even WITHOUT the `u` flag and Python's under
  // `re.ASCII` is not, so writing the separator class as `[\s-]` made the
  // client drop the trunk zero here and the server keep it. A UK number
  // pasted off a web page that wrote it with `&nbsp;` is the realistic
  // input, not a contrived one. Both suites must agree on every case.
  it.each([
    ["nbsp", "\u00a0"],
    ["thin space", "\u2009"],
    ["narrow nbsp", "\u202f"],
    ["ideographic space", "\u3000"],
    ["en dash", "\u2013"],
    ["non-breaking hyphen", "\u2011"],
    // Adversarial safety review, 2026-08-04, item 2 — the class had a
    // hole: it enumerated U+2011/U+2012/U+2013/U+2014 but skipped the
    // character literally named HYPHEN, plus the other look-alike dashes
    // and a bare line break.
    ["hyphen (the one literally named HYPHEN)", "\u2010"],
    ["horizontal bar", "\u2015"],
    ["minus sign", "\u2212"],
    ["fullwidth hyphen-minus (a CJK IME's hyphen)", "\uff0d"],
    ["line feed (a line-wrapped email-signature paste)", "\n"],
    ["carriage return", "\r"],
    // Zero-width / bidi format characters — invisible on screen, so a
    // landlord has no way to see or remove one. DELIBERATE DECISION:
    // permitted as separators (see the constant's own comment).
    ["soft hyphen", "\u00ad"],
    ["zero-width space", "\u200b"],
    ["word joiner", "\u2060"],
    ["BOM / zero-width no-break space", "\ufeff"],
    ["left-to-right mark (an RTL paste)", "\u200e"],
    ["right-to-left mark", "\u200f"],
  ])("the trunk-zero rule sees a %s separator, same as the server", (_label, sep) => {
    expect(toE164(`+44${sep}(0)20 7946 0958`)).toBe("+442079460958");
  });

  it("leaves a genuine, un-parenthesized leading 0 alone (option 1 only)", () => {
    // Out of scope for this fix — still passes straight through to the
    // international branch's own digit-count check, exactly as before.
    expect(toE164("+44 020 7946 0958")).toBe("+4402079460958");
  });

  it("never fires outside the '+'-prefixed branch", () => {
    expect(toE164("(0)4165551234")).toBeNull();
  });

  describe("country-code allowlist (adversarial safety review, 2026-08-04, BLOCKING)", () => {
    it("the UK (on the allowlist) still has its trunk zero dropped", () => {
      expect(toE164("+44 (0)20 7946 0958")).toBe("+442079460958");
    });

    it("Italy (NOT on the allowlist) is left alone — libphonenumber's italian_leading_zero case", () => {
      // Italy RETAINS the leading zero when dialed from abroad. The
      // ungated rule turned this correct, dialable Rome number into
      // "+39669821234" — undialable, on the field the escalation chain
      // dials.
      expect(toE164("+39 (0)6 6982 1234")).toBe("+390669821234");
    });

    it("San Marino (NOT on the allowlist) is left alone", () => {
      expect(toE164("+378 (0)549 882345")).toBe("+3780549882345");
    });

    // Named in the allowlist comment and in both docs, but asserted
    // nowhere until now (re-verify finding 1), which is how a country
    // quietly gets added later by someone reading only the tests.
    it("Vatican City (NOT on the allowlist) is left alone", () => {
      expect(toE164("+379 (0)6 698 12345")).toBe("+3790669812345");
    });

    it("Cote d'Ivoire (NOT on the allowlist, post-2021 numbering plan) is left alone", () => {
      expect(toE164("+225 (0)1 23 45 67 89")).toBe("+2250123456789");
    });
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

describe("validatePhone / phoneErrorMessage — issue #276", () => {
  it("blank is valid, with no value to write", () => {
    expect(validatePhone("")).toEqual({ ok: true, value: null });
    expect(validatePhone("   ")).toEqual({ ok: true, value: null });
    expect(phoneErrorMessage("")).toBeNull();
  });

  it("a dialable number is ok, carrying the normalized value", () => {
    expect(validatePhone("(416) 555-1234")).toEqual({ ok: true, value: "+14165551234" });
    expect(phoneErrorMessage("(416) 555-1234")).toBeNull();
  });

  it("a non-ASCII digit gets its own reason and its own message — not the generic line", () => {
    expect(validatePhone("+١4165551234")).toEqual({ ok: false, reason: "non_ascii_digit" });
    const message = phoneErrorMessage("+١4165551234");
    expect(message).not.toBeNull();
    expect(message).not.toBe("Use 10 digits, 11 starting with 1, or + and your country code.");
    // Copy revised (adversarial safety review, 2026-08-04): spells the
    // digits out one at a time rather than "0 to 9", which reads as a
    // repeat of the digit-COUNT rule this string exists to distinguish
    // itself from.
    expect(message).toBe(
      "Stoop can only dial a number written with 0 1 2 3 4 5 6 7 8 9. Type it again with those digits, switching your keyboard if you need to.",
    );
  });

  it("every other unparsable shape keeps the existing generic message", () => {
    expect(validatePhone("n/a")).toEqual({ ok: false, reason: "unparsable" });
    expect(phoneErrorMessage("n/a")).toBe(
      "Use 10 digits, 11 starting with 1, or + and your country code.",
    );
    expect(phoneErrorMessage("416-555-0134 x22")).toBe(
      "Use 10 digits, 11 starting with 1, or + and your country code.",
    );
  });
});
