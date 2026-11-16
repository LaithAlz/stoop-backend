/**
 * NANP-aware phone normalization, shared across every form that writes a
 * dialable phone number to the API. Ported from apps/web/src/lib/phone.ts
 * (itself moved out of apps/web/src/features/account/profileEdit.ts —
 * campaign issue #234 PR 5, five safety-review rounds — F2/F3/N1/R1/R3/R4
 * below) — issue #269 found mobile's onboarding backup-contact step, the
 * tenant form, and the Me tab's own profile edit each reimplementing a
 * DIGIT-COUNT-ONLY version of this check and sending the raw,
 * un-normalized string. Client and server must never disagree about what
 * is dialable — this is the same policy as apps/api/app/phone.py's
 * `to_e164`.
 *
 * The non-ASCII-digit guard below is shaped to match apps/web/src/lib/
 * phone.ts as of #273 (commit 155118a) — see that guard's own comment for
 * why it's a fail-CLOSED allowlist, not a denylist.
 *
 * Every phone-bearing field this app writes — `landlords.phone`
 * (src/features/account/profileEdit.ts), `backup_contact.phone`
 * (src/app/onboarding/backup.tsx), `tenants.phone`
 * (src/features/tenants/TenantFormModal.tsx) — imports from here instead
 * of defining its own copy.
 */

/**
 * Non-ASCII characters that are UNAMBIGUOUSLY not digits, and so may
 * appear in a pasted phone string and be stripped as punctuation: letters
 * (`\p{L}` — "Célular:", "携帯", "моб."), spaces (`\p{Zs}` — NBSP and
 * narrow-NBSP, the copy-off-a-webpage case), format controls (`\p{Cf}` —
 * LRM/RLM from an RTL paste, word joiners, BOM), punctuation (`\p{P}` —
 * en/em dashes from Word autocorrect, non-breaking hyphen, smart quotes)
 * and symbols (`\p{S}`).
 *
 * #273 (safety review): this is an ALLOWLIST, and the guard below rejects
 * any non-ASCII code point NOT in it. A first cut (this file's own
 * pre-#273 version) was the inverse — a denylist of "characters that might
 * be digits" (`\p{Nd}` plus a hand-picked CJK set) — which fails OPEN
 * against a moving standard: the client sends `toE164`'s OUTPUT, not the
 * landlord's raw input, so the server never sees the offending character
 * on a client write and this guard is the ONLY line of defense. Any digit
 * codepoint newer than the device's own JS engine's Unicode table would
 * fall out of `\p{Nd}` and be silently stripped exactly as before #273
 * (measured on web: an engine at Unicode 14 reintroduces the bug for the
 * Kawi and Nag Mundari digit blocks) — a real spread on React Native,
 * across OS/engine versions. Failing closed also retires the
 * hand-maintained CJK-numeral-only denylist this file used to carry,
 * which never tracked anything beyond the two concrete finding-1 examples
 * (Arabic-Indic, fullwidth) plus a short CJK list — narrower than what
 * `apps/api/app/phone.py`'s `str.isnumeric()` actually rejects
 * server-side.
 *
 * The CJK numerals below are carved BACK OUT of `\p{L}`, not retired:
 * they are General_Category `Lo` (Letter), so an allowlist that trusts
 * `\p{L}` wholesale would hand `一`/`〇` straight back through (verified
 * against apps/web/src/lib/phone.ts's own #273 safety-review history,
 * which caught exactly this on a first attempt — `U+20001` sailed
 * through). Everything else the old denylist enumerated is now covered by
 * the fail-closed default, so this list no longer has to track new digit
 * blocks; it only has to track CJK numerals, which is a closed set.
 */
const NON_ASCII_ALLOWED_RE = /[\p{L}\p{Zs}\p{Cf}\p{P}\p{S}]/u;

/** CJK ideographic numerals — `Lo`, so exempted from the `\p{L}` allowlist
 *  above. Same literal set as apps/web/src/lib/phone.ts's `CJK_NUMERALS_RE`
 *  (diff-verified there against Python's `isnumeric()` across every
 *  codepoint 0..0x10FFFF). */
