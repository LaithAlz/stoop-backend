/**
 * Expo app config (replaces the template's static app.json so config can
 * read env vars). Supabase settings come from EXPO_PUBLIC_SUPABASE_URL /
 * EXPO_PUBLIC_SUPABASE_ANON_KEY — see .env.example. They're mirrored into
 * `extra` so `expo-constants` can read them where the EXPO_PUBLIC_ inlining
 * doesn't apply (src/lib/env.ts checks both). The anon key is
 * publishable-by-design (RLS enforces access), but it still never gets
 * hardcoded or committed — env only.
 *
 * Store-facing metadata (final bundle IDs, icons, store listing) is
 * founder-gated (issue #210) — the identifiers below are placeholders for
 * simulator/dev-client use until store accounts exist.
 */
import type { ExpoConfig, ConfigContext } from "expo/config";

export default ({ config }: ConfigContext): ExpoConfig => ({
  ...config,
  name: "Stoop",
  slug: "stoop-mobile",
  version: "0.1.0",
  orientation: "portrait",
  icon: "./assets/images/icon.png",
  scheme: "stoop",
  // M0 is light-only (src/theme/tokens.ts); revisit when dark tokens land.
  userInterfaceStyle: "light",
  ios: {
    supportsTablet: false,
    bundleIdentifier: "com.stoop.app",
  },
  android: {
    package: "com.stoop.app",
    adaptiveIcon: {
      backgroundColor: "#FDFBF6", // Clarity --bg (docs/mockups/07, line 10)
      foregroundImage: "./assets/images/android-icon-foreground.png",
      backgroundImage: "./assets/images/android-icon-background.png",
      monochromeImage: "./assets/images/android-icon-monochrome.png",
    },
    predictiveBackGestureEnabled: false,
  },
  web: {
    output: "static",
    favicon: "./assets/images/favicon.png",
  },
  plugins: [
    "expo-router",
    "expo-secure-store",
    [
      "expo-splash-screen",
      {
        backgroundColor: "#FDFBF6", // Clarity --bg, not the template blue
        image: "./assets/images/splash-icon.png",
        imageWidth: 76,
      },
    ],
    // Push notifications (issue #210 M3). No custom Android icon/sound asset
    // is shipped yet — the default app icon is used for the small
    // notification icon; add a dedicated 96x96 all-white glyph here when
    // brand assets are finalized (founder-gated with the store submission).
    // `enableBackgroundRemoteNotifications` stays off: this app's push is a
    // foreground/tap approval nudge, never a silent background wake, and
    // NEVER the emergency path (CLAUDE.md rule 1).
    "expo-notifications",
  ],
  experiments: {
    typedRoutes: true,
    reactCompiler: true,
  },
  extra: {
    supabaseUrl: process.env.EXPO_PUBLIC_SUPABASE_URL,
    supabaseAnonKey: process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY,
    apiUrl: process.env.EXPO_PUBLIC_API_URL,
    // Expo push token attribution (issue #210 M3). Unset until the founder
    // creates the EAS project (founder-gated external — real push DELIVERY
    // needs an Expo/EAS account + APNs/FCM credentials); until then
    // `getExpoPushTokenAsync` has no project to attribute a token to, and
    // src/features/push/deviceRegistration.ts silently no-ops registration
    // (push stays an enhancement, never a gate). EAS Build injects this
    // automatically, but setting it explicitly here is the recommended
    // pattern (expo-notifications' own getExpoPushTokenAsync docs).
    eas: {
      projectId: process.env.EAS_PROJECT_ID,
    },
  },
});
