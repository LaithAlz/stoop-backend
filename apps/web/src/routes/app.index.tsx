import { createFileRoute, Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import {
  Bell,
  Search,
  ChevronDown,
  ChevronRight,
  Check,
  AlertOctagon,
  Image as ImageIcon,
  Mail,
  Inbox,
} from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { Wordmark } from "@/components/stoop/Wordmark";
import { SeverityBadge, type Severity } from "@/components/stoop/SeverityBadge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { properties, queue, type QueueItem } from "@/lib/mock-app";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/app/")({
  head: () => ({
    meta: [{ title: "Queue — Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: AppQueuePage,
});

type Filter = "pending" | "emergency" | "urgent" | "routine";

function AppQueuePage() {
  const [propertyId, setPropertyId] = useState<string | "all">("all");
  const [filter, setFilter] = useState<Filter>("pending");

  const scoped = useMemo(
    () => (propertyId === "all" ? queue : queue.filter((q) => q.propertyId === propertyId)),
    [propertyId],
  );

  const counts = useMemo(
    () => ({
      pending: scoped.length,
      emergency: scoped.filter((q) => q.severity === "emergency").length,
      urgent: scoped.filter((q) => q.severity === "urgent").length,
      routine: scoped.filter((q) => q.severity === "routine").length,
    }),
    [scoped],
  );

  const visible = useMemo(() => {
    if (filter === "pending") return scoped;
    return scoped.filter((q) => q.severity === (filter as Severity));
  }, [scoped, filter]);

  const propertyLabel =
    propertyId === "all"
      ? `All ${properties.length} properties`
      : (properties.find((p) => p.id === propertyId)?.nickname ?? "Property");

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col">
        {/* Header */}
        <header className="border-b border-border bg-canvas px-5 pb-3 pt-5">
          <div className="flex items-center justify-between">
            <Wordmark size="sm" />
            <div className="flex items-center gap-1">
              <IconLabelButton icon={<Search className="size-4" />} label="Search" />
              <IconLabelButton
                icon={
                  <span className="relative">
                    <Bell className="size-4" />
                    <span className="absolute -right-1 -top-1 size-2 rounded-full bg-emergency" />
                  </span>
                }
                label="Alerts"
              />
            </div>
          </div>

          <Sheet>
            <SheetTrigger asChild>
              <button className="mt-3 inline-flex min-h-11 items-center gap-2 rounded-full border border-border bg-card px-4 text-sm font-semibold text-ink">
                {propertyLabel}
                <ChevronDown className="size-4 text-ink-muted" aria-hidden="true" />
              </button>
            </SheetTrigger>
            <SheetContent side="bottom" className="rounded-t-3xl">
              <SheetHeader>
                <SheetTitle className="font-display text-2xl">Filter by property</SheetTitle>
              </SheetHeader>
              <div className="mt-4 space-y-1">
                <PropertyOption
                  label={`All ${properties.length} properties`}
                  active={propertyId === "all"}
                  onSelect={() => setPropertyId("all")}
                />
                {properties.map((p) => (
                  <PropertyOption
                    key={p.id}
                    label={p.nickname}
                    sub={p.address}
                    active={propertyId === p.id}
                    onSelect={() => setPropertyId(p.id)}
                  />
                ))}
              </div>
            </SheetContent>
          </Sheet>
        </header>

        {/* Filter pills */}
        <div className="border-b border-border bg-canvas">
          <div className="flex gap-2 overflow-x-auto px-5 py-3 [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden">
            <FilterPill
              label="Pending"
              count={counts.pending}
              active={filter === "pending"}
              onClick={() => setFilter("pending")}
            />
            <FilterPill
              label="Emergency"
              count={counts.emergency}
              tone="emergency"
              active={filter === "emergency"}
              onClick={() => setFilter("emergency")}
            />
            <FilterPill
              label="Urgent"
              count={counts.urgent}
              tone="urgent"
              active={filter === "urgent"}
              onClick={() => setFilter("urgent")}
            />
            <FilterPill
              label="Routine"
              count={counts.routine}
              tone="routine"
              active={filter === "routine"}
              onClick={() => setFilter("routine")}
            />
          </div>
        </div>

        {/* Body */}
        <main className="flex-1 overflow-y-auto bg-surface/40 px-4 py-5">
          {visible.length === 0 ? (
            <EmptyState />
          ) : (
            <>
              <div className="mb-3 flex items-baseline justify-between px-1">
                <h1 className="font-display text-lg font-bold tracking-tight">Needs you now</h1>
                <span className="text-xs font-bold uppercase tracking-widest text-ink-muted">
                  {visible.length} pending
                </span>
              </div>
              <ul className="space-y-3">
                {visible.map((q) => (
                  <li key={q.id}>
                    <QueueRow item={q} />
                  </li>
                ))}
              </ul>

              <DigestCard />
            </>
          )}
        </main>

        <AppTabBar active="queue" queueCount={counts.pending} />
      </div>
    </PhoneFrame>
  );
}

/* --------- Sub-components --------- */

function IconLabelButton({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <button
      type="button"
      className="inline-flex min-h-11 min-w-11 flex-col items-center justify-center gap-0.5 rounded-xl px-2 text-ink hover:bg-brand-muted"
    >
      {icon}
      <span className="text-[9px] font-bold uppercase tracking-widest text-ink-muted">{label}</span>
    </button>
  );
}

function PropertyOption({
  label,
  sub,
  active,
  onSelect,
}: {
  label: string;
  sub?: string;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "flex w-full items-center justify-between rounded-2xl border px-4 py-3 text-left",
        active ? "border-brand bg-brand-muted" : "border-border bg-card",
      )}
    >
      <span>
        <span className="block text-sm font-bold text-ink">{label}</span>
        {sub && <span className="block text-xs text-ink-muted">{sub}</span>}
      </span>
      {active && <Check className="size-4 text-brand" aria-hidden="true" />}
    </button>
  );
}

function FilterPill({
  label,
  count,
  tone,
  active,
  onClick,
}: {
  label: string;
  count: number;
  tone?: "emergency" | "urgent" | "routine";
  active: boolean;
  onClick: () => void;
}) {
  const toneClasses = active
    ? tone === "emergency"
      ? "border-emergency bg-emergency text-white"
      : tone === "urgent"
        ? "border-urgent bg-urgent text-white"
        : tone === "routine"
          ? "border-routine bg-routine text-white"
          : "border-brand bg-brand text-brand-foreground"
    : "border-border bg-card text-ink";

  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "inline-flex min-h-10 shrink-0 items-center gap-2 rounded-full border px-4 text-xs font-bold uppercase tracking-widest",
        toneClasses,
      )}
    >
      {label}
      <span
        className={cn(
          "inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-[10px]",
          active ? "bg-white/20" : "bg-surface text-ink",
        )}
      >
        {count}
      </span>
    </button>
  );
}

