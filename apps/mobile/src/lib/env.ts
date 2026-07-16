/**
 * Reads and validates the Supabase client config the app needs at runtime.
 *
 * Values come from EXPO_PUBLIC_SUPABASE_URL / EXPO_PUBLIC_SUPABASE_ANON_KEY
 * (see apps/mobile/.env.example). Expo inlines `process.env.EXPO_PUBLIC_*`
 * into the bundle at build time — which is why each access below is a
 * static property access, never `process.env[name]` (the bundler can't
 * inline a dynamic lookup; `expo/no-dynamic-env-var` enforces this).
 * app.config.ts also mirrors the same two vars into `expo.extra`, so a
 * build where inlining didn't apply (e.g. some EAS Update flows) can still
 * read them via `expo-constants`. Never read apps/api/.env — this app has
 * its own env surface, and only ever the two public values.
 */
import Constants from "expo-constants";

function fromExtra(key: "supabaseUrl" | "supabaseAnonKey"): string | undefined {
  const extra = Constants.expoConfig?.extra as Record<string, unknown> | undefined;
  const value = extra?.[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function required(value: string | undefined, publicEnvVar: string): string {
  if (!value) {
    throw new Error(
      `Missing ${publicEnvVar}. Copy apps/mobile/.env.example to .env and fill in your ` +
        "Supabase project's URL/anon key (Supabase dashboard -> Project Settings -> API) " +
        "before running the app.",
    );
  }
  return value;
}

/** Lazy getters — evaluated on first access, not at module import, so
 *  bundling/typechecking never depends on a real .env being present. */
export const env = {
  get supabaseUrl(): string {
    return required(
      process.env.EXPO_PUBLIC_SUPABASE_URL ?? fromExtra("supabaseUrl"),
      "EXPO_PUBLIC_SUPABASE_URL",
    );
  },
  get supabaseAnonKey(): string {
    return required(
      process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY ?? fromExtra("supabaseAnonKey"),
      "EXPO_PUBLIC_SUPABASE_ANON_KEY",
    );
  },
};
