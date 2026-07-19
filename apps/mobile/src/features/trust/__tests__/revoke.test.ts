/**
 * Trust-revoke copy tests (issue #210 M2). The confirmation must say —
 * honestly, in plain English — the three things a revoke actually does per
 * the v1.13 amendment: routine auto-replies stop, approvals return to the
 * landlord, and auto-send is only re-earned with a FRESH streak (the
 * server resets `consecutive_clean` to 0). The result line must be driven
 * by the server's `revoked_count`, including the honest zero case.
 */
import { revokeConfirmation, revokeResultNotice, TRUST_SECTION_BODY } from "../revoke";

describe("revokeConfirmation", () => {
  it.each(["property", "global"] as const)(
    "the %s confirmation covers stop + approvals-return + re-earn-by-streak",
    (scope) => {
      const copy = revokeConfirmation(scope);
      expect(copy.message).toMatch(/stop sending routine replies/i);
      expect(copy.message).toMatch(/comes back to you to approve/i);
      expect(copy.message).toMatch(/fresh streak/i);
    },
  );

  it("scopes read differently — 'this property' vs 'every property'", () => {
    expect(revokeConfirmation("property").message).toMatch(/this property/i);
    expect(revokeConfirmation("global").message).toMatch(/every property/i);
  });

  it("never claims to know the current trust state (no read contract exists)", () => {
    for (const scope of ["property", "global"] as const) {
      const copy = revokeConfirmation(scope);
      // No "currently on/enabled/active" claims — the app can't read that.
      expect(`${copy.title} ${copy.message}`).not.toMatch(/currently|is on|enabled|active/i);
    }
  });
});

describe("revokeResultNotice", () => {
  it("an actual revoke reports the new reality", () => {
    expect(revokeResultNotice("property", 1)).toMatch(/off here/i);
    expect(revokeResultNotice("global", 3)).toMatch(/every property/i);
  });

  it("revoked_count 0 is reported honestly — nothing was automatic to begin with", () => {
    const line = revokeResultNotice("property", 0);
    expect(line).toMatch(/nothing was set to send automatically/i);
    expect(line).not.toMatch(/off here|turned off/i);
  });
});

describe("the property-detail section copy", () => {
  it("says only routine replies were ever eligible, and urgent/emergency always wait", () => {
    expect(TRUST_SECTION_BODY).toMatch(/routine/i);
    expect(TRUST_SECTION_BODY).toMatch(/emergenc/i);
  });

  it("never uses the banned jargon", () => {
    expect(TRUST_SECTION_BODY).not.toMatch(/triage|autonomy|trust ladder|AI agent/i);
  });
});
