/**
 * Confirmation copy for the landlord-direct resolve (api-contracts.md
 * v1.14: POST /v1/cases/{id}/resolve). Pure strings, exported so the exact
 * wording is test-covered rather than inlined in the screen.
 *
 * The contract says resolving cancels every unsent pending/approved draft
 * on the case in the same transaction — the confirmation states that
 * plainly BEFORE the call, because a landlord who taps "Mark resolved"
 * while a reply sits drafted needs to know that reply will not go out.
 */
export const RESOLVE_CONFIRM_TITLE = "Mark this as resolved?";

export const RESOLVE_CONFIRM_MESSAGE =
  "This closes the case. If a reply is drafted but hasn't sent yet, it won't go out. " +
  "If the tenant texts again, a new case opens.";

export const RESOLVE_CONFIRM_LABEL = "Mark resolved";

/** Post-success notice — also correct on an idempotent repeat (the server
 *  returns the same 200 with the stored `resolved_at` either way). */
export const RESOLVE_DONE_NOTICE = "Resolved. It stays in your records.";
