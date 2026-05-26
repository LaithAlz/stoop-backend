import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";
import { Apple, Mail, ArrowRight } from "lucide-react";
import { MarketingNav } from "@/components/stoop/MarketingNav";
import { SiteFooter } from "@/components/stoop/SiteFooter";
import { Wordmark } from "@/components/stoop/Wordmark";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export const Route = createFileRoute("/sign-in")({
  head: () => ({
    meta: [
      { title: "Sign in — Stoop." },
      {
        name: "description",
        content:
          "Sign in to Stoop. Maintenance triage for small landlords — handles the 2am text so you don't have to.",
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
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);

  return (
    <div className="min-h-screen bg-canvas">
      <MarketingNav />

      <main className="mx-auto flex max-w-md flex-col items-stretch px-6 py-16 md:py-24">
        <div className="flex justify-center pb-8">
          <Wordmark size="md" />
        </div>

        <div className="rounded-3xl border border-border bg-card p-7 shadow-sm">
          <h1 className="font-display text-[28px] leading-tight tracking-tight text-ink">
            Welcome back.
          </h1>
          <p className="mt-2 text-sm leading-relaxed text-ink-muted">
            Sign in to triage your queue, edit drafts, and check on your properties.
          </p>

          <div className="mt-6 flex flex-col gap-2">
            <Button
              type="button"
              className="h-12 justify-center bg-ink text-canvas hover:bg-ink/90"
              onClick={() => alert("Apple sign-in (mock)")}
            >
              <Apple className="size-4" /> Continue with Apple
            </Button>
            <Button
              type="button"
              variant="outline"
              className="h-12 justify-center"
              onClick={() => alert("Google sign-in (mock)")}
            >
              <span className="inline-flex size-4 items-center justify-center font-bold">G</span>
              Continue with Google
            </Button>
          </div>

          <div className="my-6 flex items-center gap-3 text-xs font-bold uppercase tracking-widest text-ink-muted">
            <span className="h-px flex-1 bg-border" /> or email <span className="h-px flex-1 bg-border" />
          </div>

          {sent ? (
            <div className="rounded-2xl border border-brand/30 bg-brand-muted/60 p-5 text-center">
              <Mail className="mx-auto size-6 text-brand" />
              <p className="mt-2 font-display text-[18px] text-ink">Check your inbox.</p>
              <p className="mt-1 text-sm text-ink-muted">
                We sent a magic link to <strong>{email}</strong>. It expires in 15 minutes.
              </p>
            </div>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (email.includes("@")) setSent(true);
              }}
              className="flex flex-col gap-3"
            >
              <div>
                <Label htmlFor="email" className="text-xs font-bold uppercase tracking-widest text-ink-muted">
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
              <Button type="submit" className="h-12 justify-center bg-brand text-brand-foreground hover:bg-brand/90">
                Email me a sign-in link <ArrowRight className="size-4" />
              </Button>
            </form>
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
          <Link to="/terms" className="underline">Terms</Link> and{" "}
          <Link to="/privacy" className="underline">Privacy Policy</Link>.
        </p>
      </main>

      <SiteFooter />
    </div>
  );
}
