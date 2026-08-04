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
import * as SecureStore from "expo-secure-store";
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
 *  `unregisterCurrentDeviceBestEffort` below). Deliberately leaves the
 *  DURABLE marker below untouched, see its own docstring for why (B3-8,
 *  #284). */
export function clearRegisteredDeviceId(): void {
  registeredDeviceId = null;
}

/** B3-8 (#284): SecureStore key for "the device id this install's most
 *  recent successful `POST /v1/devices` returned, which we have not yet
 *  CONFIRMED unregistering." Durable, unlike `registeredDeviceId` above,
 *  because the two paths this exists for both outlive the in-memory value:
 *
 *  1. The forced (401) sign-out path (src/api/client.ts) has no live token
 *     to authenticate a DELETE with, so it never even tries, it just
 *     clears the local session, which fires `clearRegisteredDeviceId`
 *     above and wipes the in-memory id before anything could act on it.
 *  2. An offline explicit sign-out (#284's B3-5, src/auth/AuthProvider.tsx)
 *     can fail `unregisterCurrentDeviceBestEffort` below for the identical
 *     underlying reason (no network), same gap, different trigger.
 *
 *  Left alone, the server keeps a live `push_tokens` row for a device now
 *  sitting at the sign-in wall and keeps enqueuing nudges into it, wasted
 *  sends into a gate, not a security/PII issue on its own (the push body
 *  is a fixed generic string, api/app/push_outbox.py). `reconcile
 *  StaleDeviceRegistration` below is the cleanup; this key is what makes
 *  that possible even after the app is force-quit and relaunched. */
const PENDING_UNREGISTER_KEY = "stoop-pending-device-unregister";

/** Best-effort, never throws, a failure to persist just means B3-8's
 *  cleanup won't catch THIS particular registration later; the
 *  registration itself (the thing that actually matters for push to work)
 *  already succeeded by the time this is called. */
async function persistPendingUnregister(id: string): Promise<void> {
  try {
    await SecureStore.setItemAsync(PENDING_UNREGISTER_KEY, id);
  } catch {
    // See docstring above.
  }
}

/** Best-effort, never throws, same posture as every other SecureStore
 *  touch in this app (src/api/client.ts's B3-2: an unreadable keychain is
 *  not actionable, not a reason to surface an error here). */
async function clearPendingUnregister(): Promise<void> {
  try {
    await SecureStore.deleteItemAsync(PENDING_UNREGISTER_KEY);
  } catch {
    // See docstring above.
  }
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
    // B3-8: this is now the one registration this install believes is
    // live, persist it durably too (see PENDING_UNREGISTER_KEY's
    // docstring) so a forced or offline sign-out that can't unregister it
    // still leaves a trail the next successful sign-in can clean up.
    // Fire-and-forget: a SecureStore write failure here must never turn a
    // successful `POST /v1/devices` into a failed registration.
    void persistPendingUnregister(device.id);
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
 *
 * B3-8 (#284): on a CONFIRMED successful DELETE, also clears the durable
 * marker, a clean, online sign-out leaves nothing for
 * `reconcileStaleDeviceRegistration` to redo later. On failure/timeout the
 * marker is deliberately left in place; that reconcile step is exactly
 * what's supposed to catch this case on the next successful sign-in.
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
    // B3-8: confirmed gone server-side, see this function's docstring.
    await clearPendingUnregister();
  } catch {
    // Best-effort -- see docstring above. The durable marker (if any) is
    // deliberately left in place here, see this function's docstring.
  } finally {
    // Clear the deadline timer whichever side of the race won — so a
    // fast DELETE never leaves a pending 3s timer that would later reject
    // an orphan promise (and, in tests, leak an open handle).
    if (timer) clearTimeout(timer);
  }
}

const RECONCILE_TIMEOUT_MS = 3000;

/**
 * B3-8 (#284): called from src/auth/AuthProvider.tsx's `signIn`,
 * fire-and-forget, right after a successful password sign-in, see
 * `PENDING_UNREGISTER_KEY`'s docstring for the two paths (forced 401,
 * offline explicit sign-out) that can leave a marker here for this to
 * find.
 *
 * Deliberately ONE attempt, not a retry queue: this cleans up a
 * low-severity annoyance (an extra push nudge landing on a device sitting
 * at the sign-in wall, never a security/PII issue on its own, see the key
 * docstring), not a security control, so a transient failure at this exact
 * moment (the landlord's connection could still be spotty in the seconds
 * right after typing a password back on a subway) is an accepted residual
 * gap rather than justifying a durable multi-attempt queue for it, the
 * marker is cleared either way, success or failure, so this only ever
 * fires once per stale registration. A 404 (the row is already gone, e.g.
 * an admin already deleted it, or a DIFFERENT landlord's `DELETE` from a
 * shared device 403s) is swallowed the same as every other outcome here;
 * there is nothing actionable left to do with any of them.
 */
export async function reconcileStaleDeviceRegistration(): Promise<void> {
  let id: string | null;
  try {
    id = await SecureStore.getItemAsync(PENDING_UNREGISTER_KEY);
  } catch {
    // Same "an unreadable keychain isn't actionable" posture as
    // src/api/client.ts's B3-2, nothing to clean up if it can't be read.
    return;
  }
  if (!id) return;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      unregisterDevice(id),
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error("stale device unregister timed out")),
          RECONCILE_TIMEOUT_MS,
        );
      }),
    ]);
  } catch {
    // Best-effort, single attempt -- see docstring above.
  } finally {
    if (timer) clearTimeout(timer);
    await clearPendingUnregister();
  }
}
