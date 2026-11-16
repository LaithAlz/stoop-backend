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
import {
  phoneErrorMessage,
  phoneLooksLikeUnrepairedTrunkZero,
  phoneLooksValid,
  toE164,
  validatePhone,
} from "../phone";

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

  describe("#303: allowlist extension (18 newly-verified countries)", () => {
    it.each([
      ["south korea", "+82 (0)10 1234 5678", "+821012345678"],
      ["turkey", "+90 (0)532 123 4567", "+905321234567"],
      ["ukraine", "+380 (0)44 123 4567", "+380441234567"],
      ["croatia", "+385 (0)1 234 5678", "+38512345678"],
      ["slovenia", "+386 (0)1 234 5678", "+38612345678"],
      ["serbia", "+381 (0)11 234 5678", "+381112345678"],
      ["indonesia", "+62 (0)812 3456 789", "+628123456789"],
      ["malaysia", "+60 (0)12 345 6789", "+60123456789"],
      ["thailand", "+66 (0)81 234 5678", "+66812345678"],
      ["philippines", "+63 (0)917 123 4567", "+639171234567"],
      ["vietnam", "+84 (0)912 345 678", "+84912345678"],
      ["nigeria", "+234 (0)803 123 4567", "+2348031234567"],
      ["kenya", "+254 (0)712 345 678", "+254712345678"],
      ["pakistan", "+92 (0)300 1234567", "+923001234567"],
      ["bangladesh", "+880 (0)1712 345678", "+8801712345678"],
      ["morocco", "+212 (0)612 345678", "+212612345678"],
      ["ghana", "+233 (0)24 123 4567", "+233241234567"],
      ["sri lanka", "+94 (0)71 234 5678", "+94712345678"],
    ])("%s drops its trunk zero", (_label, raw, expected) => {
      expect(toE164(raw)).toBe(expected);
    });

    it("Brazil (55) was checked and deliberately left off the list", () => {
      // Brazil's domestic long-distance dialing prefix is "0" + a
      // carrier-selection code, not the simple trunk zero this rule
      // assumes (see apps/api/app/phone.py's module docstring,
      // "Allowlist extension (#303, 2026-08-05)"), left exactly as
      // before this issue, same as any other unlisted country.
      expect(toE164("+55 (0)21 91234 5678")).toBe("+55021912345678");
    });
  });
});

describe("toE164 - #304: a leading zero right after '+' is never valid E.164", () => {
  it("rejects when there are no digits between '+' and the parenthesized '(0)'", () => {
    // The exact first example from the issue: the trunk-zero rule never
    // matches (no country-code digits to anchor on), and the result used
    // to pass the international branch's digit-count-only check even
    // though a country code can never be empty or "0".
    expect(toE164("+ (0)20 7946 0958")).toBeNull();
  });

  it("rejects when the country code is not real and not on the allowlist", () => {
    // The exact second example from the issue: "044" is not a real
    // country code, so the trunk-zero rule leaves it alone, and the
    // result used to pass the international branch's digit-count-only
    // check (14 digits, 8-15) even though nothing can dial "+0...".
    expect(toE164("+044 (0)20 7946 0958")).toBeNull();
  });

  it("rejects a plain (non-parenthesized) leading zero too", () => {
    expect(toE164("+0207946 0958")).toBeNull();
  });

  it("does not fire when an allowlisted trunk zero was already dropped", () => {
    // The UK's own trunk zero is dropped first, leaving digits that start
    // with "44", never "0": a real, allowlisted country code is never
    // mistaken for a leading zero.
    expect(toE164("+44 (0)20 7946 0958")).toBe("+442079460958");
  });

  it("does not fire for a retained-zero country's real trunk zero", () => {
    // Italy is NOT on the allowlist, so its trunk zero is never dropped,
    // so `digits` starts with "3" (from "39"), never "0": confirms #304
    // and #303's allowlist compose correctly.
    expect(toE164("+39 (0)6 6982 1234")).toBe("+390669821234");
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

describe("phoneLooksLikeUnrepairedTrunkZero - issue #299", () => {
  it("flags the exact motivating case: a pre-#277-migrated UK row", () => {
    // migration 0017 canonicalized this row with the OLD normalizer, so
    // it is stored exactly as "+4402079460958": toE164 alone leaves it
    // unchanged (13 digits, 8-15), so only this function catches it.
    expect(phoneLooksLikeUnrepairedTrunkZero("+4402079460958")).toBe(true);
    expect(toE164("+4402079460958")).toBe("+4402079460958");
  });

  it("flags a newly-allowlisted country's own un-repaired shape", () => {
    expect(phoneLooksLikeUnrepairedTrunkZero("+8201012345678")).toBe(true);
  });

  it("does not flag a correctly-canonicalized number", () => {
    expect(phoneLooksLikeUnrepairedTrunkZero("+442079460958")).toBe(false);
    expect(phoneLooksLikeUnrepairedTrunkZero("+14165551234")).toBe(false);
  });

  it("does not flag a retained-zero country's real number", () => {
    // "+390669821234" is a perfectly good Italian number: Italy is not
    // on TRUNK_ZERO_COUNTRY_ALLOWLIST, so this is never flagged.
    expect(phoneLooksLikeUnrepairedTrunkZero("+390669821234")).toBe(false);
    expect(phoneLooksLikeUnrepairedTrunkZero("+3780549882345")).toBe(false);
  });

  it("does not flag a country nobody has vetted yet (fails closed, not open)", () => {
    // Not a real country code, and not on the allowlist: "we don't know"
    // is read as "don't flag it", the same posture #303's own allowlist
    // takes in the other direction.
    expect(phoneLooksLikeUnrepairedTrunkZero("+9990123456789")).toBe(false);
  });

  it("does not flag a non-'+' value", () => {
    expect(phoneLooksLikeUnrepairedTrunkZero("4402079460958")).toBe(false);
  });
});
