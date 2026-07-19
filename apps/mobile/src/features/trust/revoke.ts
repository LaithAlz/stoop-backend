/**
 * Copy + result logic for the trust-ladder revoke action (api-contracts.md
 * "Drafts" v1.13 amendment: POST /v1/properties/{id}/trust/revoke). Pure —
 * the screens wrap these in Alert dialogs and a useMutation.
 *
 * Honesty constraints baked into every line here:
 * - No read contract for trust state exists, so nothing below claims to
 *   know whether auto-send is currently on — the confirmation describes
 *   what the action DOES, and the result line is derived from the server's
 *   own `revoked_count`.
 * - What revoke does (per the amendment): Stoop stops sending routine
 *   replies automatically (property or everywhere), approvals come back to
 *   the landlord, and re-earning requires a full fresh streak of approved
 *   sends (`consecutive_clean` resets to 0) — never a single next one.
 * - Only ROUTINE replies were ever eligible for auto-send (rule 3);
 *   the copy never implies emergencies/urgent were automatic.
 */
import type { RevokeTrustScope } from "@/api/types";

export interface RevokeConfirmation {
  title: string;
  message: string;
  confirmLabel: string;
}

export function revokeConfirmation(scope: RevokeTrustScope): RevokeConfirmation {
  if (scope === "global") {
    return {
      title: "Turn off automatic sending everywhere?",
      message:
        "Stoop will stop sending routine replies on its own at every property. " +
        "Every reply comes back to you to approve. Stoop only earns automatic " +
        "sending again with a fresh streak of replies you approve unchanged.",
      confirmLabel: "Turn off everywhere",
    };
  }
  return {
    title: "Turn off automatic sending here?",
    message:
      "Stoop will stop sending routine replies on its own at this property. " +
      "Every reply comes back to you to approve. Stoop only earns automatic " +
      "sending again with a fresh streak of replies you approve unchanged.",
    confirmLabel: "Turn off",
  };
}

/** The post-call notice, honest about what actually changed — driven by
 *  the server's `revoked_count`, never assumed. */
export function revokeResultNotice(scope: RevokeTrustScope, revokedCount: number): string {
  if (revokedCount === 0) {
    return "Nothing was set to send automatically — every reply already waits for you.";
  }
  if (scope === "global") {
    return "Done. Automatic sending is off at every property — every reply now waits for your approval.";
  }
  return "Done. Automatic sending is off here — every reply now waits for your approval.";
}

/** What the property-detail section says ABOVE the button — describes the
 *  ladder without claiming to know this property's current rung (no read
 *  contract). */
export const TRUST_SECTION_TITLE = "Automatic sending";

export const TRUST_SECTION_BODY =
  "For routine repairs only, Stoop can earn the right to send a reply here " +
  "without waiting — after a streak of replies you approved unchanged. " +
  "Emergencies and anything urgent always come to you. Turning it off is " +
  "always one tap, right here.";
