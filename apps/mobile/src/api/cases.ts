/**
 * docs/03-engineering/api-contracts.md "Cases" section: the case list
 * (cursor-paginated, per the doc's "Conventions" section) and the
 * interleaved-timeline case detail read.
 */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { apiRequest } from "./client";
import type { CaseDetail, CasesResponse, CaseStatus, ResolveCaseResponse, Severity } from "./types";

export interface ListCasesParams {
  status?: CaseStatus;
  severity?: Severity;
  propertyId?: string;
  limit?: number;
}

function casesQueryString(params: ListCasesParams & { cursor?: string }): string {
  const search = new URLSearchParams();
  if (params.status) search.set("status", params.status);
  if (params.severity) search.set("severity", params.severity);
  if (params.propertyId) search.set("property_id", params.propertyId);
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export function getCases(
  params: ListCasesParams & { cursor?: string } = {},
): Promise<CasesResponse> {
  return apiRequest<CasesResponse>(`/v1/cases${casesQueryString(params)}`);
}

export function getCase(id: string): Promise<CaseDetail> {
  return apiRequest<CaseDetail>(`/v1/cases/${id}`);
}

/**
 * POST /v1/cases/{id}/resolve (v1.14 amendment) — the landlord-direct
 * resolve. `reason: "landlord"` is the only legal value on this path (the
 * other `resolved_reason` values are written by the server's own sweeps,
 * never by a client). 200-idempotent: repeating it on an already-resolved
 * case returns the same shape with the stored `resolved_at` — callers
 * treat a repeat success identically to the first. Resolving cancels any
 * unsent pending/approved draft on the case (the confirmation dialog says
 * so before this is ever called — src/features/cases/resolveCase.ts).
 */
export function resolveCase(id: string): Promise<ResolveCaseResponse> {
  return apiRequest<ResolveCaseResponse>(`/v1/cases/${id}/resolve`, {
    method: "POST",
    body: { reason: "landlord" },
  });
}

/** Conversations tab list — cursor-paginated per the doc's convention
 *  (`next_cursor`, newest... actually last-activity-ordered — see
 *  api-contracts.md's own caveat that `last_activity_at` is a mutable sort
 *  key, not monotonic; re-fetching page 1 is the documented remedy for a
 *  "skipped ahead" cursor, not an error state). */
export function useCasesList(params: ListCasesParams = {}) {
  return useInfiniteQuery({
    queryKey: ["cases", params],
    queryFn: ({ pageParam }: { pageParam: string | undefined }) =>
      getCases({ ...params, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
}

export function useCase(id: string | undefined) {
  return useQuery({
    queryKey: ["case", id],
    queryFn: () => getCase(id as string),
    enabled: Boolean(id),
  });
}
