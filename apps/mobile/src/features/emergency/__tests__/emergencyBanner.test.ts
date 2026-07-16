/**
 * Guards the exact regression api-contracts.md's Queue section calls out
 * by name: PR #181 once hardcoded "reported a flood" as a client-side
 * fallback headline. `title` is null until #197's title-writing half
 * lands, so the fallback here must stay neutral — never guess an incident.
 */
import { emergencyHeadline, emergencySubtext } from "../emergencyBanner";

describe("emergencyHeadline", () => {
  it("uses the agent-written title when present", () => {
    expect(
      emergencyHeadline({
        title: "No heat — Unit 2",
        tenant_name: "Maria",
        property_label: "41 Palmerston",
      }),
    ).toBe("No heat — Unit 2");
  });

  it("falls back to a neutral line built only from guaranteed fields — never an invented incident", () => {
    const headline = emergencyHeadline({
      title: null,
      tenant_name: "Maria Gonzalez",
      property_label: "41 Palmerston",
    });
    expect(headline).toBe("Maria needs you now — 41 Palmerston");
    expect(headline.toLowerCase()).not.toMatch(/flood|fire|gas|smoke|leak|break/);
  });
});

describe("emergencySubtext", () => {
  it("never promises a phone call the banner doesn't actually place", () => {
    const subtext = emergencySubtext({ property_label: "41 Palmerston" });
    expect(subtext.toLowerCase()).not.toContain("call");
  });
});
