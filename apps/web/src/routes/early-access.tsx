import { createFileRoute } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/react-start";
import { useEffect, useState } from "react";
import { z } from "zod";
import { ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { MarketingNav } from "@/components/stoop/MarketingNav";
import { SiteFooter } from "@/components/stoop/SiteFooter";
import { cn } from "@/lib/utils";

/* ──────────────────────────────────────────────────────────────────
 * Early-access page (issue #114).
 * A real page of the marketing site: same nav, footer, and hero
 * anatomy as "/", with email capture instead of app-store CTAs.
 * ────────────────────────────────────────────────────────────────── */

const waitlistSchema = z.object({
  email: z.string().trim().email().max(254),
  isPm: z.boolean().optional().default(false),
  website: z.string().max(0).optional().default(""), // honeypot — must stay empty
});

type D1Like = {
  prepare(q: string): {
    bind(...v: unknown[]): { run(): Promise<unknown> };
  };
};

async function getWaitlistDb(): Promise<D1Like | null> {
  try {
    const mod = (await import("cloudflare:workers")) as {
      env?: Record<string, unknown>;
    };
    return (mod.env?.WAITLIST_DB as D1Like | undefined) ?? null;
  } catch {
    return null;
  }
}

const joinWaitlist = createServerFn({ method: "POST" }).handler(
  async (ctx): Promise<{ ok: boolean; reason?: string }> => {
    const parsed = waitlistSchema.safeParse(ctx.data);
    if (!parsed.success) return { ok: false, reason: "invalid" };
    if (parsed.data.website !== "") return { ok: true }; // honeypot: pretend success
    const db = await getWaitlistDb();
    if (!db) return { ok: false, reason: "not-configured" };
    const { email, isPm } = parsed.data;
    await db
      .prepare(
        `INSERT INTO waitlist (name, email, is_pm, source)
         VALUES ('', ?, ?, 'early-access-page')
         ON CONFLICT(email) DO NOTHING`,
      )
      .bind(email.toLowerCase(), isPm ? 1 : 0)
      .run();
    return { ok: true };
  },
);

export const Route = createFileRoute("/early-access")({
  head: () => ({
    meta: [
      { title: "Get early access — Stoop." },
      {
        name: "description",
        content:
          "Early access for Ontario landlords: $5/month, locked in for life. Stoop reads every tenant text, drafts your replies, and only rings your phone for a true emergency.",
      },
    ],
  }),
  component: FoundingPage,
});

/* ── overnight example card (the right-hand visual) ──────────────── */

function OvernightCard() {
  const [stage, setStage] = useState(0);

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const delays = reduce ? [0, 0, 0] : [500, 1500, 2500];
    const timers = delays.map((ms, i) => setTimeout(() => setStage(i + 1), ms));
    return () => timers.forEach(clearTimeout);
  }, []);

  const reveal = (n: number) =>
    cn(
      "transition-all duration-500 ease-out",
      stage >= n ? "translate-y-0 opacity-100" : "translate-y-2 opacity-0",
    );

  return (
    <article className="overflow-hidden rounded-3xl border border-border bg-card shadow-sm">
      <header className="flex items-center justify-between border-b border-border bg-surface/50 px-5 py-4">
        <div>
          <p className="text-sm font-bold text-ink">Unit 2 · 41 Palmerston</p>
          <p className="text-[11px] font-medium uppercase tracking-wider text-ink-muted">
            While you were asleep
          </p>
        </div>
        <span className="rounded-full border border-urgent/30 bg-urgent-soft px-3 py-1 text-[10px] font-bold uppercase tracking-wider text-urgent">
          Urgent
        </span>
      </header>

      <div className="space-y-4 px-5 py-6">
        <div
          className={cn("max-w-[90%] rounded-2xl rounded-bl-md bg-surface px-4 py-3", reveal(1))}
        >
          <p className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-ink-muted">
            Tenant · 2:12 AM
          </p>
          <p className="text-sm leading-relaxed text-ink">
            the heat stopped working and it's getting really cold… anything you can do tonight??
          </p>
        </div>

        <p
          className={cn(
            "text-center text-[11px] font-semibold uppercase tracking-wider text-ink-muted",
            reveal(2),
          )}
        >
          Marked urgent — not an emergency. Your phone stayed silent.
        </p>

        <div
          className={cn(
            "ml-auto max-w-[90%] rounded-2xl rounded-br-md border border-brand/25 bg-brand-muted px-4 py-3",
            reveal(3),
          )}
        >
          <p className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-brand">
            Stoop's draft · waiting for your OK at 7 AM
          </p>
          <p className="text-sm leading-relaxed text-ink">
            Hi Maria — so sorry. Try the breaker in the hall closet and text me in 10 min. Still
            cold? My HVAC guy will be there at 7:30.
          </p>
        </div>
      </div>

      <footer className="border-t border-border bg-canvas px-5 py-3 text-center text-[11px] font-medium text-ink-muted">
        You approve before anything sends. Always.
      </footer>
    </article>
  );
}

