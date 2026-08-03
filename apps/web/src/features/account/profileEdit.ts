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
    payload.phone = phone;
  }

  return Object.keys(payload).length > 0 ? payload : null;
}

/** ≥10 digits reads as a real NANP number — same bar
 *  src/routes/app.properties_.add.tsx's area-code check and apps/mobile's
 *  own profileEdit.ts use. Blank is valid ("keep my current number"). */
export function phoneLooksValid(phone: string): boolean {
  const digits = phone.replace(/\D/g, "");
  return digits.length === 0 || digits.length >= 10;
}
