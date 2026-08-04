/**
 * Builds the create/update payloads for TenantFormModal (issue #292) —
 * pure and unit-tested (src/features/tenants/__tests__/tenantForm.test.ts).
 *
 * Same discipline as src/features/account/profileEdit.ts's
 * `buildMeUpdatePayload` (mirrored on apps/web's `buildPropertySettingsPayload`,
 * apps/web/src/features/properties/settings.ts): an unchanged field is
 * never re-sent. `phone` is the load-bearing case here — schema-v1.md's
 * `tenants.phone` is now canonicalized/validated on every write
 * (#232/#260), so a legacy row holding an un-normalizable number (pre-
 * #232/#260 data) would 422 on ANY edit if the rest of the form always
 * resent the stale value alongside whatever the landlord actually
 * touched. That's the wrong failure most sharply for
 * `vulnerable_occupant`, which feeds severity classification — a tenant
 * can end up unable to be marked vulnerable because of an unrelated
 * stale phone. Omitting `phone` when the landlord didn't touch it is
 * what keeps that tenant editable at all.
 *
 * `name`/`unit`/`notes`/`vulnerable_occupant` keep their pre-#292
 * "include if present" behavior unchanged here — #292 is scoped to
 * `phone` only; none of those fields share `phone`'s validated-write
 * failure mode (no server-side canonicalization to fail against).
 */
import type { CreateTenantInput, Tenant, UpdateTenantInput, VulnerableOccupant } from "@/api/types";
import { toE164 } from "@/lib/phone";

export interface TenantForm {
  name: string;
  phone: string;
  unit: string;
  vulnerable: VulnerableOccupant | null;
  notes: string;
}

/**
 * True when the phone field is exactly what was loaded from `current` —
 * the one case a legacy, un-normalizable `tenants.phone` is let through
 * WITHOUT being (re-)validated or (re-)sent. Always `false` in create mode
 * (`current === null`), so a brand-new tenant's phone is validated exactly
 * as before #292 — this only relaxes the EDIT path.
 */
export function tenantPhoneUnchanged(formPhone: string, current: Tenant | null): boolean {
  return current !== null && formPhone.trim() === current.phone;
}

/** `name`/`unit`/`vulnerable_occupant`/`notes` — deliberately narrower than
 *  `UpdateTenantInput`'s own `vulnerable_occupant?: VulnerableOccupant |
 *  null` (this never sends an explicit `null`, only ever omits or sets a
 *  real value), which is what lets ONE shape assign into both
 *  `CreateTenantInput` (no `null` allowed at all) and `UpdateTenantInput`
 *  (allows `null`, just never sees one from here). */
interface SharedTenantFields {
  name?: string;
  unit?: string;
  vulnerable_occupant?: VulnerableOccupant;
  notes?: string;
}

function sharedFields(form: TenantForm): SharedTenantFields {
  const fields: SharedTenantFields = {};
  const name = form.name.trim();
  if (name) fields.name = name;
  const unit = form.unit.trim();
  if (unit) fields.unit = unit;
  if (form.vulnerable) fields.vulnerable_occupant = form.vulnerable;
  const notes = form.notes.trim();
  if (notes) fields.notes = notes;
  return fields;
}

/**
 * `POST /v1/properties/{id}/tenants` body. `phone` is required on create
 * (api-contracts.md's Tenants & Vendors section) and always validated —
 * there is no "current" row to compare against in create mode. Returns
 * `null` only if the phone can't be normalized; callers gate Save on the
 * form's own `phoneError` first, so this is a safety net, not the primary
 * check.
 */
export function buildTenantCreatePayload(form: TenantForm): CreateTenantInput | null {
  const e164 = toE164(form.phone);
  if (!e164) return null;
  return { phone: e164, ...sharedFields(form) };
}

/**
 * `PATCH /v1/tenants/{id}` body — `null` when there is nothing to send, so
 * the caller can skip the request entirely (same as
 * src/features/account/profileEdit.ts's `buildMeUpdatePayload`). `phone`
 * is included ONLY when the landlord actually changed it
 * (`tenantPhoneUnchanged`); an unchanged-but-unnormalizable legacy value
 * is never validated and never sent, so it can't block an otherwise-valid
 * edit (e.g. setting `vulnerable_occupant`).
 */
export function buildTenantUpdatePayload(
  form: TenantForm,
  current: Tenant,
): UpdateTenantInput | null {
  const payload: UpdateTenantInput = sharedFields(form);

  if (!tenantPhoneUnchanged(form.phone, current)) {
    const e164 = toE164(form.phone);
    // An unnormalizable CHANGED phone is blocked earlier by the form's
    // `phoneError`; this stays safe on its own too.
    if (e164) payload.phone = e164;
  }

  return Object.keys(payload).length > 0 ? payload : null;
}
