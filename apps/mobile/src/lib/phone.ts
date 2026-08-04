/**
 * NANP-aware phone normalization, shared across every form that writes a
 * dialable phone number to the API. Ported verbatim from
 * apps/web/src/lib/phone.ts (itself moved out of
 * apps/web/src/features/account/profileEdit.ts — campaign issue #234 PR 5,
 * five safety-review rounds — F2/F3/N1/R1/R3/R4 below) — issue #269 found
 * mobile's onboarding backup-contact step, the tenant form, and the Me
 * tab's own profile edit each reimplementing a DIGIT-COUNT-ONLY version of
 * this check and sending the raw, un-normalized string. Client and server
 * must never disagree about what is dialable — this is the same policy as
 * apps/api/app/phone.py's `to_e164` (ASCII-digits-only; that module's own
 * docstring covers the non-ASCII-digit rejection this TS version doesn't
 * need to, since JS's `\d`/`\D` are already ASCII-only, unlike Python's).
 *
 * Every phone-bearing field this app writes — `landlords.phone`
 * (src/features/account/profileEdit.ts), `backup_contact.phone`
 * (src/app/onboarding/backup.tsx), `tenants.phone`
 * (src/features/tenants/TenantFormModal.tsx) — imports from here instead
 * of defining its own copy.
 */

// Server-parity guard (issue #269, safety review 2026-08-03 finding 1):
// apps/api/app/phone.py rejects a raw value outright — rather than
// silently stripping it — if it contains ANY non-ASCII character Python
// considers numeric (Arabic-Indic, Devanagari, fullwidth digits, CJK
// ideographic numerals like "一"/"〇" via `str.isnumeric()`), because
// JS's `\D` below is already ASCII-only (unlike Python's default,
// Unicode-aware `\d`/`\D`) and would otherwise treat a non-ASCII digit as
// ordinary punctuation and drop it — silently shifting the remaining
// digits into a DIFFERENT, still-plausible-looking number rather than
// failing loudly. `\p{Nd}` (Unicode "decimal digit") mirrors the server's
// Arabic-Indic/Devanagari/fullwidth coverage exactly (the concrete cases
// apps/api/tests/test_phone.py checks); the CJK numeral set below closes
// the one gap `\p{Nd}` itself doesn't cover (those characters are Unicode
// category Lo, not Nd, but still `str.isnumeric()`-true server-side) —
// this app has no non-ASCII-digit test case beyond what the server's own
// suite verifies, so this list is illustrative parity, not a claim of
// full Unicode Numeric_Type coverage.
const CJK_NUMERAL_RE = /[〇一二三四五六七八九十百千万億兆]/u;

function containsNonAsciiDigit(value: string): boolean {
  for (const ch of value) {
    if (/\p{Nd}/u.test(ch) && !/^[0-9]$/.test(ch)) return true;
  }
  return CJK_NUMERAL_RE.test(value);
}

/**
 * Best-effort E.164 for the NANP inputs these forms actually see. Anything
 * this can't confidently normalize is rejected by `phoneLooksValid` below
 * rather than sent — storing a string Twilio can't dial is strictly worse
 * than refusing the edit, because the failure surfaces at 2am instead of
 * here.
 */
export function toE164(phone: string): string | null {
  // Checked before anything else, including `.trim()` — never silently
  // stripped/ignored, same as the server (see the guard's own comment
  // above).
  if (containsNonAsciiDigit(phone)) return null;
  const trimmed = phone.trim();
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
 */
export function phoneLooksValid(phone: string): boolean {
  const trimmed = phone.trim();
  if (trimmed.length === 0) return true;
  // Single source of truth with the normalizer (R1): valid means exactly
  // "toE164 can produce something dialable", so the two can never disagree
  // the way the pre-F2 pair did.
  return toE164(trimmed) !== null;
}
