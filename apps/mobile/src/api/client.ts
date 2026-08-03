/**
 * The one place that calls `fetch` against apps/api. Every typed function
 * in src/api/{queue,cases,drafts,notifications,me}.ts goes through
 * `apiRequest` — nothing else in the app constructs a request by hand.
 *
 * Auth: the bearer token is read fresh from `supabase.auth.getSession()` on
 * every call (issue #210 M1 brief: "never store the token separately") —
 * there is no token cached in this module or anywhere else; supabase-js
 * already persists/refreshes the session (src/lib/supabase.ts), so this is
 * just reading its current value.
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
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  return token ? { Authorization: `Bearer ${token}` } : {};
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

/** #250: the response envelope for a caller that needs the server's OWN
 *  clock alongside the body — e.g. anchoring the undo countdown to
 *  `undo_until` without ever mixing it with the device's wall clock (a
 *  device clock a couple of minutes fast would otherwise silently swallow
 *  the whole undo window — see src/features/queue/queueEntries.ts's
 *  `computeUndoExpiresAt`). `dateHeader` is the raw `Date` response header,
 *  unparsed (`null` if the response somehow didn't carry one) — the caller
 *  decides how to parse/guard it. Ported from apps/web/src/api/client.ts
 *  (#234 PR 2's B2 finding, itself ported from this file originally — #250
 *  closes the same gap at the mobile source). */
export interface ApiResponseEnvelope<T> {
  data: T;
  dateHeader: string | null;
}

/**
 * Fetch + parse one `/v1` call. Resolves the JSON body on 2xx (or
 * `undefined` on a 204); throws `ApiError` on everything else, including a
 * dropped connection (mapped to the stable `network_error` code so callers
 * never have to special-case a raw `TypeError`).
 *
 * Shared by both exported entry points below — `apiRequest` (unchanged
 * signature, every existing caller) and `apiRequestWithDate` (#250's new
 * variant) — so there is exactly one place that builds headers, calls
 * `fetch`, and maps a non-2xx response to `ApiError`.
 */
async function apiRequestInternal<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<ApiResponseEnvelope<T>> {
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
    response = await fetch(`${env.apiUrl}${path}`, {
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

  // Optional-chained: this line runs on EVERY response (204s included) and
  // sits outside the try/catch that maps transport failures, so a
  // hypothetical `Response` without `headers` would throw a raw TypeError
  // past the client — degrading typed `ApiError` handling on paths that
  // include the emergency acknowledge. Unreachable with RN's fetch; the
  // guard costs nothing (#250 safety review).
  const dateHeader = response.headers?.get("date") ?? null;

  if (response.status === 204) {
    return { data: undefined as T, dateHeader };
  }

  const text = await response.text();
  const parsed = text.length > 0 ? safeJsonParse(text) : null;

  if (!response.ok) {
    if (response.status === 401) {
      // The server rejected a token we believed was live (expired/revoked
      // between local checks) — sign out so the root layout's auth gate
      // (src/app/_layout.tsx, resolveAuthRoute) swaps back to sign-in
      // instead of every screen quietly re-401ing forever.
      void supabase.auth.signOut();
    }
    throw new ApiError(response.status, coerceErrorBody(parsed, response.status));
  }

  return { data: parsed as T, dateHeader };
}

/** The default entry point — every caller before #250, unchanged signature. */
export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { data } = await apiRequestInternal<T>(path, options);
  return data;
}

/** #250's variant for a caller that needs the response's `Date` header
 *  alongside the body (today: the approve/edit-and-send undo countdown,
 *  src/api/drafts.ts). Everything else about the request/error handling is
 *  identical to `apiRequest` above. */
export async function apiRequestWithDate<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<ApiResponseEnvelope<T>> {
  return apiRequestInternal<T>(path, options);
}
