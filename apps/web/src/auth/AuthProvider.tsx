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
 * Never log the session/JWT/email (CLAUDE.md rule 5). `toHouseAuthError`
 * below is the only place that reads supabase-js's raw `error.message`
 * (a human-readable auth failure reason, e.g. "Email rate limit
 * exceeded") — it uses that string to CHOOSE a house-voice line, and
 * returns the mapped line, never the raw message itself; no screen ever
 * renders `error.message`, the token, or the raw credentials directly.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
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
  /** True once the 10s init watchdog (safety review A3, #234) fires
   *  without `getSession()`/`onAuthStateChange` ever settling — a dead
   *  network mid-request, since supabase-js puts no timeout on the
   *  underlying fetch itself. Distinct from a confirmed "signed out": a
   *  timeout means we genuinely don't know, so src/routes/app.tsx shows a
   *  retry instead of silently redirecting to /sign-in as if the check
   *  had come back negative. */
  initTimedOut: boolean;
  /** Re-runs the session check from scratch (clears `initTimedOut`, resets
   *  `initializing`). The only escape hatch from a timed-out check. */
  retryInit: () => void;
  /** False when `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` aren't set
   *  for this build. Screens read this to show an honest "not set up"
   *  state instead of a sign-in form that can never succeed. */
  configured: boolean;
  signInWithMagicLink: (email: string) => Promise<{ error: string | null }>;
  signOut: () => Promise<void>;
}

const NOT_CONFIGURED_ERROR = "Sign-in isn't set up for this build yet.";
const INIT_WATCHDOG_MS = 10_000;

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

// B4 (safety review, #234): `signInWithOtp` below sets
// `shouldCreateUser: false` — /onboarding, not a bare email prompt, owns
// account creation (a landlord's first property provisions their account
// server-side; there's no such thing as a bare Stoop account with zero
// properties reachable from this form). GoTrue's response for "this email
// has no account and we were told not to create one" is treated as
// SUCCESS here, not a distinct error — surfacing a different message for
// "no account" vs. "check your inbox" would let anyone probe arbitrary
// addresses to learn which ones have a Stoop account (email enumeration).
// Ops follow-up, launch-gated and NOT this PR: Supabase's own Auth →
// Attack Protection should have Turnstile/hCaptcha on the OTP endpoint —
// `shouldCreateUser: false` stops account creation but doesn't rate-limit
// probing by itself.
function isBlockedSignupError(error: { message: string }): boolean {
  return error.message.toLowerCase().includes("signup");
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [initializing, setInitializing] = useState(supabaseConfigured);
  const [initTimedOut, setInitTimedOut] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);
  const queryClient = useQueryClient();
  // B2 (safety review, #234): the PII fence has to key on IDENTITY, not on
  // the `SIGNED_OUT` event name — auth-js re-emits `SIGNED_IN` on tab
  // refocus/token refresh for the SAME account (that must NOT clear the
  // cache, or every alt-tab would drop the landlord's queue mid-read), but
  // a cross-tab account switch (a different account signs in via the
  // shared localStorage session, e.g. landlord signs out and a different
  // landlord signs into the same browser) can arrive as a `SIGNED_IN`
  // event too, with a different `session.user.id` — that case MUST clear.
  const lastUserIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!supabaseConfigured) return;

    let mounted = true;

    // A3 (safety review, #234): `getSession()` normally settles almost
    // immediately, but supabase-js puts no timeout on the underlying
    // fetch — a dead connection mid-request could otherwise hang
    // `initializing` forever with no way out. Armed unconditionally
    // (before the `!supabase` check below) so even a
    // configured-but-somehow-null client (shouldn't happen — see
    // src/lib/supabase.ts's window-gate — but this removes any silent,
    // unrecoverable trap if it ever does) still resolves into a visible,
    // retryable state instead of hanging.
    const watchdog = setTimeout(() => {
      if (!mounted) return;
      setInitTimedOut(true);
      setInitializing(false);
    }, INIT_WATCHDOG_MS);

    if (!supabase) {
      return () => {
        mounted = false;
        clearTimeout(watchdog);
      };
    }

    supabase.auth.getSession().then(({ data }) => {
      if (!mounted) return;
      clearTimeout(watchdog);
      lastUserIdRef.current = data.session?.user.id ?? null;
      setSession(data.session);
      setInitializing(false);
    });

    const { data: subscription } = supabase.auth.onAuthStateChange((event, nextSession) => {
      if (!mounted) return;
      clearTimeout(watchdog);

      const nextUserId = nextSession?.user.id ?? null;
      const identityChanged =
        lastUserIdRef.current !== null &&
        nextUserId !== null &&
        nextUserId !== lastUserIdRef.current;

      // PII fence (mirrors mobile's M1 senior review, BLOCKING finding,
      // sharpened by B2 above): the shared React Query cache holds tenant
      // messages/names and the landlord's own account data. It must be
      // emptied on an explicit sign-out OR a detected identity change —
      // otherwise a different landlord signing in on the same browser
      // could be served the previous account's data by
      // stale-while-revalidate before the refetch lands. Deliberately NOT
      // keyed on `event === "SIGNED_IN"` alone — auth-js re-emits that on
      // tab refocus for the same still-signed-in account, which must be a
      // no-op here. See PR report — web has no test runner configured
      // yet, unlike mobile's signOutClearsCache.test.tsx.
      if (event === "SIGNED_OUT" || identityChanged) {
        queryClient.clear();
      }
      lastUserIdRef.current = nextUserId;
      setSession(nextSession);
      setInitializing(false);
    });

    return () => {
      mounted = false;
      clearTimeout(watchdog);
      subscription.subscription.unsubscribe();
    };
  }, [queryClient, retryNonce]);

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      initializing,
      initTimedOut,
      retryInit: () => {
        setInitTimedOut(false);
        setInitializing(supabaseConfigured);
        setRetryNonce((n) => n + 1);
      },
      configured: supabaseConfigured,
      signInWithMagicLink: async (email) => {
        if (!supabase) return { error: NOT_CONFIGURED_ERROR };
        const { error } = await supabase.auth.signInWithOtp({
          email,
          options: {
            // B4: never silently create an account from this form — see
            // the module-level comment above `isBlockedSignupError`.
            shouldCreateUser: false,
            // Land back on /sign-in — supabase-js resolves the session
            // from the redirect URL automatically (detectSessionInUrl +
            // flowType: "pkce", src/lib/supabase.ts), and the sign-in
            // route itself redirects on to /app the moment `session` is
            // non-null.
            emailRedirectTo:
              typeof window !== "undefined" ? `${window.location.origin}/sign-in` : undefined,
          },
        });
        if (error && isBlockedSignupError(error)) {
          // Same success response as a real send — see the B4 comment
          // above; a distinct message here would be an email-enumeration
          // oracle.
          return { error: null };
        }
        return { error: error ? toHouseAuthError(error) : null };
      },
      signOut: async () => {
        if (!supabase) return;
        // B3 (safety review, #234): `scope: "local"` — supabase-js
        // defaults `signOut()` to `scope: "global"`, which revokes the
        // session server-side across EVERY device signed into this
        // account (confirmed against the installed auth-js source,
        // GoTrueClient.ts: `signOut(options: SignOut = { scope: 'global'
        // })`). A landlord clicking "Sign out" on the web dashboard must
        // not silently sign them out of the Stoop mobile app too — see
        // src/routes/app.account.tsx's confirmation copy, which promises
        // exactly this device.
        await supabase.auth.signOut({ scope: "local" });
      },
    }),
    [session, initializing, initTimedOut],
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
