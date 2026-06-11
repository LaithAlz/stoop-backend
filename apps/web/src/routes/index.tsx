import { createFileRoute, Link } from "@tanstack/react-router";
import {
  MessageSquareText,
  Brain,
  Bell,
  TrendingUp,
  Check,
  X,
  ArrowRight,
  ShieldCheck,
  Quote,
} from "lucide-react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";

import { MarketingNav } from "@/components/stoop/MarketingNav";
import { SiteFooter } from "@/components/stoop/SiteFooter";
import { ApprovalCard } from "@/components/stoop/ApprovalCard";
import { SeverityBadge } from "@/components/stoop/SeverityBadge";
import { Wordmark } from "@/components/stoop/Wordmark";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Stoop. — Tenant maintenance, handled before it wakes you up" },
      {
        name: "description",
        content:
          "Tenants text one number. Stoop reads every message, drafts a reply in your voice — you approve before it sends.",
      },
      { property: "og:title", content: "Stoop. — Tenant maintenance, handled" },
      {
        property: "og:description",
        content:
          "Drafts every tenant reply in your voice. You approve before it sends. Built for Ontario landlords.",
      },
      { property: "og:url", content: "/" },
    ],
    links: [{ rel: "canonical", href: "/" }],
  }),
  component: LandingPage,
});

function LandingPage() {
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <MarketingNav />

      <main>
        <Hero />
        <ProblemSection />
        <HowItWorksSection />
        <NotWhatSection />
        <PersonasSection />
        <PlansPreview />
        <TrustStrip />
        <FAQSection />
        <FinalCTA />
      </main>

      <SiteFooter />
    </div>
  );
}

/* ------------------------ Hero ------------------------ */

function Hero() {
  return (
    <section className="relative overflow-hidden border-b border-border">
      <div className="mx-auto grid max-w-7xl gap-12 px-6 py-16 lg:grid-cols-[1.05fr_1fr] lg:gap-16 lg:py-28">
        <div className="space-y-7">
          <div className="inline-flex items-center gap-2 rounded-full bg-brand-muted px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-brand">
            <ShieldCheck className="size-3.5" aria-hidden="true" />
            For landlords with 1–15 properties
          </div>

          <h1 className="text-balance font-display text-5xl font-bold leading-[0.95] tracking-tight md:text-6xl lg:text-7xl">
            Your tenant's 2am text,
            <br />
            <span className="italic font-semibold text-brand">handled before it wakes you up</span>.
          </h1>

          <p className="max-w-xl text-lg leading-relaxed text-ink-muted md:text-xl">
            Stoop is a quiet AI agent that sits between you and your tenants. It reads and sorts
            every message, drafts the reply in your voice, lines up your own plumber or electrician,
            and only asks for your approval when it's ready.
          </p>

          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <Button asChild className="h-14 px-6 text-base font-bold">
              <Link to="/early-access">
                Get early access
                <ArrowRight className="size-4" aria-hidden="true" />
              </Link>
            </Button>
            <Link
              to="/plans"
              className="inline-flex h-14 items-center justify-center rounded-md border-2 border-border bg-card px-6 text-base font-bold text-ink hover:bg-brand-muted"
            >
              See plans
            </Link>
          </div>

          <p className="text-xs font-medium uppercase tracking-widest text-ink-muted">
            Early access: $5/month, locked in for life · Emergency help always free
          </p>
        </div>

        <HeroPhone />
      </div>
    </section>
  );
}

function HeroPhone() {
  return (
    <div className="relative mx-auto w-full max-w-md">
      <div className="absolute -inset-6 -z-10 rounded-[3rem] bg-brand-muted/70 blur-2xl" />
      <div className="overflow-hidden rounded-[2.5rem] border-[10px] border-card bg-card shadow-2xl">
        <div className="flex items-center justify-between border-b border-border bg-surface/60 px-5 py-3 text-[11px] font-bold uppercase tracking-widest text-ink-muted">
          <span>Stoop. inbox</span>
          <span className="inline-flex items-center gap-1.5">
            <span className="size-1.5 rounded-full bg-routine" aria-hidden="true" />
            Agent active
          </span>
        </div>
        <div className="bg-canvas p-4">
          <ApprovalCard
            unit="Unit 4B"
            property="128 Wythe Ave"
            receivedAgo="2 min ago"
            severity="urgent"
            tenantMessage="Kitchen sink is backing up — water on the floor. Sorry to text so late."
            draftReply="No worries — I've reached our on-call plumber and they'll be there within 90 minutes. Please clear the area under the sink and put towels around the base. I'll text again once they're 10 minutes out."
          />
        </div>
      </div>
    </div>
  );
}

