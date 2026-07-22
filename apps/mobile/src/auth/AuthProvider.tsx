/**
 * Auth context for the whole app. Wraps @supabase/supabase-js's session
 * state in a small React context so screens never touch `supabase.auth`
 * directly — the sign-in screen calls `signIn`, the Me tab calls
 * `signOut`, and the root layout reads `session`/`initializing` to decide
 * which stack to show (see src/app/_layout.tsx and
 * src/auth/resolveAuthRoute.ts).
 *
 * Never log the session/JWT/user email (CLAUDE.md rule 5) — errors below
 * surface `error.message` from supabase-js (a human-readable auth failure
 * reason, e.g. "Invalid login credentials"), never the token or credentials
 * themselves.
 */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "@/lib/supabase";
import { queryClient } from "@/api/queryClient";
import { resetOnboardingOffer } from "@/features/onboarding/gate";
import {
  clearRegisteredDeviceId,
  unregisterCurrentDeviceBestEffort,
} from "@/features/push/deviceRegistration";

interface AuthContextValue {
  session: Session | null;
  /** True until the first `getSession()` resolves — the splash/loading
   *  window before we know whether to show sign-in or the tabs. */
  initializing: boolean;
  signIn: (email: string, password: string) => Promise<{ error: string | null }>;
  signOut: () => Promise<void>;
}

// Customer-facing copy rule (CLAUDE.md rule 8 / copy-guardian, M0 review):
// raw supabase-js error strings never reach the screen — every auth failure
// maps to the house voice. Unknown errors get one honest generic line.
function toHouseAuthError(error: { message: string }): string {
  const message = error.message.toLowerCase();
  if (message.includes("invalid login credentials")) {
    return "Email or password didn't match.";
  }
  if (message.includes("email not confirmed")) {
    return "This email hasn't been confirmed yet. Check your inbox for the confirmation link.";
  }
  if (message.includes("network") || message.includes("fetch")) {
    return "Couldn't reach Stoop. Check your connection and try again.";
  }
  return "Sign-in didn't go through. Try again.";
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [initializing, setInitializing] = useState(true);

  useEffect(() => {
    let mounted = true;

    supabase.auth.getSession().then(({ data }) => {
      if (!mounted) return;
      setSession(data.session);
      setInitializing(false);
    });

    const { data: subscription } = supabase.auth.onAuthStateChange((event, nextSession) => {
      if (!mounted) return;
      // PII fence (M1 senior review, BLOCKING): the shared React Query
      // cache holds tenant messages/names and the landlord's own account
      // data. On sign-out it must be emptied immediately — otherwise a
      // different landlord signing in on the same device within the
      // cache's gc window would be served the previous account's data by
      // stale-while-revalidate before the refetch lands.
      if (event === "SIGNED_OUT") {
        queryClient.clear();
        // The onboarding gate's once-per-session flag is per-LANDLORD in
        // spirit — a different account signing in on this device gets its
        // own zero-properties gate decision (M2).
        resetOnboardingOffer();
        // M3: drop the locally-tracked device id. This is a pure local
        // reset (no network call) — safe here even though the session is
        // already gone by the time this fires, unlike the actual
        // `DELETE /v1/devices/{id}` call, which needs a still-live token
        // and therefore runs earlier, in `signOut` below, before
        // `supabase.auth.signOut()` clears it. Covers the forced-401
        // sign-out path too (src/api/client.ts), which bypasses `signOut`
        // below entirely — that path can't authenticate a DELETE either
        // way, so clearing the stale local ref is all there is to do.
        clearRegisteredDeviceId();
      }
      setSession(nextSession);
      setInitializing(false);
    });

    return () => {
      mounted = false;
      subscription.subscription.unsubscribe();
    };
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      initializing,
      signIn: async (email, password) => {
        const { error } = await supabase.auth.signInWithPassword({ email, password });
        return { error: error ? toHouseAuthError(error) : null };
      },
      signOut: async () => {
        // M3: unregister this device BEFORE invalidating the session — the
        // DELETE needs a still-live bearer token (src/api/client.ts reads
        // it fresh from the current supabase session on every call), which
        // is gone the instant supabase.auth.signOut() below completes.
        // Bounded + best-effort (deviceRegistration.ts's own docstring) —
        // this can never throw or hang sign-out.
        await unregisterCurrentDeviceBestEffort();
        await supabase.auth.signOut();
      },
    }),
    [session, initializing],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
