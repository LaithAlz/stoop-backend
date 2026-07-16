/**
 * Root layout — the auth gate. Wraps the whole app in AuthProvider and a
 * shared React Query client (src/api/queryClient.ts — issue #210 M1's
 * typed API client uses React Query for caching/refetch, matching the web
 * app's TanStack family), then uses Expo Router's Protected Routes
 * (`Stack.Protected`) to show either the sign-in screen or the tab shell,
 * driven by `resolveAuthRoute` (the single source of truth for that
 * decision — also unit-tested directly in src/app/__tests__/
 * auth-gate.test.tsx).
 */
import { useEffect } from "react";
import { Stack } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, useAuth } from "@/auth/AuthProvider";
import { resolveAuthRoute } from "@/auth/resolveAuthRoute";
import { queryClient } from "@/api/queryClient";
import { colors } from "@/theme/tokens";

// Keep the native splash screen up until we know whether there's a session,
// so signed-in users never see a flash of the sign-in screen underneath it.
SplashScreen.preventAutoHideAsync().catch(() => {
  // Already hidden, or unsupported on this platform (e.g. web) — fine to ignore.
});

export default function RootLayout() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RootNavigator />
      </AuthProvider>
    </QueryClientProvider>
  );
}

function RootNavigator() {
  const { session, initializing } = useAuth();
  const route = resolveAuthRoute({ session, initializing });

  useEffect(() => {
    if (route !== "loading") {
      SplashScreen.hideAsync().catch(() => {});
    }
  }, [route]);

  if (route === "loading") {
    // Native splash is still covering the screen — render nothing so there's
    // no flash of empty content underneath it.
    return null;
  }

  return (
    <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: colors.bg } }}>
      <Stack.Protected guard={route === "tabs"}>
        <Stack.Screen name="(tabs)" />
      </Stack.Protected>
      <Stack.Protected guard={route === "sign-in"}>
        <Stack.Screen name="sign-in" />
      </Stack.Protected>
    </Stack>
  );
}
