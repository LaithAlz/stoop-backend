/**
 * Guards the exact regression api-contracts.md's Queue section calls out
 * by name: PR #181 once hardcoded "reported a flood" as a client-side
 * fallback headline. `title` is null until #197's title-writing half
 * lands, so the fallback here must stay neutral — never guess an incident.
 */
import {
  EMERGENCY_ACK_LABEL,
  EMERGENCY_ACK_PENDING_LABEL,
  EMERGENCY_ACK_SUBLABEL,
  emergencyHeadline,
  emergencySubtext,
  hasAcknowledgeableNotification,
} from "../emergencyBanner";

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

// api-contracts.md's Queue section, v1.15 amendment: the ack affordance
// gates on `notification_id` being non-null — nothing else (not severity,
// not any local state).
describe("hasAcknowledgeableNotification", () => {
  it("is true when notification_id is a real id", () => {
    expect(hasAcknowledgeableNotification({ notification_id: "notif-1" })).toBe(true);
  });

  it("is false when notification_id is null — no emergency call, or already acknowledged", () => {
    expect(hasAcknowledgeableNotification({ notification_id: null })).toBe(false);
  });
});

// emergency-prefilter.md's escalation-chain section: acknowledging stops
// the call/SMS chain, nothing more — it never resolves the case, and no
// human has necessarily reached the tenant yet. The copy must never
// promise either.
describe("acknowledge button copy — never overpromises", () => {
  it("only ever describes stopping the calls, never resolving/fixing/handling anything", () => {
    const all = [EMERGENCY_ACK_LABEL, EMERGENCY_ACK_PENDING_LABEL, EMERGENCY_ACK_SUBLABEL].join(
      " ",
    );
    expect(all.toLowerCase()).not.toMatch(/resolv|fix|handl|sent|done/);
  });

  it("no banned copy anywhere", () => {
    const all = [EMERGENCY_ACK_LABEL, EMERGENCY_ACK_PENDING_LABEL, EMERGENCY_ACK_SUBLABEL].join(
      " ",
    );
    expect(all).not.toMatch(/\bsoon\b|triage/i);
  });
});
