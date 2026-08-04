/**
 * PATCH/POST tenant payload-shape tests (issue #292). The core case: a
 * tenant row holding a legacy, un-normalizable `phone` (pre-#232/#260
 * data) must stay editable for everything else — most sharply
 * `vulnerable_occupant`, which feeds severity classification — without the
 * stale phone ever being (re-)validated or (re-)sent.
 */
import type { Tenant } from "@/api/types";
import {
  buildTenantCreatePayload,
  buildTenantUpdatePayload,
  tenantPhoneUnchanged,
  type TenantForm,
} from "../tenantForm";

const LEGACY_UNNORMALIZABLE_PHONE = "call the office"; // no digits at all — toE164 -> null

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
});

describe("buildTenantUpdatePayload — the #292 case", () => {
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
});

describe("buildTenantCreatePayload", () => {
  it("always includes the normalized phone — required on create", () => {
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
