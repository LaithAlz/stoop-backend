/**
 * Reads and validates the Supabase + API client config the app needs at
 * runtime.
 *
 * Values come from EXPO_PUBLIC_SUPABASE_URL / EXPO_PUBLIC_SUPABASE_ANON_KEY /
 * EXPO_PUBLIC_API_URL (see apps/mobile/.env.example). Expo inlines
 * `process.env.EXPO_PUBLIC_*` into the bundle at build time — which is why
 * each access below is a static property access, never `process.env[name]`
 * (the bundler can't inline a dynamic lookup; `expo/no-dynamic-env-var`
 * enforces this). app.config.ts also mirrors the same vars into
 * `expo.extra`, so a build where inlining didn't apply (e.g. some EAS
 * Update flows) can still read them via `expo-constants`. Never read
 * apps/api/.env — this app has its own env surface, and only ever these
 * public values (never a service-role key).
 */
import Constants from "expo-constants";

/** http://localhost:8000 — the simulator's route to a local `uvicorn`
 *  (see apps/api/CLAUDE.md); iOS Simulator/Android emulator both resolve
 *  `localhost` to the host machine (unlike a physical device, which would
 *  need the host's LAN IP — out of scope for M1's simulator-first flow). */
const DEFAULT_API_URL = "http://localhost:8000";

function fromExtra(key: "supabaseUrl" | "supabaseAnonKey" | "apiUrl"): string | undefined {
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
  /** Unlike the two Supabase values, this has a sane default (the local API
   *  server) — never throws, since a fresh checkout should boot straight
   *  into "point at localhost" without any .env edits. */
  get apiUrl(): string {
    return process.env.EXPO_PUBLIC_API_URL ?? fromExtra("apiUrl") ?? DEFAULT_API_URL;
  },
};
