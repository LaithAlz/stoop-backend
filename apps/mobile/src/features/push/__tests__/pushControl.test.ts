/**
 * Pure Me-tab notifications-control decision tests (issue #210 M3): which
 * button (if any) and which status line each OS permission state maps to.
 * The load-bearing honesty rule: never claim "on" unless the OS actually
 * reports `granted`, and never offer a dead in-app prompt once the OS
 * won't ask again (offer Settings instead).
 */
import { pushStatusLine, resolvePushControlAction, type PushPermissionState } from "../pushControl";
import {
  PUSH_STATUS_OFF_CAN_ASK,
  PUSH_STATUS_OFF_SETTINGS,
  PUSH_STATUS_ON,
  PUSH_STATUS_UNSUPPORTED,
} from "../pushCopy";

const state = (
  status: PushPermissionState["status"],
  canAskAgain: boolean,
): PushPermissionState => ({ status, canAskAgain });

describe("resolvePushControlAction", () => {
  it("shows no button when already granted", () => {
    expect(resolvePushControlAction(state("granted", false))).toBe("none");
  });

  it("shows no button when push is unsupported on this device", () => {
    expect(resolvePushControlAction(state("unsupported", false))).toBe("none");
  });

  it("offers an in-app prompt when the OS can still ask (undetermined)", () => {
    expect(resolvePushControlAction(state("undetermined", true))).toBe("request-permission");
  });

  it("offers Settings, not a dead in-app prompt, once the OS won't ask again (prior denial)", () => {
    expect(resolvePushControlAction(state("denied", false))).toBe("open-settings");
  });

  it("still offers the in-app prompt for a denial the OS will re-ask (Android first decline)", () => {
    expect(resolvePushControlAction(state("denied", true))).toBe("request-permission");
  });
});

describe("pushStatusLine", () => {
  it("only says 'on' for a genuinely granted permission", () => {
    expect(pushStatusLine(state("granted", false))).toBe(PUSH_STATUS_ON);
  });

  it("says off-and-tappable when the OS can still ask", () => {
    expect(pushStatusLine(state("undetermined", true))).toBe(PUSH_STATUS_OFF_CAN_ASK);
  });

  it("points to Settings when the OS won't ask again", () => {
    expect(pushStatusLine(state("denied", false))).toBe(PUSH_STATUS_OFF_SETTINGS);
  });

  it("says unsupported when push can't run on this device at all", () => {
    expect(pushStatusLine(state("unsupported", false))).toBe(PUSH_STATUS_UNSUPPORTED);
  });
});
