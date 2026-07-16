/**
 * Plain-English plan display for the Me tab — CLAUDE.md rule 8 pins the
 * exact prices ("free Emergency Line / $10 Full Plan / $5 early-access
 * (grandfathered)"); this never prints the raw `subscription_tier`/
 * `price_cohort` enum values from `GET /v1/me` directly.
 */
export function planDisplayName(tier: string, cohort: string): string {
  if (tier === "full" && cohort === "early_access") {
    // Price-lock phrasing per the PR #142 audit remediation: never
    // "locked for life" — "locked in for as long as you stay" is the
    // house claim (mirrors apps/web/src/routes/plans.tsx).
    return "Full Plan — $5/month early access, locked in for as long as you stay";
  }
  if (tier === "full") return "Full Plan — $10/month";
  return "Emergency Line — free";
}
