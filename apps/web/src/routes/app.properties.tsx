import { createFileRoute, Link } from "@tanstack/react-router";
import { Home, Plus, ChevronRight } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { Button } from "@/components/ui/button";
import { properties, queue } from "@/lib/mock-app";
import { getPropertyConfig } from "@/lib/mock-property";
import { autonomyModes } from "@/lib/mock-property";

export const Route = createFileRoute("/app/properties")({
  head: () => ({
    meta: [{ title: "Properties — Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: PropertiesPage,
});

function PropertiesPage() {
  const pendingByProp = queue.reduce<Record<string, number>>((acc, q) => {
    acc[q.propertyId] = (acc[q.propertyId] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 border-b border-border bg-canvas/95 px-5 py-4 backdrop-blur">
        <div className="flex items-center justify-between">
          <div>
            <p className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
              Properties
            </p>
            <h1 className="font-display text-[26px] leading-tight tracking-tight text-ink">
              {properties.length} active
            </h1>
          </div>
          <Button
            size="sm"
            className="h-10 bg-brand text-brand-foreground hover:bg-brand/90"
            asChild
          >
            <Link to="/onboarding">
              <Plus className="size-4" /> Add
            </Link>
          </Button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        <ul className="space-y-3">
          {properties.map((p) => {
            const cfg = getPropertyConfig(p.id);
            const mode = autonomyModes.find((m) => m.key === cfg.autonomy);
            const pending = pendingByProp[p.id] ?? 0;
            return (
              <li key={p.id}>
                <Link
                  to="/app/properties/$id"
                  params={{ id: p.id }}
                  className="block rounded-2xl border border-border bg-card p-4 transition hover:border-brand/30"
                >
                  <div className="flex items-start gap-3">
                    <div className="mt-0.5 flex size-10 shrink-0 items-center justify-center rounded-xl bg-brand-muted text-brand">
                      <Home className="size-5" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="font-display text-[17px] leading-tight text-ink">
                        {p.nickname}
                      </div>
                      <div className="mt-0.5 truncate text-[13px] text-ink-muted">{p.address}</div>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <span className="rounded-full bg-brand-muted px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest text-brand">
                          {mode?.label ?? "Shadow"}
                        </span>
                        {pending > 0 && (
                          <span className="rounded-full bg-urgent-soft px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest text-urgent">
                            {pending} pending
                          </span>
                        )}
                      </div>
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-ink-muted/70" />
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>

        <Link
          to="/onboarding"
          className="mt-4 flex min-h-14 items-center justify-center gap-2 rounded-2xl border border-dashed border-border bg-canvas/50 text-[14px] font-medium text-brand"
        >
          <Plus className="size-4" /> Add another property
        </Link>
      </div>

      <AppTabBar active="properties" queueCount={queue.length} />
    </PhoneFrame>
  );
}
