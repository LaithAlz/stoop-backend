/**
 * Pure decision logic for the Me tab's notifications control (issue #210
 * M3) — kept separate from src/features/push/deviceRegistration.ts (the
 * actual expo-notifications/API calls) so the "which status line, which
 * button" mapping is unit-testable without mocking a native module, same
 * split as src/features/trust/revoke.ts (pure copy) vs src/api/trust.ts
 * (the network call).
 */
import {
  PUSH_STATUS_OFF_CAN_ASK,
  PUSH_STATUS_OFF_SETTINGS,
  PUSH_STATUS_ON,
  PUSH_STATUS_UNSUPPORTED,
} from "./pushCopy";

/** Mirrors expo-modules-core's `PermissionResponse.status` vocabulary
 *  ('granted' | 'denied' | 'undetermined') plus one app-only value,
 *  'unsupported', for a platform/environment expo-notifications itself
 *  can't report on (web, or a native call that threw — see
 *  deviceRegistration.ts's `getPushPermissionState`). */
export type PushPermissionStatus = "granted" | "denied" | "undetermined" | "unsupported";

export interface PushPermissionState {
  status: PushPermissionStatus;
  /** From the OS response — false means a further in-app prompt would be a
   *  no-op (iOS after one denial); the control must offer "Open Settings"
   *  instead, never a dead "Turn on notifications" button. Always false for
   *  'unsupported' (nothing to ask for). */
  canAskAgain: boolean;
}

export type PushControlAction = "none" | "request-permission" | "open-settings";

/** What the Me tab's button (if any) should do. 'granted'/'unsupported'
 *  show no button at all — there's nothing left to ask for either way. */
export function resolvePushControlAction(state: PushPermissionState): PushControlAction {
  if (state.status === "granted" || state.status === "unsupported") return "none";
  return state.canAskAgain ? "request-permission" : "open-settings";
}

/** The status line shown above that button — never claims "on" unless the
 *  OS actually reports `granted`. */
export function pushStatusLine(state: PushPermissionState): string {
  if (state.status === "granted") return PUSH_STATUS_ON;
  if (state.status === "unsupported") return PUSH_STATUS_UNSUPPORTED;
  return state.canAskAgain ? PUSH_STATUS_OFF_CAN_ASK : PUSH_STATUS_OFF_SETTINGS;
}
