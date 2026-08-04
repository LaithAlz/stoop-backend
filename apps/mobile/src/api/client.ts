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

/** B3-1 (safety review): the 401 liveness gate below has to anchor to the
 *  SERVER's clock, not the device's. A device clock that is fast by more
 *  than the token's remaining lifetime (auto-time off, a traveler, a dead
 *  RTC) reads every still-valid session as expired, which defeats the
 *  gate on exactly the device most likely to have a wrong clock: the
 *  session gets wiped on the same transient 401 this fix exists to
 *  survive. Same clock-mixing bug class as #250, which is why the undo
 *  countdown is anchored to this same response header instead of
 *  Date.now(). Falls back to the device clock only when the header is
 *  missing or unparsable. */
function resolveNowSeconds(dateHeader: string | null): number {
  if (dateHeader) {
    const parsed = new Date(dateHeader);
    if (!Number.isNaN(parsed.getTime())) {
      return Math.floor(parsed.getTime() / 1000);
    }
  }
  return Math.floor(Date.now() / 1000);
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

  // H3-2 (safety review): mirrors the fetch try/catch above. A connection
  // dropped mid-body (more likely on a mobile network than a dropped
  // connection before any bytes arrive) used to throw a raw TypeError past
  // this module's own documented contract instead of the house
  // network_error envelope.
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
    if (response.status === 401) {
      // B3 (#263, ported from apps/web/src/api/client.ts): a bare 401 is
      // NOT proof the session is actually dead - a transient backend
      // hiccup (e.g. a JWKS rotation blip on the API side) can 401 a
      // token that's still genuinely live. This is materially worse on
      // mobile than on web (#263 safety-review comment): this is the
      // device holding the approval queue and push nudges, and the
      // forced-401 path below deliberately skips the server-side device
      // unregister (it has no live token to authenticate the DELETE
      // with), so an unconditional sign-out here used to wipe the local
      // session while push nudges kept arriving at a device now showing a
      // sign-in wall. (The emergency path itself is voice and SMS, never
      // push - CLAUDE.md rule 1, apps/api/app/push_outbox.py - so this is
      // about a landlord losing sight of the ordinary queue, not the
      // emergency chain, which reaches them by phone regardless.)
      //
      // Check the LOCAL session before reacting: only sign out when it's
      // genuinely absent or past its own `expires_at` (the server and the
      // local client disagreeing on a still-valid token is not grounds to
      // destroy it); otherwise let the `ApiError` below surface normally
      // and leave the session alone so the caller can retry.
      let sessionLooksDead = false;
      try {
        const { data } = await supabase.auth.getSession();
        const localSession = data.session;
        // B3-1: anchored to the server's own clock (this response's Date
        // header), never the device's - see resolveNowSeconds above.
        const nowSeconds = resolveNowSeconds(dateHeader);
        sessionLooksDead =
          !localSession ||
          (typeof localSession.expires_at === "number" && localSession.expires_at <= nowSeconds);
      } catch {
        // B3-2 (safety review): getSession() can REJECT, not just resolve
        // to a null session - expo-secure-store's getItemAsync throws on a
        // keychain read or decrypt failure (restore from backup, a
        // keychain error), and auth-js's own session-load path has no
        // catch around that read, so getSession() itself rejects. An
        // unreadable keychain is not evidence the session is gone, so this
        // fails toward "not dead" rather than signing out on a read it
        // could not actually complete.
        sessionLooksDead = false;
      }
      if (sessionLooksDead) {
        // `scope: "local"` - this device's session only (matches
        // src/auth/AuthProvider.tsx's explicit sign-out). A transient API
        // 401 (or a session that's actually expired here) must never
        // reach out and kill the landlord's OTHER signed-in devices.
        // B3-4: `.catch` so a storage-write failure or a non-AuthError
        // rethrow from signOut itself can never surface as an unhandled
        // rejection - this call is already fire-and-forget by design (the
        // `ApiError` thrown below is what the caller actually awaits).
        void supabase.auth.signOut({ scope: "local" }).catch(() => {});
      }
    }
    throw new ApiError(response.status, coerceErrorBody(parsed, response.status));
  }

  // H3 (#263, ported from apps/web/src/api/client.ts): a 2xx whose body is
  // empty or fails to parse as JSON is NOT a successful empty result —
  // `parsed` is `null` in both cases (`safeJsonParse`'s own catch-all, and
  // the `text.length > 0` guard above), and every typed caller in this app
  // expects an object (`{items: [...]}`, `{id: ...}`, ...). Letting a bare
  // `null` through as `T` used to read as "success with no data" — a false
  // "no conversations yet." empty state, or `result.data.undo_until`
  // throwing a raw TypeError instead of the house `unknown_error` — rather
  // than the genuinely unexpected-response failure it is. This check sits
  // BELOW the 204 early-return above (by design — an empty body IS the
  // contract there).
  if (parsed === null || typeof parsed !== "object") {
    throw new ApiError(response.status, {
      code: "unknown_error",
      message: "The server sent back something unexpected.",
      request_id: "req_unknown",
    });
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
