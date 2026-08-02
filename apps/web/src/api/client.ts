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
 * (src/lib/supabase.ts), so this is just reading its current value. When
 * `supabase` is `null` (Supabase not configured for this build,
 * src/lib/env.ts), `authHeader()` sends no `Authorization` header at all —
 * every route that could reach here is already gated behind a live session
 * (src/routes/app.tsx), so this only matters for a misconfigured deploy,
 * where the request will 401 honestly rather than throw locally.
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
      // The server rejected a token we believed was live (expired/revoked
      // between local checks) — sign out so the auth gate (src/routes/
      // app.tsx, resolveAuthRoute) swaps back to sign-in instead of every
      // screen quietly re-401ing forever. This fires `onAuthStateChange`'s
      // `SIGNED_OUT` event, which src/auth/AuthProvider.tsx uses to clear
      // the query cache (the PII fence).
      void supabase.auth.signOut();
    }
    throw new ApiError(response.status, coerceErrorBody(parsed, response.status));
  }

  return parsed as T;
}
