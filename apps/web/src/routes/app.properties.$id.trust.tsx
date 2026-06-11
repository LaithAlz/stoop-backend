import { createFileRoute, Link, notFound } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import { ArrowLeft, Check, Sparkles, Lock } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import {
  autonomyModes,
  getPropertyConfig,
  propertyConfigs,
  type AutonomyModeMeta,
} from "@/lib/mock-property";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/app/properties/$id/trust")({
  head: ({ params }) => ({
    meta: [{ title: "Trust dashboard — Stoop." }, { name: "robots", content: "noindex" }],
    links: [{ rel: "canonical", href: `/app/properties/${params.id}/trust` }],
  }),
  loader: ({ params }) => {
    if (!propertyConfigs[params.id] && params.id !== "main4") throw notFound();
    return { id: params.id };
  },
  component: TrustDashboard,
});

function TrustDashboard() {
  const { id } = Route.useParams();
  const config = getPropertyConfig(id);
  const [bannerOpen, setBannerOpen] = useState(config.trust.graduationReady);
  const [currentMode, setCurrentMode] = useState(config.autonomy);
  const [stats, setStats] = useState(config.trust);
  const [futureMode, setFutureMode] = useState<AutonomyModeMeta | null>(null);

  const currentIdx = autonomyModes.findIndex((m) => m.key === currentMode);
  const nextMode = autonomyModes[currentIdx + 1];

  const accept = () => {
    if (!nextMode) return;
    setCurrentMode(nextMode.key);
    setBannerOpen(false);
    toast.success(`You're now in ${nextMode.label}`, { duration: 2200 });
  };

  const simulate = () => {
    setStats((s) => ({
      ...s,
      approvedUnchanged: s.approvedUnchanged + 1,
      unchangedRate: Math.round(
        ((s.approvedUnchanged + 1) / (s.approvedUnchanged + 1 + s.edited)) * 100,
      ),
    }));
    toast("Mock approval recorded", { duration: 1500 });
  };

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-canvas/95 px-4 py-3 backdrop-blur">
        <Link
          to="/app/properties/$id/settings"
          params={{ id }}
          className="flex size-10 items-center justify-center -ml-2"
        >
          <ArrowLeft className="size-5" />
        </Link>
        <span className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
          Trust · {config.nickname}
        </span>
        <span className="size-10" />
      </header>

      <div className="flex-1 overflow-y-auto pb-10">
        {/* Graduation banner */}
        {bannerOpen && nextMode && (
          <div className="m-4 overflow-hidden rounded-2xl border border-brand/30 bg-gradient-to-br from-brand-muted to-canvas p-5 shadow-sm motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-top-2">
            <div className="flex items-center gap-2 font-mono text-[10px] font-bold uppercase tracking-widest text-brand">
              <span className="relative flex size-2">
                <span className="absolute inline-flex size-full rounded-full bg-brand opacity-60 motion-safe:animate-ping" />
                <span className="relative inline-flex size-2 rounded-full bg-brand" />
              </span>
              Ready to graduate · {config.nickname}
            </div>
            <h2 className="mt-2 font-display text-[24px] leading-tight tracking-tight text-ink">
              You've approved 10 drafts without changing a thing.
            </h2>
            <p className="mt-2 text-[14px] leading-relaxed text-ink-muted">
              Let the agent send routine replies on its own? You'll still approve urgent and
              emergency drafts. Reversible any time in settings.
            </p>
            <div className="mt-4 flex gap-2">
              <Button
                onClick={accept}
                className="h-12 flex-1 bg-brand text-brand-foreground hover:bg-brand/90"
              >
                <Sparkles className="size-4" /> Try {nextMode.label}
              </Button>
              <Button
                variant="outline"
                onClick={() => {
                  setBannerOpen(false);
                  toast("Saved for later", { duration: 1500 });
                }}
                className="h-12 border-brand/30 text-brand"
              >
                Not yet
              </Button>
            </div>
            <button
              type="button"
              onClick={() => {
                setBannerOpen(false);
                toast("We'll remind you in 30 days", { duration: 1800 });
              }}
              className="mt-3 text-[12px] text-ink-muted underline-offset-2 hover:underline"
            >
              Remind me later
            </button>
          </div>
        )}

        {/* Stats */}
        <section className="px-4 pb-2 pt-1">
          <h3 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
            This week
          </h3>
          <div className="grid grid-cols-3 gap-2">
            <StatCard label="Approved unchanged" value={String(stats.approvedUnchanged)} />
            <StatCard label="Edited" value={String(stats.edited)} />
            <StatCard
              label="Unchanged rate"
              value={`${stats.unchangedRate}%`}
              positive={stats.unchangedRate >= 80}
            />
          </div>
          <button
            type="button"
            onClick={simulate}
            className="mt-3 w-full rounded-xl border border-dashed border-border bg-canvas/50 py-2 font-mono text-[11px] uppercase tracking-widest text-ink-muted"
          >
            + Simulate approval
          </button>
        </section>

        {/* Trust ladder */}
        <section className="px-4 pt-5">
          <h3 className="mb-3 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
            Trust ladder
          </h3>
          <ol className="relative">
            {autonomyModes.map((m, i) => {
              const isCurrent = m.key === currentMode;
              const isDone = i < currentIdx;
              const isFuture = i > currentIdx;
              const connectorActive = i <= currentIdx;
              return (
                <li key={m.key} className="relative flex gap-4 pb-6 last:pb-0">
                  {/* connector line */}
                  {i < autonomyModes.length - 1 && (
                    <span
                      aria-hidden
                      className={cn(
                        "absolute left-[15px] top-8 h-[calc(100%-1rem)] w-px",
                        connectorActive ? "bg-brand" : "bg-border",
                      )}
                    />
                  )}
                  {/* dot */}
                  <div className="z-10 flex size-8 shrink-0 items-center justify-center">
                    {isDone ? (
                      <span className="flex size-8 items-center justify-center rounded-full bg-brand text-brand-foreground">
                        <Check className="size-4" />
                      </span>
                    ) : isCurrent ? (
                      <span className="flex size-8 items-center justify-center rounded-full border-2 border-brand bg-canvas">
                        <span className="size-2.5 rounded-full bg-brand" />
                      </span>
                    ) : (
                      <span className="flex size-8 items-center justify-center rounded-full border-2 border-border bg-canvas text-ink-muted">
                        <Lock className="size-3.5" />
                      </span>
                    )}
                  </div>
                  {/* content */}
                  <button
                    type="button"
                    onClick={() => isFuture && setFutureMode(m)}
                    className={cn(
                      "flex-1 rounded-xl border p-3 text-left transition",
                      isCurrent
                        ? "border-brand bg-brand-muted/60"
                        : isDone
                          ? "border-border bg-card"
                          : "border-border bg-card hover:border-brand/40",
                    )}
                  >
                    <div className="flex items-center justify-between">
                      <span
                        className={cn(
                          "font-display text-[16px]",
                          isFuture ? "text-ink-muted" : "text-ink",
                        )}
                      >
                        {m.label}
                      </span>
                      {isCurrent && (
                        <span className="font-mono text-[10px] font-bold uppercase tracking-widest text-brand">
                          Current
                        </span>
                      )}
                    </div>
                    <p
                      className={cn(
                        "mt-1 text-[12.5px] leading-snug",
                        isFuture ? "text-ink-muted/80" : "text-ink-muted",
                      )}
                    >
                      {m.description}
                    </p>
                  </button>
                </li>
              );
            })}
          </ol>
        </section>
      </div>

      <Sheet open={!!futureMode} onOpenChange={(o) => !o && setFutureMode(null)}>
        <SheetContent side="bottom" className="rounded-t-3xl border-t border-border">
          <SheetHeader className="text-left">
            <SheetTitle className="font-display text-[22px]">{futureMode?.label}</SheetTitle>
            <SheetDescription>{futureMode?.description}</SheetDescription>
          </SheetHeader>
          <div className="mt-4 rounded-xl border border-border bg-card p-4">
            <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
              To unlock
            </div>
            <p className="mt-1 text-[14px] text-ink">{futureMode?.requirement}</p>
          </div>
        </SheetContent>
      </Sheet>
    </PhoneFrame>
  );
}

function StatCard({
  label,
  value,
  positive,
}: {
  label: string;
  value: string;
  positive?: boolean;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-3">
      <div
        className={cn(
          "font-display text-[26px] leading-none tracking-tight",
          positive ? "text-brand" : "text-ink",
        )}
      >
        {value}
      </div>
      <div className="mt-2 font-mono text-[9.5px] font-bold uppercase tracking-widest text-ink-muted">
        {label}
      </div>
    </div>
  );
}
