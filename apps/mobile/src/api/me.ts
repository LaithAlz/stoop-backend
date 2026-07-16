/**
 * docs/03-engineering/api-contracts.md "Me" section — display-only in M1
 * (the Me tab; also the dynamic greeting's display-name source on Home).
 * `PATCH /v1/me` is M2 scope (profile editing) and isn't implemented here.
 */
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "./client";
import type { LandlordMe } from "./types";

export const meQueryKey = ["me"] as const;

export function getMe(): Promise<LandlordMe> {
  return apiRequest<LandlordMe>("/v1/me");
}

export function useMe() {
  return useQuery({ queryKey: meQueryKey, queryFn: getMe });
}
