/**
 * useAcknowledge — the emergency banner's ack mutation (api-contracts.md's
 * Queue section v1.15 amendment). Zero network: src/api/notifications.ts
 * is mocked. Covers the safety-relevant failure direction called out in
 * the hook's own docstring: success only ever invalidates the queue query
 * (src/api/queue.ts's `queueQueryKey`) — it never locally fakes removal.
 */
import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react-native";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/api/errors";
import { ackNotification } from "@/api/notifications";
import { useAcknowledge } from "../useAcknowledge";

jest.mock("@/api/notifications", () => ({
  ackNotification: jest.fn(),
}));

// useAcknowledge imports @/api/queue → @/api/client → @/lib/supabase, whose
// module top-level constructs a real client from env (throws without a
// .env). Never construct a real client in tests (same fence as
// src/features/push/__tests__/usePushRegistration.test.tsx) — no network
// call actually reaches this module in these tests anyway.
jest.mock("@/lib/supabase", () => ({ supabase: { auth: {} } }));

const mockAck = ackNotification as jest.Mock;

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: 0 } },
  });
  const invalidateSpy = jest.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { wrapper, invalidateSpy };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe("useAcknowledge — calls the right id", () => {
  it("mutation calls ackNotification with exactly the notification id passed in", async () => {
    mockAck.mockResolvedValue({ acknowledged_at: "2026-07-18T12:00:00Z" });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAcknowledge({ onNotice: jest.fn() }), { wrapper });

    act(() => result.current.acknowledge("notif-1"));

    await waitFor(() => expect(mockAck).toHaveBeenCalledWith("notif-1"));
    expect(mockAck).toHaveBeenCalledTimes(1);
  });
});

describe("useAcknowledge — success invalidates the queue query, never fakes removal", () => {
  it("invalidates queueQueryKey on a successful ack", async () => {
    mockAck.mockResolvedValue({ acknowledged_at: "2026-07-18T12:00:00Z" });
    const { wrapper, invalidateSpy } = makeWrapper();
    const { result } = renderHook(() => useAcknowledge({ onNotice: jest.fn() }), { wrapper });

    act(() => result.current.acknowledge("notif-1"));

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["queue"] }));
  });

  it("a failed ack does NOT invalidate the queue — the card must not silently disappear", async () => {
    mockAck.mockRejectedValue(
      new ApiError(500, { code: "internal_error", message: "raw", request_id: "req_1" }),
    );
    const { wrapper, invalidateSpy } = makeWrapper();
    const onNotice = jest.fn();
    const { result } = renderHook(() => useAcknowledge({ onNotice }), { wrapper });

    act(() => result.current.acknowledge("notif-1"));

    await waitFor(() => expect(onNotice).toHaveBeenCalled());
    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});

describe("useAcknowledge — errors surface the house line, never the raw server message", () => {
  it("maps an ApiError to the generic house fallback (no bespoke ack error code exists)", async () => {
    mockAck.mockRejectedValue(
      new ApiError(500, {
        code: "internal_error",
        message: "raw server text",
        request_id: "req_1",
      }),
    );
    const { wrapper } = makeWrapper();
    const onNotice = jest.fn();
    const { result } = renderHook(() => useAcknowledge({ onNotice }), { wrapper });

    act(() => result.current.acknowledge("notif-1"));

    await waitFor(() =>
      expect(onNotice).toHaveBeenCalledWith("Something didn't go through. Try again in a moment."),
    );
  });
});

describe("useAcknowledge — isAcknowledging is scoped per notification id", () => {
  it("is false for an id that was never acknowledged, even mid-flight for another one", async () => {
    let resolveAck: (value: { acknowledged_at: string }) => void = () => {};
    mockAck.mockReturnValue(
      new Promise((resolve) => {
        resolveAck = resolve;
      }),
    );
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAcknowledge({ onNotice: jest.fn() }), { wrapper });

    act(() => result.current.acknowledge("notif-1"));

    await waitFor(() => expect(result.current.isAcknowledging("notif-1")).toBe(true));
    expect(result.current.isAcknowledging("notif-2")).toBe(false);

    act(() => resolveAck({ acknowledged_at: "2026-07-18T12:00:00Z" }));
    await waitFor(() => expect(result.current.isAcknowledging("notif-1")).toBe(false));
  });
});
