/**
 * Plain-English plan display for the Me tab — CLAUDE.md rule 8 pins the
 * exact prices ("free Emergency Line / $10 Full Plan / $5 early-access
 * (grandfathered)"); this never prints the raw `subscription_tier`/
 * `price_cohort` enum values from `GET /v1/me` directly.
 */
export function planDisplayName(tier: string, cohort: string): string {
  if (tier === "full" && cohort === "early_access") {
    return "Full Plan — $5/mo (early-access price, locked in)";
  }
  if (tier === "full") return "Full Plan — $10/mo";
  return "Emergency Line — free";
}