const CJK_NUMERALS_RE =
  /[\u{3405}\u{3483}\u{382A}\u{3B4D}\u{4E00}\u{4E03}\u{4E07}\u{4E09}\u{4E5D}\u{4E8C}\u{4E94}\u{4E96}\u{4EBF}\u{4EC0}\u{4EDF}\u{4EE8}\u{4F0D}\u{4F70}\u{5104}\u{5146}\u{5169}\u{516B}\u{516D}\u{5341}\u{5343}\u{5344}\u{5345}\u{534C}\u{53C1}\u{53C2}\u{53C3}\u{53C4}\u{56DB}\u{58F1}\u{58F9}\u{5E7A}\u{5EFE}\u{5EFF}\u{5F0C}\u{5F0D}\u{5F0E}\u{5F10}\u{62FE}\u{634C}\u{67D2}\u{6F06}\u{7396}\u{767E}\u{8086}\u{842C}\u{8CAE}\u{8CB3}\u{8D30}\u{9621}\u{9646}\u{964C}\u{9678}\u{96F6}\u{F96B}\u{F973}\u{F978}\u{F9B2}\u{F9D1}\u{F9D3}\u{F9FD}\u{20001}\u{20064}\u{200E2}\u{20121}\u{2092A}\u{20983}\u{2098C}\u{2099C}\u{20AEA}\u{20AFD}\u{20B19}\u{22390}\u{22998}\u{23B1B}\u{2626D}\u{2F890}]/u;

/**
 * `true` iff *value* contains a non-ASCII character that isn't clearly
 * punctuation-or-letter — i.e. anything that might be a digit, including
 * codepoints this engine's Unicode tables don't know yet.
 *
 * Iterates by Unicode code point (`for...of` on a string), not UTF-16 code
 * unit, so a supplementary-plane character is tested whole rather than as
 * two broken surrogate halves. (Neither regex carries a `g` flag: a global
 * regex keeps `lastIndex` across `.test()` calls and would alternate
 * results on repeated input.)
 */
function containsNonAsciiDigit(value: string): boolean {
  for (const ch of value) {
    if (ch.codePointAt(0)! <= 0x7f) continue;
    if (CJK_NUMERALS_RE.test(ch)) return true;
    if (!NON_ASCII_ALLOWED_RE.test(ch)) return true;
  }
  return false;
}

/**
 * #277: anchored to the start of the string — "the country code" means
 * whatever digit run sits between "+" and the parenthesized "(0)", nothing
 * more. 1-3 digits mirrors E.164's own country-code length bound. Requires
 * the LITERAL "(0)" (not just any parenthesized digit), which is what
 * keeps "+1 (416) 555 0100" (an area code, not a trunk marker) untouched.
 * Mirrors apps/api/app/phone.py's `_PARENTHESIZED_TRUNK_ZERO_RE` (and
 * apps/web/src/lib/phone.ts's copy of it). The separator set below is why
 * that claim is true rather than aspirational: see its comment.
 */
