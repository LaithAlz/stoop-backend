/**
 * The Expo push registration lifecycle (issue #210 M3) — expo-notifications
 * natives, expo-constants, and the /v1/devices client are all mocked (zero
 * network, no real native module). Covers the brief's explicit list:
 * registration-on-sign-in, the token→POST payload shape, the
 * permission-denied path, the founder-gated (no EAS projectId) no-op, and
 * unregister-on-sign-out. Also covers #284's B3-8: the durable SecureStore
 * marker that survives a forced/offline sign-out, and its one-shot
 * reconcile-on-next-sign-in cleanup.
 */
import { Platform } from "react-native";
import Constants from "expo-constants";
import { registerDevice, unregisterDevice } from "@/api/devices";
import {
  clearRegisteredDeviceId,
  getPushPermissionState,
  getRegisteredDeviceId,
  reconcileStaleDeviceRegistration,
  registerForPushNotificationsAsync,
  requestPushPermission,
  unregisterCurrentDeviceBestEffort,
} from "../deviceRegistration";

const mockGetPermissions = jest.fn();
const mockRequestPermissions = jest.fn();
const mockGetExpoPushToken = jest.fn();
const mockSetChannel = jest.fn();

jest.mock("expo-notifications", () => ({
  getPermissionsAsync: () => mockGetPermissions(),
  requestPermissionsAsync: () => mockRequestPermissions(),
  getExpoPushTokenAsync: (options: unknown) => mockGetExpoPushToken(options),
  setNotificationChannelAsync: (id: string, config: unknown) => mockSetChannel(id, config),
  AndroidImportance: { DEFAULT: 5 },
}));

const mockSecureGetItem = jest.fn();
const mockSecureSetItem = jest.fn();
const mockSecureDeleteItem = jest.fn();

jest.mock("expo-secure-store", () => ({
  getItemAsync: (key: string) => mockSecureGetItem(key),
  setItemAsync: (key: string, value: string) => mockSecureSetItem(key, value),
  deleteItemAsync: (key: string) => mockSecureDeleteItem(key),
}));

// Inline factory (no external ref — avoids the babel-jest-hoist TDZ where a
// `mock`-prefixed const is still undefined when the factory first runs);
// tests mutate the imported `Constants` object below to vary the projectId.
jest.mock("expo-constants", () => ({
  __esModule: true,
  default: { expoConfig: { extra: { eas: { projectId: "proj-1" } } }, easConfig: undefined },
}));

const constantsMock = Constants as unknown as {
  expoConfig: { extra: { eas: { projectId: string | undefined } } };
  easConfig: { projectId?: string } | undefined;
};

jest.mock("@/api/devices", () => ({
  registerDevice: jest.fn(),
  unregisterDevice: jest.fn(),
}));

const mockRegisterDevice = registerDevice as jest.Mock;
const mockUnregisterDevice = unregisterDevice as jest.Mock;

const TOKEN = "ExponentPushToken[abc123]";

function setPlatform(os: string): void {
  (Platform as { OS: string }).OS = os;
}

const flush = () => new Promise<void>((resolve) => setImmediate(() => resolve()));

beforeEach(() => {
  jest.clearAllMocks();
  clearRegisteredDeviceId();
  setPlatform("ios");
  constantsMock.expoConfig = { extra: { eas: { projectId: "proj-1" } } };
  constantsMock.easConfig = undefined;
  mockGetPermissions.mockResolvedValue({ status: "granted", canAskAgain: true });
  mockRequestPermissions.mockResolvedValue({ status: "granted", canAskAgain: true });
  mockGetExpoPushToken.mockResolvedValue({ type: "expo", data: TOKEN });
  mockSetChannel.mockResolvedValue({});
  mockRegisterDevice.mockResolvedValue({
    id: "dev-1",
    platform: "ios",
    created_at: "2026-07-21T00:00:00Z",
  });
  mockUnregisterDevice.mockResolvedValue({ status: "deleted" });
  mockSecureGetItem.mockResolvedValue(null);
  mockSecureSetItem.mockResolvedValue(undefined);
  mockSecureDeleteItem.mockResolvedValue(undefined);
});

