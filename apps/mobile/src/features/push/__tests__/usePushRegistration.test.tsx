/**
 * usePushRegistration wiring tests (issue #210 M3): a tapped push
 * deep-links to its case, an arriving push refetches the queue, and the
 * app never navigates when there's no notification response. expo-
 * notifications, expo-router, and the native registration call are all
 * mocked — zero network, no native module.
 */
import type { ReactNode } from "react";
import { renderHook } from "@testing-library/react-native";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { usePushRegistration } from "../usePushRegistration";

const mockPush = jest.fn();
const mockUseLastNotificationResponse = jest.fn();
const mockListeners: { received?: () => void } = {};

jest.mock("expo-notifications", () => ({
  setNotificationHandler: jest.fn(),
  addNotificationReceivedListener: jest.fn((cb: () => void) => {
    mockListeners.received = cb;
    return { remove: jest.fn() };
  }),
  addPushTokenListener: jest.fn(() => ({ remove: jest.fn() })),
  useLastNotificationResponse: () => mockUseLastNotificationResponse(),
  AndroidImportance: { DEFAULT: 5 },
}));

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));

jest.mock("../deviceRegistration", () => ({
  registerForPushNotificationsAsync: jest.fn().mockResolvedValue(null),
}));

// usePushRegistration imports @/api/queue → @/api/client → @/lib/supabase,
// whose module top-level constructs a real client from env (throws without
// a .env). Never construct a real client in tests (same fence as
// src/app/__tests__/auth-gate.test.tsx) — the queue query is never actually
// fetched here anyway.
jest.mock("@/lib/supabase", () => ({ supabase: { auth: {} } }));

function response(data: Record<string, unknown>) {
  return {
    notification: { date: 0, request: { identifier: "n1", content: { data }, trigger: null } },
    actionIdentifier: "expo.modules.notifications.actions.DEFAULT",
  };
}

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = jest.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { wrapper, invalidateSpy };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockListeners.received = undefined;
  mockUseLastNotificationResponse.mockReturnValue(null);
});

describe("usePushRegistration — tap → deep-link", () => {
  it("routes a tapped push with data={case_id} to that case screen", () => {
    mockUseLastNotificationResponse.mockReturnValue(
      response({ case_id: "case-7", draft_id: "d-7" }),
    );
    const { wrapper } = makeWrapper();

    renderHook(() => usePushRegistration(), { wrapper });

    expect(mockPush).toHaveBeenCalledWith({
      pathname: "/conversations/[id]",
      params: { id: "case-7" },
    });
  });

  it("never navigates when there is no notification response", () => {
    mockUseLastNotificationResponse.mockReturnValue(null);
    const { wrapper } = makeWrapper();

    renderHook(() => usePushRegistration(), { wrapper });

    expect(mockPush).not.toHaveBeenCalled();
  });

  it("never navigates for a payload carrying no case_id (a no-op tap)", () => {
    mockUseLastNotificationResponse.mockReturnValue(response({ draft_id: "d-7" }));
    const { wrapper } = makeWrapper();

    renderHook(() => usePushRegistration(), { wrapper });

    expect(mockPush).not.toHaveBeenCalled();
  });
});

describe("usePushRegistration — a received push refreshes the queue", () => {
  it("invalidates the queue query when a notification arrives in-app", () => {
    const { wrapper, invalidateSpy } = makeWrapper();
    renderHook(() => usePushRegistration(), { wrapper });

    // Simulate an incoming push while the app is foregrounded.
    mockListeners.received?.();

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["queue"] });
  });
});
