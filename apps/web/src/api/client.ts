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

/**
 * Fetch + parse one `/v1` call. Resolves the JSON body on 2xx (or
 * `undefined` on a 204); throws `ApiError` on everything else, including a
 * dropped connection (mapped to the stable `network_error` code so callers
 * never have to special-case a raw `TypeError`) or an unconfigured API URL
 * (`not_configured`, thrown before any `fetch` happens).
 */
export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
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

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
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

  return parsed as T;
}