// The separator class is written out CHARACTER BY CHARACTER rather than as
// `\s` on purpose. JavaScript's `\s` is Unicode-aware even WITHOUT the `u`
// flag, while Python's under `re.ASCII` is not, so the two engines disagree
// on exactly the input this rule exists for: "+44\u00a0(0)20 7946 0958",
// the shape you get pasting a UK number off a web page that wrote it with
// `&nbsp;`. Under `\s` the client would drop the trunk zero and the server
// would not, which is the same Python-vs-JavaScript regex-semantics trap
// that #232/#260 already cost us once. The separator CLASS below (not the
// leading `.trim()` call in `toE164` — see that function's own comment) is,
// spelled out, literally the same set in all three files. The hyphen sits
// LAST so it is a literal, not a range operator.
//
// Follow-up (adversarial safety review, 2026-08-04): this class had a hole
// — it enumerated U+2011/U+2012/U+2013/U+2014 but skipped the character
// literally named HYPHEN (U+2010), which is what a typographically correct
// web page or Word's autocorrect actually produces, plus U+2015 HORIZONTAL
// BAR / U+2212 MINUS SIGN / U+FF0D FULLWIDTH HYPHEN-MINUS (what a CJK IME
// emits) and a bare line feed/carriage return (a line-wrapped paste off an
// email signature). A miss here doesn't throw anything — the rule this
// class exists for just silently fails to fire, storing the same
// undialable-but-length-plausible value as before #277. All six are now
// included.
//
// Zero-width / bidi format characters — U+00AD SOFT HYPHEN, U+200B ZWSP,
// U+2060 WORD JOINER, U+FEFF (BOM / ZERO WIDTH NO-BREAK SPACE), U+200E LRM,
// U+200F RLM — are invisible on screen, so a landlord who pastes
// "+44\u200e(0)20 7946 0958" (an RTL paste that picked up a stray LRM)
// has no way to see, let alone remove, the character breaking the match.
// DELIBERATE DECISION: treat them as separators (permit them here) rather
// than let the rule silently fail to fire on them — the alternative fails
// exactly the audience #276 exists for, invisibly, for a character they
// cannot see or type around. None of the six is a digit, so permitting
// them here never widens what counts as a literal "0".
const TRUNK_ZERO_SEPARATORS =
  " \t\n\r\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000" +
  "\u00ad\u200b\u2060\ufeff\u200e\u200f" +
  "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d-";
const PARENTHESIZED_TRUNK_ZERO_RE = new RegExp(
  `^(\\+\\d{1,3})[${TRUNK_ZERO_SEPARATORS}]*\\(0\\)[${TRUNK_ZERO_SEPARATORS}]*`,
);

/**
 * #277 follow-up (adversarial safety review, 2026-08-04, BLOCKING): an
 * ALLOWLIST of country codes where the trunk zero IS dropped internationally
 * — not a skip list of the ones (Italy 39, San Marino 378, Vatican City 379,
 * Cote d'Ivoire 225 — all retain it) where it is not. A skip list fails
 * OPEN: the next country nobody has thought about gets its number silently
 * mangled next. This allowlist fails CLOSED: an unlisted country code keeps
 * the pre-#277 status quo (punctuation-stripped only, no digit dropped),
 * never a new bug. Adding a country here is a deliberate act — check that
 * country's own numbering plan first, don't guess. Byte-identical to
 * apps/api/app/phone.py's `_TRUNK_ZERO_COUNTRY_ALLOWLIST` and
 * apps/web/src/lib/phone.ts's copy of this same constant.
 */
const TRUNK_ZERO_COUNTRY_ALLOWLIST = new Set([
  "44",
  "49",
  "33",
  "31",
  "32",
  "41",
  "43",
  "45",
  "46",
  "47",
  "48",
  "351",
  "353",
  "61",
  "64",
  "27",
  "91",
  "81",
  "86",
  "7",
  "20",
  "30",
  "36",
  "40",
  "420",
  "421",
  // #303 (2026-08-05): each verified to drop its national trunk prefix "0"
  // internationally, i.e. the national significant number itself never
  // starts with "0". See apps/api/app/phone.py's module docstring,
  // "Allowlist extension (#303, 2026-08-05)", for the full rationale and
  // for the one candidate (Brazil, 55) deliberately left out.
  "82", // South Korea
  "90", // Turkey
  "380", // Ukraine
  "385", // Croatia
  "386", // Slovenia
  "381", // Serbia
  "62", // Indonesia
  "60", // Malaysia
  "66", // Thailand
  "63", // Philippines
  "84", // Vietnam
  "234", // Nigeria
  "254", // Kenya
  "92", // Pakistan
  "880", // Bangladesh
  "212", // Morocco
  "233", // Ghana
  "94", // Sri Lanka
]);

