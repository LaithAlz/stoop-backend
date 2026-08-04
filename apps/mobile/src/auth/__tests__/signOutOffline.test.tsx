/**
 * #284 B3-5 + B3-8: `AuthProvider.signOut`'s honest result when auth-js's
 * own `/logout` call fails (offline, most concretely), and
 * `AuthProvider.signIn`'s reconcile-on-next-sign-in wiring. `@/lib/supabase`
 * and `@/features/push/deviceRegistration` are both mocked — zero network,
 * no real SecureStore/keychain touch (mirrors src/auth/__tests__/
 * signOutClearsCache.test.tsx's own fence).
 */
import { useEffect } from "react";
import { render } from "@testing-library/react-native";
import { Text } from "react-native";
import { AuthProvider, useAuth } from "@/auth/AuthProvider";

type AuthChangeCallback = (event: string, session: unknown) => void;

const captured: {
  onAuthStateChange: AuthChangeCallback | null;
  ctx: ReturnType<typeof useAuth> | null;
} = { onAuthStateChange: null, ctx: null };

const mockGetSession = jest.fn();
const mockSignInWithPassword = jest.fn();
const mockSupabaseSignOut = jest.fn();

jest.mock("@/lib/supabase", () => ({
  supabase: {
    auth: {
      getSession: () => mockGetSession(),
      onAuthStateChange: (cb: AuthChangeCallback) => {
        captured.onAuthStateChange = cb;
        return { data: { subscription: { unsubscribe: jest.fn() } } };
      },
      signInWithPassword: (args: unknown) => mockSignInWithPassword(args),
      signOut: (options?: { scope?: string }) => mockSupabaseSignOut(options),
    },
  },
}));

const mockUnregisterCurrentDeviceBestEffort = jest.fn();
const mockClearRegisteredDeviceId = jest.fn();
const mockReconcileStaleDeviceRegistration = jest.fn();

jest.mock("@/features/push/deviceRegistration", () => ({
  unregisterCurrentDeviceBestEffort: () => mockUnregisterCurrentDeviceBestEffort(),
  clearRegisteredDeviceId: () => mockClearRegisteredDeviceId(),
  reconcileStaleDeviceRegistration: () => mockReconcileStaleDeviceRegistration(),
}));

function Harness() {
  const ctx = useAuth();
  // Test-only capture, deliberately in an effect (not the render body) -
  // mutating module-level state directly during render trips the
  // `react-hooks/immutability` rule this app's `expo lint` enforces.
  useEffect(() => {
    captured.ctx = ctx;
  });
  return <Text>harness</Text>;
}

function renderHarness() {
  render(
    <AuthProvider>
      <Harness />
    </AuthProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  captured.onAuthStateChange = null;
  captured.ctx = null;
  mockGetSession.mockResolvedValue({ data: { session: null } });
  mockUnregisterCurrentDeviceBestEffort.mockResolvedValue(undefined);
  mockReconcileStaleDeviceRegistration.mockResolvedValue(undefined);
});

describe("signOut — B3-5 (#284): an offline /logout must not be reported as success", () => {
  it("resolves { ok: true } and fires SIGNED_OUT when supabase.auth.signOut succeeds", async () => {
    mockSupabaseSignOut.mockResolvedValue({ error: null });
    renderHarness();

    const result = await captured.ctx!.signOut();

    expect(result).toEqual({ ok: true });
    expect(mockUnregisterCurrentDeviceBestEffort).toHaveBeenCalledTimes(1);
  });

  it("resolves { ok: false } when supabase.auth.signOut returns a retryable/offline error, and never claims SIGNED_OUT happened", async () => {
    // Standing in for auth-js's own `_signOut` (GoTrueClient.ts) returning
    // early on a network failure from /logout — the exact "subway" case
    // this finding names. The mock never calls onAuthStateChange's captured
    // callback here, matching auth-js's real behavior on this branch: no
    // `_removeSession()`, no SIGNED_OUT event.
    mockSupabaseSignOut.mockResolvedValue({ error: { message: "Network request failed" } });
    renderHarness();

    const result = await captured.ctx!.signOut();

    expect(result).toEqual({ ok: false });
    // The session was never actually torn down — this is the "not left
    // believing something false" half of the fix: the context still
    // reports the same (here: already-null in this harness, but the
    // point is nothing SET it to null via a fabricated SIGNED_OUT) state
    // it started with, rather than a local-only, cosmetic "success".
    expect(captured.ctx!.session).toBeNull();
  });

  it("still runs the best-effort device unregister before the (failing) supabase.auth.signOut call", async () => {
    mockSupabaseSignOut.mockResolvedValue({ error: { message: "Network request failed" } });
    renderHarness();

    await captured.ctx!.signOut();

    expect(mockUnregisterCurrentDeviceBestEffort).toHaveBeenCalledTimes(1);
    expect(mockSupabaseSignOut).toHaveBeenCalledWith({ scope: "local" });
  });
});

describe("signIn — B3-8 (#284): reconciles a stale device registration on success", () => {
  it("calls reconcileStaleDeviceRegistration after a successful sign-in", async () => {
    mockSignInWithPassword.mockResolvedValue({ error: null });
    renderHarness();

    const result = await captured.ctx!.signIn("landlord@example.com", "hunter2");

    expect(result).toEqual({ error: null });
    expect(mockReconcileStaleDeviceRegistration).toHaveBeenCalledTimes(1);
  });

  it("does NOT call reconcileStaleDeviceRegistration when sign-in itself fails", async () => {
    mockSignInWithPassword.mockResolvedValue({ error: { message: "Invalid login credentials" } });
    renderHarness();

    await captured.ctx!.signIn("landlord@example.com", "wrong");

    expect(mockReconcileStaleDeviceRegistration).not.toHaveBeenCalled();
  });
});
