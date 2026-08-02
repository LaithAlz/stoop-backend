/**
 * Reads the Supabase + API client config the web app needs at runtime.
 *
 * Values come from `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` /
 * `VITE_API_URL` — build-time env vars Vite inlines via `import.meta.env`
 * (see apps/web/.env.example; the same convention `VITE_PLAUSIBLE_DOMAIN`
 * already uses in src/routes/__root.tsx). Never read apps/api/.env or a
 * service-role key — this app only ever gets the public anon key, same
 * boundary as apps/mobile/src/lib/env.ts.
 *
 * Unlike mobile's `env.ts`, nothing here throws. A fresh checkout or a
 * preview deploy with no Supabase project configured must still render —
 * `src/lib/supabase.ts` reads these as `undefined` and produces a `null`
 * client, and every screen that depends on it shows an honest "not set up"
 * state (mirrors the waitlist form's `reason: "not-configured"` degradation
 * in src/routes/early-access.tsx) instead of a crash.
 */

function readVar(value: string | undefined): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export const env = {
  get supabaseUrl(): string | undefined {
    return readVar(import.meta.env.VITE_SUPABASE_URL as string | undefined);
  },
  get supabaseAnonKey(): string | undefined {
    return readVar(import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined);
  },
  /** apps/api's base URL (docs/03-engineering/api-contracts.md). Unlike
   *  mobile there is no `localhost` fallback — a web build with this unset
   *  is a real misconfiguration (there's no "simulator" to default to), so
   *  src/api/client.ts turns a missing value into an explicit
   *  `not_configured` ApiError rather than silently pointing at
   *  localhost. */
  get apiUrl(): string | undefined {
    return readVar(import.meta.env.VITE_API_URL as string | undefined);
  },
};