afterAll(() => setPlatform("ios"));

describe("registerForPushNotificationsAsync — the on-sign-in path", () => {
  it("POSTs exactly { token, platform:'ios' } and stores the returned device id", async () => {
    const id = await registerForPushNotificationsAsync();

    expect(mockGetExpoPushToken).toHaveBeenCalledWith({ projectId: "proj-1" });
    expect(mockRegisterDevice).toHaveBeenCalledWith({ token: TOKEN, platform: "ios" });
    expect(id).toBe("dev-1");
    expect(getRegisteredDeviceId()).toBe("dev-1");
  });

  it("never fetches a token or POSTs when permission is denied (push is not a gate)", async () => {
    mockGetPermissions.mockResolvedValue({ status: "denied", canAskAgain: false });

    const id = await registerForPushNotificationsAsync();

    expect(id).toBeNull();
    expect(mockGetExpoPushToken).not.toHaveBeenCalled();
    expect(mockRegisterDevice).not.toHaveBeenCalled();
  });

  it("no-ops when no EAS projectId is configured yet (founder-gated external)", async () => {
    constantsMock.expoConfig.extra = { eas: { projectId: undefined } };

    const id = await registerForPushNotificationsAsync();

    expect(id).toBeNull();
    expect(mockGetExpoPushToken).not.toHaveBeenCalled();
    expect(mockRegisterDevice).not.toHaveBeenCalled();
  });

  it("returns null (never throws) when the token fetch fails, e.g. a simulator with no push", async () => {
    mockGetExpoPushToken.mockRejectedValue(new Error("no push capability"));

    await expect(registerForPushNotificationsAsync()).resolves.toBeNull();
    expect(mockRegisterDevice).not.toHaveBeenCalled();
  });

  it("returns null and stores nothing when POST /v1/devices fails (best-effort)", async () => {
    mockRegisterDevice.mockRejectedValue(new Error("network"));

    const id = await registerForPushNotificationsAsync();

    expect(id).toBeNull();
    expect(getRegisteredDeviceId()).toBeNull();
  });

  it("on android, creates the default channel first and POSTs platform 'android'", async () => {
    setPlatform("android");
    mockRegisterDevice.mockResolvedValue({ id: "dev-2", platform: "android", created_at: "x" });

    await registerForPushNotificationsAsync();

    expect(mockSetChannel).toHaveBeenCalledWith(
      "default",
      expect.objectContaining({ name: "default" }),
    );
    expect(mockRegisterDevice).toHaveBeenCalledWith({ token: TOKEN, platform: "android" });
  });

  it("no-ops on an unsupported platform (web) without even reading permission", async () => {
    setPlatform("web");

    const id = await registerForPushNotificationsAsync();

    expect(id).toBeNull();
    expect(mockGetPermissions).not.toHaveBeenCalled();
  });
});

describe("getPushPermissionState — reads without prompting", () => {
  it("maps the OS status/canAskAgain through, never calling the request dialog", async () => {
    mockGetPermissions.mockResolvedValue({ status: "denied", canAskAgain: false });

    await expect(getPushPermissionState()).resolves.toEqual({
      status: "denied",
      canAskAgain: false,
    });
    expect(mockRequestPermissions).not.toHaveBeenCalled();
  });

  it("reports 'unsupported' on a platform with no push concept (web)", async () => {
    setPlatform("web");
    await expect(getPushPermissionState()).resolves.toEqual({
      status: "unsupported",
      canAskAgain: false,
    });
  });
});

