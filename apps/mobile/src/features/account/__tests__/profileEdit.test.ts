/**
 * PATCH /v1/me payload-shape tests (issue #210 M2). The builder's job is
 * contract discipline: only documented fields, no explicit nulls ever
 * (the API 422s a null phone by DESIGN — it's the emergency-call target),
 * and no needless re-writes of unchanged values.
 */
import { buildMeUpdatePayload, phoneLooksValid } from "../profileEdit";

describe("buildMeUpdatePayload", () => {
  it("sends both fields when both changed", () => {
    expect(
      buildMeUpdatePayload({ name: "Sarah Chen", phone: "(416) 555-0134" }, { full_name: null }),
    ).toEqual({ full_name: "Sarah Chen", phone: "(416) 555-0134" });
  });

  it("omits an unchanged name — a phone-only edit never re-writes the name", () => {
    expect(
      buildMeUpdatePayload(
        { name: "Sarah Chen", phone: "(416) 555-0134" },
        { full_name: "Sarah Chen" },
      ),
    ).toEqual({ phone: "(416) 555-0134" });
  });

  it("a blank phone means 'keep the number on file' — omitted, NEVER null", () => {
    const payload = buildMeUpdatePayload({ name: "New Name", phone: "" }, { full_name: "Old" });
    expect(payload).toEqual({ full_name: "New Name" });
    expect(payload).not.toHaveProperty("phone");
  });

  it("returns null when nothing changed, so the caller can skip the PATCH entirely", () => {
    expect(
      buildMeUpdatePayload({ name: "Sarah Chen", phone: "  " }, { full_name: "Sarah Chen" }),
    ).toBeNull();
    expect(buildMeUpdatePayload({ name: "", phone: "" }, { full_name: null })).toBeNull();
  });

  it("never emits an undocumented field (the four-field contract, minus the two this form doesn't edit)", () => {
    const payload = buildMeUpdatePayload(
      { name: "Sarah Chen", phone: "4165550134" },
      { full_name: null },
    );
    expect(Object.keys(payload ?? {}).sort()).toEqual(["full_name", "phone"]);
  });

  it("trims whitespace before comparing and sending", () => {
    expect(
      buildMeUpdatePayload({ name: "  Sarah Chen  ", phone: "" }, { full_name: "Sarah Chen" }),
    ).toBeNull();
  });
});

describe("phoneLooksValid", () => {
  it("blank is valid (keep current number)", () => {
    expect(phoneLooksValid("")).toBe(true);
    expect(phoneLooksValid("   ")).toBe(true);
  });

  it("accepts 10+ digits in any formatting", () => {
    expect(phoneLooksValid("(416) 555-0134")).toBe(true);
    expect(phoneLooksValid("+1 416 555 0134")).toBe(true);
  });

  it("rejects a partial number", () => {
    expect(phoneLooksValid("416-555")).toBe(false);
  });
});
