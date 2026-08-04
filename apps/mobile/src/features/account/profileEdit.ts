/**
 * Builds the `PATCH /v1/me` body from the Me tab's edit form — pure and
 * unit-tested (src/features/account/__tests__/profileEdit.test.ts).
 *
 * Contract discipline (api-contracts.md "Me" + v1.9 amendment):
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
 * rounds — F2/F3/N1/R1/R3/R4, ported from apps/web/src/lib/phone.ts) now
 * live at src/lib/phone.ts (issue #269) — this file's own digit-count-only
 * `phoneLooksValid` sent the raw, un-normalized string to `landlords.phone`
 * (the number the emergency chain calls first), exactly the F2/R1 bug the
 * web side already closed. src/app/onboarding/backup.tsx and
 * src/features/tenants/TenantFormModal.tsx reuse the same module instead of
 * each defining their own copy.
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
    // #269: NORMALIZED, never the raw text — schema-v1.md documents
    // `landlords.phone` as E.164 and the emergency chain hands it straight
    // to Twilio's `create_call(to=...)`, which rejects anything else. An
    // unnormalizable number is never sent — the screen blocks this earlier
    // via `phoneLooksValid`, but the builder must be safe on its own too.
    const e164 = toE164(phone);
    if (e164) payload.phone = e164;
  }

  return Object.keys(payload).length > 0 ? payload : null;
}