describe("requestPushPermission — the explicit landlord tap", () => {
  it("returns granted and kicks off registration", async () => {
    const state = await requestPushPermission();
    await flush();

    expect(state.status).toBe("granted");
    expect(mockRegisterDevice).toHaveBeenCalledWith({ token: TOKEN, platform: "ios" });
  });

  it("returns denied and does NOT register when the landlord declines the dialog", async () => {
    mockRequestPermissions.mockResolvedValue({ status: "denied", canAskAgain: false });

    const state = await requestPushPermission();
    await flush();

    expect(state).toEqual({ status: "denied", canAskAgain: false });
    expect(mockRegisterDevice).not.toHaveBeenCalled();
  });
});

describe("unregisterCurrentDeviceBestEffort — the sign-out path", () => {
  it("DELETEs the stored device id and clears it", async () => {
    await registerForPushNotificationsAsync();
    expect(getRegisteredDeviceId()).toBe("dev-1");

    await unregisterCurrentDeviceBestEffort();

    expect(mockUnregisterDevice).toHaveBeenCalledWith("dev-1");
    expect(getRegisteredDeviceId()).toBeNull();
  });

  it("does nothing when there is no registered device", async () => {
    await unregisterCurrentDeviceBestEffort();
    expect(mockUnregisterDevice).not.toHaveBeenCalled();
  });

  it("never throws when the DELETE fails, and still clears the local id (a failed unregister can't block sign-out)", async () => {
    await registerForPushNotificationsAsync();
    mockUnregisterDevice.mockRejectedValue(new Error("network"));

    await expect(unregisterCurrentDeviceBestEffort()).resolves.toBeUndefined();
    expect(getRegisteredDeviceId()).toBeNull();
  });

  // B3-8 (#284): the durable marker, separate from the in-memory
  // `registeredDeviceId` above.
  it("clears the durable SecureStore marker too, on a confirmed successful DELETE", async () => {
    await registerForPushNotificationsAsync();
    expect(mockSecureSetItem).toHaveBeenCalledWith(expect.any(String), "dev-1");

    await unregisterCurrentDeviceBestEffort();

    expect(mockSecureDeleteItem).toHaveBeenCalledWith(expect.any(String));
  });

  it("leaves the durable SecureStore marker in place when the DELETE fails, B3-8's reconcile step is what's supposed to catch this", async () => {
    await registerForPushNotificationsAsync();
    mockUnregisterDevice.mockRejectedValue(new Error("network"));

    await unregisterCurrentDeviceBestEffort();

    expect(mockSecureDeleteItem).not.toHaveBeenCalled();
  });
});

describe("B3-8 (#284): registerForPushNotificationsAsync persists a durable marker", () => {
  it("writes the returned device id to SecureStore alongside the in-memory id", async () => {
    const id = await registerForPushNotificationsAsync();

    expect(id).toBe("dev-1");
    expect(mockSecureSetItem).toHaveBeenCalledWith(expect.any(String), "dev-1");
  });

  it("still returns the device id even when the SecureStore write itself fails (never turns a successful POST into a failed registration)", async () => {
    mockSecureSetItem.mockRejectedValue(new Error("keychain write failed"));

    await expect(registerForPushNotificationsAsync()).resolves.toBe("dev-1");
    expect(getRegisteredDeviceId()).toBe("dev-1");
  });
});