/* ── email capture ───────────────────────────────────────────────── */

function CaptureForm() {
  const [status, setStatus] = useState<"idle" | "sending" | "done" | "error">("idle");

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    setStatus("sending");
    try {
      const res = await joinWaitlist({
        data: {
          email: String(fd.get("email") ?? ""),
          isPm: fd.get("isPm") === "on",
          website: String(fd.get("website") ?? ""),
        },
      });
      setStatus(res.ok ? "done" : "error");
    } catch {
      setStatus("error");
    }
  }

  if (status === "done") {
    return (
      <div
        className="rounded-2xl border border-routine/30 bg-routine-soft px-6 py-5 text-[15px] leading-relaxed text-ink"
        role="status"
      >
        <b className="font-bold">You're on the list.</b> You'll hear from Laith directly — no
        newsletter, no spam.
      </div>
    );
  }

  return (
    <form className="space-y-3" onSubmit={onSubmit}>
      <div className="flex max-w-xl gap-2.5 max-sm:flex-col">
        <label className="sr-only" htmlFor="fnd-email">
          Email
        </label>
        <input
          id="fnd-email"
          name="email"
          type="email"
          required
          placeholder="you@email.com"
          autoComplete="email"
          maxLength={254}
          className="h-14 min-w-0 flex-1 rounded-md border-2 border-border bg-card px-5 text-base text-ink outline-none transition-colors focus:border-brand focus-visible:ring-2 focus-visible:ring-ring"
        />
        <Button
          type="submit"
          disabled={status === "sending"}
          className="h-14 px-6 text-base font-bold"
        >
          {status === "sending" ? "Saving…" : "Get early access"}
        </Button>
      </div>
      <label className="flex cursor-pointer items-center gap-2 text-[13px] font-medium text-ink-muted">
        <input type="checkbox" name="isPm" className="size-4 accent-brand" />
        I'm a property manager (20+ doors) — join the Stoop Desk waitlist instead
      </label>
      <input
        type="text"
        name="website"
        tabIndex={-1}
        autoComplete="off"
        aria-hidden="true"
        className="absolute -left-[9999px] size-px opacity-0"
      />
      {status === "error" && (
        <p className="text-sm text-destructive" role="alert">
          That didn't go through —{" "}
          <a
            className="font-semibold text-brand underline"
            href="mailto:allaithalzoubi2@gmail.com?subject=Stoop%20early%20access"
          >
            email us instead
          </a>
          .
        </p>
      )}
    </form>
  );
}

/* ── page ────────────────────────────────────────────────────────── */

function FoundingPage() {
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <MarketingNav />

      <main>
        <section className="relative overflow-hidden border-b border-border">
          <div className="mx-auto grid max-w-7xl gap-12 px-6 py-16 lg:grid-cols-[1.05fr_1fr] lg:gap-16 lg:py-28">
            <div className="space-y-7">
              <div className="inline-flex items-center gap-2 rounded-full bg-brand-muted px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-brand">
                <ShieldCheck className="size-3.5" aria-hidden="true" />
                Early access · Ontario
              </div>

              <h1 className="text-balance font-display text-5xl font-bold leading-[0.95] tracking-tight md:text-6xl lg:text-7xl">
                Your tenants text.
                <br />
                <span className="font-semibold italic text-brand">You sleep.</span>
              </h1>

              <p className="max-w-xl text-lg leading-relaxed text-ink-muted md:text-xl">
                Stoop reads every tenant message, drafts the reply in your voice, and only rings
                your phone for a true emergency. Every landlord is onboarded personally.
              </p>

              <CaptureForm />

              <p className="text-xs font-medium uppercase tracking-widest text-ink-muted">
                $5/month early-access rate, locked in for life · Emergency line always free ·
                Property managers: $1.50/door
              </p>
            </div>

            <div className="lg:pt-4">
              <OvernightCard />
            </div>
          </div>
        </section>
      </main>

      <SiteFooter />
    </div>
  );
}
