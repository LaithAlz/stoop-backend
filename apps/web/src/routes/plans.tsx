import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";
import { z } from "zod";
import { Check, Minus, ArrowRight, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { MarketingNav, APP_STORE_URL } from "@/components/stoop/MarketingNav";
import { SiteFooter } from "@/components/stoop/SiteFooter";
import { cn } from "@/lib/utils";

const searchSchema = z.object({
  tier: z.enum(["solo", "standard", "pro"]).optional().catch(undefined),
  billing: z.enum(["monthly", "annual"]).catch("monthly").default("monthly"),
});

export const Route = createFileRoute("/plans")({
  validateSearch: (s) => searchSchema.parse(s),
  head: () => ({
    meta: [
      { title: "Plans & pricing — Stoop." },
      {
        name: "description",
        content:
          "Compare Solo, Standard, and Pro plans. CAD pricing, 14-day free trial, no credit card. For Ontario landlords managing 1 to 15 properties.",
      },
      { property: "og:title", content: "Plans & pricing — Stoop." },
      {
        property: "og:description",
        content:
          "Three plans for one, five, or fifteen properties. CAD pricing. 14-day free trial, no card.",
      },
      { property: "og:url", content: "/plans" },
    ],
    links: [{ rel: "canonical", href: "/plans" }],
  }),
  component: PlansPage,
});

type TierId = "solo" | "standard" | "pro";

interface Tier {
  id: TierId;
  name: string;
  monthly: number;
  annual: number;
  cap: string;
  featured: boolean;
  blurb: string;
  features: string[];
}

const tiers: Tier[] = [
  {
    id: "solo",
    name: "Solo",
    monthly: 19,
    annual: 190,
    cap: "1 property",
    featured: false,
    blurb: "For the landlord with one rental and one inbox to protect.",
    features: [
      "Dedicated property SMS number",
      "AI triage on every inbound text",
      "Severity classification — Emergency, Urgent, Routine",
      "Shadow mode by default — you approve everything",
      "Photo intake from tenants",
      "Push notifications + daily digest",
    ],
  },
  {
    id: "standard",
    name: "Standard",
    monthly: 49,
    annual: 490,
    cap: "Up to 5 properties",
    featured: true,
    blurb: "Most chosen. Small portfolio, half a dozen tenants, real triage.",
    features: [
      "Everything in Solo",
      "Trust dashboard with autonomy graduation",
      "Custom FAQ per property",
      "Vendor directory + on-call rotation",
      "Severity overrides per property",
    ],
  },
  {
    id: "pro",
    name: "Pro",
    monthly: 129,
    annual: 1290,
    cap: "Up to 15 properties",
    featured: false,
    blurb: "Full-time small landlord running a real operation.",
    features: [
      "Everything in Standard",
      "Priority support",
      "Custom escalation rules",
      "Exportable conversation history",
      "Multi-user access (rolling out in v2)",
    ],
  },
];

const cad = (n: number) =>
  `$${n.toLocaleString("en-CA", { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;

const cadDec = (n: number) =>
  `$${n.toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

type Cell = boolean | string;

interface ComparisonRow {
  label: string;
  solo: Cell;
  standard: Cell;
  pro: Cell;
}

interface ComparisonGroup {
  title: string;
  rows: ComparisonRow[];
}

const groups: ComparisonGroup[] = [
  {
    title: "Properties & numbers",
    rows: [
      { label: "Properties included", solo: "1", standard: "Up to 5", pro: "Up to 15" },
      { label: "Dedicated SMS number per property", solo: true, standard: true, pro: true },
      { label: "Tenant photo intake", solo: true, standard: true, pro: true },
    ],
  },
  {
    title: "AI triage",
    rows: [
      { label: "Severity classification", solo: true, standard: true, pro: true },
      { label: "Clarifying-question follow-ups", solo: true, standard: true, pro: true },
      { label: "Draft-and-approve replies", solo: true, standard: true, pro: true },
      { label: "Hard refusals on legal/tenancy questions", solo: true, standard: true, pro: true },
    ],
  },
  {
    title: "Customization",
    rows: [
      { label: "Custom FAQ per property", solo: false, standard: true, pro: true },
      { label: "Quiet hours per property", solo: false, standard: true, pro: true },
      { label: "Severity overrides", solo: false, standard: true, pro: true },
      { label: "Custom escalation rules", solo: false, standard: false, pro: true },
    ],
  },
  {
    title: "Trust & autonomy",
    rows: [
      { label: "Shadow mode (approve everything)", solo: true, standard: true, pro: true },
      { label: "Trust dashboard", solo: false, standard: true, pro: true },
      { label: "Autonomy graduation (Auto-Routine, Auto-Urgent)", solo: false, standard: true, pro: true },
      { label: "Vendor directory + on-call rotation", solo: false, standard: true, pro: true },
    ],
  },
  {
    title: "Support",
    rows: [
      { label: "Email support", solo: true, standard: true, pro: true },
      { label: "Priority response", solo: false, standard: true, pro: true },
      { label: "Onboarding call", solo: false, standard: false, pro: true },
    ],
  },
  {
    title: "Reporting",
    rows: [
      { label: "Daily digest", solo: true, standard: true, pro: true },
      { label: "Conversation history in-app", solo: true, standard: true, pro: true },
      { label: "Exportable conversation history", solo: false, standard: false, pro: true },
      { label: "Multi-user access", solo: false, standard: false, pro: true },
    ],
  },
];

const faqs = [
  {
    q: "Can I edit a draft before it sends to my tenant?",
    a: "Yes — every draft lands on your phone for approval. Edit the words, change the tone, or rewrite it entirely. Nothing goes out until you tap approve.",
  },
  {
    q: "What if the agent says something wrong?",
    a: "Reject the draft and reply yourself. The agent learns from your edits — over time it gets your voice. You can also leave a private note explaining why, which sharpens future drafts.",
  },
  {
    q: "Will Stoop give legal advice about tenancy law?",
    a: "No. Never. The agent has hard refusals on legal interpretation and will not advise on rights, obligations, or disputes. Anything that smells legal gets escalated to you with a flag.",
  },
  {
    q: "What about Ontario's Residential Tenancies Act?",
    a: "The agent will not interpret the RTA, the LTB, or any tenancy statute. If a tenant asks about rent increases, eviction, or their rights, Stoop hands the conversation back to you immediately.",
  },
  {
    q: "Can I pause my subscription if I sell a property?",
    a: "Yes. Drop down a tier when your portfolio shrinks, or pause your account from settings. Your number and history stay parked for 60 days.",
  },
  {
    q: "What happens if I cancel?",
    a: "Cancel any time from the app. No fee, no friction. You keep access until the end of your billing cycle, and we email you an export of every conversation.",
  },
  {
    q: "What if my tenant doesn't text?",
    a: "You can still use Stoop. Send a message manually from the app — the agent helps you triage what comes back and drafts replies faster. The triage layer works whether the tenant initiates or you do.",
  },
];

function PlansPage() {
  const { tier } = Route.useSearch();
  const [annual, setAnnual] = useState(false);

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <MarketingNav />

      <main>
        {/* Hero + toggle */}
        <section className="border-b border-border px-6 py-20">
          <div className="mx-auto max-w-3xl text-center">
            <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand">Pricing</p>
            <h1 className="mt-3 text-balance font-display text-5xl font-bold leading-tight tracking-tight md:text-6xl">
              Pick the size that matches your portfolio.
            </h1>
            <p className="mx-auto mt-4 max-w-xl text-base text-ink-muted md:text-lg">
              All prices in CAD. 14-day free trial on every tier. No credit card.
            </p>

            <div
              role="radiogroup"
              aria-label="Billing cycle"
              className="mx-auto mt-8 inline-flex rounded-full border border-border bg-card p-1"
            >
              {[
                { v: false, label: "Monthly" },
                { v: true, label: "Annual · save ~17%" },
              ].map((opt) => (
                <button
                  key={opt.label}
                  role="radio"
                  aria-checked={annual === opt.v}
                  onClick={() => setAnnual(opt.v)}
                  className={cn(
                    "min-h-11 rounded-full px-5 text-sm font-semibold transition-colors",
                    annual === opt.v
                      ? "bg-brand text-brand-foreground"
                      : "text-ink-muted hover:text-ink",
                  )}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            {annual && (
              <p className="mt-3 text-xs font-medium text-brand">
                That's two months free, billed once a year.
              </p>
            )}
          </div>
        </section>

        {/* Plan cards */}
        <section className="px-6 py-16">
          <div className="mx-auto grid max-w-6xl gap-6 lg:grid-cols-3">
            {tiers.map((t) => {
              const highlighted = tier === t.id || (!tier && t.featured);
              const monthEquiv = annual ? t.annual / 12 : t.monthly;
              return (
                <article
                  key={t.id}
                  className={cn(
                    "relative flex flex-col rounded-3xl border bg-card p-8 transition-all",
                    highlighted
                      ? "border-2 border-brand shadow-xl lg:-translate-y-3"
                      : "border-border",
                  )}
                >
                  {t.featured && (
                    <span className="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-brand px-4 py-1 text-[10px] font-bold uppercase tracking-widest text-brand-foreground">
                      Most chosen
                    </span>
                  )}

                  <h2 className="text-sm font-bold uppercase tracking-widest text-ink-muted">
                    {t.name}
                  </h2>
                  <p className="mt-2 text-sm text-ink-muted">{t.blurb}</p>

                  <div className="mt-6 flex items-baseline gap-1">
                    <span className="font-display text-5xl font-bold">
                      {annual ? cadDec(monthEquiv) : cad(monthEquiv)}
                    </span>
                    <span className="text-sm font-medium text-ink-muted">/mo CAD</span>
                  </div>
                  <p className="mt-1 text-sm font-medium text-brand">{t.cap}</p>
                  <p className="mt-1 text-xs text-ink-muted">
                    {annual
                      ? `Billed ${cad(t.annual)} once a year`
                      : `Billed ${cad(t.monthly)} monthly · or ${cad(t.annual)}/yr`}
                  </p>

                  <ul className="mt-6 space-y-3 border-t border-border pt-6">
                    {t.features.map((f) => (
                      <li key={f} className="flex items-start gap-2 text-sm text-ink">
                        <Check className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden="true" />
                        {f}
                      </li>
                    ))}
                  </ul>

                  <Button
                    asChild
                    className={cn(
                      "mt-8 h-14 w-full text-base font-bold",
                      !highlighted && "bg-ink text-canvas hover:bg-ink/90",
                    )}
                  >
                    <a href={`/checkout?plan=${t.id}`}>
                      Start 14-day free trial
                      <ArrowRight className="size-4" aria-hidden="true" />
                    </a>
                  </Button>
                  <p className="mt-3 text-center text-xs text-ink-muted">
                    No credit card · Cancel anytime
                  </p>
                </article>
              );
            })}
          </div>
        </section>

        {/* Free-trial banner */}
        <section className="px-6 pb-8">
          <div className="mx-auto flex max-w-6xl flex-col items-start gap-4 rounded-2xl border border-brand/30 bg-brand-muted px-6 py-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <ShieldCheck className="mt-0.5 size-5 shrink-0 text-brand" aria-hidden="true" />
              <p className="text-sm font-semibold text-ink">
                Try any tier free for 14 days. No card required. Cancel anytime — we email you an
                export on the way out.
              </p>
            </div>
            <a
              href={APP_STORE_URL}
              className="inline-flex h-11 items-center justify-center rounded-xl bg-brand px-5 text-sm font-bold text-brand-foreground hover:bg-brand/90"
            >
              Get the app
            </a>
          </div>
        </section>

        {/* Comparison table */}
        <section className="border-t border-border px-6 py-20">
          <div className="mx-auto max-w-6xl">
            <h2 className="font-display text-3xl font-bold tracking-tight md:text-4xl">
              Compare every feature.
            </h2>
            <p className="mt-2 max-w-2xl text-ink-muted">
              Skim the table. Nothing hidden, nothing surprised by at billing.
            </p>

            {/* Desktop / tablet table */}
            <div className="mt-10 hidden overflow-x-auto rounded-2xl border border-border bg-card md:block">
              <table className="w-full min-w-[640px] text-left text-sm">
                <thead>
                  <tr className="border-b border-border bg-surface/60">
                    <th scope="col" className="w-2/5 px-6 py-4 text-xs font-bold uppercase tracking-widest text-ink-muted">
                      Feature
                    </th>
                    {tiers.map((t) => (
                      <th
                        key={t.id}
                        scope="col"
                        className={cn(
                          "px-6 py-4 text-center",
                          t.featured && "bg-brand-muted/60",
                        )}
                      >
                        <div className="font-display text-lg font-bold text-ink">{t.name}</div>
                        <div className="text-xs font-medium text-ink-muted">
                          {annual ? `${cadDec(t.annual / 12)}/mo` : `${cad(t.monthly)}/mo`}
                        </div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {groups.map((g) => (
                    <ComparisonGroupRows key={g.title} group={g} />
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile: one tier per card */}
            <div className="mt-10 space-y-6 md:hidden">
              {tiers.map((t) => (
                <div
                  key={t.id}
                  className={cn(
                    "rounded-2xl border bg-card p-6",
                    t.featured ? "border-2 border-brand" : "border-border",
                  )}
                >
                  <div className="flex items-baseline justify-between">
                    <h3 className="font-display text-xl font-bold">{t.name}</h3>
                    <span className="text-sm font-semibold text-brand">
                      {annual ? `${cadDec(t.annual / 12)}/mo` : `${cad(t.monthly)}/mo`}
                    </span>
                  </div>
                  <div className="mt-4 space-y-5">
                    {groups.map((g) => (
                      <div key={g.title}>
                        <h4 className="text-xs font-bold uppercase tracking-widest text-ink-muted">
                          {g.title}
                        </h4>
                        <ul className="mt-2 space-y-2">
                          {g.rows.map((r) => {
                            const cell = r[t.id];
                            return (
                              <li
                                key={r.label}
                                className="flex items-start justify-between gap-3 text-sm"
                              >
                                <span className="text-ink">{r.label}</span>
                                <span className="shrink-0 text-right">
                                  <CellRender value={cell} />
                                </span>
                              </li>
                            );
                          })}
                        </ul>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* FAQ */}
        <section className="border-t border-border bg-surface/40 px-6 py-20">
          <div className="mx-auto max-w-3xl">
            <h2 className="font-display text-3xl font-bold tracking-tight md:text-4xl">
              Questions landlords actually ask.
            </h2>
            <Accordion type="single" collapsible className="mt-8">
              {faqs.map((item, i) => (
                <AccordionItem key={item.q} value={`item-${i}`} className="border-border">
                  <AccordionTrigger className="py-5 text-left font-display text-lg font-semibold hover:no-underline">
                    {item.q}
                  </AccordionTrigger>
                  <AccordionContent className="pb-5 text-base leading-relaxed text-ink-muted">
                    {item.a}
                  </AccordionContent>
                </AccordionItem>
              ))}
            </Accordion>
          </div>
        </section>

        {/* Final CTA */}
        <section className="border-t border-border px-6 py-20">
          <div className="mx-auto max-w-3xl text-center">
            <h2 className="font-display text-3xl font-bold tracking-tight md:text-4xl">
              Not ready to pick a tier?
            </h2>
            <p className="mx-auto mt-3 max-w-xl text-ink-muted">
              Download the app and start your free 14 days. You can pick a plan when you're ready —
              everything you set up during the trial carries over.
            </p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <a
                href={APP_STORE_URL}
                className="inline-flex h-12 items-center justify-center rounded-xl bg-brand px-6 text-sm font-bold text-brand-foreground hover:bg-brand/90"
              >
                Get the app
              </a>
              <Link
                to="/"
                className="inline-flex h-12 items-center justify-center rounded-xl border border-border bg-card px-6 text-sm font-bold hover:bg-brand-muted"
              >
                Back to overview
              </Link>
            </div>
          </div>
        </section>
      </main>

      <SiteFooter />
    </div>
  );
}

function CellRender({ value }: { value: Cell }) {
  if (value === true) {
    return (
      <span className="inline-flex size-6 items-center justify-center rounded-full bg-brand-muted">
        <Check className="size-4 text-brand" aria-hidden="true" />
        <span className="sr-only">Included</span>
      </span>
    );
  }
  if (value === false) {
    return (
      <span className="inline-flex size-6 items-center justify-center text-ink-muted">
        <Minus className="size-4" aria-hidden="true" />
        <span className="sr-only">Not included</span>
      </span>
    );
  }
  return <span className="text-sm font-semibold text-ink">{value}</span>;
}

function ComparisonGroupRows({ group }: { group: ComparisonGroup }) {
  return (
    <>
      <tr>
        <th
          scope="colgroup"
          colSpan={4}
          className="bg-surface/80 px-6 pb-2 pt-6 text-left text-xs font-bold uppercase tracking-widest text-brand"
        >
          {group.title}
        </th>
      </tr>
      {group.rows.map((r) => (
        <tr key={r.label} className="border-t border-border">
          <td className="px-6 py-3 text-ink">{r.label}</td>
          {tiers.map((t) => (
            <td
              key={t.id}
              className={cn(
                "px-6 py-3 text-center",
                t.featured && "bg-brand-muted/40",
              )}
            >
              <CellRender value={r[t.id]} />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}
