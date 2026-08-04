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
  return `${firstName(item.tenant_name)} needs you now at ${item.property_label}`;
}

/** Deliberately doesn't promise a phone call: the queue contract carries no
 *  tenant phone number on this shape (see src/api/types.ts's `QueueItem`
 *  comment), so a "tap to call" line would describe an action the banner
 *  doesn't actually perform (rule 8 — concrete, never overpromise). */
export function emergencySubtext(item: Pick<QueueItem, "property_label">): string {
  return `${item.property_label} · tap to see what's happening`;
}

// ---------------------------------------------------------------------------
// Acknowledge action copy (v1.15 amendment — `notification_id` +
// `POST /v1/notifications/{id}/ack`; src/features/emergency/
// useAcknowledge.ts owns the mutation, src/components/clarity/
// EmergencyBanner.tsx renders these). Only ever shown when a card carries
// a non-null `notification_id` — see that field's own comment.
//
// The sub-line states exactly what acknowledging does per
// emergency-prefilter.md's escalation-chain section — "it stops the chain"
// — and nothing more: it never claims the case is handled, resolved, or
// that anyone has actually been reached, none of which acknowledging does.
// The idle label follows the same rule (copy-guardian review on this
// branch): "I'm on it" implied the landlord was addressing the emergency
// itself, which acknowledging doesn't do, and broke the app's own
// second-person-imperative button register ("Turn off", "Mark resolved").
// "Stop the calls" states only the actual, literal effect.
// ---------------------------------------------------------------------------

export const EMERGENCY_ACK_LABEL = "Stop the calls";

export const EMERGENCY_ACK_PENDING_LABEL = "Stopping the calls…";

export const EMERGENCY_ACK_SUBLABEL = "Stops the calls.";

/** Home's own gate for whether a card gets the acknowledge affordance —
 *  pulled out as a pure predicate (rather than an inline null-check at the
 *  call site) so "ack shown iff `notification_id` non-null" is directly
 *  unit-testable. */
export function hasAcknowledgeableNotification(
  item: Pick<QueueItem, "notification_id">,
): item is Pick<QueueItem, "notification_id"> & { notification_id: string } {
  return item.notification_id !== null;
}
