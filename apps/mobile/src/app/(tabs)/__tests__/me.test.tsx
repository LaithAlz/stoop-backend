/**
 * #284 B3-5: the Me tab's "Sign out" button used to fire `signOut()` and
 * forget the result (src/auth/AuthProvider.tsx). This exercises the wiring
 * end to end from a tap: a failed sign-out (per AuthProvider's own
 * `{ ok: false }` contract, see signOutOffline.test.tsx for that contract
 * itself) surfaces a house-voice alert instead of leaving the landlord
 * believing they signed out; a successful one stays silent, matching every
 * other action on this screen.
 *
 * `@/auth/AuthProvider`, `@/api/me`, `@/api/properties`, `@/api/trust`, and
 * `@/features/push/usePushPermission` are all mocked, real, unmocked
 * versions of the `@/api/*` modules construct the actual Supabase client at
 * import time (src/lib/supabase.ts) and throw without a configured .env,
 * same reason src/features/tenants/__tests__/TenantFormModal.test.tsx mocks
 * `@/api/tenants` wholesale rather than letting it load for real.
 */
import type { ReactNode } from "react";
import { Alert } from "react-native";
import { fireEvent, render, waitFor } from "@testing-library/react-native";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import MeScreen from "../me";
import { useAuth } from "@/auth/AuthProvider";
import { useMe } from "@/api/me";
import { useFirstPropertyPage } from "@/api/properties";
import { usePushPermission } from "@/features/push/usePushPermission";

jest.mock("@/auth/AuthProvider", () => ({
  useAuth: jest.fn(),
}));

jest.mock("@/api/me", () => ({
  meQueryKey: ["me"],
  useMe: jest.fn(),
  updateMe: jest.fn(),
}));

jest.mock("@/api/properties", () => ({
  useFirstPropertyPage: jest.fn(),
}));

jest.mock("@/api/trust", () => ({
  revokeTrust: jest.fn(),
}));

jest.mock("@/features/push/usePushPermission", () => ({
  usePushPermission: jest.fn(),
}));

const mockUseAuth = useAuth as jest.Mock;
const mockUseMe = useMe as jest.Mock;
const mockUseFirstPropertyPage = useFirstPropertyPage as jest.Mock;
const mockUsePushPermission = usePushPermission as jest.Mock;

let mockSignOut: jest.Mock;

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockSignOut = jest.fn();
  mockUseAuth.mockReturnValue({
    session: { user: { email: "landlord@example.com" } },
    signOut: mockSignOut,
  });
  mockUseMe.mockReturnValue({ isSuccess: false, isError: false, data: undefined });
  // No properties → the "Automatic sending" revoke card doesn't render,
  // keeping this test scoped to the sign-out wiring only.
  mockUseFirstPropertyPage.mockReturnValue({ data: { items: [] } });
  mockUsePushPermission.mockReturnValue({
    loading: false,
    state: { status: "granted", canAskAgain: true },
    requesting: false,
    requestPermission: jest.fn(),
  });
  jest.spyOn(Alert, "alert").mockImplementation(() => undefined);
});

describe("Me tab, Sign out feedback (#284 B3-5)", () => {
  it("shows no alert on a successful sign-out", async () => {
    mockSignOut.mockResolvedValue({ ok: true });
    const { getByTestId } = render(<MeScreen />, { wrapper });

    fireEvent.press(getByTestId("sign-out"));

    await waitFor(() => expect(mockSignOut).toHaveBeenCalledTimes(1));
    expect(Alert.alert).not.toHaveBeenCalled();
  }, 30000);

  it("tells the landlord sign-out didn't go through when it fails (e.g. offline), rather than leaving them silently still signed in", async () => {
    mockSignOut.mockResolvedValue({ ok: false });
    const { getByTestId } = render(<MeScreen />, { wrapper });

    fireEvent.press(getByTestId("sign-out"));

    await waitFor(() =>
      expect(Alert.alert).toHaveBeenCalledWith(
        "Stoop",
        "Couldn't reach Stoop. Check your connection and try again.",
      ),
    );
  }, 30000);

  it("FIX 1 (#284 adversarial review): still tells the landlord sign-out didn't go through when signOut() itself REJECTS, rather than letting it escape unhandled through onPress", async () => {
    mockSignOut.mockRejectedValue(new Error("keychain read failed"));
    const { getByTestId } = render(<MeScreen />, { wrapper });

    fireEvent.press(getByTestId("sign-out"));

    await waitFor(() =>
      expect(Alert.alert).toHaveBeenCalledWith(
        "Stoop",
        "Couldn't reach Stoop. Check your connection and try again.",
      ),
    );
  }, 30000);
});
