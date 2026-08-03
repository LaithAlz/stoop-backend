import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { Apple, Mail, ArrowRight, Loader2 } from "lucide-react";
import { MarketingNav } from "@/components/stoop/MarketingNav";
import { SiteFooter } from "@/components/stoop/SiteFooter";
import { Wordmark } from "@/components/stoop/Wordmark";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/auth/AuthProvider";

// A1 (safety review, #234): a used/expired magic link (a corporate email
// scanner "Safe Links"-style prefetch can burn a single-use link before
// the real person clicks it) redirects back here with `error`/
// `error_code`/`error_description` params — historically as a `#hash`
// fragment, though PKCE's `?code=` convention means a future GoTrue
// version could plausibly move errors to the query string too, so this
// checks both. One unified house-voice line rather than per-code
// branching — "expired" and "already used" are the same story to a
// landlord either way.
function toHouseCallbackError(): string {
  return "That sign-in link didn't work — it may have expired or already been used. Send yourself a new one below.";
}

// #248 (fast-follow to the #234 PR-1 safety review's NEW-2): with
// `flowType: "pkce"`, a magic link opened on a DIFFERENT device than the
// one that requested it has no `code_verifier` in that device's
// localStorage. auth-js's own `_isPKCECallback()` check (installed
// @supabase/auth-js, GoTrueClient.ts) then quietly treats the URL as if it
// carried no callback at all — no exchange attempt, no error, `?code=`
// left sitting in the address bar. Ordinary behavior (request the link on
// a laptop, open the email on a phone), not an attack, so the copy stays
// blame-free and just names what to do next.
// F1 (safety review, #248): the guard below can't actually distinguish
// WHY the exchange produced no session. Cross-device (no verifier) is the
// common case, but the same landing also covers a superseded verifier
// (two links requested, the older one opened), an already-consumed code
// (double-click / duplicate tab), a GoTrue rejection, and a legacy
// implicit link — three of which happen ON the right device. Asserting
// "wrong device" would be confidently wrong there and would send the
// landlord back to the laptop to hit the same wall. So: hedge the cause,
// keep the remedy, which is correct on every one of those paths.
function toHouseCrossDeviceNotice(): string {
  return "We couldn't finish that sign-in here. Links have to be opened on the same device you asked for them from — send yourself a new one below.";
}

// F2 (safety review, #248): the 10s watchdog path. Distinct from the
// above — here the session check never came back at all, so nothing is
// known about the link. `retryInit` cannot rescue this one: auth-js
// memoizes `initializePromise`, so a retry re-awaits the SAME hung
// promise and lands in the same silence 10s later. Only a reload
// re-runs the exchange, so that's what this says — and the caller
// deliberately does NOT strip `?code=` on this path, so the reload can
// still complete the sign-in.
function toHouseInitTimeoutNotice(): string {
  return "We couldn't finish signing you in just now — check your connection and reload this page.";
}

// F-runtime (safety review, #248): survives a remount. The notice is
// triggered by a URL that the same effect then strips, and the strip goes
// through TanStack's patched `window.history.replaceState`, which fires a
// router load. That happens not to remount this route today (no
// `loaderDeps`, no `remountDeps`, so the match key is stable) — but the
// whole fix would silently become a no-op if that ever changed, and the
// evidence for it is three layers deep in the router. Recording the
// detection outside React removes the dependency on that coincidence
// entirely: a remount re-reads this and renders the notice again.
//
// SSR INVARIANT (safety re-verify): this module-scope value is shared
// across requests in a warm Cloudflare Worker isolate — the exact hazard
// src/auth/AuthProvider.tsx warns about. It is safe ONLY because both
// writes below are browser-only (a mount effect and a submit handler), so
// the server-side value stays `false` forever, SSR renders no notice, and
// hydration matches. Never write this from a loader, `beforeLoad`, or a
// server function: that turns it into cross-request state bleed.
let crossDeviceNoticeLatch = false;

// Mount counter for the latch. A remount goes 1 → 0 → 1 within a single
// commit, so the microtask below still sees a live mount and keeps the
// latch; a genuine navigation away (this page links to itself from the
// nav and footer) leaves 0 and clears it, so returning later can't show
// a stale "we couldn't finish that sign-in".
let signInMounts = 0;

