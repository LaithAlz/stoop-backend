import { createFileRoute, Link } from "@tanstack/react-router";
import { Check, ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { MarketingNav } from "@/components/stoop/MarketingNav";
import { SiteFooter } from "@/components/stoop/SiteFooter";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/plans")({
  head: () => ({
    meta: [
      { title: "Plans & pricing — Stoop." },
      {
        name: "description",
        content:
          "Free emergency line for every landlord. Full plan $10/month — $5/month locked in for as long as you stay with early access. Property managers from $1.50 per door.",
      },
      { property: "og:title", content: "Plans & pricing — Stoop." },
      {
        property: "og:description",
        content: "Free emergency line. Full plan $10/month. CAD pricing, cancel anytime.",
      },
      { property: "og:url", content: "/plans" },
    ],
    links: [{ rel: "canonical", href: "/plans" }],
  }),
  component: PlansPage,
});

interface Plan {
  name: string;
  price: string;
  per: string;
  note?: string;
  blurb: string;
  features: string[];
  cta: { label: string; to: "/early-access" };
  featured: boolean;
}

const plans: Plan[] = [
  {
    name: "Emergency Line",
    price: "$0",
    per: "free, for good",
    blurb: "Every message read. Real emergencies ring your phone. You reply yourself.",
    features: [
      "A dedicated text number for your tenants",
      "Every message read and sorted for you",
      "Emergencies call your phone immediately",
      "Everything else lands in one tidy inbox",
    ],
    cta: { label: "Get started", to: "/early-access" },
    featured: false,
  },
  {
    name: "Full Plan",
    price: "$10",
    per: "per month · up to 10 properties",
    note: "Early access: $5/month, locked in for as long as you stay",
    blurb: "Stoop writes the replies in your voice. You tap approve. That's the whole job.",
    features: [
      "Everything in Emergency Line",
      "Replies drafted in your voice, ready to send",
      "You approve everything — with a 5-second undo",
      "Routine replies go hands-off once Stoop earns your trust",
      "Lines up your own plumber, electrician, or handyman — you approve every text",
      "Full message history for every property",
    ],
    cta: { label: "Get early access", to: "/early-access" },
    featured: true,
  },
  {
    name: "Property Managers",
    price: "$1.50",
    per: "per door · per month",
    blurb: "After-hours coverage for portfolios of 20+ doors. Works alongside your team.",
    features: [
      "Everything in Full Plan",
      "Routes emergencies to your on-call staff",
      "Team access for coordinators",
      "Hands work orders to your existing software",
    ],
    cta: { label: "Join the waitlist", to: "/early-access" },
    featured: false,
  },
];

const faqs = [
  {
    q: "What counts as an emergency?",
    a: "Things that can't wait — burst pipes, water coming through a ceiling, gas smell, no heat in a deep freeze. Those ring your phone right away, day or night. A dripping tap waits politely until morning.",
  },
  {
    q: "Does anything get sent without me seeing it?",
    a: "No. Every reply waits for your approval. The only exception is safety instructions to a tenant during a real emergency — and you're being called at the same time.",
  },
  {
    q: "What do my tenants need to do?",
    a: "Nothing new. They text a phone number, the same way they text you today. No app to download, no account to create.",
  },
  {
    q: "What happens if I cancel?",
    a: "Cancel any time, no fee. You keep access until the end of your billing cycle, and we email you a copy of every conversation.",
  },
];

function PlansPage() {
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <MarketingNav />

      <main>
        {/* Hero */}
        <section className="border-b border-border px-6 py-20">
          <div className="mx-auto max-w-3xl text-center">
            <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand">Pricing</p>
            <h1 className="mt-3 text-balance font-display text-5xl font-bold leading-tight tracking-tight md:text-6xl">
              Simple pricing. The emergency line is always free.
            </h1>
            <p className="mx-auto mt-4 max-w-xl text-base text-ink-muted md:text-lg">
              All prices in CAD. No credit card to start. Cancel anytime.
            </p>
          </div>
        </section>

        {/* Plan cards */}
        <section className="px-6 py-16">
          <div className="mx-auto grid max-w-6xl gap-6 lg:grid-cols-3">
            {plans.map((p) => (
              <article
                key={p.name}
                className={cn(
                  "flex flex-col rounded-3xl border bg-card p-8 shadow-sm",
                  p.featured ? "border-2 border-brand" : "border-border",
                )}
              >
                {p.featured && (
                  <p className="mb-4 -mt-2 inline-flex self-start rounded-full bg-brand-muted px-3 py-1 text-[11px] font-bold uppercase tracking-wider text-brand">
                    Most landlords
                  </p>
                )}
                <h2 className="font-display text-2xl font-bold">{p.name}</h2>
                <p className="mt-4 font-display text-5xl font-bold tracking-tight">{p.price}</p>
                <p className="mt-1 text-sm font-medium text-ink-muted">{p.per}</p>
                {p.note && (
                  <p className="mt-3 rounded-lg bg-brand-muted px-3 py-2 text-[13px] font-semibold text-brand">
                    {p.note}
                  </p>
                )}
                <p className="mt-4 text-[15px] leading-relaxed text-ink-muted">{p.blurb}</p>
                <ul className="mt-6 flex-1 space-y-3">
                  {p.features.map((f) => (
                    <li key={f} className="flex items-start gap-2.5 text-sm leading-relaxed">
                      <Check className="mt-0.5 size-4 flex-none text-brand" aria-hidden="true" />
                      {f}
                    </li>
                  ))}
                </ul>
                <Button
                  asChild
                  variant={p.featured ? "default" : "outline"}
                  className="mt-8 h-12 w-full font-bold"
                >
                  <Link to={p.cta.to}>
                    {p.cta.label}
                    <ArrowRight className="size-4" aria-hidden="true" />
                  </Link>
                </Button>
              </article>
            ))}
          </div>
        </section>

        {/* FAQ */}
        <section className="border-t border-border px-6 py-16">
          <div className="mx-auto max-w-2xl">
            <h2 className="text-center font-display text-3xl font-bold tracking-tight md:text-4xl">
              Common questions
            </h2>
            <Accordion type="single" collapsible className="mt-8">
              {faqs.map((f) => (
                <AccordionItem key={f.q} value={f.q}>
                  <AccordionTrigger className="text-left text-base font-semibold">
                    {f.q}
                  </AccordionTrigger>
                  <AccordionContent className="text-[15px] leading-relaxed text-ink-muted">
                    {f.a}
                  </AccordionContent>
                </AccordionItem>
              ))}
            </Accordion>
          </div>
        </section>

        {/* CTA */}
        <section className="px-6 pb-20">
          <div className="mx-auto max-w-4xl rounded-3xl border border-border bg-card p-10 text-center shadow-sm md:p-14">
            <h2 className="mx-auto max-w-2xl text-balance font-display text-3xl font-bold leading-tight tracking-tight md:text-4xl">
              Get your evenings back this week.
            </h2>
            <p className="mx-auto mt-3 max-w-xl text-lg text-ink-muted">
              Five-minute setup. $5/month locked in for as long as you stay with early access.
            </p>
            <div className="mt-7">
              <Button asChild className="h-14 px-7 text-base font-bold">
                <Link to="/early-access">
                  Get early access
                  <ArrowRight className="size-4" aria-hidden="true" />
                </Link>
              </Button>
            </div>
          </div>
        </section>
      </main>

      <SiteFooter />
    </div>
  );
}
