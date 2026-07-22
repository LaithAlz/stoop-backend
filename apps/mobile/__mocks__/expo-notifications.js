/**
 * Automatic manual mock for expo-notifications (issue #210 M3). Jest
 * applies a `__mocks__` entry adjacent to `node_modules` automatically for
 * a node module — so ANY test that renders the signed-in tab shell (which
 * mounts src/features/push/usePushRegistration.ts) gets a safe, inert
 * native surface instead of the real module, which has no JS runtime under
 * jest and would hang/throw (e.g. src/app/__tests__/auth-gate.test.tsx's
 * "lands a signed-in user in the tab shell").
 *
 * Deliberately inert defaults: `getPermissionsAsync` resolves
 * 'undetermined', so the tab shell's auto-registration
 * (`registerForPushNotificationsAsync`) stops at the permission check and
 * never reaches a token fetch or `POST /v1/devices` — zero network, no
 * prompt, in any test that doesn't opt into more.
 *
 * The two push suites that exercise the real registration/handler logic
 * (deviceRegistration.test.ts, usePushRegistration.test.tsx) declare their
 * OWN `jest.mock("expo-notifications", ...)` with a factory, which takes
 * precedence over this file for those files only.
 */
const noopSubscription = { remove: () => {} };

module.exports = {
  setNotificationHandler: () => {},
  useLastNotificationResponse: () => null,
  addNotificationReceivedListener: () => noopSubscription,
  addNotificationResponseReceivedListener: () => noopSubscription,
  addPushTokenListener: () => noopSubscription,
  getPermissionsAsync: async () => ({ status: "undetermined", canAskAgain: true }),
  requestPermissionsAsync: async () => ({ status: "undetermined", canAskAgain: true }),
  getExpoPushTokenAsync: async () => ({ type: "expo", data: "ExponentPushToken[test]" }),
  setNotificationChannelAsync: async () => null,
  AndroidImportance: {
    UNKNOWN: 0,
    UNSPECIFIED: 1,
    NONE: 2,
    MIN: 3,
    LOW: 4,
    DEFAULT: 5,
    HIGH: 6,
    MAX: 7,
  },
};