/* ------------------------ Problem ------------------------ */

function ProblemSection() {
  const items = [
    {
      title: "The 2am text",
      body: "\"Sorry to bother — the heat's out and the baby's room is freezing.\" You're in bed. You're trying to figure out if you call a tech tonight or if a space heater gets them to morning.",
    },
    {
      title: "Is this an emergency?",
      body: "\"The sink's been acting weird.\" Could be nothing. Could be a slab leak. You don't have time to interview them — and they don't have time to write you a novel.",
    },
    {
      title: "Same questions, every week",
      body: "When's rent due. Where do I park the second car. What day is garbage. You've answered each of these forty times. Your evenings deserve better.",
    },
  ];

  return (
    <section className="border-b border-border bg-surface/40 px-6 py-20 lg:py-28">
      <div className="mx-auto max-w-5xl">
        <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand">The job today</p>
        <h2 className="mt-3 max-w-3xl font-display text-4xl font-bold leading-tight tracking-tight md:text-5xl">
          You didn't sign up to be on call.
        </h2>
        <p className="mt-4 max-w-2xl text-lg text-ink-muted">
          Most landlord days are fine. It's the few minutes scattered across each week that wear you
          down.
        </p>

        <div className="mt-12 grid gap-6 md:grid-cols-3">
          {items.map((item) => (
            <div key={item.title} className="rounded-2xl border border-border bg-card p-6">
              <h3 className="font-display text-xl font-bold text-ink">{item.title}</h3>
              <p className="mt-3 text-[15px] leading-relaxed text-ink-muted">{item.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ------------------------ How it works ------------------------ */

function HowItWorksSection() {
  const steps = [
    {
      icon: MessageSquareText,
      eyebrow: "Step 1",
      title: "Tenant texts your property's number",
      body: "Each property gets a dedicated phone number. Tenants text it like a person. No app to install on their end.",
    },
    {
      icon: Brain,
      eyebrow: "Step 2",
      title: "Stoop sorts the message",
      body: "Asks clarifying questions, requests photos when useful, and classifies severity — Emergency, Urgent, or Routine.",
    },
    {
      icon: Bell,
      eyebrow: "Step 3",
      title: "You approve every message",
      body: "The reply to your tenant, the text lining up your plumber for Thursday — each one is a draft until you tap Approve. Edit first if you want. Fifteen seconds.",
    },
    {
      icon: TrendingUp,
      eyebrow: "Step 4",
      title: "It earns trust over time",
      body: "Approve enough drafts unedited and Stoop graduates from Shadow mode toward Auto-Routine, then further — only if you decide.",
    },
  ];

  return (
    <section id="how-it-works" className="scroll-mt-20 border-b border-border px-6 py-20 lg:py-28">
      <div className="mx-auto max-w-6xl">
        <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand">How it works</p>
        <h2 className="mt-3 max-w-3xl font-display text-4xl font-bold leading-tight tracking-tight md:text-5xl">
          Four moving parts. You're in charge of all of them.
        </h2>

        <ol className="mt-12 grid gap-6 md:grid-cols-2 lg:grid-cols-4">
          {steps.map((s, i) => {
            const Icon = s.icon;
            return (
              <li
                key={s.title}
                className="relative flex flex-col gap-4 rounded-3xl border border-border bg-card p-6"
              >
                <div className="flex items-center justify-between">
                  <div className="inline-flex size-12 items-center justify-center rounded-2xl bg-brand-muted text-brand">
                    <Icon className="size-6" aria-hidden="true" />
                  </div>
                  <span className="font-display text-4xl font-bold text-brand/20">0{i + 1}</span>
                </div>
                <div className="space-y-2">
                  <p className="text-[11px] font-bold uppercase tracking-widest text-ink-muted">
                    {s.eyebrow}
                  </p>
                  <h3 className="font-display text-xl font-bold leading-snug">{s.title}</h3>
                  <p className="text-sm leading-relaxed text-ink-muted">{s.body}</p>
                </div>
              </li>
            );
          })}
        </ol>

        <div className="mt-10 flex flex-wrap items-center gap-3">
          <span className="text-sm font-semibold text-ink-muted">
            Severity, always with a label:
          </span>
          <SeverityBadge severity="emergency" />
          <SeverityBadge severity="urgent" />
          <SeverityBadge severity="routine" />
        </div>
      </div>
    </section>
  );
}

/* ------------------------ What it's not ------------------------ */

function NotWhatSection() {
  const isNot = ["Property management software", "A chatbot replacing your judgment"];
  const isIt = [
    "A smart filter for tenant text messages",
    "A draft-and-approve assistant in your voice",
    "A trust ladder you control — Shadow to Full Auto",
  ];

  return (
    <section className="border-b border-border bg-surface/40 px-6 py-20 lg:py-28">
      <div className="mx-auto grid max-w-5xl gap-10 md:grid-cols-2">
        <div className="rounded-3xl border border-border bg-card p-8">
          <p className="text-xs font-bold uppercase tracking-[0.2em] text-emergency">
            What Stoop is not
          </p>
          <ul className="mt-6 space-y-4">
            {isNot.map((line) => (
              <li key={line} className="flex items-start gap-3 text-[15px] font-medium text-ink">
                <X
                  className="mt-0.5 size-5 shrink-0 rounded-full bg-emergency-soft p-0.5 text-emergency"
                  aria-hidden="true"
                />
                {line}
              </li>
            ))}
          </ul>
        </div>
        <div className="rounded-3xl border border-brand bg-brand text-brand-foreground p-8">
          <p className="text-xs font-bold uppercase tracking-[0.2em] opacity-80">What Stoop is</p>
          <ul className="mt-6 space-y-4">
            {isIt.map((line) => (
              <li key={line} className="flex items-start gap-3 text-[15px] font-medium">
                <Check
                  className="mt-0.5 size-5 shrink-0 rounded-full bg-brand-foreground/15 p-0.5"
                  aria-hidden="true"
                />
                {line}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

/* ------------------------ Personas ------------------------ */

function PersonasSection() {
  const personas = [
    {
      tag: "Side-hustle",
      title: "You have a day job and 2 rentals.",
      body: "Stoop handles the inbox between meetings. You see one draft, tap Approve, get back to work.",
      quote: {
        text: "I stopped checking my phone during my kid's hockey practice. Stoop drafts the reply, I approve at intermission.",
        author: "Devraj, Mississauga",
        meta: "2 units",
      },
    },
    {
      tag: "Empty-nester",
      title: "You hold a few paid-off properties.",
      body: "Set it up in 5 minutes with one of our property managers on the phone. Then leave it in Shadow mode forever if you want.",
      quote: {
        text: "I'm not interested in learning a new app every six months. Stoop is one screen. I read the message, I tap approve.",
        author: "Margaret, Burlington",
        meta: "4 units",
      },
    },
    {
      tag: "Accidental",
      title: "You inherited a place. You're learning.",
      body: "Stoop knows what's actually an emergency, what can wait, and what vendor to suggest. You don't need to figure it out at 11pm.",
      quote: {
        text: "I didn't know a slow drain wasn't an emergency. Stoop did. It asked the right questions before I even saw the text.",
        author: "Priya, Toronto",
        meta: "1 unit",
      },
    },
  ];

  return (
    <section className="border-b border-border px-6 py-20 lg:py-28">
      <div className="mx-auto max-w-6xl">
        <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand">
          For every kind of landlord
        </p>
        <h2 className="mt-3 max-w-3xl font-display text-4xl font-bold leading-tight tracking-tight md:text-5xl">
          Whether this is a side thing or your retirement.
        </h2>

        <div className="mt-12 grid gap-6 lg:grid-cols-3">
          {personas.map((p) => (
            <article
              key={p.tag}
              className="flex flex-col gap-6 rounded-3xl border border-border bg-card p-7"
            >
              <span className="self-start rounded-full border border-brand/30 bg-brand-muted px-3 py-1 text-xs font-bold uppercase tracking-wider text-brand">
                {p.tag}
              </span>
              <div className="space-y-3">
                <h3 className="font-display text-2xl font-bold leading-snug">{p.title}</h3>
                <p className="text-[15px] leading-relaxed text-ink-muted">{p.body}</p>
              </div>
              <figure className="mt-auto rounded-2xl border border-border bg-surface/60 p-5">
                <Quote className="size-5 text-brand/50" aria-hidden="true" />
                <blockquote className="mt-2 text-sm leading-relaxed text-ink">
                  "{p.quote.text}"
                </blockquote>
                <figcaption className="mt-3 text-xs font-semibold text-ink-muted">
                  {p.quote.author} · {p.quote.meta}
                </figcaption>
              </figure>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ------------------------ Plans preview ------------------------ */

function PlansPreview() {
  const tiers = [
    {
      id: "free",
      name: "Emergency Line",
      price: "$0",
      cap: "Free, for good",
      featured: false,
      bullets: [
        "Every text read and sorted",
        "Emergencies ring your phone immediately",
        "One tidy inbox for the rest",
      ],
    },
    {
      id: "full",
      name: "Full Plan",
      price: "$10",
      cap: "Up to 10 properties · $5/month with early access, locked in for life",
      featured: true,
      bullets: [
        "Replies drafted in your voice — you approve",
        "Lines up your own plumber, electrician, handyman",
        "Routine replies go hands-off as trust builds",
      ],
    },
    {
      id: "pm",
      name: "Property Managers",
      price: "$1.50",
      cap: "Per door · 20+ doors",
      featured: false,
      bullets: [
        "After-hours coverage for your whole portfolio",
        "Emergencies routed to on-call staff",
        "Works with your existing software",
      ],
    },
  ] as const;

  return (
    <section
      id="plans"
      className="scroll-mt-20 border-b border-border bg-surface/40 px-6 py-20 lg:py-28"
    >
      <div className="mx-auto max-w-6xl">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand">Plans</p>
            <h2 className="mt-3 font-display text-4xl font-bold leading-tight tracking-tight md:text-5xl">
              Pick a plan. Cancel anytime.
            </h2>
            <p className="mt-3 max-w-xl text-base text-ink-muted">
              All prices in CAD. No credit card to start. Cancel anytime.
            </p>
          </div>
          <Link to="/plans" className="text-sm font-semibold text-brand hover:underline">
            Compare all features →
          </Link>
        </div>

        <div className="mt-12 grid gap-6 lg:grid-cols-3">
          {tiers.map((t) => (
            <article
              key={t.id}
              className={
                t.featured
                  ? "relative rounded-3xl border-2 border-brand bg-card p-8 shadow-xl lg:-translate-y-3"
                  : "rounded-3xl border border-border bg-card p-8"
              }
            >
              {t.featured && (
                <span className="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-brand px-4 py-1 text-[10px] font-bold uppercase tracking-widest text-brand-foreground">
                  Most chosen
                </span>
              )}
              <h3 className="text-sm font-bold uppercase tracking-widest text-ink-muted">
                {t.name}
              </h3>
              <div className="mt-3 flex items-baseline gap-1">
                <span className="font-display text-5xl font-bold">{t.price}</span>
                <span className="text-sm font-medium text-ink-muted">/month CAD</span>
              </div>
              <p className="mt-1 text-sm font-medium text-brand">{t.cap}</p>

              <ul className="mt-6 space-y-3">
                {t.bullets.map((b) => (
                  <li key={b} className="flex items-start gap-2 text-sm text-ink">
                    <Check className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden="true" />
                    {b}
                  </li>
                ))}
              </ul>

              <Link
                to="/plans"
                search={{ tier: t.id }}
                className={
                  t.featured
                    ? "mt-8 inline-flex h-12 w-full items-center justify-center rounded-xl bg-brand font-bold text-brand-foreground hover:bg-brand/90"
                    : "mt-8 inline-flex h-12 w-full items-center justify-center rounded-xl border-2 border-border font-bold text-ink hover:bg-brand-muted"
                }
              >
                Choose {t.name}
              </Link>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ------------------------ Trust strip ------------------------ */

function TrustStrip() {
  const items = [
    "You approve every message before it sends — until you decide otherwise.",
    "Cancel anytime. Your data exports in one click.",
  ];

  return (
    <section className="border-b border-border bg-brand px-6 py-12 text-brand-foreground">
      <div className="mx-auto grid max-w-6xl gap-6 md:grid-cols-3">
        {items.map((line) => (
          <div key={line} className="flex items-start gap-3">
            <ShieldCheck className="mt-0.5 size-5 shrink-0 opacity-80" aria-hidden="true" />
            <p className="text-sm font-medium leading-relaxed">{line}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ------------------------ FAQ ------------------------ */

function FAQSection() {
  const faqs = [
    {
      q: "Will the AI say something that gets me in trouble?",
      a: "Every message is a draft by default. Nothing sends without your tap, and anything sensitive gets handed straight back to you.",
    },
    {
      q: "Can I edit a draft before it goes out?",
      a: "Yes. Edit Draft is on every approval card. Whatever you edit becomes part of how Stoop learns your voice over time.",
    },
    {
      q: "What about Ontario tenant law?",
      a: "Stoop handles maintenance and everyday questions — parking, garbage, rent reminders. Anything beyond that, it hands the conversation back to you with context.",
    },
    {
      q: "Can I pause my subscription?",
      a: "Yes. Pause anytime from your account — your number stays active for inbound texts so nothing breaks for tenants. Resume when you want.",
    },
    {
      q: "Do tenants know they're texting an AI?",
      a: "Every agent-drafted message is sent in your voice but Stoop carries an AI-assistant tag in your dashboard so you always see what was drafted vs. what you wrote. Disclosure to tenants is configurable per property.",
    },
  ];

  return (
    <section id="faq" className="scroll-mt-20 border-b border-border px-6 py-20 lg:py-28">
      <div className="mx-auto max-w-3xl">
        <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand">FAQ</p>
        <h2 className="mt-3 font-display text-4xl font-bold leading-tight tracking-tight md:text-5xl">
          Reasonable questions from skeptical landlords.
        </h2>

        <Accordion type="single" collapsible className="mt-10 w-full">
          {faqs.map((f, i) => (
            <AccordionItem key={f.q} value={`item-${i}`} className="border-border">
              <AccordionTrigger className="py-5 text-left font-display text-lg font-bold hover:no-underline">
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
  );
}

/* ------------------------ Final CTA ------------------------ */

function FinalCTA() {
  return (
    <section className="px-6 py-20 lg:py-28">
      <div className="mx-auto max-w-4xl rounded-3xl border border-border bg-card p-10 text-center shadow-sm md:p-16">
        <Wordmark size="md" />
        <h2 className="mx-auto mt-5 max-w-2xl text-balance font-display text-4xl font-bold leading-tight tracking-tight md:text-5xl">
          Get your evenings back this week.
        </h2>
        <p className="mx-auto mt-4 max-w-xl text-lg text-ink-muted">
          Five-minute setup. Early access: $5/month, locked in for life. Cancel any time without a
          phone call.
        </p>
        <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
          <Button asChild className="h-14 px-7 text-base font-bold">
            <Link to="/early-access">
              Get early access
              <ArrowRight className="size-4" aria-hidden="true" />
            </Link>
          </Button>
        </div>
        <p className="mt-5 text-xs font-medium uppercase tracking-widest text-ink-muted">
          Ontario landlords · No credit card
        </p>
      </div>
    </section>
  );
}
