import { createFileRoute } from "@tanstack/react-router";
import { MarketingNav } from "@/components/stoop/MarketingNav";
import { SiteFooter } from "@/components/stoop/SiteFooter";

export const Route = createFileRoute("/terms")({
  head: () => ({
    meta: [
      { title: "Terms of Service — Stoop." },
      {
        name: "description",
        content:
          "Terms for using Stoop. — a maintenance triage tool for small landlords. We handle comms, not legal advice.",
      },
      { property: "og:title", content: "Terms of Service — Stoop." },
      {
        property: "og:description",
        content: "Terms of service for Stoop.",
      },
    ],
  }),
  component: TermsPage,
});

function TermsPage() {
  return (
    <div className="min-h-screen bg-canvas">
      <MarketingNav />

      <main className="mx-auto max-w-3xl px-6 py-16 md:py-24">
        <p className="font-mono text-xs font-bold uppercase tracking-widest text-ink-muted">
          Last updated · May 2026
        </p>
        <h1 className="mt-3 font-display text-[44px] leading-[1.05] tracking-tight text-ink md:text-[56px]">
          Terms of Service
        </h1>
        <p className="mt-4 text-base leading-relaxed text-ink-muted">
          You're agreeing to these by using Stoop. Read them. They're short.
        </p>

        <Section title="What Stoop is">
          <p>
            Stoop is a tool that receives SMS from your tenants, drafts replies, and routes urgent
            messages to you. It runs alongside you — not instead of you. You always retain the right
            to take over a conversation.
          </p>
        </Section>

        <Section title="What Stoop is not">
          <ul>
            <li>Not a property manager of record.</li>
            <li>Not a law firm. Nothing we say constitutes legal advice on tenancy law.</li>
            <li>Not a substitute for emergency services. Tenants in danger should call 911.</li>
            <li>Not a tenant screening, payments, or accounting product.</li>
          </ul>
        </Section>

        <Section title="Your account">
          <p>
            You're responsible for keeping your sign-in credentials secure and for the accuracy of
            the information you enter (lease facts, vendor contacts, house rules). The agent's
            replies are only as accurate as what you give it.
          </p>
        </Section>

        <Section title="Tenant communications">
          <p>
            By giving the dedicated property number to a tenant, you confirm you have a reasonable
            basis to communicate with them about the property. Stoop sends a one-time disclosure
            message on first contact explaining how the assistant works.
          </p>
          <p>
            You're responsible for compliance with Ontario's RTA and the CRTC's SMS rules in your
            jurisdiction. We provide a tool — you provide the legal basis.
          </p>
        </Section>

        <Section title="Billing">
          <p>
            Plans are billed monthly or annually in CAD. The 14-day free trial does not require a
            card. After the trial, you can cancel anytime; cancellations stop the next renewal but
            don't refund the current period.
          </p>
          <p>
            Annual plans are non-refundable after 30 days. Pro-rated refunds for service outages
            over 24 hours are available on request.
          </p>
        </Section>

        <Section title="Acceptable use">
          <p>
            Don't use Stoop to harass, threaten, or deceive tenants. Don't use it to impersonate a
            human in jurisdictions that prohibit AI agents from doing so without disclosure. Don't
            try to break our infrastructure.
          </p>
          <p>We may suspend accounts that violate these rules. We'll tell you why.</p>
        </Section>

        <Section title="Liability">
          <p>
            Stoop is provided "as is." We're a small company and our liability is capped at the
            amount you've paid us in the prior 12 months. We're not liable for downstream damages
            from a missed message, an incorrect draft, or a tenant dispute.
          </p>
          <p>
            For emergencies, the agent always advises 911 first. We design for safety, but you
            remain the landlord of record.
          </p>
        </Section>

        <Section title="Changes & disputes">
          <p>
            We may update these Terms. Material changes are emailed 14 days in advance. Disputes are
            governed by the laws of Ontario, Canada.
          </p>
          <p>
            Questions?{" "}
            <a href="mailto:hello@stoop.co" className="underline">
              hello@stoop.co
            </a>
            .
          </p>
        </Section>
      </main>

      <SiteFooter />
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-10 border-t border-border pt-8">
      <h2 className="font-display text-[24px] leading-tight tracking-tight text-ink">{title}</h2>
      <div className="mt-3 space-y-3 text-[15px] leading-relaxed text-ink-muted [&_li]:list-disc [&_ul]:ml-5 [&_ul]:space-y-1.5">
        {children}
      </div>
    </section>
  );
}
