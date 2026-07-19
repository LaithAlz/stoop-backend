/**
 * The trust ladder's ONE endpoint — docs/03-engineering/api-contracts.md
 * "Drafts" section, v1.13 amendment: `POST /v1/properties/{id}/trust/
 * revoke`. `scope: "property"` turns off that property's routine
 * auto-send; `scope: "global"` turns it off across the landlord's whole
 * portfolio (the path still requires SOME property id even for global —
 * a contract awkwardness flagged in the M2 report). Idempotent: nothing
 * left to revoke still 200s with `revoked_count: 0`.
 *
 * There is deliberately no read function in this module: no read contract
 * for trust state exists anywhere in api-contracts.md (see the Trust
 * section note in src/api/types.ts) — the app shows the revoke action
 * without claiming to know the current state.
 */
import { apiRequest } from "./client";
import type { RevokeTrustResponse, RevokeTrustScope } from "./types";

export function revokeTrust(
  propertyId: string,
  scope: RevokeTrustScope,
): Promise<RevokeTrustResponse> {
  return apiRequest<RevokeTrustResponse>(`/v1/properties/${propertyId}/trust/revoke`, {
    method: "POST",
    body: { scope },
  });
}
