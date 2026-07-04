import "./lib/error-capture";

import { consumeLastCapturedError } from "./lib/error-capture";
import { renderErrorPage } from "./lib/error-page";

type ServerEntry = {
  fetch: (request: Request, env: unknown, ctx: unknown) => Promise<Response> | Response;
};

let serverEntryPromise: Promise<ServerEntry> | undefined;

async function getServerEntry(): Promise<ServerEntry> {
  if (!serverEntryPromise) {
    serverEntryPromise = import("@tanstack/react-start/server-entry").then(
      (m) => (m as { default?: ServerEntry }).default ?? (m as unknown as ServerEntry),
    );
  }
  return serverEntryPromise;
}

function brandedErrorResponse(): Response {
  return new Response(renderErrorPage(), {
    status: 500,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

// ADR-5 hard rule: analytics events must be proxied through our own domain
// (loading plausible.io directly loses signal to adblockers). This is the
// documented Plausible same-origin proxy — see
// https://plausible.io/docs/proxy/introduction — wired up as two exact,
// narrowly-guarded routes. Every other request falls through untouched.
const PLAUSIBLE_SCRIPT_URL = "https://plausible.io/js/script.js";
const PLAUSIBLE_EVENT_URL = "https://plausible.io/api/event";

async function proxyPlausibleScript(request: Request): Promise<Response> {
  try {
    const upstream = await fetch(PLAUSIBLE_SCRIPT_URL, {
      headers: { "user-agent": request.headers.get("user-agent") ?? "" },
    });
    const body = await upstream.arrayBuffer();
    return new Response(body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/javascript",
        "cache-control": upstream.headers.get("cache-control") ?? "public, max-age=86400",
      },
    });
  } catch (error) {
    console.error(error);
    return new Response("Analytics script unavailable", { status: 502 });
  }
}

async function proxyPlausibleEvent(request: Request): Promise<Response> {
  try {
    const body = await request.text();
    // Cloudflare's real-client-IP header — forwarded so Plausible can still
    // geolocate the pageview/event. No cookies are ever forwarded (Plausible
    // is cookieless and we build the outbound headers from scratch below).
    const forwardedFor = request.headers.get("cf-connecting-ip");
    const upstream = await fetch(PLAUSIBLE_EVENT_URL, {
      method: "POST",
      body,
      headers: {
        "content-type": request.headers.get("content-type") ?? "application/json",
        "user-agent": request.headers.get("user-agent") ?? "",
        ...(forwardedFor ? { "x-forwarded-for": forwardedFor } : {}),
      },
    });
    const responseBody = await upstream.text();
    return new Response(responseBody, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
      },
    });
  } catch (error) {
    console.error(error);
    return new Response("Analytics event forwarding failed", { status: 502 });
  }
}

// Returns null for every request except the two exact Plausible proxy
// routes — a transparent no-op for all app routes.
async function maybeProxyPlausible(request: Request): Promise<Response | null> {
  const { pathname } = new URL(request.url);
  if (request.method === "GET" && pathname === "/js/script.js") {
    return proxyPlausibleScript(request);
  }
  if (request.method === "POST" && pathname === "/api/event") {
    return proxyPlausibleEvent(request);
  }
  return null;
}

function isCatastrophicSsrErrorBody(body: string, responseStatus: number): boolean {
  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    return false;
  }

  if (!payload || Array.isArray(payload) || typeof payload !== "object") {
    return false;
  }

  const fields = payload as Record<string, unknown>;
  const expectedKeys = new Set(["message", "status", "unhandled"]);
  if (!Object.keys(fields).every((key) => expectedKeys.has(key))) {
    return false;
  }

  return (
    fields.unhandled === true &&
    fields.message === "HTTPError" &&
    (fields.status === undefined || fields.status === responseStatus)
  );
}

// h3 swallows in-handler throws into a normal 500 Response with body
// {"unhandled":true,"message":"HTTPError"} — try/catch alone never fires for those.
async function normalizeCatastrophicSsrResponse(response: Response): Promise<Response> {
  if (response.status < 500) return response;
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) return response;

  const body = await response.clone().text();
  if (!isCatastrophicSsrErrorBody(body, response.status)) {
    return response;
  }

  console.error(consumeLastCapturedError() ?? new Error(`h3 swallowed SSR error: ${body}`));
  return brandedErrorResponse();
}

export default {
  async fetch(request: Request, env: unknown, ctx: unknown) {
    const plausibleResponse = await maybeProxyPlausible(request);
    if (plausibleResponse) return plausibleResponse;

    try {
      const handler = await getServerEntry();
      const response = await handler.fetch(request, env, ctx);
      return await normalizeCatastrophicSsrResponse(response);
    } catch (error) {
      console.error(error);
      return brandedErrorResponse();
    }
  },
};