function QueueRow({ item }: { item: QueueItem }) {
  const isEmergency = item.severity === "emergency";
  const isUrgent = item.severity === "urgent";

  return (
    <article
      className={cn(
        "overflow-hidden rounded-2xl border bg-card shadow-sm",
        isEmergency
          ? "border-2 border-emergency bg-emergency-soft/60"
          : isUrgent
            ? "border-urgent/30"
            : "border-border",
      )}
    >
      <header className="flex items-center justify-between px-4 pt-3">
        <div className="flex items-center gap-2">
          <SeverityBadge severity={item.severity} />
          {isEmergency && (
            <span className="inline-flex size-2 animate-pulse rounded-full bg-emergency motion-reduce:animate-none" />
          )}
        </div>
        <span className="font-mono text-[11px] font-medium text-ink-muted">{item.receivedAgo}</span>
      </header>

      <div className="px-4 pb-3 pt-2">
        <p className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
          {item.propertyLabel} · {item.unit.split("·")[1]?.trim() ?? item.unit}
        </p>
        <p
          className={cn(
            "mt-2 line-clamp-2 font-display text-[17px] leading-snug text-ink",
            isEmergency && "font-bold",
          )}
        >
          “{item.tenantMessage}”
        </p>
        {item.hasPhoto && (
          <p className="mt-2 inline-flex items-center gap-1 text-[11px] font-semibold text-ink-muted">
            <ImageIcon className="size-3" aria-hidden="true" />
            Photo attached
          </p>
        )}
      </div>

      <footer className="border-t border-border bg-canvas/70 p-3">
        {isEmergency ? (
          <Link
            to="/app/conversations/$id/emergency"
            params={{ id: item.id }}
            className="flex h-14 w-full items-center justify-center gap-2 rounded-xl bg-emergency text-base font-bold text-white"
          >
            <AlertOctagon className="size-4" aria-hidden="true" />
            Open emergency
          </Link>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            <Link
              to="/app/conversations/$id"
              params={{ id: item.id }}
              className="flex h-12 items-center justify-center rounded-xl border border-border bg-card text-sm font-bold text-ink"
            >
              Review
            </Link>
            <button className="flex h-12 items-center justify-center gap-1.5 rounded-xl bg-brand text-sm font-bold text-brand-foreground">
              <Check className="size-4" aria-hidden="true" />
              Approve
            </button>
          </div>
        )}
      </footer>
    </article>
  );
}

function DigestCard() {
  return (
    <div className="mt-6 flex items-start gap-3 rounded-2xl border border-border bg-card p-4">
      <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-routine-soft text-routine">
        <Mail className="size-5" aria-hidden="true" />
      </div>
      <div className="flex-1">
        <p className="text-sm font-bold text-ink">Daily digest at 6:00 PM</p>
        <p className="mt-1 text-xs leading-relaxed text-ink-muted">
          Routine chatter gets summarized in one email so you can stay out of the queue when it
          doesn't need you.
        </p>
      </div>
      <ChevronRight className="size-4 text-ink-muted" aria-hidden="true" />
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex h-full flex-col items-center justify-center px-6 pt-20 text-center">
      <div className="flex size-16 items-center justify-center rounded-2xl bg-brand-muted text-brand">
        <Inbox className="size-7" aria-hidden="true" />
      </div>
      <h2 className="mt-6 font-display text-2xl font-bold tracking-tight">You're caught up.</h2>
      <p className="mt-2 text-sm text-ink-muted">Last activity 14 min ago.</p>
      <p className="mt-1 text-sm text-ink-muted">Next digest: today at 6:00 PM.</p>
    </div>
  );
}
