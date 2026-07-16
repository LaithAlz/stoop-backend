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
 * src/auth/AuthProvider.tsx's `toHouseAuthError`. Only codes M1 actually
 * surfaces get a bespoke line; everything else — including real but
 * out-of-scope-for-M1 codes like `property_limit_reached` — gets the
 * honest generic fallback rather than a guessed-at line for a screen this
 * phase doesn't ship.
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
    default:
      return GENERIC_ERROR;
  }
}
