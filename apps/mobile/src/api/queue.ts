/**
 * GET /v1/queue — docs/03-engineering/api-contracts.md "Queue" section.
 * The dashboard's/app's main read: the queue IS the app's heartbeat, so
 * Home polls it on an interval in addition to pull-to-refresh.
 */
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "./client";
import type { QueueResponse } from "./types";

/** Deliberately not aggressive — the queue already refetches on focus and
 *  pull-to-refresh; this interval is the "someone left the app open"
 *  backstop, not the primary freshness mechanism. */
export const QUEUE_REFETCH_INTERVAL_MS = 20_000;

export const queueQueryKey = ["queue"] as const;

export function getQueue(): Promise<QueueResponse> {
  return apiRequest<QueueResponse>("/v1/queue");
}

export function useQueue() {
  return useQuery({
    queryKey: queueQueryKey,
    queryFn: getQueue,
    refetchInterval: QUEUE_REFETCH_INTERVAL_MS,
  });
}
