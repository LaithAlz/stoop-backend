/**
 * Supabase client for the web dashboard — mirrors the auth model
 * docs/03-engineering/api-contracts.md describes for the API
 * (`Authorization: Bearer <supabase JWT>`, `role: authenticated`), and the
 * client shape apps/mobile/src/lib/supabase.ts already implements there.
 *
 * Two exports, deliberately split:
 *
 * - `supabaseConfigured` — pure env check (`VITE_SUPABASE_URL` +
 *   `VITE_SUPABASE_ANON_KEY` both set at build time). Identical on the
 *   server and the client (Vite inlines `VITE_*` the same way into both
 *   bundles), so it's safe to use for the very first render in
 *   src/auth/AuthProvider.tsx without a hydration mismatch.
 * - `supabase` — the actual client, or `null`. Gated on
 *   `typeof window !== "undefined"` IN ADDITION to the env check — this is
 *   load-bearing, not defensive styling. `createClient()`'s constructor
 *   unconditionally builds a Realtime sub-client
 *   (`@supabase/realtime-js`), which eagerly resolves a WebSocket
 *   constructor and THROWS if it can't find one — confirmed locally (`bun
 *   run dev` under this env's Node runtime has no global `WebSocket`) and,
 *   per `websocket-factory.ts`'s own environment detection, Cloudflare
 *   Workers (this app's actual SSR target, wrangler.jsonc) is explicitly
 *   called out as unsupported too ("WebSocket clients are not supported in
 *   Cloudflare Workers"). Constructing the client at module scope
 *   unconditionally would 500 every SSR request — not just `/app` or
 *   `/sign-in`, ANY route, since src/routes/__root.tsx wraps everything in
 *   `AuthProvider` — the moment real Supabase credentials are set. This
 *   app never needs Supabase Realtime (no live-subscription feature
 *   exists), so the fix is to simply never construct the client outside a
 *   browser, where `window` exists and the constructor's WebSocket check
 *   passes fine. Every caller reads `supabase` (not `supabaseConfigured`)
 *   before touching `.auth` — `null` there always means "don't call
 *   Supabase," server-side included.
 *
 * Session storage: supabase-js's own default (`window.localStorage` when
 * present) — the web equivalent of mobile's `expo-secure-store` adapter.
 * There's no secure-enclave-backed storage option in a browser; this is
 * the standard, documented pattern for a Supabase browser client and is
 * the explicit call in this PR's brief ("supabase-js default localStorage
 * is fine for web"). `detectSessionInUrl: true` is what lets the magic-link
 * callback resolve into a session automatically on load.
 *
 * `flowType: "pkce"` (safety review B1, #234) — the default `"implicit"`
 * flow puts the actual access/refresh token pair directly in the magic
 * link's redirect URL (as a `#access_token=...` fragment), which then also
 * sits in Plausible's page-view beacon (the referrer/URL) and in browser
 * history until `_getSessionFromURL` clears the hash. PKCE instead sends a
 * single-use `?code=` that's worthless without the `code_verifier`
 * supabase-js stashed in `localStorage` when `signInWithOtp` was called
 * (AuthProvider.tsx) — so a leaked/forwarded/prefetched link URL alone
 * can't be replayed into a session. `detectSessionInUrl` needs no other
 * change: confirmed against the installed auth-js source
 * (GoTrueClient.ts's `_getSessionFromURL`) that it exchanges `?code=` for a
 * session and cleans the URL via `history.replaceState` the same
 * automatic way it already handled the implicit hash.
 */
import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { env } from "./env";

export const supabaseConfigured = Boolean(env.supabaseUrl && env.supabaseAnonKey);

// A7 (safety review, #234, advisory — flagged not fixed): supabase-js
// pulls in @supabase/storage-js, which itself depends on `iceberg-js` — a
// transitive dependency for a Storage feature this app never uses either
// (same shape as the Realtime/WebSocket issue fixed above). Worth an
// audit of whether any of storage-js/iceberg-js's own module-scope side
// effects have a similar SSR/edge-runtime landmine, but out of scope to
// chase down for this PR.
export const supabase: SupabaseClient | null =
  typeof window !== "undefined" && supabaseConfigured
    ? createClient(env.supabaseUrl as string, env.supabaseAnonKey as string, {
        auth: {
          persistSession: true,
          autoRefreshToken: true,
          detectSessionInUrl: true,
          flowType: "pkce",
        },
      })
    : null;
