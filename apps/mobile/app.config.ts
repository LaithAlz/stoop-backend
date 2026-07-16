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
  ],
  experiments: {
    typedRoutes: true,
    reactCompiler: true,
  },
  extra: {
    supabaseUrl: process.env.EXPO_PUBLIC_SUPABASE_URL,
    supabaseAnonKey: process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY,
    apiUrl: process.env.EXPO_PUBLIC_API_URL,
  },
});
