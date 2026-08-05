/**
 * #284 B3-5 + B3-8: `AuthProvider.signOut`'s honest result when auth-js's
 * own `/logout` call fails (offline, most concretely), and
 * `AuthProvider.signIn`'s reconcile-on-next-sign-in wiring. `@/lib/supabase`
 * and `@/features/push/deviceRegistration` are both mocked, zero network,
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

describe("signOut, B3-5 (#284): an offline /logout must not be reported as success", () => {
  it("resolves { ok: true } and fires SIGNED_OUT when supabase.auth.signOut succeeds", async () => {
    mockSupabaseSignOut.mockResolvedValue({ error: null });
    renderHarness();

    const result = await captured.ctx!.signOut();

    expect(result).toEqual({ ok: true });
    expect(mockUnregisterCurrentDeviceBestEffort).toHaveBeenCalledTimes(1);
  });

  it("resolves { ok: false } when supabase.auth.signOut returns a retryable/offline error, and never claims SIGNED_OUT happened", async () => {
    // Standing in for auth-js's own `_signOut` (GoTrueClient.ts) returning
    // early on a network failure from /logout, the exact "subway" case
    // this finding names. The mock never calls onAuthStateChange's captured
    // callback here, matching auth-js's real behavior on this branch: no
    // `_removeSession()`, no SIGNED_OUT event.
    mockSupabaseSignOut.mockResolvedValue({ error: { message: "Network request failed" } });
    renderHarness();

    const result = await captured.ctx!.signOut();

    expect(result).toEqual({ ok: false });
    // The session was never actually torn down, this is the "not left
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

describe("signOut, FIX 1 (#284 adversarial review): a signOut() that itself rejects must not escape", () => {
  it("resolves { ok: false }, never rejects, when signOut() REJECTS and the session is STILL LIVE", async () => {
    // Mirrors src/api/client.test.ts's B3-4 test for the same underlying
    // auth-js gap (an unguarded SecureStore read/write inside `signOut()`
    // rejecting rather than resolving `{ error }`), on this file's explicit
    // sign-out path instead of the 401 liveness gate's fire-and-forget one.
    mockSupabaseSignOut.mockRejectedValue(new Error("keychain read failed"));
    mockGetSession.mockResolvedValue({ data: { session: { access_token: "still-live" } } });
    renderHarness();

    const result = await captured.ctx!.signOut();

    expect(result).toEqual({ ok: false });
    // The best-effort device unregister still ran first, same as every
    // other signOut() outcome.
    expect(mockUnregisterCurrentDeviceBestEffort).toHaveBeenCalledTimes(1);
  });

  it("F1 (re-verify): resolves { ok: true } when signOut() rejects AFTER the session was already torn down", async () => {
    // The reject is not the answer, the session is. auth-js awaits
    // `_removeSession()` and THEN a second unguarded keychain write, and
    // `_notifyAllSubscribers` rethrows a subscriber's throw after the
    // event has already been delivered. So a sign-out that genuinely
    // worked can still reject on the way out.
    //
    // Before this, that landlord was navigated to the sign-in wall AND
    // told to check their connection. Reporting a failure for a sign-out
    // that succeeded trains people to ignore the message, which matters
    // because the true case (still signed in on a borrowed device) is
    // the one that has to land.
    mockSupabaseSignOut.mockRejectedValue(new Error("code-verifier write failed"));
    mockGetSession.mockResolvedValue({ data: { session: null } });
    renderHarness();

    const result = await captured.ctx!.signOut();

    expect(result).toEqual({ ok: true });
  });

  it("F1 (re-verify, HIGH): resolves { ok: false } when getSession() returns a NULL session WITH an error", async () => {
    // The state no test covered, and the one the first version of this
    // fix got wrong in the catastrophic direction.
    //
    // `data.session === null` is not "there is no session on this
    // device". Past EXPIRY_MARGIN_MS, `__loadSession` calls
    // `_callRefreshToken`, and on a RETRYABLE fetch error (offline)
    // auth-js deliberately does NOT call `_removeSession()`. The refresh
    // token stays in SecureStore and this resolves `{ session: null,
    // error }` on a device that is still fully signed in.
    //
    // The landlord: phone backgrounded two hours so the access token is
    // expired, basement unit with no data, hands the phone to their
    // super. Reading this as "signed out" gave them no alert, no
    // SIGNED_OUT, an intact tenant cache, and full access restored the
    // moment the phone found signal.
    mockSupabaseSignOut.mockRejectedValue(new Error("keychain read failed"));
    renderHarness();
    mockGetSession.mockResolvedValueOnce({
      data: { session: null },
      error: { name: "AuthRetryableFetchError", message: "network" },
    });

    await expect(captured.ctx!.signOut()).resolves.toEqual({ ok: false });
  });

  it("F1 (re-verify): resolves { ok: false } when the reject-path getSession() ALSO throws", async () => {
    // An unreadable keychain leaves us no better informed than the throw
    // did, so report the failure. That is the honest answer and the safe
    // direction: it over-warns, it never claims a sign-out worked when
    // nothing is known.
    mockSupabaseSignOut.mockRejectedValue(new Error("keychain read failed"));
    renderHarness();
    // Rejected only AFTER mount, so this pins the catch path's own read
    // rather than the provider's startup read.
    //
    // To be accurate about why that ordering is needed: the startup read
    // does NOT have its own handling. `AuthProvider`'s mount `.then` has
    // no `.catch`, and auth-js does not cover for it either, so a
    // keychain throw at launch means no INITIAL_SESSION, an unhandled
    // rejection, `initializing` stuck true, and a splash screen that
    // never hides. Pre-existing since M0 and out of this PR's diff,
    // tracked separately. An earlier version of this comment said the
    // opposite, which would have steered the next reader away from it.
    mockGetSession.mockRejectedValueOnce(new Error("keychain unreadable"));

    await expect(captured.ctx!.signOut()).resolves.toEqual({ ok: false });
  });
});

describe("signIn, B3-8 (#284): reconciles a stale device registration on success", () => {
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
