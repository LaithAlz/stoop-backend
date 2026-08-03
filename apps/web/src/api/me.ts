/**
 * docs/03-engineering/api-contracts.md "Me" section (+ its v1.9 amendment):
 * `GET/PATCH /v1/me` — the landlord's own profile read/edit. Ported
 * near-verbatim from apps/mobile/src/api/me.ts (campaign issue #234 PR 5,
 * the campaign's final PR).
 *
 * `phone` is settable via PATCH but never echoed back by GET (the backend's
 * MeResponse excludes it — write-only on this contract, api/types.ts's own
 * `UpdateMeInput` doc comment). `timezone`/`voice_profile` are also
 * PATCH-able per the contract but no shipped web screen edits them yet
 * (mirrors src/api/properties.ts's `updateProperty` — kept for contract
 * parity, not wired to a form this PR).
 */
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "./client";
import type { LandlordMe, UpdateMeInput } from "./types";

export const meQueryKey = ["me"] as const;

export function getMe(): Promise<LandlordMe> {
  return apiRequest<LandlordMe>("/v1/me");
}

/** PATCH /v1/me → the full updated `LandlordMe` (mirrors GET). */
export function updateMe(input: UpdateMeInput): Promise<LandlordMe> {
  return apiRequest<LandlordMe>("/v1/me", { method: "PATCH", body: input });
}

export interface UseMeOptions {
  /** Web-only addition (mirrors src/api/queue.ts's own `enabled` option) —
   *  defense-in-depth session gate on top of the route guard. */
  enabled?: boolean;
}

export function useMe({ enabled = true }: UseMeOptions = {}) {
  return useQuery({
    queryKey: meQueryKey,
    queryFn: getMe,
    enabled,
  });
}
