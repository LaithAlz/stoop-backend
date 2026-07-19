/**
 * Contract-shape tests for the M2 request builders — `apiRequest` is
 * mocked (zero network, hard rule; the fetch/envelope layer itself is
 * covered by client.test.ts), and each assertion pins the path/method/body
 * the api-contracts.md section documents, so a drive-by "simplification"
 * of a URL or body key fails loudly here.
 */
import { apiRequest } from "@/api/client";
import { resolveCase } from "@/api/cases";
import { deleteProperty, createProperty } from "@/api/properties";
import { createTenant, updateTenant } from "@/api/tenants";
import { revokeTrust } from "@/api/trust";
import { updateMe } from "@/api/me";

jest.mock("@/api/client", () => ({
  apiRequest: jest.fn(),
}));

const mockApiRequest = apiRequest as jest.Mock;

beforeEach(() => {
  mockApiRequest.mockReset();
  mockApiRequest.mockResolvedValue({});
});

describe("resolveCase (POST /v1/cases/{id}/resolve, v1.14)", () => {
  it("POSTs the documented body — reason is always the landlord-direct value", async () => {
    await resolveCase("case-1");
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/cases/case-1/resolve", {
      method: "POST",
      body: { reason: "landlord" },
    });
  });

  it("repeat-resolve is passed straight through — the contract's idempotent 200 needs no client guard", async () => {
    const stored = { status: "resolved", resolved_at: "2026-07-18T12:00:00Z" };
    mockApiRequest.mockResolvedValue(stored);

    const first = await resolveCase("case-1");
    const second = await resolveCase("case-1");

    // Same result both times, both calls actually made — no client-side
    // "already resolved" short-circuit exists to disagree with the server.
    expect(first).toEqual(stored);
    expect(second).toEqual(stored);
    expect(mockApiRequest).toHaveBeenCalledTimes(2);
  });
});

describe("revokeTrust (POST /v1/properties/{id}/trust/revoke, v1.13)", () => {
  it("sends scope 'property' for a per-property revoke", async () => {
    await revokeTrust("prop-1", "property");
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/properties/prop-1/trust/revoke", {
      method: "POST",
      body: { scope: "property" },
    });
  });

  it("global revoke still routes through a property-scoped path (the contract's own shape)", async () => {
    await revokeTrust("prop-1", "global");
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/properties/prop-1/trust/revoke", {
      method: "POST",
      body: { scope: "global" },
    });
  });
});

describe("deleteProperty (DELETE /v1/properties/{id}, v1.12)", () => {
  it("always carries confirm=true — the endpoint 400s without it", async () => {
    await deleteProperty("prop-1");
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/properties/prop-1?confirm=true", {
      method: "DELETE",
    });
  });
});

describe("createProperty (POST /v1/properties)", () => {
  it("passes area_code through as a body field (transient provisioning hint)", async () => {
    await createProperty({
      label: "The Palmerston Duplex",
      address_line1: "41 Palmerston Ave",
      city: "Toronto",
      province: "ON",
      area_code: "416",
    });
    const [, options] = mockApiRequest.mock.calls[0];
    expect(options.body.area_code).toBe("416");
    expect(options.method).toBe("POST");
  });
});

describe("tenants — the sub-resource route shape (Tenants & Vendors section)", () => {
  it("creates via POST /v1/properties/{id}/tenants, not a top-level collection", async () => {
    await createTenant("prop-1", { phone: "+14165550134", name: "Elena" });
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/properties/prop-1/tenants", {
      method: "POST",
      body: { phone: "+14165550134", name: "Elena" },
    });
  });

  it("updates via PATCH /v1/tenants/{id}", async () => {
    await updateTenant("tenant-1", { unit: "2" });
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/tenants/tenant-1", {
      method: "PATCH",
      body: { unit: "2" },
    });
  });
});

describe("updateMe (PATCH /v1/me)", () => {
  it("PATCHes the built payload verbatim — no extra fields ever injected", async () => {
    await updateMe({ full_name: "Sarah Chen", phone: "+14165550134" });
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/me", {
      method: "PATCH",
      body: { full_name: "Sarah Chen", phone: "+14165550134" },
    });
  });
});
