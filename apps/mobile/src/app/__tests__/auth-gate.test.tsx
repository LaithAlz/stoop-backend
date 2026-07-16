/**
 * Auth-gate routing tests — the M0 test harness (issue #210: "at least one
 * component test for the auth gate routing logic so the harness exists for
 * M1").
 *
 * Two layers:
 *  1. resolveAuthRoute — the pure decision (fast, no rendering).
 *  2. The real root layout rendered via expo-router's testing-library with
 *     a mocked src/lib/supabase — asserting signed-out users land on
 *     /sign-in and signed-in users land in the tab shell.
 *
 * NO real network calls anywhere (hard rule): the supabase module is
 * jest-mocked below, so no client is ever constructed and no URL/key is
 * ever read.
 */
import { renderRouter, screen } from "expo-router/testing-library";
import { resolveAuthRoute } from "@/auth/resolveAuthRoute";

// --- Mock the supabase module (never construct a real client in tests) ---
type AuthChangeCallback = (event: string, session: FakeSession | null) => void;

interface FakeSession {
  user: { email: string };
}

const mockAuthState: { session: FakeSession | null } = { session: null };

jest.mock("@/lib/supabase", () => ({
  supabase: {
    auth: {
      getSession: jest.fn(() => Promise.resolve({ data: { session: mockAuthState.session } })),
      onAuthStateChange: jest.fn((_cb: AuthChangeCallback) => ({
        data: { subscription: { unsubscribe: jest.fn() } },
      })),
      signInWithPassword: jest.fn(() => Promise.resolve({ error: null })),
      signOut: jest.fn(() => Promise.resolve({ error: null })),
    },
  },
}));

// expo-splash-screen touches native modules the test env doesn't have.
jest.mock("expo-splash-screen", () => ({
  preventAutoHideAsync: jest.fn(() => Promise.resolve(true)),
  hideAsync: jest.fn(() => Promise.resolve(true)),
}));

describe("resolveAuthRoute (pure gate decision)", () => {
  it("shows nothing while the stored session is still loading", () => {
    expect(resolveAuthRoute({ session: null, initializing: true })).toBe("loading");
    // Even a session that's already known stays behind the splash until init completes.
    expect(resolveAuthRoute({ session: { user: {} }, initializing: true })).toBe("loading");
  });

  it("routes signed-out users to sign-in", () => {
    expect(resolveAuthRoute({ session: null, initializing: false })).toBe("sign-in");
  });

  it("routes signed-in users to the tabs", () => {
    expect(resolveAuthRoute({ session: { user: {} }, initializing: false })).toBe("tabs");
  });
});

describe("root layout auth gate (rendered)", () => {
  beforeEach(() => {
    mockAuthState.session = null;
  });

  it("lands a signed-out user on the sign-in screen", async () => {
    renderRouter("src/app", { initialUrl: "/" });

    // The Stack.Protected guard should redirect / (tabs) -> /sign-in.
    expect(await screen.findByTestId("sign-in-submit")).toBeOnTheScreen();
    expect(screen.getByText("Welcome back.")).toBeOnTheScreen();
  });

  it("lands a signed-in user in the tab shell", async () => {
    mockAuthState.session = { user: { email: "landlord@example.com" } };

    renderRouter("src/app", { initialUrl: "/" });

    // The tab bar's own labels only render inside (tabs) — unlike Home's
    // content (M1: a real, data-dependent queue fetch), these are static
    // regardless of network/query state, so they're a stable "we're inside
    // (tabs), not sign-in" signal for this test.
    expect(await screen.findByText("Conversations")).toBeOnTheScreen();
    expect(screen.queryByTestId("sign-in-submit")).toBeNull();
  });
});
