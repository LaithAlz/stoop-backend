/**
 * The React Query defaults for the whole app — mirrors
 * apps/mobile/src/api/queryClient.ts's `retry`/`staleTime` numbers
 * (`retry: 1` on queries — a landlord staring at a stuck queue card is
 * worse than one extra round trip; the API client's own `ApiError` already
 * carries a stable `code` for cases that shouldn't be retried blindly,
 * e.g. `draft_stale`, which the calling mutation handles explicitly rather
 * than relying on this default. `retry: 0` on mutations — an approve/send
 * must never silently fire twice).
 *
 * Deliberately a FACTORY, not a module-level singleton like mobile's
 * `export const queryClient`. TanStack Start's SSR entry can run this
 * module inside a long-lived Cloudflare Workers isolate serving multiple
 * requests; a shared singleton risks one landlord's cached queue data
 * bleeding into another's SSR render. src/router.tsx already creates a
 * fresh `QueryClient` per `getRouter()` call for exactly this reason — this
 * factory just gives that instance the same tuned defaults mobile uses,
 * via `createQueryClient()` instead of the bare `new QueryClient()` it used
 * to call.
 *
 * The PII fence itself (clearing the cache on sign-out) lives in
 * src/auth/AuthProvider.tsx, which reads the live instance via
 * `useQueryClient()` off the same `QueryClientProvider` src/routes/
 * __root.tsx already wires up — not from an import of this module — so it
 * always clears the exact client the app is rendering with, never a
 * different instance than the one the routes read from.
 *
 * No AppState/focusManager wiring here (unlike mobile) — that's a
 * React-Native-only shim for `visibilitychange`; a real browser already
 * fires that event, which is what React Query's default
 * `refetchOnWindowFocus` is built for.
 */
import { QueryClient } from "@tanstack/react-query";

export function createQueryClient(): QueryClient {
  return new QueryClient({
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
}
