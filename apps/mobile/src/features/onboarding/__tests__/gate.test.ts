/**
 * The zero-properties onboarding gate (issue #210 M2) — pure decision
 * tests. The load-bearing rule: only a REAL, successful GET /v1/properties
 * read that comes back empty may open the wizard; loading and error states
 * must never (an unreachable API is not "you have no properties").
 */
import {
  hasOfferedOnboarding,
  markOnboardingOffered,
  resetOnboardingOffer,
  shouldOfferOnboarding,
} from "../gate";

describe("shouldOfferOnboarding", () => {
  it("offers the wizard on a confirmed-empty portfolio, once", () => {
    expect(shouldOfferOnboarding({ fetched: true, itemCount: 0, alreadyOffered: false })).toBe(
      true,
    );
  });

  it("never fires while the read hasn't succeeded (loading OR error)", () => {
    // itemCount 0 here is just "no data yet" — indistinguishable from a
    // failed fetch, which is exactly why `fetched` gates everything.
    expect(shouldOfferOnboarding({ fetched: false, itemCount: 0, alreadyOffered: false })).toBe(
      false,
    );
  });

  it("never fires for a landlord who already has a property", () => {
    expect(shouldOfferOnboarding({ fetched: true, itemCount: 1, alreadyOffered: false })).toBe(
      false,
    );
  });

  it("respects a skip for the rest of the session", () => {
    expect(shouldOfferOnboarding({ fetched: true, itemCount: 0, alreadyOffered: true })).toBe(
      false,
    );
  });
});

describe("the once-per-session flag", () => {
  afterEach(() => resetOnboardingOffer());

  it("starts unoffered, marks, and resets (the sign-out hook)", () => {
    expect(hasOfferedOnboarding()).toBe(false);
    markOnboardingOffered();
    expect(hasOfferedOnboarding()).toBe(true);
    // A different landlord signing in on this device must get their own
    // gate decision — AuthProvider calls this on SIGNED_OUT.
    resetOnboardingOffer();
    expect(hasOfferedOnboarding()).toBe(false);
  });
});
