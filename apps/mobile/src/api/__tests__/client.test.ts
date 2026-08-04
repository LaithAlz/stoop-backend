/**
 * apiRequest / apiRequestWithDate unit tests — fetch mocked, NO real
 * network (hard rule). Covers the issue #210 M1 brief's explicit list:
 * envelope parsing, auth header injection, 401 handling, and the
 * `draft_stale` extra-field (`fresh_draft_id`) passthrough; plus issue
 * #250's `apiRequestWithDate` variant, which surfaces the response's own
 * `Date` header for the undo-countdown anchor
 * (src/features/queue/queueEntries.ts's `computeUndoExpiresAt`); plus
 * #263's B3 finding — the 401 handler must consult the LOCAL session
 * before signing out, and must only ever sign out `{ scope: "local" }`
 * (this device only, never the landlord's other devices).
 */
import { apiRequest, apiRequestWithDate } from "@/api/client";
import { ApiError } from "@/api/errors";

jest.mock("@/lib/env", () => ({
  env: { apiUrl: "http://test.local", supabaseUrl: "https://x", supabaseAnonKey: "anon" },
}));

const mockGetSession = jest.fn();
const mockSignOut = jest.fn((_options?: { scope?: string }) => Promise.resolve({ error: null }));

jest.mock("@/lib/supabase", () => ({
  supabase: {
    auth: {
      getSession: () => mockGetSession(),
      // Forwards the call's argument (unlike a bare `() => mockSignOut()`)
      // so tests below can assert exactly what scope was requested — B3's
      // whole point is that this must always be `{ scope: "local" }`.
      signOut: (options?: { scope?: string }) => mockSignOut(options),
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

/** For H3 tests — a response whose raw text body is NOT run through
 *  `JSON.stringify` first, so a truly empty string or genuinely-invalid
 *  JSON can be exercised (jsonResponse above always produces valid JSON). */
function rawResponse(status: number, text: string, dateHeader: string | null = null): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: () => Promise.resolve(text),
    headers: { get: (name: string) => (name.toLowerCase() === "date" ? dateHeader : null) },
  } as unknown as Response;
}

/** For H3-2 - a response whose `.text()` itself rejects, standing in for a
 *  connection dropped mid-body (headers already arrived, so `response.ok`/
 *  `status` are readable, but the body stream never completes). */
function textRejectsResponse(status: number, dateHeader: string | null = null): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: () => Promise.reject(new Error("stream closed")),
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

  it("F7 (re-verify): apiRequest hands the caller's AbortSignal to fetch", async () => {
    // #284's reconcile DELETE aborts on its own timeout so an abandoned
    // request cannot land later and kill a registration that was recreated
    // in the meantime. That only works if the signal survives the whole
    // chain: unregisterDevice -> apiRequest -> fetch. The re-verify found
    // that deleting `signal: options?.signal` from devices.ts left all 25
    // device tests green.
    //
    // This test covers the apiRequest -> fetch half ONLY. It does NOT
    // catch that devices.ts mutation, verified by running it against the
    // mutation and watching it pass. The half that does catch it lives in
    // src/api/__tests__/devices.test.ts, and the two together pin the
    // whole chain. Worth stating out loud, because a test named after a
    // finding it does not actually cover is worse than no test at all.
    (globalThis.fetch as jest.Mock).mockResolvedValue(jsonResponse(200, { ok: true }));
    const controller = new AbortController();

    await apiRequest("/v1/devices/device-1", { method: "DELETE", signal: controller.signal });

    const init = (globalThis.fetch as jest.Mock).mock.calls[0][1] as RequestInit;
    expect(init.signal).toBe(controller.signal);
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

  it("signs out (scope: local) on a 401 when the local session is genuinely absent", async () => {
    mockGetSession.mockResolvedValue({ data: { session: null } });
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(401, {
        error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
      }),
    );

    await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
    expect(mockSignOut).toHaveBeenCalledTimes(1);
    expect(mockSignOut).toHaveBeenCalledWith({ scope: "local" });
  });

  it("signs out on a 401 when the local session is past its own expires_at", async () => {
    const pastSeconds = Math.floor(Date.now() / 1000) - 60;
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "stale-token", expires_at: pastSeconds } },
    });
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(401, {
        error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
      }),
    );

    await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
    expect(mockSignOut).toHaveBeenCalledWith({ scope: "local" });
  });

  it("B3 (#263): does NOT sign out on a 401 when the local session still looks live — a transient server-side hiccup is not proof the session is dead", async () => {
    const futureSeconds = Math.floor(Date.now() / 1000) + 3600;
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "still-live-token", expires_at: futureSeconds } },
    });
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(401, {
        error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
      }),
    );

    // The caller still sees the failure (so it can retry / surface it) —
    // only the destructive sign-out side effect is suppressed.
    await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
    expect(mockSignOut).not.toHaveBeenCalled();
  });

  it("B3 (#263): a live session with no expires_at at all is treated as live, not dead", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "no-expiry-token" } },
    });
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(401, {
        error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
      }),
    );

    await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
    expect(mockSignOut).not.toHaveBeenCalled();
  });

  describe("B3-1 (safety review): the liveness clock anchors to the server's Date header, not the device clock", () => {
    afterEach(() => {
      jest.useRealTimers();
    });

    it("does NOT sign out when a fast device clock alone would read the session as expired", async () => {
      // Server time is "now"; the token is still live for another hour by
      // the server's own clock.
      const serverNow = new Date();
      const expiresAtSeconds = Math.floor(serverNow.getTime() / 1000) + 3600;
      mockGetSession.mockResolvedValue({
        data: { session: { access_token: "still-live-token", expires_at: expiresAtSeconds } },
      });
      // The device clock is 2 hours fast (no auto-time, a traveler, a dead
      // RTC) -- past the token's real expiry if it were ever consulted.
      jest.useFakeTimers({ doNotFake: ["nextTick", "queueMicrotask"] });
      jest.setSystemTime(new Date(serverNow.getTime() + 2 * 60 * 60 * 1000));
      (globalThis.fetch as jest.Mock).mockResolvedValue(
        jsonResponse(
          401,
          { error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" } },
          serverNow.toUTCString(),
        ),
      );

      await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
      expect(mockSignOut).not.toHaveBeenCalled();
    });

    it("falls back to the device clock when the response carries no Date header", async () => {
      const pastSeconds = Math.floor(Date.now() / 1000) - 60;
      mockGetSession.mockResolvedValue({
        data: { session: { access_token: "stale-token", expires_at: pastSeconds } },
      });
      (globalThis.fetch as jest.Mock).mockResolvedValue(
        jsonResponse(
          401,
          { error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" } },
          null,
        ),
      );

      await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
      expect(mockSignOut).toHaveBeenCalledWith({ scope: "local" });
    });

    it("falls back to the device clock when the Date header is unparsable", async () => {
      const pastSeconds = Math.floor(Date.now() / 1000) - 60;
      mockGetSession.mockResolvedValue({
        data: { session: { access_token: "stale-token", expires_at: pastSeconds } },
      });
      (globalThis.fetch as jest.Mock).mockResolvedValue(
        jsonResponse(
          401,
          { error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" } },
          "not a real date",
        ),
      );

      await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
      expect(mockSignOut).toHaveBeenCalledWith({ scope: "local" });
    });
  });

  describe("B3-3 (#284): the liveness check's own getSession() is raced against ~2s, not left to auth-js's 30s retry backoff", () => {
    afterEach(() => {
      jest.useRealTimers();
    });

    it("does NOT sign out when getSession() itself never resolves, and surfaces at the ~2s race window rather than after 30s", async () => {
      jest.useFakeTimers({ doNotFake: ["nextTick", "queueMicrotask"] });
      mockGetSession
        // authHeader()'s own read, on the way OUT (unrelated to this
        // finding) - resolves normally.
        .mockResolvedValueOnce({ data: { session: null } })
        // The 401 liveness check's getSession() - stands in for auth-js's
        // `_refreshAccessToken` hanging on a flaky network. Never settles.
        .mockReturnValueOnce(new Promise(() => {}));
      (globalThis.fetch as jest.Mock).mockResolvedValue(
        jsonResponse(401, {
          error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
        }),
      );

      const pending = apiRequest("/v1/me").catch((e: unknown) => e);
      // Advancing by exactly the race window (not 30s) is the proof: if the
      // implementation still awaited the real getSession() unraced, this
      // promise would still be pending after this and the `await` below
      // would hang past Jest's own test timeout.
      await jest.advanceTimersByTimeAsync(2000);
      const error = await pending;

      expect(error).toBeInstanceOf(ApiError);
      expect(mockSignOut).not.toHaveBeenCalled();
    });

    it("still signs out normally when getSession() resolves well within the race window", async () => {
      mockGetSession.mockResolvedValue({ data: { session: null } });
      (globalThis.fetch as jest.Mock).mockResolvedValue(
        jsonResponse(401, {
          error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
        }),
      );

      await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
      expect(mockSignOut).toHaveBeenCalledWith({ scope: "local" });
    });
  });

  it("B3-2 (safety review): a getSession() that REJECTS (unreadable keychain) still surfaces a typed ApiError, and never signs out", async () => {
    // First call is authHeader()'s own (pre-existing, unrelated to B3) read
    // to build the Authorization header -- that one succeeds normally.
    // The SECOND call is the 401 liveness check this finding is about; only
    // that one rejects, standing in for a keychain read/decrypt failure.
    mockGetSession
      .mockResolvedValueOnce({ data: { session: null } })
      .mockRejectedValueOnce(new Error("keychain read failed"));
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(401, {
        error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
      }),
    );

    const error = await apiRequest("/v1/me").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("unauthorized");
    // Fails toward "not dead": an unreadable keychain is not evidence the
    // session is gone.
    expect(mockSignOut).not.toHaveBeenCalled();
  });

  it("R3 (safety review): a getSession() that rejects on the REQUEST path still produces a typed ApiError, not a raw Error", async () => {
    // R3 is B3-2 one function up. authHeader() reads the keychain on EVERY
    // request, not only on 401s, and an unwrapped rejection there escaped
    // before fetch was ever attempted: callers got a raw Error, breaking
    // this module's contract that everything it throws is an ApiError.
    // Every getSession() call rejects here, including authHeader's own.
    mockGetSession.mockRejectedValue(new Error("keychain read failed"));
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(401, {
        error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
      }),
    );

    const error = await apiRequest("/v1/me").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("unauthorized");
    // The request went out anonymously rather than not going out at all.
    expect(globalThis.fetch as jest.Mock).toHaveBeenCalled();
    const headers = ((globalThis.fetch as jest.Mock).mock.calls[0][1] as RequestInit)
      .headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    // And the unreadable keychain is still not treated as a dead session.
    expect(mockSignOut).not.toHaveBeenCalled();
  });

  it("B3-4 (safety review): a signOut() that itself rejects never surfaces as an unhandled rejection", async () => {
    mockGetSession.mockResolvedValue({ data: { session: null } });
    mockSignOut.mockRejectedValue(new Error("storage write failed"));
    (globalThis.fetch as jest.Mock).mockResolvedValue(
      jsonResponse(401, {
        error: { code: "unauthorized", message: "Token expired.", request_id: "req_1" },
      }),
    );

    // The call still resolves to the normal ApiError rejection -- a
    // rejecting signOut() must not become the request's own error, and
    // must not go unhandled (jest fails the run on an unhandled rejection
    // it observes during the test).
    await expect(apiRequest("/v1/me")).rejects.toBeInstanceOf(ApiError);
    expect(mockSignOut).toHaveBeenCalledWith({ scope: "local" });
  });

  it("H3-2 (safety review): a connection dropped mid-body maps to network_error, never a raw exception", async () => {
    (globalThis.fetch as jest.Mock).mockResolvedValue(textRejectsResponse(200));

    const error = await apiRequest("/v1/queue").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("network_error");
  });

  it("maps a dropped connection to a house-voice network_error, never the raw fetch failure", async () => {
    (globalThis.fetch as jest.Mock).mockRejectedValue(new TypeError("Network request failed"));

    const error = await apiRequest("/v1/queue").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("network_error");
    expect((error as ApiError).message).not.toMatch(/TypeError/);
  });

  describe("H3 (#263): a 2xx with an empty/unparsable body is a failure, never 'success with null data'", () => {
    it("resolves undefined (never throws) on a genuine 204", async () => {
      (globalThis.fetch as jest.Mock).mockResolvedValue(rawResponse(204, ""));

      await expect(
        apiRequest("/v1/drafts/draft-1/reject", { method: "DELETE" }),
      ).resolves.toBeUndefined();
    });

    it("throws unknown_error on a 200 with a totally empty body", async () => {
      (globalThis.fetch as jest.Mock).mockResolvedValue(rawResponse(200, ""));

      const error = await apiRequest("/v1/queue").catch((e: unknown) => e);

      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).code).toBe("unknown_error");
    });

    it("throws unknown_error on a 200 with unparsable (non-JSON) text", async () => {
      (globalThis.fetch as jest.Mock).mockResolvedValue(rawResponse(200, "not json{"));

      const error = await apiRequest("/v1/queue").catch((e: unknown) => e);

      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).code).toBe("unknown_error");
    });

    it("throws unknown_error on a 200 whose body is the JSON literal null", async () => {
      (globalThis.fetch as jest.Mock).mockResolvedValue(rawResponse(200, "null"));

      const error = await apiRequest("/v1/queue").catch((e: unknown) => e);

      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).code).toBe("unknown_error");
    });

    it("throws unknown_error on a 200 whose body is a bare JSON primitive (not an object)", async () => {
      (globalThis.fetch as jest.Mock).mockResolvedValue(rawResponse(200, "42"));

      const error = await apiRequest("/v1/queue").catch((e: unknown) => e);

      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).code).toBe("unknown_error");
    });

    it("still resolves normally on a well-formed 2xx object body", async () => {
      (globalThis.fetch as jest.Mock).mockResolvedValue(jsonResponse(200, { items: [] }));

      await expect(apiRequest("/v1/queue")).resolves.toEqual({ items: [] });
    });
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

  it("H3 (#263): an empty 2xx body throws unknown_error rather than resolving `data.undo_until` as null", async () => {
    (globalThis.fetch as jest.Mock).mockResolvedValue(rawResponse(200, ""));

    const error = await apiRequestWithDate("/v1/drafts/draft-1/approve", { method: "POST" }).catch(
      (e: unknown) => e,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("unknown_error");
  });
});
