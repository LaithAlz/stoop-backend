/**
 * PATCH/POST tenant payload-shape tests (issue #292). The core case: a
 * tenant row holding a legacy, un-normalizable `phone` (pre-#232/#260
 * data) must stay editable for everything else, most sharply
 * `vulnerable_occupant`, which feeds severity classification, without the
 * stale phone ever being (re-)validated or (re-)sent.
 */
import type { Tenant } from "@/api/types";
import {
  buildTenantCreatePayload,
  buildTenantUpdatePayload,
  tenantPhoneUnchanged,
  type TenantForm,
} from "../tenantForm";

const LEGACY_UNNORMALIZABLE_PHONE = "call the office"; // no digits at all, toE164 -> null

function makeTenant(overrides: Partial<Tenant> = {}): Tenant {
  return {
    id: "tenant-1",
    property_id: "prop-1",
    name: "Elena Petrova",
    phone: "+14165550134",
    unit: "2",
    vulnerable_occupant: null,
    notes: null,
    active: true,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function formFrom(tenant: Tenant, overrides: Partial<TenantForm> = {}): TenantForm {
  return {
    name: tenant.name ?? "",
    phone: tenant.phone,
    unit: tenant.unit ?? "",
    vulnerable: tenant.vulnerable_occupant,
    notes: tenant.notes ?? "",
    ...overrides,
  };
}

describe("tenantPhoneUnchanged", () => {
  it("is true when the form phone matches the stored tenant phone exactly", () => {
    const tenant = makeTenant({ phone: "+14165550134" });
    expect(tenantPhoneUnchanged("+14165550134", tenant)).toBe(true);
  });

  it("tolerates surrounding whitespace in the form field", () => {
    const tenant = makeTenant({ phone: "+14165550134" });
    expect(tenantPhoneUnchanged("  +14165550134  ", tenant)).toBe(true);
  });

  it("is false once the landlord types anything different", () => {
    const tenant = makeTenant({ phone: "+14165550134" });
    expect(tenantPhoneUnchanged("+14165550199", tenant)).toBe(false);
  });

  it("is true for a legacy, un-normalizable phone left exactly as loaded", () => {
    const tenant = makeTenant({ phone: LEGACY_UNNORMALIZABLE_PHONE });
    expect(tenantPhoneUnchanged(LEGACY_UNNORMALIZABLE_PHONE, tenant)).toBe(true);
  });

  it("is always false in create mode (current === null)", () => {
    expect(tenantPhoneUnchanged("+14165550134", null)).toBe(false);
  });

  // Adversarial safety review, 2026-08-04, item 1 (FIX 1, HIGH): the
  // stored side must be trimmed too, not just the form side. Migration
  // 0017 leaves an un-canonicalizable row completely untouched
  // (schema-v1.md v1.21 point 5), so the surviving legacy rows are
  // exactly the ones most likely to carry surrounding whitespace (a CSV
  // import, a paste from a spreadsheet, a trailing newline).
  describe("a legacy stored phone with surrounding whitespace (item 1)", () => {
    it("tolerates a trailing space on the stored value", () => {
      const tenant = makeTenant({ phone: `${LEGACY_UNNORMALIZABLE_PHONE} ` });
      expect(tenantPhoneUnchanged(LEGACY_UNNORMALIZABLE_PHONE, tenant)).toBe(true);
    });

    it("tolerates a leading space on the stored value", () => {
      const tenant = makeTenant({ phone: ` ${LEGACY_UNNORMALIZABLE_PHONE}` });
      expect(tenantPhoneUnchanged(LEGACY_UNNORMALIZABLE_PHONE, tenant)).toBe(true);
    });

    it("tolerates a trailing newline on the stored value", () => {
      const stored = "416-555-0134 x22\n";
      const tenant = makeTenant({ phone: stored });
      expect(tenantPhoneUnchanged("416-555-0134 x22", tenant)).toBe(true);
    });

    it("still matches when both sides carry (different) surrounding whitespace", () => {
      const tenant = makeTenant({ phone: `  ${LEGACY_UNNORMALIZABLE_PHONE}\n` });
      expect(tenantPhoneUnchanged(` ${LEGACY_UNNORMALIZABLE_PHONE} `, tenant)).toBe(true);
    });
  });

  // Item 4 (FIX 4, MEDIUM): a pre-#260 blank stored phone must never read
  // as "unchanged", or the required-field branch in TenantFormModal never
  // runs and the PATCH goes out with the empty phone left in place.
  describe("a blank stored phone (item 4)", () => {
    it("is false when the stored phone is empty, even though the form field is also empty", () => {
      const tenant = makeTenant({ phone: "" });
      expect(tenantPhoneUnchanged("", tenant)).toBe(false);
    });

    it("is false when the stored phone is whitespace-only", () => {
      const tenant = makeTenant({ phone: "   " });
      expect(tenantPhoneUnchanged("", tenant)).toBe(false);
    });
  });

  it("is false, not a throw, when current is undefined (item 8)", () => {
    // TypeScript's `Tenant | null` forbids `undefined` at the call sites
    // today; this guards the runtime behavior anyway, cheaply.
    expect(tenantPhoneUnchanged("+14165550134", undefined as unknown as null)).toBe(false);
  });
});

describe("buildTenantUpdatePayload, the #292 case", () => {
  it("a legacy un-normalizable phone left untouched: vulnerable_occupant can still be set, and the payload carries no phone key", () => {
    const tenant = makeTenant({
      phone: LEGACY_UNNORMALIZABLE_PHONE,
      name: null,
      unit: null,
      notes: null,
      vulnerable_occupant: null,
    });
    const form = formFrom(tenant, { vulnerable: "infant" });

    const payload = buildTenantUpdatePayload(form, tenant);

    expect(payload).toEqual({ vulnerable_occupant: "infant" });
    expect(payload).not.toHaveProperty("phone");
  });

  it("omits phone when unchanged even alongside other real edits", () => {
    const tenant = makeTenant({ phone: "+14165550134" });
    const form = formFrom(tenant, { notes: "Leaves a spare key with unit 1" });

    const payload = buildTenantUpdatePayload(form, tenant);

    expect(payload).not.toHaveProperty("phone");
    expect(payload).toMatchObject({ notes: "Leaves a spare key with unit 1" });
  });

  it("includes phone, normalized, when the landlord actually changes it", () => {
    const tenant = makeTenant({ phone: "+14165550134" });
    const form = formFrom(tenant, { phone: "(416) 555-0199" });

    const payload = buildTenantUpdatePayload(form, tenant);

    expect(payload).toMatchObject({ phone: "+14165550199" });
  });

  it("never sends an unnormalizable CHANGED phone (safe on its own, not just via phoneError)", () => {
    const tenant = makeTenant({ phone: "+14165550134" });
    const form = formFrom(tenant, { phone: "not a number" });

    const payload = buildTenantUpdatePayload(form, tenant);

    expect(payload).not.toHaveProperty("phone");
  });

  it("returns null when nothing at all changed, so the caller can skip the PATCH", () => {
    const tenant = makeTenant({
      phone: "+14165550134",
      name: null,
      unit: null,
      notes: null,
      vulnerable_occupant: null,
    });
    const form = formFrom(tenant);

    expect(buildTenantUpdatePayload(form, tenant)).toBeNull();
  });

  it("never emits an undocumented field", () => {
    const tenant = makeTenant({ phone: "+14165550134" });
    const form = formFrom(tenant, { phone: "(416) 555-0199", vulnerable: "elderly" });

    const payload = buildTenantUpdatePayload(form, tenant);

    expect(Object.keys(payload ?? {}).sort()).toEqual(
      ["name", "phone", "unit", "vulnerable_occupant"].sort(),
    );
  });

  // Item 1 (FIX 1, HIGH), end to end: a legacy stored phone with
  // surrounding whitespace, untouched by the landlord, must still omit
  // `phone` from the PATCH (the whole point of #292) instead of resending
  // (and 422ing on) the untrimmed stale value.
  it("item 1: a legacy phone with a trailing space, left untouched, still omits phone", () => {
    const tenant = makeTenant({
      phone: `${LEGACY_UNNORMALIZABLE_PHONE} `,
      name: null,
      unit: null,
      notes: null,
      vulnerable_occupant: null,
    });
    const form = formFrom(tenant, {
      phone: LEGACY_UNNORMALIZABLE_PHONE,
      vulnerable: "medical_device",
    });

    const payload = buildTenantUpdatePayload(form, tenant);

    expect(payload).toEqual({ vulnerable_occupant: "medical_device" });
    expect(payload).not.toHaveProperty("phone");
  });

  // Item 4 (FIX 4, MEDIUM), end to end: a pre-#260 blank stored phone must
  // block the PATCH via the (normal) "unnormalizable, never sent" branch,
  // same as any other changed-but-invalid phone, rather than letting the
  // blank phone through unnoticed.
  it("item 4: a blank stored phone is treated as changed, so it is never silently left in place", () => {
    const tenant = makeTenant({
      phone: "",
      name: null,
      unit: null,
      notes: null,
      vulnerable_occupant: null,
    });
    const form = formFrom(tenant, { phone: "", vulnerable: "infant" });

    expect(tenantPhoneUnchanged(form.phone, tenant)).toBe(false);
    const payload = buildTenantUpdatePayload(form, tenant);

    // `phone` is still omitted here (an empty string can't normalize), but
    // that is now the "changed and unnormalizable" branch, not "unchanged":
    // TenantFormModal's `phoneError` (gated on the same `tenantPhoneUnchanged`)
    // runs its required-field check and blocks Save before this is ever
    // called for real; this only proves the payload builder's own half.
    expect(payload).toEqual({ vulnerable_occupant: "infant" });
    expect(payload).not.toHaveProperty("phone");
  });
});

describe("buildTenantCreatePayload", () => {
  it("always includes the normalized phone, required on create", () => {
    const form: TenantForm = {
      name: "New Tenant",
      phone: "(416) 555-0134",
      unit: "",
      vulnerable: null,
      notes: "",
    };

    expect(buildTenantCreatePayload(form)).toEqual({
      phone: "+14165550134",
      name: "New Tenant",
    });
  });

  it("returns null for an unnormalizable phone (safety net behind phoneError)", () => {
    const form: TenantForm = {
      name: "",
      phone: "not a number",
      unit: "",
      vulnerable: null,
      notes: "",
    };

    expect(buildTenantCreatePayload(form)).toBeNull();
  });
});
