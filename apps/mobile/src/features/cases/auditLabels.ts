/**
 * Plain-English labels for `audit_log` timeline entries (schema-v1.md
 * `audit_log.action` — the full enum is mirrored in src/api/types.ts's
 * `AuditAction`). CLAUDE.md rule 8 / plain-language-rules.md: no jargon
 * ("triage" never appears here), grade-5-reading-level phrasing.
 *
 * Two actions are deliberately suppressed (return `null` — the case-detail
 * screen renders no row for them):
 * - `message_received` duplicates the adjacent message bubble itself.
 * - `drafted` duplicates the `DraftBubble` the timeline already renders
 *   for that same event.
 * Every other action gets a one-line, past-tense, landlord-facing note.
 * This is an interpretive call (api-contracts.md specifies the timeline
 * carries every audit entry, not which ones a client should visually
 * suppress) — flagged in the mobile M1 report for spec-guardian review.
 */
import type { AuditAction } from "@/api/types";

const LABELS: Partial<Record<AuditAction, string>> = {
  case_opened: "Case opened.",
  case_reopened: "Case reopened.",
  case_resolved: "Case marked resolved.",
  approved: "You approved this reply.",
  edited: "You edited this reply before sending.",
  rejected: "You skipped this reply — case stayed open.",
  sent: "Sent.",
  send_cancelled: "Sending was cancelled.",
  auto_sent: "Stoop sent this one automatically for a routine reply here.",
  emergency_triggered: "Stoop flagged this as an emergency.",
  emergency_call_attempt: "Stoop called you about this emergency.",
  acknowledged: "You acknowledged this emergency.",
  vendor_engaged: "A vendor was contacted.",
  degraded_mode: "Stoop couldn't sort this one automatically and flagged it for you.",
  trust_unlocked: "Stoop can now send routine replies here automatically.",
  trust_revoked: "Automatic sending was turned off here.",
  billing_changed: "Billing changed.",
  settings_changed: "Settings changed.",
  draft_stale: "That draft was replaced after a new message came in.",
};

/** Returns `null` for an action the timeline should render no row for. */
export function auditActionLabel(action: AuditAction): string | null {
  return LABELS[action] ?? null;
}
