/**
 * The one place that calls `fetch` against apps/api. Ported from
 * apps/mobile/src/api/client.ts (campaign issue #234) — every typed
 * function future PRs add under src/api/{queue,cases,drafts,notifications,
 * me}.ts goes through `apiRequest`; nothing else in the app constructs a
 * request by hand.
 *
 * Auth: the bearer token is read fresh from `supabase.auth.getSession()` on
 * every call (mirrors mobile's #210 M1 brief: "never store the token
 * separately") — there is no token cached in this module or anywhere else;
 * supabase-js already persists/refreshes the session
 * (src/lib/supabase.ts), so this is just reading its current value.
 *
 * Never log a request/response body, header, or token (CLAUDE.md rule 5 —
 * tenant messages are PII-adjacent, JWTs are secrets). This module has no
 * console.log/warn/error of any payload; a network failure below throws a
 * house-voice `ApiError`, never the raw `TypeError` fetch throws.
 */
import { supabase } from "@/lib/supabase";
import { env } from "@/lib/env";
import { ApiError } from "./errors";
import type { ApiErrorBody } from "./types";

export type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

export interface ApiRequestOptions {
  method?: HttpMethod;
  body?: unknown;
  signal?: AbortSignal;
}

async function authHeader(): Promise<Record<string, string>> {
  // A5 (safety review, #234): `supabase` is unconditionally `null` during
  // SSR (src/lib/supabase.ts's window-gate), which used to make this
  // function silently degrade to an anonymous request whenever it ran on
  // the server — indistinguishable from the genuinely-unconfigured case.
  // No route calls `apiRequest` from a server context today, but a future
  // loader/server function that did would otherwise fire an unauthenticated
  // request instead of failing loudly. Distinct code from `not_configured`
  // on purpose — this is a programming-contract violation, not a
  // deployment config problem, and should never reach a real screen.
  if (typeof window === "undefined") {
    throw new ApiError(0, {
      code: "server_context",
      message: "This request needs a browser session and can't run on the server.",
      request_id: "req_local",
    });
  }
  if (!supabase) return {};
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Web-only guard (no equivalent in the mobile client, whose `env.apiUrl`
 *  always has a `localhost` fallback): a build with `VITE_API_URL` unset
 *  has nowhere to send the request, so this fails locally with the same
 *  `ApiError` shape every other failure uses, instead of `fetch`ing a
 *  relative/undefined URL. */
function requireApiUrl(): string {
  if (!env.apiUrl) {
    throw new ApiError(0, {
      code: "not_configured",
      message: "Stoop's API isn't set up for this build yet.",
      request_id: "req_local",
    });
  }
  return env.apiUrl;
}

/** A malformed/non-JSON error body still has to produce a usable ApiError
 *  instead of throwing a second, unrelated parse error on top of the first. */
function coerceErrorBody(parsed: unknown, status: number): ApiErrorBody {
  if (
    parsed &&
    typeof parsed === "object" &&
    "error" in parsed &&
    parsed.error &&
    typeof parsed.error === "object"
  ) {
    const candidate = parsed.error as Record<string, unknown>;
    if (typeof candidate.code === "string" && typeof candidate.message === "string") {
      return {
        ...candidate,
        code: candidate.code,
        message: candidate.message,
        request_id: typeof candidate.request_id === "string" ? candidate.request_id : "req_unknown",
      };
    }
  }
  return {
    code: status === 401 ? "unauthorized" : "unknown_error",
    message: "The server sent back something unexpected.",
    request_id: "req_unknown",
  };
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** B2 (safety review, #234 PR 2): the response envelope for callers that
 *  need the server's OWN clock alongside the body — e.g. anchoring the
 *  undo countdown to `undo_until` without ever mixing it with the
 *  client's wall clock (a client clock a couple of minutes fast would
 *  otherwise silently swallow the whole undo window). `dateHeader` is the
 *  raw `Date` response header, unparsed (`null` if the response somehow
 *  didn't carry one) — the caller decides how to parse/guard it. */
export interface ApiResponseEnvelope<T> {
  data: T;
  dateHeader: string | null;
}

/**
 * Fetch + parse one `/v1` call. Resolves the JSON body on 2xx (or
 * `undefined` on a 204); throws `ApiError` on everything else, including a
 * dropped connection (mapped to the stable `network_error` code so callers
 * never have to special-case a raw `TypeError`) or an unconfigured API URL
 * (`not_configured`, thrown before any `fetch` happens).
 *
 * Shared by both exported entry points below — `apiRequest` (unchanged
 * signature, every existing caller) and `apiRequestWithDate` (B2's new
 * variant) — so there is exactly one place that builds headers, calls
 * `fetch`, and maps a non-2xx response to `ApiError`.
 */
async function apiRequestInternal<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<ApiResponseEnvelope<T>> {
  const apiUrl = requireApiUrl();

  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(await authHeader()),
  };

  let body: string | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(`${apiUrl}${path}`, {
      method: options.method ?? "GET",
      headers,
      body,
      signal: options.signal,
    });
  } catch {
    throw new ApiError(0, {
      code: "network_error",
      message: "Couldn't reach Stoop. Check your connection and try again.",
      request_id: "req_local",
    });
  }

  const dateHeader = response.headers.get("date");

  if (response.status === 204) {
    return { data: undefined as T, dateHeader };
  }

  // F5 (safety re-verify, #252): reading the body is inside its own
  // try/catch mapped to `network_error`. It used to sit OUTSIDE the fetch
  // try/catch above, so a connection dropped AFTER response headers — the
  // classic "the response was lost" case, and the one the approve loop's
  // whole ambiguity guard exists for — rejected here as a raw TypeError.
  // Callers saw a non-ApiError, showed "Something didn't go through. Try
  // again in a moment.", and never raised the unverified-send flag: an
  // explicit invitation to retype and resend a reply that may already
  // have gone out.
  let text: string;
  try {
    text = await response.text();
  } catch {
    throw new ApiError(0, {
      code: "network_error",
      message: "Couldn't reach Stoop. Check your connection and try again.",
      request_id: "req_local",
    });
  }
  const parsed = text.length > 0 ? safeJsonParse(text) : null;

  if (!response.ok) {
    if (response.status === 401 && supabase) {
      // B3 (safety review, #234): a bare 401 is NOT proof the session is
      // actually dead — a transient backend hiccup (e.g. a JWKS rotation
      // blip on the API side) can 401 a token that's still genuinely
      // live. Check the LOCAL session before reacting: only sign out when
      // it's genuinely absent or past its own `expires_at` (the server
      // and the local client disagreeing on a still-valid token is not
      // grounds to destroy it); otherwise let the `ApiError` below
      // surface normally and leave the session alone so the caller can
      // retry.
      const { data } = await supabase.auth.getSession();
      const localSession = data.session;
      const nowSeconds = Math.floor(Date.now() / 1000);
      const sessionLooksDead =
        !localSession ||
        (typeof localSession.expires_at === "number" && localSession.expires_at <= nowSeconds);
      if (sessionLooksDead) {
        // `scope: "local"` — this device's session only. A transient API
        // 401 (or a session that's actually expired here) must never
        // reach out and kill the landlord's OTHER signed-in devices; see
        // the matching comment on AuthProvider.signOut(). This fires
        // `onAuthStateChange`'s `SIGNED_OUT` event, which
        // src/auth/AuthProvider.tsx uses to clear the query cache (the
        // PII fence) and src/routes/app.tsx's guard uses to swap back to
        // sign-in.
        void supabase.auth.signOut({ scope: "local" });
      }
    }
    throw new ApiError(response.status, coerceErrorBody(parsed, response.status));
  }

  // H3 (safety review, #234 PR 3 fix round): a 2xx whose body is empty or
  // fails to parse as JSON is NOT a successful empty result — `parsed` is
  // `null` in both cases (`safeJsonParse`'s own catch-all, and the
  // `text.length > 0` guard above), and every typed caller in this app
  // expects an object (`{items: [...]}`, `{id: ...}`, ...). Letting a
  // bare `null` through as `T` used to read as "success with no data" —
  // a blank emergency takeover, a false "no conversations yet." empty
  // state — instead of the genuinely unexpected-response failure it is.
  // Same shape `coerceErrorBody`'s own unparsable-body fallback already
  // uses, applied here to a SUCCESS body instead of an error one. `204`s
  // are unaffected (handled above, before this point, by design — an
  // empty body IS the contract there).
  if (parsed === null || typeof parsed !== "object") {
    throw new ApiError(response.status, {
      code: "unknown_error",
      message: "The server sent back something unexpected.",
      request_id: "req_unknown",
    });
  }

  return { data: parsed as T, dateHeader };
}

/** The default entry point — every caller before B2, unchanged signature. */
export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { data } = await apiRequestInternal<T>(path, options);
  return data;
}

/** B2's variant for a caller that needs the response's `Date` header
 *  alongside the body (today: the approve/edit-and-send undo countdown,
 *  src/api/drafts.ts). Everything else about the request/error handling
 *  is identical to `apiRequest` above. */
export async function apiRequestWithDate<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<ApiResponseEnvelope<T>> {
  return apiRequestInternal<T>(path, options);
}
