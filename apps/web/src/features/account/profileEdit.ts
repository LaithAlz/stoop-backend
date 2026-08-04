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
 *
 * `toE164`/`phoneLooksValid` (the safety-reviewed NANP normalizer, five
 * rounds — F2/F3/N1/R1/R3/R4 below) moved to src/lib/phone.ts (issue
 * #261) so src/features/properties/settings.ts's `backup_contact` phone —
 * the same "undialable value silently stored" failure mode, one hop
 * further down the emergency chain — reuses this exact logic instead of a
 * second implementation.
 */
import type { UpdateMeInput } from "@/api/types";
import { toE164 } from "@/lib/phone";
export { phoneErrorMessage, phoneLooksValid } from "@/lib/phone";

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