/**
 * Drop a leading trunk `0` written parenthesized directly after the
 * country code (`"+44\u00a0(0)20 7946 0958"` -> `"+4420 7946 0958"`) — the
 * single most common written form of a UK number, previously normalizing
 * to a 13-digit non-number Twilio rejects (21211) on the field the
 * emergency chain dials. Must be called on the RAW (punctuation-intact)
 * string, before any digit stripping — once digits are collapsed a real
 * `(0)` can no longer be told apart from an ordinary digit `0`. A no-op
 * when the pattern doesn't match (e.g. `"+1 (416) 555 0100"`, where the
 * parenthesized content is an area code, not a literal `"0"`), a genuine
 * un-parenthesized leading `0` (e.g. `"+44 020 7946 0958"`, intentionally
 * left alone — narrower "option 1" fix only, issue #277), OR the captured
 * country code is not on `TRUNK_ZERO_COUNTRY_ALLOWLIST` (e.g.
 * `"+39 (0)6 6982 1234"` — Italy retains its trunk zero internationally;
 * dropping it here would produce an undialable number).
 */
function dropParenthesizedTrunkZero(value: string): string {
  const match = PARENTHESIZED_TRUNK_ZERO_RE.exec(value);
  if (match === null) return value;
  const countryCode = match[1].slice(1); // strip the leading "+"
  if (!TRUNK_ZERO_COUNTRY_ALLOWLIST.has(countryCode)) return value;
  return value.replace(PARENTHESIZED_TRUNK_ZERO_RE, "$1");
}

/**
 * Best-effort E.164 for the NANP inputs these forms actually see. Anything
 * this can't confidently normalize is rejected by `phoneLooksValid` below
 * rather than sent — storing a string Twilio can't dial is strictly worse
 * than refusing the edit, because the failure surfaces at 2am instead of
 * here.
 */
export function toE164(phone: string): string | null {
  // KNOWN, NARROW divergence from apps/api/app/phone.py (flagged, not
  // fixed — adversarial safety review, 2026-08-04, item 4; out of #277's
  // scope): `String.prototype.trim()` DOES strip U+FEFF (BOM / ZERO WIDTH
  // NO-BREAK SPACE), while Python's `str.strip()` does not (it is
  // General_Category `Cf`, not whitespace, to Python). So
  // "\ufeff+44 (0)20 7946 0958" is accepted HERE (the BOM is trimmed away
  // before the "+" check) but would be rejected by the server if it ever
  // saw that exact raw string. This is the LESS strict side of a
  // fail-closed divergence (server is stricter, never the other way
  // around) and harmless today because every write path sends the server
  // this function's OUTPUT, never a landlord's raw pasted text. The
  // TRUNK_ZERO_SEPARATORS comment above claims the three files' separator
  // CLASSES are identical, which is still true; it does not claim the
  // three files' overall trim behavior is identical, which is not.
  const trimmed = phone.trim();
  // #273 (safety review, client-side half — BLOCKING): must run before ANY
  // other processing, including the digit-strip below. `.replace(/\D/g,
  // "")` without the `u` flag is ASCII-only, so a non-ASCII "digit"
  // (Arabic-Indic "١", CJK "一"/"〇", fullwidth "４", …) used to be treated
  // as punctuation and silently STRIPPED rather than rejected — shifting
  // the remaining digits into a DIFFERENT, still-plausible-looking number
  // instead of failing loudly: toE164("+١4165551234") used to return
  // "+4165551234", a number that is NOT the one the landlord typed,
  // silently stored on `landlords.phone` or `backup_contact.phone` — the
  // field the emergency escalation chain dials
  // (apps/api/app/agent/emergency_chain.py). Reject outright instead.
  if (containsNonAsciiDigit(trimmed)) return null;
  // R3 (safety re-verify): the +country test runs on the PUNCTUATION-
  // STRIPPED string, so "+44 20 7946 0958" is accepted the same as
  // "+442079460958" — a landlord with an international mobile shouldn't
  // have to guess our spacing rules.
  const plus = trimmed.startsWith("+");
  // #277: only the international branch, and only on the RAW (not-yet-
  // digit-stripped) string — a bare NANP input has no country code for
  // "(0)" to sit after, and once digits are collapsed a real "(0)" is
  // indistinguishable from a bare digit "0".
  const normalized = plus ? dropParenthesizedTrunkZero(trimmed) : trimmed;
  const digits = normalized.replace(/\D/g, "");
  if (plus) {
    // N1 (safety re-verify): the international escape hatch must NOT
    // disable the NANP gate for NANP numbers. "+1416555013" — one digit
    // dropped, the likeliest typo there is — was being accepted and
    // stored, while the same typo without the plus was correctly
    // rejected. A +1 number gets exactly the same scrutiny as a bare one.
    if (digits.startsWith("1")) {
      return digits.length === 11 && isPlausibleNanp(digits.slice(1)) ? `+${digits}` : null;
    }
    // #304: a country code never begins with "0", the one shape rule this
    // codebase enforces on a non-NANP international number without
    // needing to know anything else about the destination country.
    // Checked on `digits` (after any allowlisted trunk-zero drop above),
    // so a real, allowlisted country code is never mistaken for a leading
    // zero. Mirrors apps/api/app/phone.py's `to_e164`.
    if (digits.startsWith("0")) return null;
    return digits.length >= 8 && digits.length <= 15 ? `+${digits}` : null;
  }
  if (digits.length === 10 && isPlausibleNanp(digits)) return `+1${digits}`;
  if (digits.length === 11 && digits.startsWith("1") && isPlausibleNanp(digits.slice(1))) {
    return `+${digits}`;
  }
  // R1 (safety re-verify): returns NULL rather than `+${digits}`. The old
  // fallback could emit a bare "+" (or a digit-soup like "+416555013422"
  // from "416-555-0134 x22") onto the field emergency calls ring — it was
  // unreachable only because a guard in a THIRD file ran first, which is
  // exactly the validator/builder split that produced F2. Now no caller
  // can write an undialable value regardless of discipline.
  return null;
}

