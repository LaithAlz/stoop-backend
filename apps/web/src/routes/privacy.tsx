import { createFileRoute } from "@tanstack/react-router";
import { MarketingNav } from "@/components/stoop/MarketingNav";
import { SiteFooter } from "@/components/stoop/SiteFooter";

export const Route = createFileRoute("/privacy")({
  head: () => ({
    meta: [
      { title: "Privacy Policy — Stoop." },
      {
        name: "description",
        content:
          "How Stoop. handles landlord and tenant data — what we collect, what we don't, and your rights.",
      },
      { property: "og:title", content: "Privacy Policy — Stoop." },
      {
        property: "og:description",
        content: "Privacy practices for Stoop. maintenance triage.",
      },
    ],
  }),
  component: PrivacyPage,
});

function PrivacyPage() {
  return (
    <div className="min-h-screen bg-canvas">
      <MarketingNav />

      <main className="mx-auto max-w-3xl px-6 py-16 md:py-24">
        <p className="font-mono text-xs font-bold uppercase tracking-widest text-ink-muted">
          Last updated · May 2026
        </p>
        <h1 className="mt-3 font-display text-[44px] leading-[1.05] tracking-tight text-ink md:text-[56px]">
          Privacy Policy
        </h1>
        <p className="mt-4 text-base leading-relaxed text-ink-muted">
          Plain English version. The long form lives in our Terms. If anything here is unclear,
          email{" "}
          <a href="mailto:hello@stoop.co" className="underline">
            hello@stoop.co
          </a>
          .
        </p>

        <Section title="What we collect">
          <p>
            <strong>From landlords:</strong> name, email, phone, property addresses, lease facts you
            enter, vendor contact info, the messages you approve or edit.
          </p>
          <p>
            <strong>From tenants:</strong> the SMS messages they send to the dedicated property
            number, plus the phone number itself. We do <em>not</em> create tenant accounts.
          </p>
          <p>
            <strong>Technical:</strong> standard server logs, device type, app version. We do not
            use third-party advertising trackers.
          </p>
        </Section>

        <Section title="What we don't collect">
          <ul>
            <li>Tenant credit history, identity documents, or background checks.</li>
            <li>Location data beyond the property address you enter.</li>
            <li>Anything from your phone's contacts, camera roll, or calendar.</li>
          </ul>
        </Section>

        <Section title="How we use it">
          <p>
            To run the service — receive tenant messages, draft replies, route emergencies to you,
            and keep a record of the conversation.
          </p>
          <p>
            We use a large language model from a sub-processor to draft replies. Messages sent to
            that sub-processor are not used to train their public models.
          </p>
        </Section>

        <Section title="Who we share it with">
          <p>
            Sub-processors we rely on: SMS provider (Twilio), hosting (Cloudflare, Supabase), LLM
            provider (OpenAI / Anthropic), payment processor (Stripe). Full list on request.
          </p>
          <p>We do not sell your data. We do not share it for advertising.</p>
        </Section>

        <Section title="Your rights">
          <p>
            Export your data, delete your account, or request a copy of what we hold at any time.
            Email{" "}
            <a href="mailto:privacy@stoop.co" className="underline">
              privacy@stoop.co
            </a>
            . We respond within 7 days.
          </p>
          <p>
            Under PIPEDA (Canada) and GDPR (EU), you can ask us to correct, delete, or restrict use
            of your personal information.
          </p>
        </Section>

        <Section title="Retention">
          <p>
            We keep conversation history for as long as your property is active. If you delete a
            property, its conversations are removed within 30 days. Account closure deletes
            everything within 30 days, except records we're legally required to retain (e.g.,
            billing).
          </p>
        </Section>

        <Section title="Security">
          <p>
            Data is encrypted in transit (TLS) and at rest. Access is limited to engineers who need
            it for support. We notify affected users within 72 hours of a confirmed breach.
          </p>
        </Section>

        <Section title="Changes">
          <p>
            Material changes are emailed to active accounts at least 14 days before they take
            effect. Non-material edits update the "Last updated" date.
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
