/**
 * Typed API error + the house-voice mapping seam.
 *
 * `ApiError` carries the parsed error envelope (docs/03-engineering/
 * api-contracts.md "Conventions" — `{code, message, request_id}` plus any
 * endpoint-specific extra field, e.g. `fresh_draft_id`) so callers can
 * branch on `.code` without string-matching `.message` (the doc says
 * messages "may change freely" — codes are the stable contract).
 *
 * `toHouseApiError` is the ONE place a raw server/library string turns into
 * a landlord-facing line (CLAUDE.md rule 8 / plain-language-rules.md) — no
 * screen renders `error.message` directly, same seam shape as
 * src/auth/AuthProvider.tsx's `toHouseAuthError`. Only codes a shipped
 * screen actually surfaces get a bespoke line; everything else gets the
 * honest generic fallback rather than a guessed-at line for a screen that
 * doesn't exist yet. M2 added the provisioning/property/tenant/trust codes
 * (api-contracts.md's Properties + Tenants sections, v1.12 amendment) —
 * each line states what happened and what the landlord can actually do,
 * never a raw code, never "soon".
 */
import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  readonly body: ApiErrorBody;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.requestId = body.request_id;
    this.body = body;
  }
}

const GENERIC_ERROR = "Something didn't go through. Try again in a moment.";

export function toHouseApiError(error: ApiError): string {
  switch (error.code) {
    case "network_error":
      return "Couldn't reach Stoop. Check your connection and try again.";
    case "draft_stale":
      return "A new message came in — this draft just updated.";
    case "already_sent":
      return "That reply already went out — there's nothing left to undo.";
    case "draft_not_undoable":
      return "That draft isn't waiting to send anymore.";
    case "case_not_found":
      return "That conversation isn't there anymore.";
    case "rate_limited":
      return "Too many tries — wait a moment and try again.";
    case "account_deleted":
      return "This account isn't active. Contact support if that's unexpected.";
    case "invalid_cursor":
      return "Couldn't load more — try refreshing the list.";
    // --- Property provisioning (POST /v1/properties, v1.12 amendment) ---
    case "property_limit_reached":
      return "Your account is at its property limit, so this one wasn't added. Contact support to raise it.";
    case "duplicate_property":
      return "You've already added a property at this address — it's in your Properties list.";
    case "no_numbers_available":
      return "No phone numbers were available just now, so nothing was set up. Try a different area code, or try again in a few minutes.";
    case "provisioning_failed":
      return "Setting up this property's phone number didn't work, so nothing was saved. Try again.";
    // --- Property delete (DELETE /v1/properties/{id}) ---
    case "has_open_cases":
      return "This property still has open cases, so it can't be deleted yet.";
    case "has_dependents":
      return "This property has tenants or saved history attached, so it can't be deleted.";
    case "property_not_found":
      return "That property isn't there anymore.";
    // --- Tenants ---
    case "tenant_not_found":
      return "That tenant isn't on file anymore.";
    case "duplicate_phone":
      return "That phone number is already on a tenant at this property.";
    case "invalid_field":
      return "Something in the form didn't look right. Check it and try again.";
    default:
      return GENERIC_ERROR;
  }
}
