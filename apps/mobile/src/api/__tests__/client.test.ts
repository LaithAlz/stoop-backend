/**
 * apiRequest / apiRequestWithDate unit tests — fetch mocked, NO real
 * network (hard rule). Covers the issue #210 M1 brief's explicit list:
 * envelope parsing, auth header injection, 401 handling, and the
 * `draft_stale` extra-field (`fresh_draft_id`) passthrough; plus issue
 * #250's `apiRequestWithDate` variant, which surfaces the response's own
 * `Date` header for the undo-countdown anchor
 * (src/features/queue/queueEntries.ts's `computeUndoExpiresAt`).
 */
import { apiRequest, apiRequestWithDate } from "@/api/client";
import { ApiError } from "@/api/errors";

jest.mock("@/lib/env", () => ({
  env: { apiUrl: "http://test.local", supabaseUrl: "https://x", supabaseAnonKey: "anon" },
}));

const mockGetSession = jest.fn();
const mockSignOut = jest.fn(() => Promise.resolve({ error: null }));

jest.mock("@/lib/supabase", () => ({
  supabase: {
    auth: {
      getSession: () => mockGetSession(),
      signOut: () => mockSignOut(),
    },
  },
}));

function jsonResponse(status: number, body: unknown, dateHeader: string | null = null): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: () => Promise.resolve(JSON.stringify(body)),
    headers: { get: (name: string) => (name.toLowerCase() === "date" ? dateHeader : null) },
  } as unknown as Response;
}

describe("apiRequest", () => {
  beforeEach(() => {
    jest.resetAllMocks();
    mockGetSession.mockResolvedValue({ data: { session: null } });
    mockSignOut.mockResolvedValue({ error: null });
    globalThis.fetch = jest.fn();
  });

  it("resolves the parsed JSON body on success", async () => {
    (globalThis.fetch as jest.Mock).mockResolvedValue(jsonResponse(200, { items: [], counts: {} }));

    const result = await apiRequest<{ items: unknown[] }>("/v1/queue");

    expect(result).toEqual({ items: [], counts: {} });
  });

  it("injects the live session's access token as a bearer header, never a cached one", async () => {
    mockGetSession.mockResolvedValue({ data: { session: { access_token: "live-token-1" } } });
    (globalThis.fetch as jest.Mock).mockResolvedValue(jsonResponse(200, {}));

    await apiRequest("/v1/me");

    expect(mockGetSession).toHaveBeenCalledTimes(1);
    const [, init] = (globalThis.fetch as jest.Mock).mock.calls[0];
    expect(init.headers.Authorization).toBe("Bearer live-token-1");
  });

  it("sends no Authorization header when there is no live session", async () => {
    (globalThis.fetch as jest.Mock).mockResolvedValue(jsonResponse(200, {}));

    await apiRequest("/v1/me");

    const [, init] = (globalThis.fetch as jest.Mock).mock.calls[0];
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("parses the error envelope into a typed ApiError, extra fields included", async () => {
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(409, {
        error: {
          code: "draft_stale",
          message: "A newer message superseded this draft.",
          request_id: "req_abc123",
          fresh_draft_id: "draft-999",
        },
      }),
    );

    await expect(
      apiRequest("/v1/drafts/draft-1/approve", { method: "POST" }),
    ).rejects.toMatchObject({
      code: "draft_stale",
      requestId: "req_abc123",
      body: { fresh_draft_id: "draft-999" },
    });
  });

  it("signs out on a 401 so the auth gate swaps back to sign-in", async () => {
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(401, {
        error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
      }),
    );

    await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
    expect(mockSignOut).toHaveBeenCalledTimes(1);
  });

  it("maps a dropped connection to a house-voice network_error, never the raw fetch failure", async () => {
    (globalThis.fetch as jest.Mock).mockRejectedValue(new TypeError("Network request failed"));

    const error = await apiRequest("/v1/queue").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("network_error");
    expect((error as ApiError).message).not.toMatch(/TypeError/);
  });
});

describe("apiRequestWithDate (#250 — the undo-countdown anchor)", () => {
  beforeEach(() => {
    jest.resetAllMocks();
    mockGetSession.mockResolvedValue({ data: { session: null } });
    mockSignOut.mockResolvedValue({ error: null });
    globalThis.fetch = jest.fn();
  });

  it("resolves both the parsed body and the raw Date response header", async () => {
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(200, { undo_until: "2026-07-16T08:00:05Z" }, "Thu, 16 Jul 2026 08:00:00 GMT"),
    );

    const result = await apiRequestWithDate<{ undo_until: string }>("/v1/drafts/draft-1/approve", {
      method: "POST",
    });

    expect(result.data).toEqual({ undo_until: "2026-07-16T08:00:05Z" });
    expect(result.dateHeader).toBe("Thu, 16 Jul 2026 08:00:00 GMT");
  });

  it("returns a null dateHeader (never throws) when the response carries none", async () => {
    (globalThis.fetch as jest.Mock).mockResolvedValue(jsonResponse(200, { ok: true }, null));

    const result = await apiRequestWithDate("/v1/drafts/draft-1/approve", { method: "POST" });

    expect(result.dateHeader).toBeNull();
  });

  it("still throws a typed ApiError on a non-2xx, same as apiRequest", async () => {
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(409, {
        error: { code: "draft_stale", message: "stale.", request_id: "req_1" },
      }),
    );

    await expect(
      apiRequestWithDate("/v1/drafts/draft-1/approve", { method: "POST" }),
    ).rejects.toMatchObject({ code: "draft_stale" });
  });
});
