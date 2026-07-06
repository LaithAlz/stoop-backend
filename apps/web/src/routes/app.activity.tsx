import { createFileRoute, Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { Check, Pencil, AlertOctagon, PhoneCall, Wrench, ChevronRight } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { queue } from "@/lib/mock-app";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/app/activity")({
  head: () => ({
    meta: [{ title: "Activity — Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: ActivityPage,
});

type EventKind = "approved" | "edited" | "emergency" | "vendor" | "call";

interface ActivityEvent {
  id: string;
  kind: EventKind;
  when: string;
  day: "Today" | "Yesterday" | "Earlier";
  property: string;
  title: string;
  detail: string;
  conversationId?: string;
}

const events: ActivityEvent[] = [
  {
    id: "a1",
    kind: "emergency",
    when: "2 min ago",
    day: "Today",
    property: "123 Main #4",
    title: "Emergency escalated — Maria",
    detail: "Water flooding from the ceiling. Draft sent to Mike's Plumbing.",
    conversationId: "c-maria-flood",
  },
  {
    id: "a2",
    kind: "approved",
    when: "18 min ago",
    day: "Today",
    property: "Walmer Unit 2",
    title: "Approved unchanged — Jesse",
    detail: "Slow leak under kitchen sink. Mike's Plumbing dispatched.",
    conversationId: "c-jesse-sink",
  },
  {
    id: "a3",
    kind: "approved",
    when: "1 hr ago",
    day: "Today",
    property: "Stoop House",
    title: "Approved unchanged — Sam",
    detail: "Garbage day question. Routine reply sent.",
    conversationId: "c-sam-garbage",
  },
  {
    id: "a4",
    kind: "vendor",
    when: "Yesterday · 4:12 PM",
    day: "Yesterday",
    property: "123 Main #4",
    title: "Vendor confirmed",
    detail: "Mike's Plumbing booked for Tue 9–11 AM. Tenant notified.",
  },
  {
    id: "a5",
    kind: "edited",
    when: "Yesterday · 11:03 AM",
    day: "Yesterday",
    property: "Walmer Unit 2",
    title: "Edited draft before sending",
    detail: 'You changed "this afternoon" → "tomorrow morning".',
  },
  {
    id: "a6",
    kind: "call",
    when: "Mon · 9:48 PM",
    day: "Earlier",
    property: "123 Main #4",
    title: "Tap-to-call placed",
    detail: "You called Maria after the smoke alarm message.",
  },
];

const filters: { key: "all" | EventKind; label: string }[] = [
  { key: "all", label: "All" },
  { key: "approved", label: "Approved" },
  { key: "edited", label: "Edited" },
  { key: "emergency", label: "Emergencies" },
];

function ActivityPage() {
  const [filter, setFilter] = useState<(typeof filters)[number]["key"]>("all");

  const filtered = useMemo(
    () => (filter === "all" ? events : events.filter((e) => e.kind === filter)),
    [filter],
  );

  const grouped = useMemo(() => {
    const map = new Map<string, ActivityEvent[]>();
    for (const e of filtered) {
      if (!map.has(e.day)) map.set(e.day, []);
      map.get(e.day)!.push(e);
    }
    return Array.from(map.entries());
  }, [filtered]);

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 border-b border-border bg-canvas/95 px-5 py-4 backdrop-blur">
        <p className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
          Activity
        </p>
        <h1 className="font-display text-[26px] leading-tight tracking-tight text-ink">
          Last 7 days
        </h1>
        <div className="-mx-1 mt-3 flex gap-1.5 overflow-x-auto pb-1">
          {filters.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setFilter(f.key)}
              className={cn(
                "shrink-0 rounded-full px-3 py-1.5 font-mono text-[10px] font-bold uppercase tracking-widest transition",
                filter === f.key ? "bg-brand text-brand-foreground" : "bg-brand-muted text-brand",
              )}
            >
              {f.label}
            </button>
          ))}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {grouped.length === 0 ? (
          <p className="px-2 py-12 text-center text-[13px] text-ink-muted">Nothing here yet.</p>
        ) : (
          grouped.map(([day, items]) => (
            <section key={day} className="mb-6 last:mb-0">
              <h2 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                {day}
              </h2>
              <ul className="space-y-2">
                {items.map((e) => (
                  <li key={e.id}>
                    <EventRow event={e} />
                  </li>
                ))}
              </ul>
            </section>
          ))
        )}
      </div>

      {/* Activity is no longer one of the four tabs (Tab IA decision,
          2026-07-06) — this route is still reachable by URL, it just has
          no corresponding tab to highlight. */}
      <AppTabBar queueCount={queue.length} />
    </PhoneFrame>
  );
}

const META: Record<EventKind, { icon: typeof Check; tint: string }> = {
  approved: { icon: Check, tint: "bg-brand-muted text-brand" },
  edited: { icon: Pencil, tint: "bg-urgent-soft text-urgent" },
  emergency: { icon: AlertOctagon, tint: "bg-emergency-soft text-emergency" },
  vendor: { icon: Wrench, tint: "bg-routine-soft text-routine" },
  call: { icon: PhoneCall, tint: "bg-brand-muted text-brand" },
};

function EventRow({ event }: { event: ActivityEvent }) {
  const meta = META[event.kind];
  const Icon = meta.icon;
  const body = (
    <div className="flex items-start gap-3 rounded-2xl border border-border bg-card p-4">
      <div className={cn("flex size-9 shrink-0 items-center justify-center rounded-xl", meta.tint)}>
        <Icon className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <p className="truncate font-display text-[15px] text-ink">{event.title}</p>
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-widest text-ink-muted">
            {event.when}
          </span>
        </div>
        <p className="mt-1 text-[13px] leading-snug text-ink-muted">{event.detail}</p>
        <p className="mt-2 font-mono text-[10px] uppercase tracking-widest text-ink-muted">
          {event.property}
        </p>
      </div>
      {event.conversationId && <ChevronRight className="mt-1 size-4 text-ink-muted/70" />}
    </div>
  );
  return event.conversationId ? (
    <Link to="/app/conversations/$id" params={{ id: event.conversationId }} className="block">
      {body}
    </Link>
  ) : (
    body
  );
}
