/**
 * Copy invariant tests (issue #210 M3 + CLAUDE.md rule 1). These aren't
 * about exact wording (copy "may change freely") — they pin the ONE thing
 * that must never be edited out: the Me-tab explainer must state, in plain
 * English, that push is NOT how an emergency reaches a landlord (an
 * emergency always rings the phone). A landlord must never silence/decline
 * push believing they might miss an emergency. Also a jargon guard
 * (rule 8): no "triage"/"AI agent" in any customer-facing push string.
 */
import * as copy from "../pushCopy";

describe("push copy — the never-the-emergency-channel invariant (rule 1)", () => {
  it("the explainer says push is not the emergency channel", () => {
    const text = copy.PUSH_EXPLAINER.toLowerCase();
    expect(text).toContain("emergency");
    expect(text).toContain("never");
  });

  it("the explainer promises an emergency reaches the phone by call", () => {
    const text = copy.PUSH_EXPLAINER.toLowerCase();
    // "a true emergency always calls your phone" — the honest, load-bearing
    // clause. Both the call verb and the phone noun must survive any edit.
    expect(text).toContain("call");
    expect(text).toContain("phone");
  });

  it("frames push as an approval nudge, not an alert of record", () => {
    const text = copy.PUSH_EXPLAINER.toLowerCase();
    expect(text).toContain("approval");
  });
});

describe("push copy — plain-English jargon guard (rule 8)", () => {
  const bannedTerms = ["triage", "ai agent"];
  const allStrings = Object.values(copy).filter((v): v is string => typeof v === "string");

  it("no customer-facing push string uses a banned term", () => {
    for (const value of allStrings) {
      const lower = value.toLowerCase();
      for (const term of bannedTerms) {
        expect(lower).not.toContain(term);
      }
    }
  });
});
