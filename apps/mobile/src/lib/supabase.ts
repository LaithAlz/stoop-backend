/**
 * Supabase client for the mobile app — mirrors the auth model
 * docs/03-engineering/api-contracts.md describes for the API
 * (`Authorization: Bearer <supabase JWT>`, `role: authenticated`).
 *
 * apps/web's own /sign-in route (apps/web/src/routes/sign-in.tsx) is a
 * non-functional mock today (magic-link/social buttons that just call
 * `alert(...)`) — there is no working client-side auth call to mirror
 * there. This client implements the real thing directly against
 * @supabase/supabase-js, email+password, per issue #210's M0 scope.
 *
 * Session storage uses `expo-secure-store` via the adapter shape from
 * Supabase's own Expo/React Native quickstart
 * (https://supabase.com/docs/guides/auth/quickstarts/react-native) — "the
 * recommended Expo secure storage adapter". Known caveat: SecureStore has a
 * ~2048-byte per-item limit on some platforms; a Supabase session (access +
 * refresh token + user metadata) usually fits, but if a future field pushes
 * it over, the documented next step is a hybrid AES-encrypted AsyncStorage
 * blob (Supabase calls this pattern "LargeSecureStore") — worth revisiting
 * in M1/M2 if `setItem` ever throws for a real user.
 */
import "react-native-url-polyfill/auto";
import { AppState } from "react-native";
import * as SecureStore from "expo-secure-store";
import { createClient, type SupportedStorage } from "@supabase/supabase-js";
import { env } from "@/lib/env";

const ExpoSecureStoreAdapter: SupportedStorage = {
  getItem: (key) => SecureStore.getItemAsync(key),
  setItem: (key, value) => SecureStore.setItemAsync(key, value),
  removeItem: (key) => SecureStore.deleteItemAsync(key),
};

export const supabase = createClient(env.supabaseUrl, env.supabaseAnonKey, {
  auth: {
    storage: ExpoSecureStoreAdapter,
    autoRefreshToken: true,
    persistSession: true,
    // No URL-based OAuth/magic-link redirect flow in M0 (email+password
    // only), and there's no browser location to parse on native anyway.
    detectSessionInUrl: false,
  },
});

// Supabase's client only refreshes the session on a timer while something is
// actively driving it; on native that means pausing while the app is
// backgrounded (saves battery/network) and resuming on foreground. This is
// the exact pattern from Supabase's Expo quickstart.
AppState.addEventListener("change", (state) => {
  if (state === "active") {
    void supabase.auth.startAutoRefresh();
  } else {
    void supabase.auth.stopAutoRefresh();
  }
});
