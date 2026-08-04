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
 *
 * #292: EXPO_PUBLIC_API_URL's scheme is checked in production builds
 * (`requireHttpsInProduction` below), matching apps/api/app/config.py's
 * `_require_non_local_dashboard_origins_in_production` (#255) — read that
 * before touching this. The #263 401 liveness gate anchors the "is this
 * session actually dead" decision on the response's `Date` header; that
 * anchor is only tamper-proof over HTTPS. A plaintext `http://` API origin
 * shipped in a production build would hand an on-path attacker a forced
 * sign-out lever (a forged/rewritten Date header) and worse, so this
 * refuses to boot rather than silently run a shipped app against a
 * plaintext API. Dev/staging keep working on `http://localhost:8000` and a
 * LAN address (a physical device's route to a local `uvicorn`) unchanged —
 * the check only ever runs in a genuine production build.
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

/**
 * True only in a genuine release/production JS bundle. `__DEV__` is a
 * build-time constant the bundler inlines (`false` in every packaged
 * production .ipa/.apk/AAB, `true` under Metro/Expo Go's dev server or a
 * dev client) — never a runtime environment variable, so nothing that
 * reaches this app at runtime (including the very `EXPO_PUBLIC_API_URL`
 * value being checked below) can flip it. This app's own equivalent of
 * apps/api/app/config.py's `Settings.is_production` — same "which
 * environment am I in" question; there is no `ENVIRONMENT` var on this
 * side of the fence, so `__DEV__` is the honest, attacker-can't-touch-it
 * signal Expo/React Native already provides for it.
 */
function isProductionBuild(): boolean {
  return !__DEV__;
}

/**
 * The URL's scheme (lowercased, no trailing `:`/`//`), or `null` when the
 * value isn't shaped like `scheme://...` at all. A narrow regex extract,
 * not a full `new URL(...)` parse — this only ever needs to report the
 * scheme in an error message, and must never risk round-tripping the rest
 * of the configured value (which can carry embedded credentials,
 * CLAUDE.md rule 5) through anything that might stringify it back out.
 */
function schemeOf(url: string): string | null {
  const match = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\//.exec(url.trim());
  return match ? match[1].toLowerCase() : null;
}

/**
 * Refuses a non-`https` API origin in a production build (#292), matching
 * apps/api/app/config.py's `_require_non_local_dashboard_origins_in_
 * production` (#255) — same "reject plaintext in production" call, made
 * for the mobile client's own EXPO_PUBLIC_API_URL rather than the API's
 * DASHBOARD_ORIGINS. Dev/staging (`isProd = false`) skip this entirely, so
 * `http://localhost:8000` and a LAN address keep working unchanged.
 *
 * Fails LOUD — throws at first access, never silently falls back — because
 * a misconfigured production build must be obvious at startup, not
 * discovered live. Never includes the full configured value in the thrown
 * message (only the scheme): the value can carry embedded credentials.
 */
function requireHttpsInProduction(url: string, isProd: boolean): string {
  if (!isProd) return url;
  const scheme = schemeOf(url);
  if (scheme !== "https") {
    throw new Error(
      `EXPO_PUBLIC_API_URL must use the 'https' scheme in a production build — got ` +
        `'${scheme ?? "no scheme"}'. Refusing to boot with a plaintext API origin (#292; ` +
        "matches apps/api's production DASHBOARD_ORIGINS gate, #255): under HTTPS an " +
        "on-path attacker can't forge the response Date header the 401 liveness gate " +
        "anchors on (#263); under plaintext in a shipped build they could force a " +
        "sign-out, and worse.",
    );
  }
  return url;
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
   *  server) — never throws for a MISSING value, since a fresh checkout
   *  should boot straight into "point at localhost" without any .env
   *  edits. It DOES throw for a plaintext value in a production build
   *  (`requireHttpsInProduction` above, #292) — that failure mode is never
   *  silent, in dev or production. */
  get apiUrl(): string {
    const url = process.env.EXPO_PUBLIC_API_URL ?? fromExtra("apiUrl") ?? DEFAULT_API_URL;
    return requireHttpsInProduction(url, isProductionBuild());
  },
};
