/**
 * Typed API error + the house-voice mapping seam. Ported from
 * apps/mobile/src/api/errors.ts nearly verbatim (mobile is the production-
 * wired reference client, campaign issue #234).
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
 * doesn't exist yet. Screens stay on mock data in this PR (#234 PR 1) — no
 * shipped web screen surfaces these yet, but the map is ported in full so
 * later PRs (queue/cases/properties) don't have to touch this file.
 */
import type { ApiErrorBody } from "./types";

// A6 (safety review, #234, advisory — flagged not fixed): `.code` below is
// a plain `string`, so `error.code === "draft_stale"` and a typo like
// `"draft_stle"` both typecheck identically. A branded/union type (the
// server's real code vocabulary, `docs/03-engineering/api-contracts.md`,
// plus this file's client-synthesized ones) would catch that at compile
// time. Not done here — the server's code vocabulary isn't centrally typed
// anywhere yet either, and this file's `switch` is the only place codes
// are string-matched today.
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
    // Web-only addition (not in the mobile map — mobile's VITE_API_URL
    // equivalent always has a localhost fallback and can't hit this):
    // src/lib/env.ts's `apiUrl` has no default, so an unconfigured build
    // reaches here instead of ever making a request.
    case "not_configured":
      return "Stoop's API isn't set up for this build yet.";
    // `server_context` (src/api/client.ts's authHeader, A5 amendment) is
    // deliberately NOT given a bespoke line here — it's a programming-
    // contract violation (something called `apiRequest` outside the
    // browser), not a state a real landlord should ever see, so it falls
    // through to the honest generic default like any other code no
    // shipped screen surfaces.
    case "draft_stale":
      return "A new message came in. This draft just updated.";
    case "already_sent":
      return "That reply already went out. There's nothing left to undo.";
    case "draft_not_undoable":
      return "That draft isn't waiting to send anymore.";
    // NEW-4 (safety review round 3, #234 PR 2): without a bespoke line, a
    // 404'd draft got the generic "try again in a moment" — an invitation
    // to retry forever against a draft that no longer exists (the editor
    // also closes on this code, useDraftActions.ts).
    case "draft_not_found":
      return "That draft isn't there anymore.";
    case "case_not_found":
      return "That conversation isn't there anymore.";
    // M1 (safety review, #234 PR 4): the global 422 handler's code
    // (api-contracts.md's error envelope) — reached whenever a route param
    // isn't the shape the API expects, e.g. a bookmarked/hand-typed
    // non-UUID property or case id. Without a line of its own it fell to
    // the generic "try again in a moment" beside a Try-again button that
    // can never succeed — a permanent dead end dressed as a transient one.
    case "invalid_request":
      return "That link doesn't point to anything on your account.";
    case "rate_limited":
      return "Too many tries. Wait a moment and try again.";
    case "account_deleted":
      return "This account isn't active. Contact support if that's unexpected.";
    case "invalid_cursor":
      return "Couldn't load more. Try refreshing the list.";
    // --- Property provisioning (POST /v1/properties, v1.12 amendment) ---
    case "property_limit_reached":
      return "Your account is at its property limit, so this one wasn't added. Contact support to raise it.";
    case "duplicate_property":
      return "You've already added a property at this address. It's in your Properties list.";
    case "no_numbers_available":
      return "No phone numbers were available just now, so nothing was set up. Try a different area code, or try again in a few minutes.";
    case "provisioning_failed":
      return "Setting up this property's phone number didn't work, so nothing was saved. Try again.";
    // R4 (#258 follow-up): a 500 config error (PUBLIC_BASE_URL unset,
    // api-contracts.md's Properties v1.12 amendment) — permanent until
    // someone fixes the deployment, not a per-request glitch. Without a
    // bespoke line this fell to the generic "try again in a moment" next
    // to a Try-again button that can never succeed while it's broken.
    case "public_base_url_unconfigured":
      return "Setting up phone numbers is broken on our end right now, so nothing was saved. Contact support. Trying again won't help.";
    // --- Property delete (DELETE /v1/properties/{id}) ---
    case "has_open_cases":
      return "This property still has open cases, so it can't be deleted yet.";
    case "has_dependents":
      return "This property has tenants or saved history attached, so it can't be deleted.";
    case "property_not_found":
      return "That property isn't there anymore.";
    // --- Backup contact (POST/PATCH /v1/properties, #290/api-contracts.md
    // v1.27 amendment): deliberately distinct from `invalid_field` below
    // (a `phone` that's present but can't be turned into a real number).
    // This code fires when the phone is missing, `null`, or blank instead,
    // so retrying the exact same submission can never succeed. #307: no
    // shipped client sends that shape today (the web settings form and
    // mobile onboarding step both already require a name AND a phone
    // together, or send neither), so this was unmapped and fell through to
    // the generic retry line below, telling a landlord to do the one thing
    // guaranteed not to work. Mapped now, ahead of the first writer that
    // can actually reach it.
    case "backup_contact_no_phone":
      return "A backup contact needs a phone number. Add one.";
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
