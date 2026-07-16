/**
 * One shared React Query client for the whole app (wired in
 * src/app/_layout.tsx). `retry: 1` — a landlord staring at a stuck queue
 * card is worse than one extra network round trip; the API client's own
 * `ApiError` already carries a stable `code` for the cases that shouldn't
 * be retried blindly (e.g. `draft_stale`), which the calling mutation
 * handles explicitly rather than relying on this default.
 */
import { AppState, type AppStateStatus } from "react-native";
import { QueryClient, focusManager } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 10_000,
    },
    mutations: {
      retry: 0,
    },
  },
});

// React Query's refetch-on-focus has no native signal on React Native (it's
// built for the browser's `visibilitychange`) — this is the documented
// TanStack Query + React Native wiring, mirroring src/lib/supabase.ts's own
// AppState listener, so "the queue is the app's heartbeat" (issue #210 M1
// brief) actually refetches when the app comes back to the foreground, not
// just on the polling interval.
function onAppStateChange(status: AppStateStatus): void {
  focusManager.setFocused(status === "active");
}

AppState.addEventListener("change", onAppStateChange);
