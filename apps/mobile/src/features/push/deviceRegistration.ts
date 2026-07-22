/**
 * Expo push registration (issue #210 M3) — the one place that touches
 * expo-notifications' native calls and `POST/DELETE /v1/devices`
 * (src/api/devices.ts). Kept out of React entirely so both the
 * registration hook (src/features/push/usePushRegistration.ts, mounted
 * only inside the signed-in tab shell) and src/auth/AuthProvider.tsx's
 * sign-out flow can call into it without either importing the other's
 * hook.
 *
 * Never logs the push token itself (credential-adjacent, CLAUDE.md rule
 * 5-adjacent) — nothing below has a `console.*`/logging call of any kind,
 * mirroring src/api/client.ts's own "never log a payload" discipline and
 * the backend's `app/routers/devices.py` docstring ("Never logs a push
 * token").
 *
 * Push is an enhancement, never a gate (issue #210 M3 brief) — every
 * function below fails silently (returns `null`/resolves) rather than
 * throwing, so a missing EAS project id, a denied permission, a simulator
 * that can't produce a real token, or a network failure all leave the
 * rest of the app working exactly as if push didn't exist.
 */
import { Platform } from "react-native";
import Constants from "expo-constants";
import * as Notifications from "expo-notifications";
import { registerDevice, unregisterDevice } from "@/api/devices";
import type { DevicePlatform } from "@/api/types";
import type { PushPermissionState } from "./pushControl";

/** The one physical-token registration this app install currently has
 *  live with the backend — set after a successful `POST /v1/devices`,
 *  cleared on sign-out (src/auth/AuthProvider.tsx). In-memory only: a
 *  fresh app launch re-registers from scratch, and the upsert is
 *  idempotent (api-contracts.md's Devices section — "re-registering the
 *  SAME token under the SAME landlord... is a no-op, never a 409"), so
 *  this never needs to survive a process restart. */
let registeredDeviceId: string | null = null;

/** Test seam. */
export function getRegisteredDeviceId(): string | null {
  return registeredDeviceId;
}

/** Called from src/auth/AuthProvider.tsx's SIGNED_OUT handling, alongside
 *  queryClient.clear()/resetOnboardingOffer() — a pure local-state reset
 *  with no network call, safe to run even when there's no live session
 *  left to authenticate a DELETE with (unlike
 *  `unregisterCurrentDeviceBestEffort` below). */
export function clearRegisteredDeviceId(): void {
  registeredDeviceId = null;
}

function currentPlatform(): DevicePlatform | null {
  // Expo push tokens have no 'web' concept (schema-v1.md's push_tokens.
  // platform CHECK note) -- Platform.OS on web/other resolves to `null`
  // here, which every caller treats as "push doesn't apply on this run".
  return Platform.OS === "ios" || Platform.OS === "android" ? Platform.OS : null;
}

/** SDK 57's `getExpoPushTokenAsync` reads this itself when `projectId` is
 *  omitted, but this app checks it explicitly first so "no EAS project
 *  configured yet" (issue #210: "founder-gated externals") is a visible,
 *  testable no-op branch rather than an implicit fallback inside a
 *  try/catch. `app.config.ts` mirrors `EAS_PROJECT_ID` into
 *  `extra.eas.projectId` — unset today, so this always returns
 *  `undefined` until that founder step happens, which is exactly the
 *  "registration silently no-ops" path exercised in this module's tests. */
function projectId(): string | undefined {
  const extra = Constants.expoConfig?.extra as { eas?: { projectId?: string } } | undefined;
  return (
    extra?.eas?.projectId ??
    (Constants as { easConfig?: { projectId?: string } }).easConfig?.projectId
  );
}

const ANDROID_DEFAULT_CHANNEL_ID = "default";

/** Android 8+ requires at least one notification channel to exist before
 *  the system permission prompt / a push token even makes sense (Expo's
 *  own push-notifications-setup guide). A no-op on iOS. Safe to call
 *  repeatedly — `setNotificationChannelAsync` re-asserts the same
 *  channel's config idempotently, it does not duplicate/reset it. */
async function ensureAndroidChannelAsync(): Promise<void> {
  if (Platform.OS !== "android") return;
  await Notifications.setNotificationChannelAsync(ANDROID_DEFAULT_CHANNEL_ID, {
    name: "default",
    importance: Notifications.AndroidImportance.DEFAULT,
  });
}

