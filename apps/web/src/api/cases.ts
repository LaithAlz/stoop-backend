/**
 * docs/03-engineering/api-contracts.md "Cases" section: the case list
 * (cursor-paginated, per the doc's "Conventions" section) and the
 * interleaved-timeline case detail read. Ported near-verbatim from
 * apps/mobile/src/api/cases.ts (campaign issue #234 PR 3).
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

/** `id` is always a real case uuid in every current caller (a route param
 *  or a value the server itself returned), but `encodeURIComponent` here
 *  (and on `resolveCase` below) is a cheap belt-and-braces path-injection
 *  guard — safety review, #234 PR 3 fix round, LOW. */
export function getCase(id: string): Promise<CaseDetail> {
  return apiRequest<CaseDetail>(`/v1/cases/${encodeURIComponent(id)}`);
}

/**
 * POST /v1/cases/{id}/resolve (v1.14 amendment) — the landlord-direct
 * resolve. `reason: "landlord"` is the only legal value on this path (the
 * other `resolved_reason` values are written by the server's own sweeps,
 * never by a client). 200-idempotent: repeating it on an already-resolved
 * case returns the same shape with the stored `resolved_at` — callers treat
 * a repeat success identically to the first. Resolving cancels any unsent
 * pending/approved draft on the case (the confirmation dialog says so
 * before this is ever called — src/features/cases/resolveCase.ts).
 */
export function resolveCase(id: string): Promise<ResolveCaseResponse> {
  return apiRequest<ResolveCaseResponse>(`/v1/cases/${encodeURIComponent(id)}/resolve`, {
    method: "POST",
    body: { reason: "landlord" },
  });
}

export const casesQueryKey = (params: ListCasesParams = {}) => ["cases", params] as const;

export const caseQueryKey = (id: string | undefined) => ["case", id] as const;

export interface UseCasesListOptions extends ListCasesParams {
  /** Web-only addition (mirrors src/api/queue.ts's own `enabled` option) —
   *  defense-in-depth session gate on top of the route guard, see that
   *  file's docstring for the full reasoning. Mobile's `useCasesList` has
   *  no equivalent because it never mounts without a session. */
  enabled?: boolean;
}

/** Conversations tab list — cursor-paginated per the doc's convention
 *  (`next_cursor`; sorted by `last_activity_at`, a MUTABLE key per the
 *  Cases section's A3 amendment — a "skipped ahead" cursor isn't an error
 *  state, re-fetching page 1 is the documented remedy). */
export function useCasesList({ enabled = true, ...params }: UseCasesListOptions = {}) {
  return useInfiniteQuery({
    queryKey: casesQueryKey(params),
    queryFn: ({ pageParam }: { pageParam: string | undefined }) =>
      getCases({ ...params, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled,
  });
}

export interface UseCaseOptions {
  enabled?: boolean;
}

export function useCase(id: string | undefined, { enabled = true }: UseCaseOptions = {}) {
  return useQuery({
    queryKey: caseQueryKey(id),
    queryFn: () => getCase(id as string),
    enabled: Boolean(id) && enabled,
  });
}
