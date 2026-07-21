/**
 * Push registration lifecycle (issue #210 M3) — mounted once from
 * src/app/(tabs)/_layout.tsx, alongside the onboarding gate. That
 * placement is deliberate: `(tabs)` only ever renders behind the root
 * layout's `Stack.Protected guard={route === "tabs"}` (src/app/_layout.tsx),
 * so this hook only runs for an authenticated session — exactly "on
 * sign-in" (and again on every subsequent cold launch with a still-live
 * session, which is a safe no-op thanks to the registration upsert).
 *
 * Four independent effects:
 * 1. Registers (or silently no-ops) on mount — see
 *    deviceRegistration.ts's `registerForPushNotificationsAsync` for every
 *    reason this can be a no-op (permission not granted, no EAS project
 *    yet, simulator, network failure); never prompts for permission here
 *    (see src/features/push/usePushPermission.ts for the explicit ask).
 * 2. Foreground display — the OS default is to suppress a banner while the
 *    app is open; this app wants the landlord to actually see the nudge.
 * 3. Received listener — refetches the queue (its own "heartbeat",
 *    src/api/queue.ts) the instant a push arrives, so the badge/list is
 *    fresh without waiting for the next poll/focus event.
 * 4. Tap → deep-link — `Notifications.useLastNotificationResponse()`
 *    covers both a tap while the app is running/backgrounded AND the
 *    "launched fresh by tapping a push" cold-start case in one seam (its
 *    own docstring), so there's no second `addNotificationResponseReceived
 *    Listener` racing it. Routes via src/features/push/deepLink.ts's pure
 *    resolver — this hook never reads/renders anything from the payload
 *    beyond what that resolver returns (no tenant content ever transits a
 *    push; see app/push_outbox.py's "Payload safety").
 * 5. Token rotation — Expo can roll the underlying device token at
 *    runtime; `addPushTokenListener` re-runs the same registration call
 *    when that happens.
 */
import { useEffect } from "react";
import * as Notifications from "expo-notifications";
import { useRouter } from "expo-router";
import { useQueryClient } from "@tanstack/react-query";
import { queueQueryKey } from "@/api/queue";
import { registerForPushNotificationsAsync } from "./deviceRegistration";
import { resolveNotificationDeepLink } from "./deepLink";

// Module-scope, run-once side effect (mirrors src/api/queryClient.ts's own
// top-level `AppState.addEventListener` — set up exactly once, not per
// hook mount, since remounting this hook must never re-register a second
// competing handler).
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: false,
    shouldSetBadge: false,
  }),
});

export function usePushRegistration(): void {
  const router = useRouter();
  const queryClient = useQueryClient();
  const lastResponse = Notifications.useLastNotificationResponse();

  useEffect(() => {
    void registerForPushNotificationsAsync();
  }, []);

  useEffect(() => {
    const subscription = Notifications.addNotificationReceivedListener(() => {
      void queryClient.invalidateQueries({ queryKey: queueQueryKey });
    });
    return () => subscription.remove();
  }, [queryClient]);

  useEffect(() => {
    if (!lastResponse) return; // undefined (not yet known) or null (none received) -- nothing to do.
    const target = resolveNotificationDeepLink(lastResponse.notification.request.content.data);
    if (target) router.push(target);
  }, [lastResponse, router]);

  useEffect(() => {
    const subscription = Notifications.addPushTokenListener(() => {
      void registerForPushNotificationsAsync();
    });
    return () => subscription.remove();
  }, []);
}