/**
 * #299: `toE164` (and its Python mirror, `app/phone.py::to_e164`) leaves a
 * "+<cc>0..." value UNCHANGED once it already has 8 to 15 digits, because
 * that shape passes the international branch's own length check with no
 * further shape validation. Rows canonicalized by migration 0017 before
 * #277 landed are stored exactly this way (e.g. "+4402079460958", a
 * 13-digit non-number Twilio rejects with 21211), so `toE164` alone can
 * never flag them after the fact. This function detects that STORED shape
 * given a value that already looks canonical. Mirrors
 * apps/web/src/lib/phone.ts's `phoneLooksLikeUnrepairedTrunkZero`.
 *
 * Reuses `TRUNK_ZERO_COUNTRY_ALLOWLIST` above (issue #299: "share the
 * #303 list rather than duplicating it") instead of maintaining a second,
 * separate "countries that retain a leading zero" list: a stored value is
 * only flagged when its own country-code prefix IS on that allowlist,
 * i.e. we are CONFIDENT that country drops its trunk zero
 * internationally, so a literal "0" sitting right after it can only be
 * the un-repaired pre-#277 shape, never a legitimate number. A country
 * NOT on the allowlist (Italy, or simply one nobody has vetted yet) is
 * never flagged, the same fail-closed, don't-guess posture #303's own
 * allowlist uses, applied here in reverse: `TRUNK_ZERO_COUNTRY_ALLOWLIST`
 * not containing a code is read as "we don't know", not "this is fine".
 *
 * Not wired to any mobile screen today (#299's own reported case is the
 * web dashboard's `backupContactPhoneLooksInvalid`). Kept in sync here
 * anyway, same as every other rule in this file, so mobile never
 * disagrees with the web/server definition of this shape if a mobile
 * screen ever needs it.
 */
export function phoneLooksLikeUnrepairedTrunkZero(phone: string): boolean {
  if (!phone.startsWith("+")) return false;
  const digits = phone.slice(1);
  for (let length = 1; length <= 3; length++) {
    const countryCode = digits.slice(0, length);
    if (TRUNK_ZERO_COUNTRY_ALLOWLIST.has(countryCode) && digits[length] === "0") {
      return true;
    }
  }
  return false;
}

/** R4: NANP area code and exchange both start 2-9, and neither is an N11
 *  service code (411/911/…), which is never assignable (N3) — cheap
 *  regex that catches a dropped or mistyped digit before it becomes a
 *  permanently un-ringable emergency number. Deliberately permissive
 *  about everything else: rejecting a real number here is its own
 *  failure, since this is the field a landlord uses to be reachable. */
