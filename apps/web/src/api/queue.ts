/**
 * GET /v1/queue — docs/03-engineering/api-contracts.md "Queue" section.
 * Ported near-verbatim from apps/mobile/src/api/queue.ts (campaign issue
 * #234 PR 2) — the dashboard's main read; Home polls it on an interval in
 * addition to the router's own refetch-on-focus default
 * (src/api/queryClient.ts's `staleTime`/`retry`).
 *
 * Web-only addition: an `enabled` option (default `true`). Mobile never
 * mounts this hook without a session; this app's route guard
 * (src/routes/app.tsx) already guarantees the same for every real render
 * of src/routes/app.index.tsx (its parent's `<Outlet/>` only renders once
 * `resolveAuthRoute` says `"app"`, which requires a non-null session), but
 * the query is ALSO explicitly gated on the caller's own session check as
 * defense-in-depth — a query that COULD run without a session must never
 * be the only thing standing between an unauthenticated request and this
 * data. This is also why the live hook never fires during SSR: the
 * `<Outlet/>` that would mount it isn't rendered server-side either way
 * (see the PR report's SSR probe), so `enabled` is a second, independent
 * gate, not the only one.
 */
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "./client";
import type { QueueResponse } from "./types";

/** Deliberately not aggressive — the queue already refetches on window
 *  focus (React Query's default); this interval is the "someone left the
 *  tab open" backstop, not the primary freshness mechanism. */
export const QUEUE_REFETCH_INTERVAL_MS = 20_000;

export const queueQueryKey = ["queue"] as const;

export function getQueue(): Promise<QueueResponse> {
  return apiRequest<QueueResponse>("/v1/queue");
}

export interface UseQueueOptions {
  enabled?: boolean;
}

export function useQueue({ enabled = true }: UseQueueOptions = {}) {
  return useQuery({
    queryKey: queueQueryKey,
    queryFn: getQueue,
    refetchInterval: QUEUE_REFETCH_INTERVAL_MS,
    enabled,
  });
}
