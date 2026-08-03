/**
 * docs/03-engineering/api-contracts.md "Properties" section (+ the v1.12
 * provisioning amendment): cursor-paginated list, single read, create
 * (which provisions the property's own Twilio number — the number tenants
 * text), and the confirm-gated delete. Ported near-verbatim from
 * apps/mobile/src/api/properties.ts (campaign issue #234 PR 4).
 *
 * Delete semantics worth restating at the call seam (v1.12): `confirm=true`
 * is REQUIRED (absent → 400 `confirmation_required` — this client always
 * sends it, after its own confirmation dialog); the property row's delete
 * is immediate and permanent, but the number's release is NOT synchronous —
 * it enters a 24-hour grace window server-side. The screens' copy says so
 * plainly instead of pretending the number vanishes instantly.
 */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { apiRequest } from "./client";
import type {
  CreatePropertyInput,
  PropertiesResponse,
  Property,
  UpdatePropertyInput,
} from "./types";

/** Root key — every properties read (list, gate, detail) starts with this
 *  so one `invalidateQueries({ queryKey: propertiesQueryKey })` after a
 *  create/delete refreshes them all. */
export const propertiesQueryKey = ["properties"] as const;

/** One property's detail read. Exported so a caller that must drop
 *  exactly this property's cache (e.g. after a delete — a prefix
 *  invalidate would refetch the still-mounted detail and 404 it) doesn't
 *  hand-assemble the key. */
export function propertyQueryKey(id: string) {
  return [...propertiesQueryKey, "detail", id] as const;
}

export interface ListPropertiesParams {
  cursor?: string;
  limit?: number;
}

export function getProperties(params: ListPropertiesParams = {}): Promise<PropertiesResponse> {
  const search = new URLSearchParams();
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  return apiRequest<PropertiesResponse>(`/v1/properties${qs ? `?${qs}` : ""}`);
}

export function getProperty(id: string): Promise<Property> {
  return apiRequest<Property>(`/v1/properties/${encodeURIComponent(id)}`);
}

export function createProperty(input: CreatePropertyInput): Promise<Property> {
  return apiRequest<Property>("/v1/properties", { method: "POST", body: input });
}

/** How long a property write may hang before we stop waiting on it, ONCE
 *  the request has actually started — same bound and reasoning as
 *  src/api/me.ts's `updateMe` (#234 PR 5, R2).
 *
 *  F1 (HIGH, safety review): this alone does NOT bound the settings
 *  form's Save button. src/api/queryClient.ts sets no `networkMode`, so
 *  mutations default to react-query's `"online"` — while the device is
 *  offline, the retryer never calls this function's caller at all (the
 *  mutation stays pending forever; `onError`/`onSettled` never run), so
 *  this `AbortSignal` never starts counting because nothing here runs.
 *  The settings form's own `useMutation` call sets
 *  `networkMode: "always"` for exactly that reason — an offline attempt
 *  then actually reaches `apiRequest`, whose `fetch` throws immediately
 *  and maps to `network_error`. This timeout's job is the remaining,
 *  narrower case: a device that IS online but whose connection stalls
 *  mid-request. Either way the failure maps to `network_error` (status
 *  0), which src/routes/app.properties_.$id_.settings.tsx's
 *  `isAmbiguousFailure` already treats as ambiguous, not a definite
 *  failure. */
const UPDATE_PROPERTY_TIMEOUT_MS = 20_000;

/** PATCH /v1/properties/{id} — "same fields + quiet_hours, heating_season"
 *  (Properties section). Wired to the property settings form (issue
 *  #261: backup_contact/quiet_hours/house_rules) — no longer just kept
 *  for mobile-client parity. */
export function updateProperty(id: string, input: UpdatePropertyInput): Promise<Property> {
  return apiRequest<Property>(`/v1/properties/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: input,
    signal: AbortSignal.timeout(UPDATE_PROPERTY_TIMEOUT_MS),
  });
}

/** DELETE /v1/properties/{id}?confirm=true → 204. Callers show a real
 *  confirmation dialog FIRST — the query param is the contract's guard, not
 *  a substitute for asking the landlord. */
export function deleteProperty(id: string): Promise<void> {
  return apiRequest<void>(`/v1/properties/${encodeURIComponent(id)}?confirm=true`, {
    method: "DELETE",
  });
}

export interface UsePropertiesListOptions {
  /** Web-only addition (mirrors src/api/cases.ts's own `enabled` option) —
   *  defense-in-depth session gate on top of the route guard. */
  enabled?: boolean;
}

/** Properties tab list — standard cursor pagination, same shape as
 *  src/api/cases.ts's `useCasesList`. */
export function usePropertiesList({ enabled = true }: UsePropertiesListOptions = {}) {
  return useInfiniteQuery({
    queryKey: [...propertiesQueryKey, "list"],
    queryFn: ({ pageParam }: { pageParam: string | undefined }) =>
      getProperties({ cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled,
  });
}

export interface UsePropertyOptions {
  enabled?: boolean;
}

export function useProperty(id: string | undefined, { enabled = true }: UsePropertyOptions = {}) {
  return useQuery({
    queryKey: propertyQueryKey(id ?? "none"),
    queryFn: () => getProperty(id as string),
    enabled: Boolean(id) && enabled,
  });
}
