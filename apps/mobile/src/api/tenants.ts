/**
 * docs/03-engineering/api-contracts.md "Tenants & Vendors" section —
 * tenants are a PROPERTY SUB-RESOURCE: `GET/POST /v1/properties/{id}/
 * tenants` (not a `?property_id=` filter on a top-level collection) ·
 * `PATCH/DELETE /v1/tenants/{id}`. The list is unpaginated (v1.9:
 * "per-property tenant counts are small"). DELETE is a soft delete
 * (`active = false`, returns the updated row) — not wired to any M2
 * screen, so it isn't implemented here; a "remove tenant" surface is a
 * later phase's call.
 */
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "./client";
import type { CreateTenantInput, Tenant, TenantsResponse, UpdateTenantInput } from "./types";

export function tenantsQueryKey(propertyId: string) {
  return ["tenants", propertyId] as const;
}

export function getTenants(propertyId: string): Promise<TenantsResponse> {
  return apiRequest<TenantsResponse>(`/v1/properties/${propertyId}/tenants`);
}

/** 409 `duplicate_phone` on a `(property_id, phone)` collision (v1.10). */
export function createTenant(propertyId: string, input: CreateTenantInput): Promise<Tenant> {
  return apiRequest<Tenant>(`/v1/properties/${propertyId}/tenants`, {
    method: "POST",
    body: input,
  });
}

export function updateTenant(tenantId: string, input: UpdateTenantInput): Promise<Tenant> {
  return apiRequest<Tenant>(`/v1/tenants/${tenantId}`, { method: "PATCH", body: input });
}

export function useTenants(propertyId: string | undefined) {
  return useQuery({
    queryKey: tenantsQueryKey(propertyId ?? "none"),
    queryFn: () => getTenants(propertyId as string),
    enabled: Boolean(propertyId),
  });
}
