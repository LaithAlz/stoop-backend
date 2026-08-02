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
  const { session, initializing, configured, signInWithMagicLink } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const alreadySignedIn = !initializing && session !== null;

  useEffect(() => {
    if (alreadySignedIn) {
      void navigate({ to: "/app", replace: true });
    }
  }, [alreadySignedIn, navigate]);

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
                <a href="mailto:hello@stoop.co" className="font-semibold text-brand underline">
                  hello@stoop.co
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
