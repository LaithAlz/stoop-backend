/**
 * Builds the `PATCH /v1/me` body from the account screen's edit form.
 * Ported near-verbatim from apps/mobile/src/features/account/profileEdit.ts
 * (campaign issue #234 PR 5) — same contract discipline:
 *
 * - Only documented fields ever appear (`full_name`, `phone` — this form
 *   doesn't edit `timezone`/`voice_profile`).
 * - NEVER an explicit null: the API 422s a null `phone` by design (it's
 *   the emergency-call target — clearing it must never happen by
 *   accident), so an empty phone field means "don't touch it", not
 *   "clear it".
 * - An unchanged name is omitted, so a phone-only edit doesn't re-write
 *   the name (and vice versa); returns null when there's nothing to send
 *   so the caller can skip the PATCH entirely.
 *
 * `phone` is write-only on this contract (GET /v1/me never returns it), so
 * the form can't prefill it — the screen says "leave blank to keep your
 * current number" and this builder enforces exactly that.
 */
import type { UpdateMeInput } from "@/api/types";

export interface ProfileEditForm {
  /** The name field's current text. */
  name: string;
  /** The phone field's current text — blank means "keep as is". */
  phone: string;
}

export function buildMeUpdatePayload(
  form: ProfileEditForm,
  current: { full_name: string | null },
): UpdateMeInput | null {
  const payload: UpdateMeInput = {};

  const name = form.name.trim();
  if (name.length > 0 && name !== (current.full_name ?? "")) {
    payload.full_name = name;
  }

  const phone = form.phone.trim();
  if (phone.length > 0) {
    // F3 (safety review, #234 PR 5): NORMALIZED, never the raw text.
    // schema-v1.md documents `landlords.phone` as E.164 and the emergency
    // chain hands it straight to Twilio's `create_call(to=...)`, which
    // rejects anything else (21211). The placeholder this form shows is
    // "(416) 555-0134", so the EXPECTED happy-path input was being stored
    // un-dialable — and `_execute_action` swallows a bad-number failure by
    // design, so the landlord's phone would simply never ring, forever,
    // with no error anywhere.
    const e164 = toE164(phone);
    // R1: an unnormalizable number is never sent. The form blocks this
    // earlier, but the builder must be safe on its own.
    if (e164) payload.phone = e164;
  }

  return Object.keys(payload).length > 0 ? payload : null;
}

/**
 * Best-effort E.164 for the NANP inputs this form actually sees. Anything
 * this can't confidently normalize is rejected by `phoneLooksValid` below
 * rather than sent — storing a string Twilio can't dial is strictly worse
 * than refusing the edit, because the failure surfaces at 2am instead of
 * here.
 */
export function toE164(phone: string): string | null {
  const trimmed = phone.trim();
  // R3 (safety re-verify): the +country test runs on the PUNCTUATION-
  // STRIPPED string, so "+44 20 7946 0958" is accepted the same as
  // "+442079460958" — a landlord with an international mobile shouldn't
  // have to guess our spacing rules.
  const plus = trimmed.startsWith("+");
  const digits = trimmed.replace(/\D/g, "");
  if (plus) return digits.length >= 8 && digits.length <= 15 ? `+${digits}` : null;
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

/** R4: NANP area code and exchange both start 2-9 — three characters of
 *  regex that catch a dropped or mistyped digit before it becomes a
 *  permanently un-ringable emergency number. */
function isPlausibleNanp(digits: string): boolean {
  return /^[2-9]\d{2}[2-9]\d{6}$/.test(digits);
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
