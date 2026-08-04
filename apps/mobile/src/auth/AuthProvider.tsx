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
  reconcileStaleDeviceRegistration,
  unregisterCurrentDeviceBestEffort,
} from "@/features/push/deviceRegistration";

/** B3-5 (#284): `ok: false` means `supabase.auth.signOut()` came back with
 *  an error and, per auth-js's own `_signOut` (GoTrueClient.ts), the LOCAL
 *  session was never actually cleared - see `signOut` below for exactly
 *  why. The Me tab uses this to tell the landlord honestly rather than the
 *  fire-and-forget it used to be. */
interface SignOutResult {
  ok: boolean;
}

interface AuthContextValue {
  session: Session | null;
  /** True until the first `getSession()` resolves — the splash/loading
   *  window before we know whether to show sign-in or the tabs. */
  initializing: boolean;
  signIn: (email: string, password: string) => Promise<{ error: string | null }>;
  signOut: () => Promise<SignOutResult>;
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
        if (!error) {
          // B3-8 (#284): fire-and-forget, never awaited or allowed to
          // affect this sign-in's own result. See
          // deviceRegistration.ts's `reconcileStaleDeviceRegistration`
          // docstring for what this cleans up and why it's called here -
          // the first moment after a sign-in this device has both a live
          // token again and (if the landlord left one behind on a forced
          // or offline sign-out) a stale server-side device registration
          // still worth clearing out.
          void reconcileStaleDeviceRegistration();
        }
        return { error: error ? toHouseAuthError(error) : null };
      },
      signOut: async (): Promise<SignOutResult> => {
        // M3: unregister this device BEFORE invalidating the session — the
        // DELETE needs a still-live bearer token (src/api/client.ts reads
        // it fresh from the current supabase session on every call), which
        // is gone the instant supabase.auth.signOut() below completes.
        // Bounded + best-effort (deviceRegistration.ts's own docstring) —
        // this can never throw or hang sign-out. B3-8: when this can't
        // complete (e.g. offline), the id it would have unregistered is
        // durably persisted there and retried on the next successful
        // sign-in instead of lost.
        await unregisterCurrentDeviceBestEffort();
        // B3 (#263, ported from apps/web/src/auth/AuthProvider.tsx):
        // `scope: "local"` - supabase-js defaults `signOut()` to
        // `scope: "global"`, which revokes the session server-side across
        // EVERY device signed into this account. A landlord tapping "Sign
        // out" on their phone must not silently sign them out of the
        // Stoop web dashboard too, and, the sharper version of this on
        // mobile, must never kill the session on a DIFFERENT phone/tablet
        // that's still the device holding that account's approval queue
        // and push nudges. (The emergency path is voice and SMS, never
        // push - CLAUDE.md rule 1, apps/api/app/push_outbox.py - so a
        // sign-out on one device never affects whether the emergency
        // chain can still reach the landlord.)
        //
        // B3-5 (#284): auth-js's own `_signOut` (GoTrueClient.ts) calls
        // the `/logout` endpoint FIRST and, when that call fails with
        // anything other than a 404/401/403/session-missing (the cases it
        // already treats as "nothing left to log out of"), returns that
        // error WITHOUT ever calling its local `_removeSession()` - a
        // retryable fetch error (no connection - the concrete "signing out
        // on the subway" case this finding names) takes that exact branch.
        // So a non-null `error` here is not a maybe: this device's local
        // session, and the working bearer token src/api/client.ts reads
        // fresh from `getSession()` on every request, are BOTH still
        // fully live.
        //
        // Deliberately NOT force-clearing the local session ourselves on
        // that path. "Sign out" is a security action - the wrong failure
        // direction is claiming success on a device that's still fully
        // signed in - but the only way to force a clear here would be
        // reaching past this SDK's public API into its private storage
        // internals (there is no supported "local-only, no network"
        // signOut), which would (a) leave supabase-js's own in-memory
        // session cache untouched anyway, so `getSession()` and every
        // subsequent `apiRequest` bearer token would still work exactly as
        // before behind a UI now falsely showing signed-out, and (b)
        // duplicate exactly the kind of undocumented-internals coupling
        // that produced B3-3's finding above. An honest failure the Me tab
        // can surface (see `SignOutResult`) is the safer and the more
        // maintainable choice; the landlord can retry once back online.
        const { error } = await supabase.auth.signOut({ scope: "local" });
        return { ok: !error };
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
