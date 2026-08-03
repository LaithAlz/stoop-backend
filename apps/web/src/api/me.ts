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

/** How long a profile write may hang before we stop waiting on it. The
 *  edit dialog holds itself open (and un-dismissable) for the duration —
 *  see R2 below — so this is also the ceiling on how long a landlord can
 *  be stuck on that screen. */
const UPDATE_ME_TIMEOUT_MS = 20_000;

/**
 * PATCH /v1/me → the full updated `LandlordMe` (mirrors GET).
 *
 * R2 (safety re-verify, #234 PR 5): bounded. This write can change the
 * number emergency calls ring, so the dialog refuses to close while it's
 * in flight (a mid-flight dismissal would swallow the outcome on a field
 * `GET /v1/me` never echoes back). Without a timeout, a stalled
 * connection — captive portal, dead cell handoff — held that dialog
 * un-exitable for the browser's own stall timeout, up to several minutes.
 * An abort surfaces as `network_error` (status 0), which is exactly the
 * ambiguous-failure branch: "we couldn't confirm — save it again."
 */
export function updateMe(input: UpdateMeInput): Promise<LandlordMe> {
  return apiRequest<LandlordMe>("/v1/me", {
    method: "PATCH",
    body: input,
    signal: AbortSignal.timeout(UPDATE_ME_TIMEOUT_MS),
  });
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
