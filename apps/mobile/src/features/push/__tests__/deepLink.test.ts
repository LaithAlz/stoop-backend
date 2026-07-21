/**
 * Pure deep-link resolver tests (issue #210 M3). The load-bearing rule: a
 * tapped push carries ONLY uuids in `data` (the backend's
 * app/push_outbox.py::_build_message reads only case_id/draft_id) — this
 * resolver reads only `case_id` and never crashes on a malformed/foreign
 * payload (a bad tap is a silent no-op, never an error), so no tenant
 * content is ever read from a push.
 */
import { resolveNotificationDeepLink } from "../deepLink";

describe("resolveNotificationDeepLink", () => {
  it("routes a data={case_id, draft_id} payload to the case screen", () => {
    expect(resolveNotificationDeepLink({ case_id: "case-9", draft_id: "draft-9" })).toEqual({
      pathname: "/conversations/[id]",
      params: { id: "case-9" },
    });
  });

  it("ignores draft_id for navigation — case_id alone drives the route", () => {
    // The destination screen fetches the draft itself; the tap only needs
    // to land on the case.
    const target = resolveNotificationDeepLink({ case_id: "case-1" });
    expect(target).toEqual({ pathname: "/conversations/[id]", params: { id: "case-1" } });
  });

  it("returns null for a payload with no case_id (a no-op tap, never a crash)", () => {
    expect(resolveNotificationDeepLink({ draft_id: "draft-1" })).toBeNull();
    expect(resolveNotificationDeepLink({ kind: "draft_awaiting_approval" })).toBeNull();
  });

  it("returns null for a non-string or empty case_id", () => {
    expect(resolveNotificationDeepLink({ case_id: 123 })).toBeNull();
    expect(resolveNotificationDeepLink({ case_id: "" })).toBeNull();
  });

  it("returns null for null / undefined / non-object data", () => {
    expect(resolveNotificationDeepLink(null)).toBeNull();
    expect(resolveNotificationDeepLink(undefined)).toBeNull();
    expect(resolveNotificationDeepLink("case-1")).toBeNull();
  });
});
