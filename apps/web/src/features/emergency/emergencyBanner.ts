/**
 * Pure copy logic for Home's emergency banner — kept separate from the
 * presentational component (src/components/clarity/EmergencyBanner.tsx) so
 * the headline fallback rule stays testable without rendering. Ported
 * near-verbatim from apps/mobile/src/features/emergency/emergencyBanner.ts
 * (#237, campaign issue #234 PR 2).
 *
 * api-contracts.md's Queue section, v1.1 amendments: "`title` is the
 * emergency-banner headline. Agent-written plain English per case, never
 * client-side template copy — the dashboard must not hardcode incident
 * wording (PR #181 shipped a hardcoded 'reported a flood' once; never
 * again)." `title` is null until #197's title-writing half lands, so this
 * needs a fallback — but the fallback must not guess at what the
 * emergency IS (no "there's a fire", no "the pipe burst"): only fields
 * the queue contract guarantees are always present (`tenant_name`,
 * `property_label`) go into it.
 */
import { firstName } from "@/lib/tenantName";
import type { QueueItem } from "@/api/types";

export function emergencyHeadline(
  item: Pick<QueueItem, "title" | "tenant_name" | "property_label">,
): string {
  if (item.title) return item.title;
  return `${firstName(item.tenant_name)} needs you now — ${item.property_label}`;
}

/**
 * Deliberately doesn't promise a phone call: the queue contract carries no
 * tenant phone number on this shape (see `QueueItem`'s comment in
 * src/api/types.ts), so a "tap to call" line would describe an action the
 * banner doesn't actually perform (rule 8 — concrete, never overpromise).
 * Replaces this screen's PREVIOUS mock subtext
 * (`"{property} · tap to call {tenant} now"`, src/lib/mock-app.ts's
 * `title`-adjacent wiring in the old app.index.tsx), which made exactly
 * that promise — flagged for copy-guardian in the PR report.
 *
 * B3 (safety review, #234 PR 2): no "tap to …" wording of any kind — the
 * live banner doesn't navigate until PR 3 lands the live cases screen, so
 * any tap promise would be a dead end on the emergency path. Property
 * label only.
 */
export function emergencySubtext(item: Pick<QueueItem, "property_label">): string {
  return item.property_label;
}

// ---------------------------------------------------------------------------
// Acknowledge action copy (v1.15 amendment — `notification_id` +
// `POST /v1/notifications/{id}/ack`; src/features/emergency/
// useAcknowledge.ts owns the mutation, src/components/clarity/
// EmergencyBanner.tsx renders these). Only ever shown when a card carries
// a non-null `notification_id` — see that field's own comment.
//
// The sub-line states exactly what acknowledging does per
// emergency-prefilter.md's escalation-chain section — "it stops the
// chain" — and nothing more: it never claims the case is handled,
// resolved, or that anyone has actually been reached, none of which
// acknowledging does. "Stop the calls" states only the actual, literal
// effect (copy-guardian-reviewed on mobile's #237; carried verbatim here).
// ---------------------------------------------------------------------------

export const EMERGENCY_ACK_LABEL = "Stop the calls";

export const EMERGENCY_ACK_PENDING_LABEL = "Stopping the calls…";

export const EMERGENCY_ACK_SUBLABEL = "Stops the calls.";

/** Home's own gate for whether a card gets the acknowledge affordance —
 *  pulled out as a pure predicate (rather than an inline null-check at the
 *  call site) so "ack shown iff `notification_id` non-null" is directly
 *  testable. */
export function hasAcknowledgeableNotification(
  item: Pick<QueueItem, "notification_id">,
): item is Pick<QueueItem, "notification_id"> & { notification_id: string } {
  return item.notification_id !== null;
}
