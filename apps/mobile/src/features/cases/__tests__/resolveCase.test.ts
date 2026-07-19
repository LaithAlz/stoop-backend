/**
 * Resolve-confirmation copy tests (issue #210 M2, api-contracts.md v1.14).
 * The contract cancels unsent drafts on resolve — the confirmation must
 * say so BEFORE the call, in plain English, because a landlord with a
 * drafted reply on screen needs to know it won't go out.
 */
import {
  RESOLVE_CONFIRM_LABEL,
  RESOLVE_CONFIRM_MESSAGE,
  RESOLVE_CONFIRM_TITLE,
  RESOLVE_DONE_NOTICE,
} from "../resolveCase";

describe("resolve confirmation copy", () => {
  it("asks before acting", () => {
    expect(RESOLVE_CONFIRM_TITLE).toMatch(/\?$/);
  });

  it("warns that a drafted-but-unsent reply is cancelled (the v1.14 draft-safety rule)", () => {
    expect(RESOLVE_CONFIRM_MESSAGE).toMatch(/hasn't sent.*won't go out/i);
  });

  it("says what happens if the tenant texts again — closing never silences anyone", () => {
    expect(RESOLVE_CONFIRM_MESSAGE).toMatch(/texts again.*new case/i);
  });

  it("the action label matches the button that opened it (no 'OK'/'Yes' mystery buttons)", () => {
    expect(RESOLVE_CONFIRM_LABEL).toBe("Mark resolved");
  });

  it("the done notice reflects the append-only record promise", () => {
    expect(RESOLVE_DONE_NOTICE).toMatch(/records/i);
  });

  it("no banned copy anywhere", () => {
    const all = [
      RESOLVE_CONFIRM_TITLE,
      RESOLVE_CONFIRM_MESSAGE,
      RESOLVE_CONFIRM_LABEL,
      RESOLVE_DONE_NOTICE,
    ].join(" ");
    expect(all).not.toMatch(/\bsoon\b|triage/i);
  });
});
