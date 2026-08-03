/**
 * Builds the `PATCH /v1/properties/{id}` body for the property settings
 * form (issue #261): `backup_contact` (the second phone in the emergency
 * escalation chain, apps/api/app/agent/emergency_chain.py — the
 * redundancy that covers a wrong/undialable primary number), `quiet_hours`,
 * and `house_rules`. All three are real, PATCH-able `properties` columns
 * per docs/03-engineering/api-contracts.md's Properties section.
 *
 * Deliberately narrower than what the endpoint accepts: `label`/
 * `address_line1`/`city`/`province`/`postal_code` and `heating_season` are
 * also documented PATCH fields, but this form doesn't expose them — #261
 * only asks for the three above, and this file never invents a field or
 * shape beyond the contract (left-out fields are reported in the PR, not
 * silently added).
 *
 * Same discipline as src/features/account/profileEdit.ts's
 * `buildMeUpdatePayload` (#234 PR 5, five safety-review rounds):
 * - Never an explicit empty string / null. Blank means "don't touch it".
 * - Phone is normalized via src/lib/phone.ts's `toE164` before it's ever
 *   sent — never the raw text.
 * - An unchanged value is omitted, so re-saving the form with nothing
 *   edited sends no field for it.
 *
 * One real difference from `buildMeUpdatePayload`: `landlords.phone`
 * (PATCH /v1/me) is write-only — GET never echoes it back, so that form
 * can't prefill or compare against the current value. `backup_contact`
 * and `quiet_hours` ARE both returned by GET/PATCH /v1/properties/{id},
 * so this form prefills both and can skip sending a value that hasn't
 * changed.
 *
 * Neither `backup_contact` nor `quiet_hours` can be explicitly CLEARED
 * back to null through this builder: `UpdatePropertyInput`'s fields are
 * typed as the full object (`BackupContact`/`QuietHours`), not
 * `| null` — matching api-contracts.md's documented PATCH body, which
 * gives no shape for "unset this". A landlord who blanks both fields in a
 * pair that was previously set is NOT clearing it — the pair is simply
 * left out of the payload and the existing value stays on the property.
 * The form surfaces this honestly (see `backupContactClearAttempted`/
 * `quietHoursClearAttempted` below) rather than silently no-op'ing like a
 * successful clear.
 */
import type { BackupContact, Property, QuietHours, UpdatePropertyInput } from "@/api/types";
import { toE164 } from "@/lib/phone";

export interface PropertySettingsForm {
  houseRules: string;
  backupName: string;
  backupPhone: string;
  /** `<input type="time">` value — "" or "HH:MM" (24-hour, no seconds),
   *  which is exactly api-contracts.md's `QuietHours` shape — no parsing
   *  library needed either direction. */
  quietStart: string;
  quietEnd: string;
}

export function propertySettingsFormFromProperty(property: Property): PropertySettingsForm {
  return {
    houseRules: property.house_rules ?? "",
    backupName: property.backup_contact?.name ?? "",
    backupPhone: property.backup_contact?.phone ?? "",
    quietStart: property.quiet_hours?.start ?? "",
    quietEnd: property.quiet_hours?.end ?? "",
  };
}

function sameBackupContact(current: BackupContact | null, candidate: BackupContact): boolean {
  return current !== null && current.name === candidate.name && current.phone === candidate.phone;
}

function sameQuietHours(current: QuietHours | null, candidate: QuietHours): boolean {
  return current !== null && current.start === candidate.start && current.end === candidate.end;
}

/**
 * Both fields blank is valid ("leave the backup contact as is"). Either
 * field alone is not — `BackupContact` has no optional half, and the
 * builder below can't send a name with no dialable phone or a phone with
 * no name. Mirrors src/features/account/profileEdit.ts's `phoneLooksValid`
 * shape for the phone half.
 */
export function backupContactError(form: PropertySettingsForm): string | null {
  const name = form.backupName.trim();
  const phone = form.backupPhone.trim();
  if (name.length === 0 && phone.length === 0) return null;
  if (phone.length === 0) return "Add their phone number too, or clear the name.";
  if (name.length === 0) return "Add their name too, or clear the phone number.";
  return toE164(phone) === null
    ? "Use 10 digits, 11 starting with 1, or + and your country code."
    : null;
}

/** Same "both or neither" shape as `backupContactError` — `QuietHours`
 *  has no optional half either. The browser's own `<input type="time">`
 *  can only ever produce a valid `HH:MM` or an empty string, so there's
 *  no format to validate beyond the pairing. */
export function quietHoursError(form: PropertySettingsForm): string | null {
  const start = form.quietStart.trim();
  const end = form.quietEnd.trim();
  if (start.length === 0 && end.length === 0) return null;
  if (start.length === 0) return "Set a start time too, or clear the end time.";
  if (end.length === 0) return "Set an end time too, or clear the start time.";
  return null;
}

/** True when blanking both fields would silently leave an EXISTING
 *  backup contact untouched rather than clearing it — the form has to say
 *  so instead of letting a deliberate "remove this" read as a no-op
 *  success. Only meaningful once `backupContactError` is already null
 *  (both blank, not one-sided). */
export function backupContactClearAttempted(
  form: PropertySettingsForm,
  current: Property,
): boolean {
  return (
    current.backup_contact !== null &&
    form.backupName.trim().length === 0 &&
    form.backupPhone.trim().length === 0
  );
}

export function quietHoursClearAttempted(form: PropertySettingsForm, current: Property): boolean {
  return (
    current.quiet_hours !== null &&
    form.quietStart.trim().length === 0 &&
    form.quietEnd.trim().length === 0
  );
}

/**
 * `null` when there is nothing to send (every field blank-or-unchanged) —
 * the caller skips the PATCH entirely rather than firing a no-op request.
 * Assumes `backupContactError`/`quietHoursError` are already checked
 * (null) by the caller; this never sends a one-sided pair.
 */
export function buildPropertySettingsPayload(
  form: PropertySettingsForm,
  current: Property,
): UpdatePropertyInput | null {
  const payload: UpdatePropertyInput = {};

  const houseRules = form.houseRules.trim();
  if (houseRules.length > 0 && houseRules !== (current.house_rules ?? "")) {
    payload.house_rules = houseRules;
  }

  const backupName = form.backupName.trim();
  const backupPhone = form.backupPhone.trim();
  if (backupName.length > 0 && backupPhone.length > 0) {
    const e164 = toE164(backupPhone);
    if (e164) {
      const candidate: BackupContact = { name: backupName, phone: e164 };
      if (!sameBackupContact(current.backup_contact, candidate)) {
        payload.backup_contact = candidate;
      }
    }
    // else: unnormalizable phone — blocked by backupContactError before
    // this is ever called; never sent regardless.
  }

  const quietStart = form.quietStart.trim();
  const quietEnd = form.quietEnd.trim();
  if (quietStart.length > 0 && quietEnd.length > 0) {
    const candidate: QuietHours = { start: quietStart, end: quietEnd };
    if (!sameQuietHours(current.quiet_hours, candidate)) {
      payload.quiet_hours = candidate;
    }
  }

  return Object.keys(payload).length > 0 ? payload : null;
}
