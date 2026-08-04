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
 * `house_rules` is NOT "blank means don't touch" — it's the one field of
 * the three actually consumed downstream (verbatim into the draft prompt,
 * `apps/api/app/agent/nodes/draft_response.py`), and `UpdatePropertyInput.
 * house_rules?: string` (not `| null`) already accepts an empty string,
 * which the backend writes straight through. Blanking it and saving DOES
 * clear it (B4, safety review): the earlier `length > 0` guard silently
 * dropped a deliberate clear from the payload, so a landlord who wiped a
 * retracted house rule and saved got either a false "Saved" or "Nothing
 * to update." while the old text quietly reappeared from the next server
 * echo — with Stoop still quoting the retracted rule to tenants.
 *
 * `quiet_hours` still can NOT be explicitly cleared back to null through
 * this builder: `UpdatePropertyInput.quiet_hours` is typed as the full
 * `QuietHours` object, not `| null`, matching api-contracts.md's
 * documented PATCH body, which gives no shape for "unset this" on it. A
 * landlord who blanks both quiet-hours fields on a previously-set pair is
 * NOT clearing it — the pair is simply left out of the payload and the
 * existing value stays on the property (see `quietHoursClearAttempted`
 * below, which the form uses to say so honestly).
 *
 * `backup_contact` is DIFFERENT (#268, api-contracts.md's v1.25
 * amendment): `UpdatePropertyInput.backup_contact` is typed `| null`, and
 * an explicit `null` genuinely clears the stored contact — verified
 * end-to-end against the real column (`_backup_phone` reads it back as
 * Python `None`, not the string `"null"`; see that amendment and
 * `apps/api/tests/test_properties_router.py`'s step-1 precondition test).
 * A landlord blanking both `backupName`/`backupPhone` on a previously-set
 * contact IS clearing it: pass `confirmedClear: true` (only after the
 * caller has shown a real confirmation — this removes redundancy from the
 * emergency escalation chain's T+10m step AND from every T+20m+ repeat
 * cycle) and this builder sends
 * `backup_contact: null`. Without that flag, a blank-both pair is left out
 * of the payload exactly like the old behavior, so the caller can compute
 * `backupContactClearAttempted` first, show a confirm dialog, and only
 * call this again with `confirmedClear: true` once the landlord agrees.
 */
import type { BackupContact, Property, QuietHours, UpdatePropertyInput } from "@/api/types";
import { phoneErrorMessage, toE164 } from "@/lib/phone";

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

/**
 * #307: every field pulled out of `backup_contact`/`quiet_hours` here is
 * `String(x ?? "")`, not a bare `?? ""`: the coercion the type declares
 * (`BackupContact.phone: string`, `QuietHours.start/end: string`)
 * overstates what a legacy row can actually hold. `backup_contact` and
 * `quiet_hours` are both `jsonb` with no column-level shape check, and
 * `backup_contact.phone` was writable as a JSON *number* (not a string)
 * until the 2026-08-03 review closed that path client-side and #290 closed
 * it at the router (neither closes it for rows already written). A bare
 * `property.backup_contact?.phone ?? ""` passes that number straight into
 * `PropertySettingsForm.backupPhone` (typed `string`, but not actually one
 * at runtime), and every downstream consumer that calls a string method on
 * it (`backupContactError`'s `.trim()` here, `toE164`'s `.trim()` in
 * src/lib/phone.ts) throws, white-screening both the settings form (this
 * function runs on first render, unconditionally) and, via
 * `backupContactPhoneLooksInvalid` below, the property detail page too.
 *
 * Fixed HERE, at the one place the API response becomes form state, rather
 * than teaching every `.trim()` call site to defend itself: the same
 * "fix at the boundary" discipline as the #234 campaign's `QueueItem.
 * severity` fix. `backupName`/`quietStart`/`quietEnd` get the same
 * treatment as `backupPhone` even though this issue's reported crash was
 * phone-specific: `backupContactError` also calls `.trim()` on
 * `backupName`, and `quietHoursError` also calls `.trim()` on
 * `quietStart`/`quietEnd`, unconditionally, on every render, so a legacy
 * row with a numeric `name` or a numeric quiet-hours boundary would
 * white-screen exactly the same way. `houseRules` is untouched: it's a
 * plain `text` column (schema-v1.md), not a field inside one of these
 * untyped `jsonb` blobs, so it has no equivalent legacy-shape risk here.
 */
export function propertySettingsFormFromProperty(property: Property): PropertySettingsForm {
  return {
    houseRules: property.house_rules ?? "",
    backupName: String(property.backup_contact?.name ?? ""),
    backupPhone: String(property.backup_contact?.phone ?? ""),
    quietStart: String(property.quiet_hours?.start ?? ""),
    quietEnd: String(property.quiet_hours?.end ?? ""),
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
 * shape for the phone half. The phone message itself comes from
 * `phoneErrorMessage` (issue #276) so a non-ASCII-digit input gets its own
 * line here too, not just on the account screen.
 */
export function backupContactError(form: PropertySettingsForm): string | null {
  const name = form.backupName.trim();
  const phone = form.backupPhone.trim();
  if (name.length === 0 && phone.length === 0) return null;
  if (phone.length === 0) return "Add their phone number too, or clear the name.";
  if (name.length === 0) return "Add their name too, or clear the phone number.";
  return phoneErrorMessage(phone);
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

/**
 * #268: confirm-dialog copy for actually removing a backup contact — shown
 * only when `backupContactClearAttempted` is true, i.e. right before the
 * caller sends `buildPropertySettingsPayload(form, current, {confirmedClear:
 * true})`. States the real, concrete consequence (what stops happening on
 * the emergency escalation chain's T+10m step and its T+20m+ repeat
 * cycles, `apps/api/app/agent/emergency_chain.py`) rather than a generic
 * "are you sure" — this removes
 * redundancy on the emergency line, not an ordinary settings edit.
 * `contactName` is `current.backup_contact.name`, always present here since
 * callers only reach this once `backupContactClearAttempted` is true (which
 * itself requires `current.backup_contact !== null`).
 */
export function backupContactClearTitle(contactName: string): string {
  return `Remove ${contactName} as your backup contact?`;
}

export const BACKUP_CONTACT_CLEAR_MESSAGE =
  "Right now, if you don't answer during a real emergency, I call and text them about ten " +
  "minutes later. They get what happened, your tenant's name, and a link that can stop the " +
  "whole alert. After this, no one else gets contacted. I'll just keep calling you, every " +
  "fifteen minutes, until you answer.";

export const BACKUP_CONTACT_CLEAR_CONFIRM_LABEL = "Remove backup contact";

/**
 * M1 (safety review): a `backup_contact` already stored with an
 * undialable phone (a pre-`toE164` row, or one written by a path that
 * never validated) is invisible until the landlord happens to open this
 * form and re-submit — the settings form only prefills it, and the
 * property detail page renders the contact's name without ever looking
 * at `.phone`, so both screens silently assert a redundancy that may not
 * exist. Callers on BOTH screens run this against the loaded/current
 * `backup_contact` (not the live, still-being-typed form values, which
 * already get their own `backupContactError`) and show a persistent
 * warning when it's `true`.
 *
 * #307: `typeof contact.phone !== "string"` is checked FIRST and
 * separately, not folded into a `String(contact.phone ?? "")` coercion
 * before the `toE164` call. Two reasons, both load-bearing:
 *
 * - Crash: this function runs directly against the RAW
 *   `Property.backup_contact` on both screens (see above: never against
 *   `PropertySettingsForm`, so `propertySettingsFormFromProperty`'s own
 *   `String(...)` coercion doesn't cover it). A legacy row with a numeric
 *   `phone` (writable until the 2026-08-03 review closed that path
 *   client-side; #290 closes it at the router going forward, but neither
 *   fixes rows already written) reaches `toE164` here un-coerced and
 *   throws at its `.trim()`, white-screening the property detail page.
 * - Correctness: coercing the number to a string first would be wrong, not
 *   just crash-safe. `String(4165550134)` is `"4165550134"`, a real
 *   10-digit NANP number that `toE164` normalizes successfully, so a naive
 *   coercion would report this row as REACHABLE. The backend disagrees:
 *   `_backup_phone` (apps/api/app/agent/emergency_chain.py) requires
 *   `isinstance(phone, str)` and treats any non-string `phone` (a JSON
 *   number, or an explicit JSON `null` on the `phone` key) as "no backup
 *   contact configured" (`skipped`, `reason: "no_backup_contact"`),
 *   regardless of what digits it might spell out. This function has to
 *   agree with the code that actually places the call, not with what the
 *   value would mean if it had been written as a string.
 */
export function backupContactPhoneLooksInvalid(contact: BackupContact | null): boolean {
  if (contact === null) return false;
  if (typeof contact.phone !== "string") return true;
  return toE164(contact.phone) === null;
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
 *
 * `confirmedClear` (#268): the caller must set this `true` only after
 * showing a real confirmation for the specific consequence of clearing
 * `backup_contact` (it removes the emergency escalation chain's backup
 * call/text entirely, at T+10m and in every T+20m+ repeat cycle) — see `backupContactClearAttempted`. Without
 * it, a blank-both backup-contact pair is treated as "leave it alone",
 * same as before this issue.
 */
export function buildPropertySettingsPayload(
  form: PropertySettingsForm,
  current: Property,
  options: { confirmedClear?: boolean } = {},
): UpdatePropertyInput | null {
  const payload: UpdatePropertyInput = {};

  // B4: no `.length > 0` guard here — a blank house_rules that differs
  // from the current value is a deliberate clear, not "don't touch it"
  // (see the module docstring above). `UpdatePropertyInput.house_rules`
  // already accepts `""`.
  //
  // LOW (safety review): `current.house_rules` is also `.trim()`ed before
  // the comparison — without that, a stored value with incidental
  // surrounding whitespace (this builder always trims before sending, but
  // nothing guarantees every past/future writer does) compared unequal to
  // the trimmed form value on every Save, firing a write — and an
  // audit_log row — for an edit that never happened.
  const houseRules = form.houseRules.trim();
  if (houseRules !== (current.house_rules ?? "").trim()) {
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
  } else if (options.confirmedClear && backupContactClearAttempted(form, current)) {
    // #268: an explicitly confirmed clear of a previously-set contact —
    // the one case this builder sends a real `null`, never guessed at.
    payload.backup_contact = null;
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
