import { createFileRoute, Link, notFound } from "@tanstack/react-router";
import { ArrowLeft, Settings, TrendingUp, MessageSquare, ChevronRight, Phone } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { SeverityBadge } from "@/components/stoop/SeverityBadge";
import { queue, properties } from "@/lib/mock-app";
import { autonomyModes, getPropertyConfig, propertyConfigs } from "@/lib/mock-property";

export const Route = createFileRoute("/app/properties/$id")({
  head: ({ params }) => ({
    meta: [{ title: "Property — Stoop." }, { name: "robots", content: "noindex" }],
    links: [{ rel: "canonical", href: `/app/properties/${params.id}` }],
  }),
  loader: ({ params }) => {
    const p = properties.find((x) => x.id === params.id);
    if (!p && !propertyConfigs[params.id]) throw notFound();
    return { id: params.id };
  },
  component: PropertyHub,
});

function PropertyHub() {
  const { id } = Route.useParams();
  const property = properties.find((p) => p.id === id) ?? properties[0];
  const cfg = getPropertyConfig(id);
  const mode = autonomyModes.find((m) => m.key === cfg.autonomy);
  const propertyQueue = queue.filter((q) => q.propertyId === id);

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-canvas/95 px-4 py-3 backdrop-blur">
        <Link to="/app/properties" className="flex size-10 items-center justify-center -ml-2">
          <ArrowLeft className="size-5" />
        </Link>
        <span className="rounded-full bg-brand-muted px-3 py-1.5 font-mono text-[10px] font-bold uppercase tracking-widest text-brand">
          {mode?.label}
        </span>
      </header>

      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-5 pb-4 pt-5">
          <p className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
            {property.nickname}
          </p>
          <h1 className="mt-1 font-display text-[26px] leading-tight tracking-tight text-ink">
            {property.address}
          </h1>
          <p className="mt-2 flex items-center gap-1.5 font-mono text-[12px] text-ink-muted">
            <Phone className="size-3" />
            {cfg.phoneNumber}
          </p>
        </div>

        {/* Quick stats */}
        <div className="px-4">
          <div className="grid grid-cols-3 gap-2">
            <Stat label="Pending" value={String(propertyQueue.length)} />
            <Stat label="Approved unchanged" value={String(cfg.trust.approvedUnchanged)} />
            <Stat label="Unchanged rate" value={`${cfg.trust.unchangedRate}%`} positive />
          </div>
        </div>

        {/* Nav links */}
        <section className="px-4 pt-5">
          <h2 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
            Manage
          </h2>
          <div className="overflow-hidden rounded-2xl border border-border bg-card">
            <HubLink
              icon={Settings}
              label="Settings"
              helper="Notifications, rules, vendors, FAQ"
              to="/app/properties/$id/settings"
              id={id}
            />
            <Divider />
            <HubLink
              icon={TrendingUp}
              label="Trust dashboard"
              helper="See your graduation progress"
              to="/app/properties/$id/trust"
              id={id}
            />
          </div>
        </section>

        {/* Recent conversations */}
        <section className="px-4 pt-6">
          <h2 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
            Recent conversations
          </h2>
          {propertyQueue.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-border bg-canvas/50 px-5 py-8 text-center">
              <MessageSquare className="mx-auto size-5 text-ink-muted" />
              <p className="mt-2 text-[13px] text-ink-muted">No conversations yet.</p>
            </div>
          ) : (
            <ul className="space-y-2">
              {propertyQueue.map((q) => (
                <li key={q.id}>
                  <Link
                    to="/app/conversations/$id"
                    params={{ id: q.id }}
                    className="flex items-start gap-3 rounded-2xl border border-border bg-card p-4 hover:border-brand/30"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-display text-[15px] text-ink">{q.tenantFirst}</span>
                        <SeverityBadge severity={q.severity} />
                      </div>
                      <p className="mt-1 line-clamp-2 text-[13px] text-ink-muted">
                        {q.tenantMessage}
                      </p>
                      <p className="mt-1 font-mono text-[10px] uppercase tracking-widest text-ink-muted">
                        {q.receivedAgo}
                      </p>
                    </div>
                    <ChevronRight className="mt-1 size-4 text-ink-muted/70" />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <AppTabBar active="properties" queueCount={queue.length} />
    </PhoneFrame>
  );
}

function HubLink({
  icon: Icon,
  label,
  helper,
  to,
  id,
}: {
  icon: typeof Settings;
  label: string;
  helper: string;
  to: "/app/properties/$id/settings" | "/app/properties/$id/trust";
  id: string;
}) {
  return (
    <Link to={to} params={{ id }} className="flex min-h-14 items-center gap-3 px-4 py-3">
      <div className="flex size-9 items-center justify-center rounded-lg bg-brand-muted text-brand">
        <Icon className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-[14px] font-medium text-ink">{label}</p>
        <p className="text-[12px] text-ink-muted">{helper}</p>
      </div>
      <ChevronRight className="size-4 text-ink-muted/70" />
    </Link>
  );
}

function Divider() {
  return <div className="mx-4 border-t border-border" />;
}

function Stat({ label, value, positive }: { label: string; value: string; positive?: boolean }) {
  return (
    <div className="rounded-xl border border-border bg-card p-3">
      <div
        className={`font-display text-[22px] leading-none tracking-tight ${positive ? "text-brand" : "text-ink"}`}
      >
        {value}
      </div>
      <div className="mt-2 font-mono text-[9.5px] font-bold uppercase tracking-widest text-ink-muted">
        {label}
      </div>
    </div>
  );
}
