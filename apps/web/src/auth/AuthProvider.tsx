/**
 * Auth context for the whole app. Wraps @supabase/supabase-js's session
 * state in a small React context so screens never touch `supabase.auth`
 * directly — src/routes/sign-in.tsx calls `signInWithMagicLink`, the
 * account screen's sign-out row (src/routes/app.account.tsx) calls
 * `signOut`, and src/routes/app.tsx reads `session`/`initializing` (via
 * `resolveAuthRoute`) to decide whether to render the dashboard or bounce
 * to sign-in. Ported from apps/mobile/src/auth/AuthProvider.tsx (campaign
 * issue #234) — the web app uses a magic link instead of mobile's M0
 * email+password (the existing /sign-in mock is already magic-link-shaped;
 * see the PR report for the OAuth-buttons decision), and reads the shared
 * query client via `useQueryClient()` instead of a module-level singleton
 * (src/api/queryClient.ts explains why: SSR isolate reuse).
 *
 * Never log the session/JWT/email (CLAUDE.md rule 5) — errors below
 * surface `error.message` from supabase-js (a human-readable auth failure
 * reason, e.g. "Email rate limit exceeded"), never the token or the raw
 * credentials.
 */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";
import { useQueryClient } from "@tanstack/react-query";
import { supabase, supabaseConfigured } from "@/lib/supabase";

interface AuthContextValue {
  session: Session | null;
  /** True until the first `getSession()` resolves (or immediately false
   *  when Supabase isn't configured — there's nothing to wait on) — the
   *  loading window before we know whether to show sign-in or `/app`.
   *  Seeded from `supabaseConfigured` (an env check, identical on the
   *  server and the client), NOT from `supabase !== null` (which is always
   *  `null` during SSR by construction — see src/lib/supabase.ts) — using
   *  the client-existence check here would make the server think an
   *  actually-configured build is unconfigured on every request, a
   *  hydration mismatch the moment the client resolves the real state. */
  initializing: boolean;
  /** False when `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` aren't set
   *  for this build. Screens read this to show an honest "not set up"
   *  state instead of a sign-in form that can never succeed. */
  configured: boolean;
  signInWithMagicLink: (email: string) => Promise<{ error: string | null }>;
  signOut: () => Promise<void>;
}

const NOT_CONFIGURED_ERROR = "Sign-in isn't set up for this build yet.";

// Customer-facing copy rule (CLAUDE.md rule 8 / copy-guardian): raw
// supabase-js error strings never reach the screen — every auth failure
// maps to the house voice. Unknown errors get one honest generic line.
function toHouseAuthError(error: { message: string }): string {
  const message = error.message.toLowerCase();
  if (message.includes("rate limit")) {
    return "Too many tries — wait a moment and try again.";
  }
  if (message.includes("unable to validate email") || message.includes("invalid format")) {
    return "That doesn't look like a valid email address.";
  }
  if (message.includes("network") || message.includes("fetch")) {
    return "Couldn't reach Stoop. Check your connection and try again.";
  }
  return "Sign-in didn't go through. Try again.";
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [initializing, setInitializing] = useState(supabaseConfigured);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!supabase) return;
    let mounted = true;

    supabase.auth.getSession().then(({ data }) => {
      if (!mounted) return;
      setSession(data.session);
      setInitializing(false);
    });

    const { data: subscription } = supabase.auth.onAuthStateChange((event, nextSession) => {
      if (!mounted) return;
      // PII fence (mirrors mobile's M1 senior review, BLOCKING finding):
      // the shared React Query cache holds tenant messages/names and the
      // landlord's own account data. On sign-out it must be emptied
      // immediately — otherwise a different landlord signing in on the
      // same browser inside the cache's gc window would be served the
      // previous account's data by stale-while-revalidate before the
      // refetch lands. See src/auth/__tests__ port note in the PR report
      // — web has no test runner configured yet, unlike mobile's
      // signOutClearsCache.test.tsx.
      if (event === "SIGNED_OUT") {
        queryClient.clear();
      }
      setSession(nextSession);
      setInitializing(false);
    });

    return () => {
      mounted = false;
      subscription.subscription.unsubscribe();
    };
  }, [queryClient]);

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      initializing,
      configured: supabaseConfigured,
      signInWithMagicLink: async (email) => {
        if (!supabase) return { error: NOT_CONFIGURED_ERROR };
        const { error } = await supabase.auth.signInWithOtp({
          email,
          options: {
            // Land back on /sign-in — supabase-js resolves the session
            // from the redirect URL automatically (detectSessionInUrl,
            // src/lib/supabase.ts), and the sign-in route itself redirects
            // on to /app the moment `session` is non-null.
            emailRedirectTo:
              typeof window !== "undefined" ? `${window.location.origin}/sign-in` : undefined,
          },
        });
        return { error: error ? toHouseAuthError(error) : null };
      },
      signOut: async () => {
        if (!supabase) return;
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
