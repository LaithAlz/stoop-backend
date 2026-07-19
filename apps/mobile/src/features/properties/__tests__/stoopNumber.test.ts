/**
 * Stoop-number display tests (issue #210 M2): honest formatting — never
 * invented digits — and the null-state copy stays a statement of fact, not
 * a promise.
 */
import { formatStoopNumber, NO_NUMBER_BODY, NO_NUMBER_TITLE } from "../stoopNumber";

describe("formatStoopNumber", () => {
  it("formats a stored E.164 Canadian number for reading aloud", () => {
    expect(formatStoopNumber("+14165550134")).toBe("(416) 555-0134");
  });

  it("handles a bare 10-digit string", () => {
    expect(formatStoopNumber("6475550199")).toBe("(647) 555-0199");
  });

  it("renders anything unexpected exactly as stored — no invented digits", () => {
    expect(formatStoopNumber("+4479460958")).toBe("+4479460958");
    expect(formatStoopNumber("short")).toBe("short");
  });
});

describe("the no-number state copy", () => {
  it("states the consequence plainly: tenants can't text this property", () => {
    expect(NO_NUMBER_BODY).toMatch(/tenants can't text it/i);
  });

  it("makes no timeline promise ('soon' is banned; concrete over relative)", () => {
    expect(`${NO_NUMBER_TITLE} ${NO_NUMBER_BODY}`).not.toMatch(/\bsoon\b|shortly|on it/i);
  });
});
