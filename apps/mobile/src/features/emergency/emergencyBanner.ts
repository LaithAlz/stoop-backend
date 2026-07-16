/**
 * Pure copy logic for Home's emergency banner — kept separate from the
 * presentational component (src/components/clarity/EmergencyBanner.tsx) so
 * the headline fallback rule is unit-testable without rendering.
 *
 * api-contracts.md's Queue section, v1.1 amendments: "`title` is the
 * emergency-banner headline. Agent-written plain English per case, never
 * client-side template copy — the dashboard must not hardcode incident
 * wording (PR #181 shipped a hardcoded 'reported a flood' once; never
 * again)." `title` is null until #197's title-writing half lands, so this
 * needs a fallback — but the fallback must not guess at what the emergency
 * IS (no "there's a fire", no "the pipe burst"): only fields the queue
 * contract guarantees are always present (`tenant_name`, `property_label`)
 * go into it.
 */
import { firstName } from "@/lib/tenantName";
import type { QueueItem } from "@/api/types";

export function emergencyHeadline(
  item: Pick<QueueItem, "title" | "tenant_name" | "property_label">,
): string {
  if (item.title) return item.title;
  return `${firstName(item.tenant_name)} needs you now — ${item.property_label}`;
}

/** Deliberately doesn't promise a phone call: the queue contract carries no
 *  tenant phone number on this shape (see src/api/types.ts's `QueueItem`
 *  comment), so a "tap to call" line would describe an action the banner
 *  doesn't actually perform (rule 8 — concrete, never overpromise). */
export function emergencySubtext(item: Pick<QueueItem, "property_label">): string {
  return `${item.property_label} · tap to see what's happening`;
}
