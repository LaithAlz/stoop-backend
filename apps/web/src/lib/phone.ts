/**
 * NANP-aware phone normalization, shared across every form that writes a
 * dialable phone number to the API. Moved out of
 * src/features/account/profileEdit.ts (campaign issue #234 PR 5, five
 * safety-review rounds — F2/F3/N1/R1/R3/R4 below) verbatim, unchanged
 * logic, so this exact reviewed behavior is reused rather than
 * reimplemented (issue #261: `backup_contact`'s phone gets the same
 * scrutiny as `landlords.phone` did there — it's the second number the
 * emergency chain dials, apps/api/app/agent/emergency_chain.py).
 * src/features/account/profileEdit.ts now imports from here instead of
 * defining its own copy.
 */

/**
 * CJK ideographic numerals (simple and financial/"trusted" forms across
 * Chinese/Japanese/Korean — e.g. 一二三四五六七八九十百千万億, plus their
 * CJK Compatibility Ideograph and Extension-B variants). Python's
 * `str.isnumeric()` (apps/api/app/phone.py's `_contains_non_ascii_digit`,
 * the server-side authority this file must not diverge from — #232/#260,
 * #273) treats these as numeric because Unicode gives them a
 * Numeric_Type=Numeric property, but their General_Category is `Lo`
 * (Letter, other), NOT one of the Number categories (`Nd`/`Nl`/`No`) — so
 * a JS `\p{N}` Unicode-property-escape test alone MISSES them; JS regex
 * has no Numeric_Type escape to fall back on. This literal list closes
 * that gap. It was generated (and diff-verified against Python's
 * `ch.isnumeric() and not ch.isascii()` across every codepoint
 * 0..0x10FFFF, zero misses) rather than hand-picked — see #273's PR
 * description for the generation script.
 */
const NON_ASCII_NUMERIC_RE =
  /[\p{Nd}\p{Nl}\p{No}\u{3405}\u{3483}\u{382A}\u{3B4D}\u{4E00}\u{4E03}\u{4E07}\u{4E09}\u{4E5D}\u{4E8C}\u{4E94}\u{4E96}\u{4EBF}\u{4EC0}\u{4EDF}\u{4EE8}\u{4F0D}\u{4F70}\u{5104}\u{5146}\u{5169}\u{516B}\u{516D}\u{5341}\u{5343}\u{5344}\u{5345}\u{534C}\u{53C1}\u{53C2}\u{53C3}\u{53C4}\u{56DB}\u{58F1}\u{58F9}\u{5E7A}\u{5EFE}\u{5EFF}\u{5F0C}\u{5F0D}\u{5F0E}\u{5F10}\u{62FE}\u{634C}\u{67D2}\u{6F06}\u{7396}\u{767E}\u{8086}\u{842C}\u{8CAE}\u{8CB3}\u{8D30}\u{9621}\u{9646}\u{964C}\u{9678}\u{96F6}\u{F96B}\u{F973}\u{F978}\u{F9B2}\u{F9D1}\u{F9D3}\u{F9FD}\u{20001}\u{20064}\u{200E2}\u{20121}\u{2092A}\u{20983}\u{2098C}\u{2099C}\u{20AEA}\u{20AFD}\u{20B19}\u{22390}\u{22998}\u{23B1B}\u{2626D}\u{2F890}]/u;

/**
 * `true` iff *value* contains a character that is numeric (by the test
 * above — a JS-reachable equivalent of Python's `str.isnumeric()`) but is
 * not plain ASCII `0`-`9`.
 *
 * Iterates by Unicode code point (`for...of` on a string), not UTF-16 code
 * unit, so a supplementary-plane character (e.g. the Extension-B ideograph
 * U+20001, above) is tested whole rather than as two broken surrogate
 * halves.
 */
function containsNonAsciiDigit(value: string): boolean {
  for (const ch of value) {
    if (ch.codePointAt(0)! > 0x7f && NON_ASCII_NUMERIC_RE.test(ch)) return true;
  }
  return false;
}

/**
 * Best-effort E.164 for the NANP inputs these forms actually see. Anything
 * this can't confidently normalize is rejected by `phoneLooksValid` below
 * rather than sent — storing a string Twilio can't dial is strictly worse
 * than refusing the edit, because the failure surfaces at 2am instead of
 * here.
 */
export function toE164(phone: string): string | null {
  const trimmed = phone.trim();
  // #273 (safety review, 2026-08-03 finding 1, client-side half — BLOCKING):
  // must run before ANY other processing, including the digit-strip below.
  // `.replace(/\D/g, "")` without the `u` flag is ASCII-only, so a
  // non-ASCII "digit" (Arabic-Indic "١", CJK "一"/"〇", fullwidth "４", …)
  // used to be treated as punctuation and silently STRIPPED rather than
  // rejected — shifting the remaining digits into a DIFFERENT, still-
  // plausible-looking number instead of failing loudly:
  // toE164("+١4165551234") used to return "+4165551234", a number that is
  // NOT the one the landlord typed, silently stored on `landlords.phone`
  // or `backup_contact.phone` — the field the emergency escalation chain
  // dials (apps/api/app/agent/emergency_chain.py). Reject outright
  // instead, mirroring apps/api/app/phone.py's `_contains_non_ascii_digit`
  // exactly (verified above) so the client never accepts what the server
  // would 422 on, and never silently rewrites what the server would
  // accept.
  if (containsNonAsciiDigit(trimmed)) return null;
  // R3 (safety re-verify): the +country test runs on the PUNCTUATION-
  // STRIPPED string, so "+44 20 7946 0958" is accepted the same as
  // "+442079460958" — a landlord with an international mobile shouldn't
  // have to guess our spacing rules.
  const plus = trimmed.startsWith("+");
  const digits = trimmed.replace(/\D/g, "");
  if (plus) {
    // N1 (safety re-verify): the international escape hatch must NOT
    // disable the NANP gate for NANP numbers. "+1416555013" — one digit
    // dropped, the likeliest typo there is — was being accepted and
    // stored, while the same typo without the plus was correctly
    // rejected. A +1 number gets exactly the same scrutiny as a bare one.
    if (digits.startsWith("1")) {
      return digits.length === 11 && isPlausibleNanp(digits.slice(1)) ? `+${digits}` : null;
    }
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
 * Blank is valid ("keep my current number"). Anything NON-blank must carry
 * a dialable number.
 *
 * F2 (safety review, #234 PR 5): the old check counted digits only, so
 * zero-digit text — "n/a", "same as before", "-", exactly what a hurried
 * landlord types when the helper says "leave it blank to keep the number
 * already on file" — passed validation AND passed the builder's
 * `length > 0` send rule. The result was `landlords.phone = "n/a"`, a
 * green "Saved", and an emergency chain that silently never reaches
 * anyone. A non-empty field now has to look like a real number.
 */
export function phoneLooksValid(phone: string): boolean {
  const trimmed = phone.trim();
  if (trimmed.length === 0) return true;
  // Single source of truth with the normalizer (R1): valid means exactly
  // "toE164 can produce something dialable", so the two can never disagree
  // the way the pre-F2 pair did.
  return toE164(trimmed) !== null;
}
