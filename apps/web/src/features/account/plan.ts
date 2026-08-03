/**
 * Plain-English plan display for the Me screen — CLAUDE.md rule 8 pins the
 * exact prices ("free Emergency Line / $10 Full Plan / $5 early-access
 * (grandfathered) / PMs $1.50/door"); this never prints the raw
 * `subscription_tier`/`price_cohort` enum values from `GET /v1/me` directly.
 * Ported from apps/mobile/src/features/account/plan.ts (campaign issue #234
 * PR 5), extended with the `desk` (Property Managers) tier mobile's Me
 * screen doesn't show — schema-v1.md's `landlords.subscription_tier` CHECK
 * is `('free','full','desk')`.
 */
export function planDisplayName(tier: string, cohort: string): string {
  if (tier === "full" && cohort === "early_access") {
    // Price-lock phrasing per the PR #142 audit remediation: never
    // "locked for life" — "locked in for as long as you stay" is the
    // house claim (mirrors src/routes/plans.tsx and apps/mobile's own copy).
    return "Full Plan — $5/month early access, locked in for as long as you stay";
  }
  if (tier === "full") return "Full Plan — $10/month";
  if (tier === "desk") return "Property Managers — $1.50/door/month";
  return "Emergency Line — free";
}

/**
 * `subscription_status` (schema-v1.md CHECK: `'none','active','past_due',
 * 'canceled'`) — surfaced only for the two states worth a landlord's
 * attention; `none`/`active` need no extra line (the plan name above
 * already says what's active, and `none` is the ordinary free-tier state,
 * not a problem to flag). Returns `null` when there's nothing to say.
 */
export function planStatusNotice(status: string): string | null {
  if (status === "past_due") {
    return "Your last payment didn't go through. Update your payment method to keep the Full Plan active.";
  }
  if (status === "canceled") {
    return "Your Full Plan subscription is canceled — you're on the free Emergency Line until you resubscribe.";
  }
  return null;
}