function toPermissionState(
  settings: Notifications.NotificationPermissionsStatus,
): PushPermissionState {
  return { status: settings.status, canAskAgain: settings.canAskAgain };
}

/** Reads the OS permission WITHOUT prompting. The Me tab's status display
 *  and this module's own "should I even try to register" check both go
 *  through this — never `requestPushPermission` (which shows the native
 *  dialog), so neither ever nags. */
export async function getPushPermissionState(): Promise<PushPermissionState> {
  if (currentPlatform() === null) return { status: "unsupported", canAskAgain: false };
  try {
    return toPermissionState(await Notifications.getPermissionsAsync());
  } catch {
    // e.g. a host/simulator with no push capability at all.
    return { status: "unsupported", canAskAgain: false };
  }
}

/** Prompts the native dialog — only ever called from an explicit landlord
 *  tap on the Me tab's "Turn on notifications" button (see
 *  src/features/push/usePushPermission.ts), never automatically. On
 *  success, also kicks off registration so the landlord doesn't have to
 *  background/foreground the app for the POST to fire. */
export async function requestPushPermission(): Promise<PushPermissionState> {
  if (currentPlatform() === null) return { status: "unsupported", canAskAgain: false };
  let state: PushPermissionState;
  try {
    await ensureAndroidChannelAsync();
    state = toPermissionState(await Notifications.requestPermissionsAsync());
  } catch {
    return { status: "unsupported", canAskAgain: false };
  }
  if (state.status === "granted") {
    void registerForPushNotificationsAsync();
  }
  return state;
}

/**
 * The registration attempt: permission must ALREADY be granted (this
 * function never prompts — see `requestPushPermission` for the explicit
 * ask). Resolves `null` (never throws) on anything that keeps push an
 * enhancement rather than a gate: permission not granted, no EAS project
 * id yet, a simulator/host that can't produce a real token, or a
 * `POST /v1/devices` failure.
 */
export async function registerForPushNotificationsAsync(): Promise<string | null> {
  const platform = currentPlatform();
  if (platform === null) return null;

  const permission = await getPushPermissionState();
  if (permission.status !== "granted") return null;

  const id = projectId();
  if (!id) return null; // No EAS project configured yet (founder-gated, issue #210).

  await ensureAndroidChannelAsync();

  let token: string;
  try {
    const result = await Notifications.getExpoPushTokenAsync({ projectId: id });
    token = result.data;
  } catch {
    return null; // Simulator with no push capability, or a transient Expo API failure.
  }

  try {
    const device = await registerDevice({ token, platform });
    registeredDeviceId = device.id;
    return device.id;
  } catch {
    return null; // Network/server failure -- best-effort, never surfaced as a gate.
  }
}

const UNREGISTER_TIMEOUT_MS = 3000;

/**
 * Sign-out unregister — called from src/auth/AuthProvider.tsx's `signOut`
 * BEFORE `supabase.auth.signOut()` runs (deliberately, not from the
 * SIGNED_OUT listener): `DELETE /v1/devices/{id}` needs a still-live
 * bearer token, which src/api/client.ts's `authHeader()` reads fresh from
 * the CURRENT supabase session on every call — once `supabase.auth.
 * signOut()` has actually cleared that session, the same call would 401
 * before ever reaching the server. Bounded to `UNREGISTER_TIMEOUT_MS` and
 * never throws (a failed OR slow unregister must not block sign-out —
 * issue #210 M3; the backend also fails closed on delivery to a
 * reassigned/deleted device regardless, per app/push_outbox.py's
 * ownership-transfer safety guard).
 */
export async function unregisterCurrentDeviceBestEffort(): Promise<void> {
  const id = registeredDeviceId;
  registeredDeviceId = null;
  if (!id) return;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      unregisterDevice(id),
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error("device unregister timed out")),
          UNREGISTER_TIMEOUT_MS,
        );
      }),
    ]);
  } catch {
    // Best-effort -- see docstring above.
  } finally {
    // Clear the deadline timer whichever side of the race won — so a
    // fast DELETE never leaves a pending 3s timer that would later reject
    // an orphan promise (and, in tests, leak an open handle).
    if (timer) clearTimeout(timer);
  }
}