function parseAuthCallbackParams(): { hasError: boolean; hasPendingExchange: boolean } {
  if (typeof window === "undefined") return { hasError: false, hasPendingExchange: false };
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const search = new URLSearchParams(window.location.search);
  const hasError = hash.has("error") || search.has("error");
  // A2: `code` is the PKCE callback shape (src/lib/supabase.ts's
  // `flowType: "pkce"`); `access_token` is kept as a defensive check for
  // the legacy implicit shape in case any old magic link is still in
  // flight from before that change.
  const hasPendingExchange = !hasError && (search.has("code") || hash.has("access_token"));
  return { hasError, hasPendingExchange };
}

export const Route = createFileRoute("/sign-in")({
  head: () => ({
    meta: [
      { title: "Sign in — Stoop." },
      {
        name: "description",
        content:
          "Sign in to Stoop. Tenant maintenance, sorted and drafted — handles the 2am text so you don't have to.",
      },
      { property: "og:title", content: "Sign in — Stoop." },
      {
        property: "og:description",
        content: "Sign in to your Stoop. account.",
      },
    ],
  }),
  component: SignInPage,
});

function SignInPage() {
  const { session, initializing, initTimedOut, configured, signInWithMagicLink } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [callbackError, setCallbackError] = useState<string | null>(null);
  // #248: set once the pending PKCE/implicit exchange this page waited on
  // (`isFinishingSignIn` below) settles with no session — i.e. the plain
  // form is about to render silently. See `toHouseCrossDeviceNotice`.
  // Initialized FROM the module latch so a remount (see its comment)
  // re-renders the notice instead of silently losing it.
  const [crossDeviceNotice, setCrossDeviceNotice] = useState(() => crossDeviceNoticeLatch);
  // A2: starts `false` on both the server and the client's first paint
  // (there's no URL to read during SSR) and only settles in the mount
  // effect below — the same stable-then-settle shape as GreetingHeader
  // (apps/web/AGENTS.md's SSR/hydration lesson), so this never disagrees
  // with the server-rendered form on hydration.
  const [isFinishingSignIn, setIsFinishingSignIn] = useState(false);

  const alreadySignedIn = !initializing && session !== null;

  useEffect(() => {
    signInMounts += 1;
    return () => {
      signInMounts -= 1;
      queueMicrotask(() => {
        if (signInMounts === 0) crossDeviceNoticeLatch = false;
      });
    };
  }, []);

  useEffect(() => {
    if (alreadySignedIn) {
      // A session landing invalidates any stale notice outright —
      // otherwise a landlord who signs out later is greeted with
      // "we couldn't finish that sign-in" right after a deliberate,
      // successful sign-out.
      crossDeviceNoticeLatch = false;
      void navigate({ to: "/app", replace: true });
    }
  }, [alreadySignedIn, navigate]);

  useEffect(() => {
    const { hasError, hasPendingExchange } = parseAuthCallbackParams();
    if (hasError) {
      setCallbackError(toHouseCallbackError());
      // Strip the error (and anything alongside it) so a refresh, share,
      // or browser-history replay of this URL never re-shows a stale
      // error or carries a token/code in the visible address bar.
      window.history.replaceState(window.history.state, "", window.location.pathname);
      return;
    }
    if (hasPendingExchange) {
      setIsFinishingSignIn(true);
    }
  }, []);

  // #248: gates the cross-device notice on `initializing` having actually
  // SETTLED, not merely on a code being present — this is what keeps a
  // legitimate same-device exchange (still in flight, `initializing` still
  // true) from ever flashing this message. `getSession()`
  // (src/auth/AuthProvider.tsx) awaits the SAME `initializePromise` that
  // GoTrue's own constructor kicked off the code exchange on, so by the
  // time `initializing` goes false, a real exchange attempt has already
  // fully resolved — success sets `session` in the same batched update, so
  // the `session === null` check below can never race a success. Skips
  // `initTimedOut`: a 10s watchdog abort (AuthProvider) means the check
  // never came back at all, which is a network problem, not a wrong-device
  // one — misdiagnosing it here would be worse than the prior silence.
  useEffect(() => {
    if (!isFinishingSignIn || initializing || initTimedOut || session) return;
    crossDeviceNoticeLatch = true;
    setCrossDeviceNotice(true);
    // Same reasoning as the `hasError` strip above: a refresh must not
    // leave `?code=` sitting in the address bar / history, or re-attempt
    // the exchange auth-js already silently declined to make.
    window.history.replaceState(window.history.state, "", window.location.pathname);
  }, [isFinishingSignIn, initializing, initTimedOut, session]);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!email.includes("@")) return;
    setSubmitting(true);
    setError(null);
    const { error: signInError } = await signInWithMagicLink(email);
    setSubmitting(false);
    if (signInError) {
      setError(signInError);
      return;
    }
    // The notice told them to send a new link and they just did — clear
    // it (and its latch) so it can't sit above "Check your inbox" still
    // asking for the thing that already happened.
    crossDeviceNoticeLatch = false;
    setCrossDeviceNotice(false);
    setSent(true);
  }

  return (
    <div className="min-h-screen bg-canvas">
      <MarketingNav />

      <main className="mx-auto flex max-w-md flex-col items-stretch px-6 py-16 md:py-24">
        <div className="flex justify-center pb-8">
          <Wordmark size="md" />
        </div>

        <div className="rounded-3xl border border-border bg-card p-7 shadow-sm">
          {alreadySignedIn ? (
            <div className="flex flex-col items-center gap-3 py-6 text-center">
              <Loader2
                className="size-6 animate-spin text-brand motion-reduce:animate-none"
                aria-hidden="true"
              />
              <p className="text-sm text-ink-muted">You're signed in — taking you to Stoop…</p>
            </div>
          ) : isFinishingSignIn && initializing ? (
            // A2: the round trip after clicking the email link — Supabase
            // resolves the session from the URL automatically
            // (detectSessionInUrl + PKCE, src/lib/supabase.ts). Once that
            // settles, `initializing` goes false: on success `session` is
            // already set by then too, so the `alreadySignedIn` branch
            // above takes over on the next render; on a silent failure
            // this falls through to the normal form below so the landlord
            // can just try again.
            <div className="flex flex-col items-center gap-3 py-6 text-center">
              <Loader2
                className="size-6 animate-spin text-brand motion-reduce:animate-none"
                aria-hidden="true"
              />
              <p className="text-sm text-ink-muted">Finishing sign-in…</p>
            </div>
          ) : isFinishingSignIn && initTimedOut ? (
            // F2 (safety review, #248): the watchdog path used to fall
            // straight through to the bare form — no spinner, no message,
            // no way forward — which is the same silence this PR exists to
            // remove, just narrowed to the flaky-network cause (and flaky
            // network IS the 2am case). No retry button on purpose:
            // auth-js memoizes its init promise, so a retry would re-await
            // the same hung request and go quiet again 10s later. Note
            // this branch deliberately does NOT strip `?code=` — leaving
            // it is what makes the reload able to finish the sign-in.
            <div className="flex flex-col items-center gap-3 py-6 text-center">
              <p role="alert" className="text-sm leading-relaxed text-ink-muted">
                {toHouseInitTimeoutNotice()}
              </p>
            </div>
          ) : !configured ? (
            <div>
              <h1 className="font-display text-[28px] leading-tight tracking-tight text-ink">
                Welcome back.
              </h1>
              <p
                className="mt-4 rounded-2xl border border-border bg-surface px-4 py-4 text-sm leading-relaxed text-ink-muted"
                role="status"
              >
                Sign-in isn't set up on this build yet. If you're expecting access, email us at{" "}
                <a
                  href="mailto:allaithalzoubi2@gmail.com"
                  className="font-semibold text-brand underline"
                >
                  allaithalzoubi2@gmail.com
                </a>
                .
              </p>
            </div>
          ) : (
            <>
              <h1 className="font-display text-[28px] leading-tight tracking-tight text-ink">
                Welcome back.
              </h1>
              <p className="mt-2 text-sm leading-relaxed text-ink-muted">
                Sign in to sort your queue, edit drafts, and check on your properties.
              </p>

              {callbackError && (
                <p
                  className="mt-4 rounded-2xl border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm leading-relaxed text-ink"
                  role="alert"
                >
                  {callbackError}
                </p>
              )}

              {/* #248: neutral, not destructive-red — opening a link on a
                  different device is expected behavior, not a fault, so
                  this stays in the same honest/no-blame register as the
                  `!configured` box above rather than the `callbackError`
                  alert above. */}
              {/* F3 (safety review, #248): `role="alert"`, not `status`.
                  This element is INSERTED when the notice fires, and
                  live-region announcement on insertion of the region
                  itself is unreliable for `status` across assistive tech
                  — which would leave a screen-reader user with exactly
                  the silence this PR removes for everyone else. The
                  neutral styling still carries "expected, not a fault";
                  the role is about whether it gets announced at all.
                  Matches the sibling `callbackError` block. */}
              {crossDeviceNotice && (
                <p
                  className="mt-4 rounded-2xl border border-border bg-surface px-4 py-3 text-sm leading-relaxed text-ink-muted"
                  role="alert"
                >
                  {toHouseCrossDeviceNotice()}
                </p>
              )}

              <div className="mt-6 flex flex-col gap-2">
                <Button
                  type="button"
                  disabled
                  aria-disabled="true"
                  className="h-12 justify-center bg-ink text-canvas hover:bg-ink/90"
                >
                  <Apple className="size-4" /> Continue with Apple
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  disabled
                  aria-disabled="true"
                  className="h-12 justify-center"
                >
                  <span className="inline-flex size-4 items-center justify-center font-bold">
                    G
                  </span>
                  Continue with Google
                </Button>
              </div>
              <p className="mt-2 text-center text-xs text-ink-muted">
                Apple and Google sign-in are coming soon — use email for now.
              </p>

              <div className="my-6 flex items-center gap-3 text-xs font-bold uppercase tracking-widest text-ink-muted">
                <span className="h-px flex-1 bg-border" /> or email{" "}
                <span className="h-px flex-1 bg-border" />
              </div>

              {sent ? (
                <div className="rounded-2xl border border-brand/30 bg-brand-muted/60 p-5 text-center">
                  <Mail className="mx-auto size-6 text-brand" />
                  <p className="mt-2 font-display text-[18px] text-ink">Check your inbox.</p>
                  <p className="mt-1 text-sm text-ink-muted">
                    We sent a sign-in link to <strong>{email}</strong>. Open it on this device to
                    finish signing in.
                  </p>
                </div>
              ) : (
                <form onSubmit={handleSubmit} className="flex flex-col gap-3">
                  <div>
                    <Label
                      htmlFor="email"
                      className="text-xs font-bold uppercase tracking-widest text-ink-muted"
                    >
                      Email
                    </Label>
                    <Input
                      id="email"
                      type="email"
                      required
                      autoComplete="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder="you@example.com"
                      className="mt-1 h-12"
                    />
                  </div>
                  {error && (
                    <p className="text-sm text-destructive" role="alert">
                      {error}
                    </p>
                  )}
                  <Button
                    type="submit"
                    disabled={submitting}
                    className="h-12 justify-center bg-brand text-brand-foreground hover:bg-brand/90"
                  >
                    {submitting ? (
                      <>
                        <Loader2
                          className="size-4 animate-spin motion-reduce:animate-none"
                          aria-hidden="true"
                        />
                        Sending…
                      </>
                    ) : (
                      <>
                        Email me a sign-in link <ArrowRight className="size-4" />
                      </>
                    )}
                  </Button>
                </form>
              )}
            </>
          )}
        </div>

        <p className="mt-6 text-center text-sm text-ink-muted">
          New here?{" "}
          <Link to="/onboarding" className="font-semibold text-brand hover:underline">
            Set up your first property
          </Link>
        </p>

        <p className="mt-2 text-center text-xs text-ink-muted">
          By signing in, you agree to our{" "}
          <Link to="/terms" className="underline">
            Terms
          </Link>{" "}
          and{" "}
          <Link to="/privacy" className="underline">
            Privacy Policy
          </Link>
          .
        </p>
      </main>

      <SiteFooter />
    </div>
  );
}
