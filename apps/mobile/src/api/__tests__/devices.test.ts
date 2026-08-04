/**
 * Contract-shape tests for the Devices client (api-contracts.md "Devices"
 * section, v1.18 amendment) — `apiRequest` is mocked (zero network, hard
 * rule; the fetch/envelope layer is covered by client.test.ts). Each
 * assertion pins the exact path/method/body the amendment documents, so a
 * drive-by rename of a URL or the `token`/`platform` body keys (which are
 * `push_tokens`' own column names verbatim — schema-v1.md) fails loudly.
 */
import { apiRequest } from "@/api/client";
import { registerDevice, unregisterDevice } from "@/api/devices";

jest.mock("@/api/client", () => ({
  apiRequest: jest.fn(),
}));

const mockApiRequest = apiRequest as jest.Mock;

beforeEach(() => {
  mockApiRequest.mockReset();
  mockApiRequest.mockResolvedValue({});
});

describe("registerDevice (POST /v1/devices, v1.18)", () => {
  it("POSTs exactly { token, platform } — the push_tokens column names, no expo_push_token field", async () => {
    await registerDevice({ token: "ExponentPushToken[abc123]", platform: "ios" });
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/devices", {
      method: "POST",
      body: { token: "ExponentPushToken[abc123]", platform: "ios" },
    });
  });

  it("carries the android platform verbatim", async () => {
    await registerDevice({ token: "ExponentPushToken[xyz]", platform: "android" });
    const [, options] = mockApiRequest.mock.calls[0];
    expect(options.body).toEqual({ token: "ExponentPushToken[xyz]", platform: "android" });
  });

  it("returns the server's { id, platform, created_at } upsert response unchanged", async () => {
    const response = { id: "dev-1", platform: "ios", created_at: "2026-07-21T12:00:00Z" };
    mockApiRequest.mockResolvedValue(response);
    await expect(
      registerDevice({ token: "ExponentPushToken[t]", platform: "ios" }),
    ).resolves.toEqual(response);
  });
});

describe("unregisterDevice (DELETE /v1/devices/{id}, v1.18)", () => {
  it("DELETEs by the row's own id — never the raw token in the path (credential-adjacent)", async () => {
    await unregisterDevice("dev-1");
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/devices/dev-1", { method: "DELETE" });
  });

  it("F7 (re-verify): forwards the caller's AbortSignal, so a timeout can actually abandon the request", () => {
    // #284's reconcile aborts its DELETE on a timeout so an abandoned
    // request cannot land later and delete a registration that was
    // recreated in the meantime (the backend upsert is ON CONFLICT
    // (token) DO UPDATE, which PRESERVES the row id, so a late DELETE
    // matches the new row). That only works if the signal survives
    // unregisterDevice, and nothing pinned it: deleting
    // `signal: options?.signal` left all 25 device tests green.
    //
    // The test above cannot catch it. `unregisterDevice("dev-1")` passes
    // `signal: undefined`, and `toHaveBeenCalledWith` uses recursive
    // equality that ignores undefined properties, so `{method}` and
    // `{method, signal: undefined}` compare equal either way. Passing a
    // REAL signal is what makes the assertion bite.
    const controller = new AbortController();
    void unregisterDevice("dev-1", { signal: controller.signal });
    expect(mockApiRequest).toHaveBeenCalledWith("/v1/devices/dev-1", {
      method: "DELETE",
      signal: controller.signal,
    });
  });
});
