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

    const { data: subscription } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      if (!mounted) return;
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
