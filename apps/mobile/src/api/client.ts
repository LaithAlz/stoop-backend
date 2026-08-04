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

/** R3 (safety review): the same defensive read as the 401 liveness gate
 *  below, one function up and on EVERY request rather than only on 401s.
 *  `getSession()` touches the keychain, and a read/decrypt rejection there
 *  used to reject `apiRequest` with a raw `Error` before `fetch` was even
 *  attempted, breaking this module's own contract (everything it throws is
 *  an `ApiError`) and bypassing `ApiError` typing in every caller's error
 *  handling. Failing to an anonymous request is the right direction: the
 *  request 401s into the liveness branch, which is now itself safe. */
async function authHeader(): Promise<Record<string, string>> {
  try {
    const { data } = await supabase.auth.getSession();
    const token = data.session?.access_token;
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
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

/** B3-3 (#284, follow-up to #263): the 401 liveness gate's own
 *  `getSession()` call below can itself go slow - if the access token sits
 *  inside auth-js's ~90s expiry margin, `getSession()` kicks off
 *  `_refreshAccessToken`, which retries a NETWORK call with its BACKOFF
 *  bounded by a 30s tick. That 30s bounds only the gap BETWEEN retry
 *  attempts, not any single attempt: each underlying `fetch` inside the
 *  retry loop carries no `AbortSignal` of its own, so one hanging POST can
 *  hold this open past 30s - on iOS, up to ~60s, `NSURLSession`'s own
 *  default request timeout (adversarial review, finding 7 - strengthens
 *  this fix, since the real old worst case was longer than the 30s figure
 *  alone suggests). On a flaky connection a 401 that used to throw
 *  instantly now held this branch (and the caller's spinner) open for up to
 *  that long before the `ApiError` below ever threw - only partly absorbed
 *  by the 60s refresh-failure cooldown. Racing the read against ~2s bounds
 *  that. ON TIMEOUT this must NOT sign out: a slow read is not evidence the
 *  session is dead, same "fail toward not dead" direction B3-2's catch
 *  below already established for this exact call - a timeout is just a
 *  different way for it to not complete cleanly, not a different verdict. */
const LIVENESS_CHECK_TIMEOUT_MS = 2000;
/** Sentinel `Promise.race` result for the timeout side - a plain string
 *  literal (not a rejection) so the timeout path runs through the same
 *  `sessionLooksDead = false` assignment as every other "couldn't tell"
 *  case here, rather than a second, parallel code path. */
const LIVENESS_CHECK_TIMED_OUT = "liveness-check-timed-out" as const;

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

  // Adversarial safety review, 2026-08-04, item 2 (FIX 2, HIGH): read
  // OUTSIDE the try below. `env.apiUrl` throws for a misconfigured
  // production build (src/lib/env.ts's `requireHttpsInProduction`, #292),
  // and that throw used to happen INSIDE this function call expression,
  // which put it inside the try's scope even though it has nothing to do
  // with `fetch` itself. The unbound `catch` below mapped it to the same
  // generic "Couldn't reach Stoop" `network_error` every dropped connection
  // gets, silently swallowing the one diagnostic that would have told a
  // developer or a landlord's device WHY nothing was ever sent (there is no
  // Sentry in this app and no console.* on this path). Hoisting the read
  // here means a config error surfaces as itself; only a genuine `fetch`
  // failure below still maps to `network_error`.
  const base = env.apiUrl;

  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
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
      // B3-3: raced against LIVENESS_CHECK_TIMEOUT_MS - see that constant's
      // docstring above for why. `livenessTimer` is cleared in `finally`
      // regardless of which side of the race wins, so a fast getSession()
      // never leaves a pending ~2s timer behind (mirrors
      // deviceRegistration.ts's `unregisterCurrentDeviceBestEffort`).
      let livenessTimer: ReturnType<typeof setTimeout> | undefined;
      try {
        const outcome = await Promise.race([
          supabase.auth.getSession(),
          new Promise<typeof LIVENESS_CHECK_TIMED_OUT>((resolve) => {
            livenessTimer = setTimeout(
              () => resolve(LIVENESS_CHECK_TIMED_OUT),
              LIVENESS_CHECK_TIMEOUT_MS,
            );
          }),
        ]);
        if (outcome === LIVENESS_CHECK_TIMED_OUT) {
          sessionLooksDead = false;
        } else {
          const localSession = outcome.data.session;
          // B3-1: anchored to the server's own clock (this response's Date
          // header), never the device's - see resolveNowSeconds above.
          const nowSeconds = resolveNowSeconds(dateHeader);
          sessionLooksDead =
            !localSession ||
            (typeof localSession.expires_at === "number" && localSession.expires_at <= nowSeconds);
        }
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
      } finally {
        // `!== undefined`, not bare truthiness (#284 adversarial review,
        // finding 6): RN/Hermes' `setTimeout` returns a numeric id (unlike
        // Node's truthy `Timeout` object), and id `0` would be falsy. Not
        // reachable in practice (Hermes' ids start at 1), but this is the
        // strictly correct check, not a "trust the runtime" one.
        if (livenessTimer !== undefined) clearTimeout(livenessTimer);
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