describe("reconcileStaleDeviceRegistration, B3-8's reconcile-on-next-sign-in cleanup", () => {
  it("does nothing when there is no persisted marker", async () => {
    mockSecureGetItem.mockResolvedValue(null);

    await reconcileStaleDeviceRegistration();

    expect(mockUnregisterDevice).not.toHaveBeenCalled();
    expect(mockSecureDeleteItem).not.toHaveBeenCalled();
  });

  it("DELETEs the persisted stale device id and clears the marker on success", async () => {
    mockSecureGetItem.mockResolvedValue("dev-stale-from-a-forced-signout");

    await reconcileStaleDeviceRegistration();

    expect(mockUnregisterDevice).toHaveBeenCalledWith(
      "dev-stale-from-a-forced-signout",
      expect.objectContaining({ signal: expect.anything() }),
    );
    expect(mockSecureDeleteItem).toHaveBeenCalledWith(expect.any(String));
  });

  it("still clears the marker even when the DELETE fails, one best-effort attempt, not a retry queue (the accepted residual gap)", async () => {
    mockSecureGetItem.mockResolvedValue("dev-stale-from-a-forced-signout");
    mockUnregisterDevice.mockRejectedValue(new Error("network"));

    await expect(reconcileStaleDeviceRegistration()).resolves.toBeUndefined();

    expect(mockUnregisterDevice).toHaveBeenCalledWith(
      "dev-stale-from-a-forced-signout",
      expect.objectContaining({ signal: expect.anything() }),
    );
    expect(mockSecureDeleteItem).toHaveBeenCalledWith(expect.any(String));
  });

  it("never throws when SecureStore itself can't be read (unreadable keychain isn't actionable)", async () => {
    mockSecureGetItem.mockRejectedValue(new Error("keychain read failed"));

    await expect(reconcileStaleDeviceRegistration()).resolves.toBeUndefined();
    expect(mockUnregisterDevice).not.toHaveBeenCalled();
  });
});

describe("reconcileStaleDeviceRegistration, #284 adversarial review finding 2: the DELETE it fires cannot outlive a concurrent registration", () => {
  afterEach(() => {
    jest.useRealTimers();
  });

  it("ABORTS the in-flight DELETE at the timeout deadline, rather than merely walking away from it client-side", async () => {
    jest.useFakeTimers({ doNotFake: ["nextTick", "queueMicrotask"] });
    mockSecureGetItem.mockResolvedValue("dev-stale-from-a-forced-signout");
    let capturedSignal: AbortSignal | undefined;
    let settleDelete: (() => void) | undefined;
    mockUnregisterDevice.mockImplementation(
      (_id: string, options?: { signal?: AbortSignal }) =>
        new Promise((resolve) => {
          capturedSignal = options?.signal;
          settleDelete = () => resolve({ status: "deleted" });
        }),
    );

    const pending = reconcileStaleDeviceRegistration();
    // RECONCILE_TIMEOUT_MS - the deadline this DELETE is raced against.
    await jest.advanceTimersByTimeAsync(3000);

    // The proof this fix exists for: the request itself is aborted, not
    // just abandoned. Before this fix there was no AbortController at all,
    // so a real fetch behind this mock would still be in flight here and
    // could still land on the server after a concurrent registration under
    // the SAME id (the backend upsert preserves it) had already gone live.
    expect(capturedSignal).toBeDefined();
    expect(capturedSignal?.aborted).toBe(true);

    // Let the now-aborted request settle late (standing in for a slow
    // connection) so this test doesn't leave a dangling promise behind.
    settleDelete?.();
    await pending;
  });

  it("bails without DELETing when this install's current live registration already claims the marker's exact id (upsert preserves id on the same token)", async () => {
    await registerForPushNotificationsAsync();
    expect(getRegisteredDeviceId()).toBe("dev-1");
    mockSecureGetItem.mockResolvedValue("dev-1");

    await reconcileStaleDeviceRegistration();

    expect(mockUnregisterDevice).not.toHaveBeenCalled();
    // The marker already correctly points at the live registration - bail
    // leaves it alone rather than wiping it.
    expect(mockSecureDeleteItem).not.toHaveBeenCalled();
  });

  it("does NOT clobber a fresher marker a concurrent successful registration wrote while the DELETE was in flight", async () => {
    // reconcile's own initial read of the marker it will act on...
    mockSecureGetItem.mockResolvedValueOnce("dev-stale-from-a-forced-signout");
    mockUnregisterDevice.mockResolvedValue({ status: "deleted" });
    // ...but by the time this call re-reads the key to decide whether to
    // clear it, a concurrent registration has already overwritten it with
    // a DIFFERENT, fresher id.
    mockSecureGetItem.mockResolvedValueOnce("dev-fresh-from-a-concurrent-registration");

    await reconcileStaleDeviceRegistration();

    expect(mockSecureDeleteItem).not.toHaveBeenCalled();
  });
});
