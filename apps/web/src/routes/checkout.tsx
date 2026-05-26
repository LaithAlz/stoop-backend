import { createFileRoute, Link } from "@tanstack/react-router";
import { z } from "zod";
import { MarketingNav } from "@/components/stoop/MarketingNav";
import { SiteFooter } from "@/components/stoop/SiteFooter";

const searchSchema = z.object({
  plan: z.enum(["solo", "standard", "pro"]).catch("standard").default("standard"),
});

export const Route = createFileRoute("/checkout")({
  validateSearch: (s) => searchSchema.parse(s),
  head: () => ({
    meta: [
      { title: "Checkout — Stoop." },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: CheckoutPage,
});

function CheckoutPage() {
  const { plan } = Route.useSearch();
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <MarketingNav />
      <main className="mx-auto max-w-2xl px-6 py-24 text-center">
        <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand">Checkout</p>
        <h1 className="mt-3 font-display text-4xl font-bold tracking-tight">
          Stripe checkout — coming soon
        </h1>
        <p className="mt-4 text-ink-muted">
          You picked the <span className="font-semibold text-ink">{plan}</span> plan. Hosted Stripe
          checkout will live here. For now, start your trial in the app.
        </p>
        <Link
          to="/plans"
          className="mt-8 inline-flex h-12 items-center rounded-xl border border-border bg-card px-6 text-sm font-bold hover:bg-brand-muted"
        >
          Back to plans
        </Link>
      </main>
      <SiteFooter />
    </div>
  );
}