function isPlausibleNanp(digits: string): boolean {
  return /^(?!\d11)[2-9]\d{2}(?!\d11)[2-9]\d{6}$/.test(digits);
}

/**
 * Small discriminated result for a phone field (issue #276). `toE164`
 * collapses every rejection into the same `null`, so a caller has no way
 * to tell "this can't possibly be a phone number" (a non-ASCII digit —
 * someone typing on an Arabic, Persian, or Devanagari keyboard, whose
 * screen already shows ten digits when the generic message tells them to
 * "use 10 digits") apart from any other unparsable shape. Blank is its
 * own `ok: true` case (`value: null`, "keep current" / "don't set one" —
 * the existing contract every call site already relies on) — it is not a
 * rejection.
 */
export type PhoneValidation =
  // `value: null` means "no change" (blank field, keep the current
  // number) — NOT "clear the number". A caller sending this straight to
  // an API body must omit the key on `null`, never send it as an
  // explicit clear (adversarial safety review, 2026-08-04, item 4).
  { ok: true; value: string | null } | { ok: false; reason: "non_ascii_digit" | "unparsable" };

/** Same rule as `toE164`, but reports WHY a non-blank value was rejected
 *  instead of collapsing every reason into `null` (issue #276). */
export function validatePhone(phone: string): PhoneValidation {
  const trimmed = phone.trim();
  if (trimmed.length === 0) return { ok: true, value: null };
  if (containsNonAsciiDigit(trimmed)) return { ok: false, reason: "non_ascii_digit" };
  const value = toE164(trimmed);
  return value === null ? { ok: false, reason: "unparsable" } : { ok: true, value };
}

/** Unchanged from before #276 — every rejection reason except
 *  `non_ascii_digit` still gets this line. */
export const PHONE_ERROR_UNPARSABLE =
  "Use 10 digits, 11 starting with 1, or + and your country code.";

/** #276, revised (adversarial safety review, 2026-08-04): names Stoop's
 *  own limitation rather than the landlord's mistake ("those aren't the
 *  digits" reads as a flat contradiction of what's on screen — their ten
 *  characters ARE digits, to them), spells the digits out one at a time
 *  so the line can't be misread as a repeat of the "use 10 digits" COUNT
 *  rule they already believe they're following, and gives a next step
 *  ("switching your keyboard") for the Arabic/Persian-keyboard case this
 *  string exists for, where there's often no obvious toggle. Byte
 *  -identical to apps/web/src/lib/phone.ts's copy of this string. */
export const PHONE_ERROR_NON_ASCII_DIGIT =
  "Stoop can only dial a number written with 0 1 2 3 4 5 6 7 8 9. Type it again with those digits, switching your keyboard if you need to.";

/**
 * The message a landlord should see for `phone`, or `null` when it's
 * valid (including blank). Every call site's old
 * `phoneLooksValid(x) ? null : "<generic line>"` pattern collapses to this
 * one call (issue #276).
 */
export function phoneErrorMessage(phone: string): string | null {
  const result = validatePhone(phone);
  if (result.ok) return null;
  return result.reason === "non_ascii_digit" ? PHONE_ERROR_NON_ASCII_DIGIT : PHONE_ERROR_UNPARSABLE;
}

/**
 * Blank is valid ("keep my current number" / "don't set one"). Anything
 * NON-blank must carry a dialable number.
 *
 * F2 (safety review, #234 PR 5): the old check counted digits only, so
 * zero-digit text — "n/a", "same as before", "-", exactly what a hurried
 * landlord types when the helper says "leave it blank to keep the number
 * already on file" — passed validation AND passed the builder's
 * `length > 0` send rule. The result was `landlords.phone = "n/a"`, a
 * green "Saved", and an emergency chain that silently never reaches
 * anyone. A non-empty field now has to look like a real number.
 *
 * Kept (issue #276 added `validatePhone`/`phoneErrorMessage` alongside
 * this, not instead of it) — existing callers and tests that only need a
 * yes/no answer still get one, now defined in terms of the same single
 * source of truth.
 */
export function phoneLooksValid(phone: string): boolean {
  return validatePhone(phone).ok;
}
