/**
 * docs/03-engineering/api-contracts.md "Tenants & Vendors" section —
 * tenants are a PROPERTY SUB-RESOURCE: `GET/POST /v1/properties/{id}/
 * tenants` (not a `?property_id=` filter on a top-level collection) ·
 * `PATCH/DELETE /v1/tenants/{id}`. The list is unpaginated (v1.9:
 * "per-property tenant counts are small"). Ported near-verbatim from
 * apps/mobile/src/api/tenants.ts (campaign issue #234 PR 4) — mobile's own
 * comment notes DELETE (a soft delete, `active = false`) isn't wired to any
 * shipped screen there either, so it isn't implemented here either.
 *
 * `createTenant`/`updateTenant` are ported for API parity with mobile
 * (typed functions + the documented 409 `duplicate_phone` behavior) but
 * this PR's property-detail screen (app.properties_.$id.tsx) only reads the
 * list — no add/edit tenant UI ships this PR (see the PR report); these two
 * functions are unused by any route today, ready for that follow-up.
 */
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "./client";
import type { CreateTenantInput, Tenant, TenantsResponse, UpdateTenantInput } from "./types";

export function tenantsQueryKey(propertyId: string) {
  return ["tenants", propertyId] as const;
}

export function getTenants(propertyId: string): Promise<TenantsResponse> {
  return apiRequest<TenantsResponse>(`/v1/properties/${encodeURIComponent(propertyId)}/tenants`);
}

/** 409 `duplicate_phone` on a `(property_id, phone)` collision (v1.10). */
export function createTenant(propertyId: string, input: CreateTenantInput): Promise<Tenant> {
  return apiRequest<Tenant>(`/v1/properties/${encodeURIComponent(propertyId)}/tenants`, {
    method: "POST",
    body: input,
  });
}

export function updateTenant(tenantId: string, input: UpdateTenantInput): Promise<Tenant> {
  return apiRequest<Tenant>(`/v1/tenants/${encodeURIComponent(tenantId)}`, {
    method: "PATCH",
    body: input,
  });
}

export interface UseTenantsOptions {
  enabled?: boolean;
}

export function useTenants(
  propertyId: string | undefined,
  { enabled = true }: UseTenantsOptions = {},
) {
  return useQuery({
    queryKey: tenantsQueryKey(propertyId ?? "none"),
    queryFn: () => getTenants(propertyId as string),
    enabled: Boolean(propertyId) && enabled,
  });
}
