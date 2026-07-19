/**
 * docs/03-engineering/api-contracts.md "Me" section — the Me tab's read
 * (also the dynamic greeting's display-name source on Home) and, since M2,
 * `PATCH /v1/me` profile editing. The PATCH body is built by
 * src/features/account/profileEdit.ts's `buildMeUpdatePayload` (only the
 * documented fields, never an explicit null — the contract 422s a null
 * `timezone`/`phone` with `invalid_field` by design).
 */
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "./client";
import type { LandlordMe, UpdateMeInput } from "./types";

export const meQueryKey = ["me"] as const;

export function getMe(): Promise<LandlordMe> {
  return apiRequest<LandlordMe>("/v1/me");
}

/** PATCH /v1/me → the full updated `LandlordMe` (mirrors GET — the
 *  backend's own MeResponse; `phone` is settable but never echoed back). */
export function updateMe(input: UpdateMeInput): Promise<LandlordMe> {
  return apiRequest<LandlordMe>("/v1/me", { method: "PATCH", body: input });
}

export function useMe() {
  return useQuery({ queryKey: meQueryKey, queryFn: getMe });
}
