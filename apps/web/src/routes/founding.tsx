import { createFileRoute } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/react-start";
import { useEffect, useState } from "react";
import { z } from "zod";
import { Wordmark } from "@/components/stoop/Wordmark";
import { cn } from "@/lib/utils";

/* ──────────────────────────────────────────────────────────────────
 * Founding-cohort waitlist page (issue #114) — minimal, Heritage design.
 * One idea, one action: email capture. Uses the site's existing tokens
 * (canvas / ink / brand / urgent) so / and /founding feel like one site.
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
         VALUES ('', ?, ?, 'founding-page')
         ON CONFLICT(email) DO NOTHING`,
      )
      .bind(email.toLowerCase(), isPm ? 1 : 0)
      .run();
    return { ok: true };
  },
);

export const Route = createFileRoute("/founding")({
  head: () => ({
    meta: [
      { title: "Stoop. — Your tenants text. You sleep." },
      {
        name: "description",
        content:
          "Stoop reads every tenant message, handles what it can, and only rings your phone for a true emergency. Founding Ontario landlords: $5/month flat, locked for life.",
      },
    ],
  }),
  component: FoundingPage,
});

/* ── single quiet example (no console, no buttons) ───────────────── */

function Example() {
  const [stage, setStage] = useState(0);

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const delays = reduce ? [0, 0, 0] : [400, 1400, 2400];
    const timers = delays.map((ms, i) => setTimeout(() => setStage(i + 1), ms));
    return () => timers.forEach(clearTimeout);
  }, []);

  const reveal = (n: number) =>
    cn(
      "transition-all duration-500 ease-out",
      stage >= n ? "translate-y-0 opacity-100" : "translate-y-2 opacity-0",
    );

  return (
    <figure
      className="mt-10 flex w-full max-w-md flex-col gap-3 text-left"
      aria-label="Example: how Stoop handles a 2 AM tenant text"
    >
      <div
        className={cn(
          "max-w-[88%] self-start rounded-2xl rounded-bl-md border border-border bg-surface px-4 py-3",
          reveal(1),
        )}
      >
        <p className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-ink-muted">
          Tenant · 2:12 AM
        </p>
        <p className="text-sm leading-relaxed text-ink">
          the heat stopped working and it's getting really cold… anything you can do tonight??
        </p>
      </div>

      <div className={cn("flex items-center justify-center gap-2.5", reveal(2))}>
        <span className="rounded-full border border-urgent/30 bg-urgent-soft px-3 py-1 text-[10px] font-bold uppercase tracking-wider text-urgent">
          Urgent — not an emergency
        </span>
        <span className="text-[11px] font-medium text-ink-muted">your phone stays silent</span>
      </div>

      <div
        className={cn(
          "max-w-[88%] self-end rounded-2xl rounded-br-md border border-brand/25 bg-brand-muted px-4 py-3",
          reveal(3),
        )}
      >
        <p className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-brand">
          Stoop's draft · waiting for your OK at 7 AM
        </p>
        <p className="text-sm leading-relaxed text-ink">
          Hi Maria — so sorry. Try the breaker in the hall closet and text me in 10 min. Still cold?
          My HVAC guy will be there at 7:30.
        </p>
      </div>
    </figure>
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
      <p
        className="rounded-full border border-routine/30 bg-routine-soft px-7 py-4 text-[15px] text-ink"
        role="status"
      >
        <b className="font-bold">You're on the list.</b> You'll hear from Laith directly — no
        newsletter, no spam.
      </p>
    );
  }

  return (
    <form className="mt-1 flex w-full max-w-md flex-col gap-3" onSubmit={onSubmit}>
      <div className="flex gap-2.5 max-sm:flex-col">
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
          className="min-h-12 min-w-0 flex-1 rounded-full border border-input bg-card px-5 text-[15px] text-ink outline-none transition-colors focus:border-brand focus-visible:ring-2 focus-visible:ring-ring"
        />
        <button
          type="submit"
          disabled={status === "sending"}
          className="min-h-12 whitespace-nowrap rounded-full bg-brand px-6 text-[15px] font-bold text-brand-foreground transition-all hover:-translate-y-0.5 hover:shadow-lg disabled:translate-y-0 disabled:opacity-60"
        >
          {status === "sending" ? "Saving…" : "Get early access"}
        </button>
      </div>
      <label className="flex cursor-pointer items-center justify-center gap-2 text-[13px] text-ink-muted">
        <input type="checkbox" name="isPm" className="size-4 accent-brand" />
        I'm a property manager (20+ doors) — Stoop Desk waitlist
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
        <p className="text-[13.5px] text-destructive" role="alert">
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
    <main className="flex min-h-screen flex-col items-center bg-canvas px-5 pb-16 pt-12 text-center text-ink">
      <Wordmark size="sm" />

      <h1 className="mt-12 font-display text-5xl font-semibold leading-[1.05] tracking-tight sm:text-6xl">
        Your tenants text.
        <br />
        <em className="text-brand">You sleep.</em>
      </h1>

      <p className="mt-6 max-w-[42ch] text-[17px] leading-relaxed text-ink-muted">
        Stoop reads every tenant message, drafts the reply in your voice, and only rings your phone
        for a true emergency.
      </p>

      <div className="mt-8 flex w-full flex-col items-center">
        <CaptureForm />
      </div>

      <p className="mt-5 max-w-[52ch] text-[12.5px] font-medium leading-relaxed text-ink-muted">
        First 10 Ontario landlords: <b className="text-brand">$5/month flat — locked for life.</b>{" "}
        Emergency triage free forever. Property managers: <b className="text-brand">$1.50/door</b> —
        check the box above.
      </p>

      <Example />

      <footer className="mt-14 text-[11px] font-medium uppercase tracking-wider text-ink-muted">
        Stoop. · Toronto · Built for Ontario landlords
      </footer>
    </main>
  );
}
