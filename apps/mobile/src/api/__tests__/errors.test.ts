/**
 * House-voice mapping tests (issue #210 M2): every documented provisioning
 * failure code — and the delete/tenant codes M2's screens surface — maps
 * to its own landlord-facing line, never the raw code, never a raw server
 * message, and never a vague open-ended promise ("soon" is banned;
 * "concrete over relative", plain-language-rules.md).
 */
import { ApiError, toHouseApiError } from "@/api/errors";

function houseLine(code: string): string {
  return toHouseApiError(
    new ApiError(400, { code, message: "raw server text", request_id: "req_x" }),
  );
}

describe("toHouseApiError — provisioning failure paths (POST /v1/properties, v1.12)", () => {
  const PROVISIONING_CODES = [
    "property_limit_reached",
    "duplicate_property",
    "no_numbers_available",
    "provisioning_failed",
  ] as const;

  it.each(PROVISIONING_CODES)("gives %s its own line, not the generic fallback", (code) => {
    const line = houseLine(code);
    expect(line).not.toBe(houseLine("some_unknown_code"));
    expect(line.length).toBeGreaterThan(0);
  });

  it("each provisioning code gets a DISTINCT line (no two failures share copy)", () => {
    const lines = PROVISIONING_CODES.map(houseLine);
    expect(new Set(lines).size).toBe(PROVISIONING_CODES.length);
  });

  it("the cap line says the property was NOT added and offers the real remedy", () => {
    const line = houseLine("property_limit_reached");
    expect(line).toMatch(/wasn't added/);
    expect(line).toMatch(/support/i);
  });

  it("the duplicate line points at the existing property instead of a dead end", () => {
    expect(houseLine("duplicate_property")).toMatch(/Properties list/);
  });

  it("no_numbers_available is honest that nothing was set up and gives both remedies", () => {
    const line = houseLine("no_numbers_available");
    expect(line).toMatch(/nothing was set up/i);
    expect(line).toMatch(/area code/i);
    expect(line).toMatch(/try again/i);
  });

  it("provisioning_failed says nothing was saved (the server compensates) and retry is safe", () => {
    const line = houseLine("provisioning_failed");
    expect(line).toMatch(/nothing was saved/i);
    expect(line).toMatch(/try again/i);
  });

  it("never leaks the raw code, raw server message, or vendor names", () => {
    for (const code of PROVISIONING_CODES) {
      const line = houseLine(code);
      expect(line).not.toContain(code);
      expect(line).not.toContain("raw server text");
      expect(line).not.toMatch(/twilio/i);
    }
  });

  it("never says 'soon' (concrete over relative — the standing copy ruling)", () => {
    for (const code of PROVISIONING_CODES) {
      expect(houseLine(code)).not.toMatch(/\bsoon\b/i);
    }
  });
});

describe("toHouseApiError — delete-property and tenant codes", () => {
  it("has_open_cases explains WHY the delete is blocked", () => {
    expect(houseLine("has_open_cases")).toMatch(/open cases/i);
  });

  it("has_dependents explains the block without database words", () => {
    const line = houseLine("has_dependents");
    expect(line).toMatch(/tenants|history/i);
    expect(line).not.toMatch(/foreign key|constraint|row/i);
  });

  it("duplicate_phone names the actual conflict", () => {
    expect(houseLine("duplicate_phone")).toMatch(/phone number/i);
  });

  it("unknown codes still get the honest generic line", () => {
    expect(houseLine("some_unknown_code")).toBe(
      "Something didn't go through. Try again in a moment.",
    );
  });
});
