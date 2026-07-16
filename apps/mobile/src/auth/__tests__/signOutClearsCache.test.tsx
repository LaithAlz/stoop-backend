/**
 * PII fence regression test (M1 senior review, BLOCKING finding): the
 * shared React Query cache carries tenant messages and account data, so a
 * SIGNED_OUT auth transition must empty it immediately — otherwise a
 * different landlord signing in on the same device inside the cache's gc
 * window would be served the previous account's data by
 * stale-while-revalidate before any refetch lands.
 */
import { render } from "@testing-library/react-native";
import { Text } from "react-native";

import { queryClient } from "@/api/queryClient";
import { AuthProvider } from "@/auth/AuthProvider";

type AuthChangeCallback = (event: string, session: null) => void;

const captured: { callback: AuthChangeCallback | null } = { callback: null };

jest.mock("@/lib/supabase", () => ({
  supabase: {
    auth: {
      getSession: jest.fn(() => Promise.resolve({ data: { session: null } })),
      onAuthStateChange: jest.fn((cb: AuthChangeCallback) => {
        captured.callback = cb;
        return { data: { subscription: { unsubscribe: jest.fn() } } };
      }),
      signInWithPassword: jest.fn(() => Promise.resolve({ error: null })),
      signOut: jest.fn(() => Promise.resolve({ error: null })),
    },
  },
}));

describe("sign-out clears the shared query cache", () => {
  afterEach(() => {
    queryClient.clear();
    captured.callback = null;
  });

  it("empties every cached query on the SIGNED_OUT transition", () => {
    render(
      <AuthProvider>
        <Text>child</Text>
      </AuthProvider>,
    );
    expect(captured.callback).not.toBeNull();

    // Seed the cache the way a signed-in session would (tenant-PII-bearing
    // query keys used by the real screens).
    queryClient.setQueryData(["queue"], { items: [{ tenant_message: "sensitive" }] });
    queryClient.setQueryData(["me"], { email: "landlord@example.com" });
    expect(queryClient.getQueryCache().getAll()).toHaveLength(2);

    captured.callback?.("SIGNED_OUT", null);

    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  it("leaves the cache alone on non-sign-out transitions", () => {
    render(
      <AuthProvider>
        <Text>child</Text>
      </AuthProvider>,
    );
    queryClient.setQueryData(["queue"], { items: [] });

    captured.callback?.("TOKEN_REFRESHED", null);

    expect(queryClient.getQueryCache().getAll()).toHaveLength(1);
  });
});
