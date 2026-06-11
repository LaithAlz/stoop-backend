import { createFileRoute, Link, notFound } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import {
  ArrowLeft,
  ChevronRight,
  Plus,
  Phone,
  Share2,
  RefreshCw,
  Pencil,
  Trash2,
  ShieldCheck,
  TrendingUp,
} from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  autonomyModes,
  getPropertyConfig,
  propertyConfigs,
  type AutonomyMode,
} from "@/lib/mock-property";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/app/properties/$id/settings")({
  head: ({ params }) => ({
    meta: [{ title: "Property settings — Stoop." }, { name: "robots", content: "noindex" }],
    links: [{ rel: "canonical", href: `/app/properties/${params.id}/settings` }],
  }),
  loader: ({ params }) => {
    if (!propertyConfigs[params.id] && params.id !== "main4") throw notFound();
    return { id: params.id };
  },
  component: PropertySettings,
});

const modeLabel = (m: AutonomyMode) => autonomyModes.find((x) => x.key === m)?.label ?? m;

function PropertySettings() {
  const { id } = Route.useParams();
  const initial = getPropertyConfig(id);
  const [config, setConfig] = useState(initial);
  const [modeSheet, setModeSheet] = useState(false);
  const [pendingMode, setPendingMode] = useState<AutonomyMode | null>(null);
  const [pauseConfirm, setPauseConfirm] = useState(false);
  const [paused, setPaused] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);

  const update = <K extends keyof typeof config>(patch: Partial<typeof config>) => {
    setConfig((c) => ({ ...c, ...patch }));
  };
  const inlineConfirm = (msg: string) => toast.success(msg, { duration: 1800 });

  const confirmModeChange = () => {
    if (!pendingMode) return;
    update({ autonomy: pendingMode });
    inlineConfirm(`Mode set to ${modeLabel(pendingMode)}`);
    setPendingMode(null);
    setModeSheet(false);
  };

  const progressPct = Math.min(
    100,
    Math.round((config.streak.current / config.streak.target) * 100),
  );

  return (
    <PhoneFrame>
      {/* Header */}
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-canvas/95 px-4 py-3 backdrop-blur">
        <Link to="/app" className="flex size-10 items-center justify-center -ml-2">
          <ArrowLeft className="size-5" />
        </Link>
        <button
          type="button"
          onClick={() => setModeSheet(true)}
          className="rounded-full bg-brand-muted px-3 py-1.5 font-mono text-[10px] font-bold uppercase tracking-widest text-brand"
        >
          {modeLabel(config.autonomy)}
        </button>
      </header>

      <div className="flex-1 overflow-y-auto pb-24">
        {/* Property header */}
        <div className="px-5 pb-4 pt-5">
          <h1 className="font-display text-[26px] leading-tight tracking-tight text-ink">
            {config.address}
          </h1>
          <p className="mt-1 font-mono text-[11px] uppercase tracking-widest text-ink-muted">
            Active since {config.activeSince}
          </p>
        </div>

        {/* Autonomy Mode card */}
        <Section>
          <div className="rounded-2xl border border-brand/15 bg-brand-muted/60 p-5">
            <div className="flex items-center gap-2 font-mono text-[10px] font-bold uppercase tracking-widest text-brand">
              <ShieldCheck className="size-3.5" /> Autonomy
            </div>
            <p className="mt-2 font-display text-[20px] leading-snug text-ink">
              {config.autonomy === "shadow"
                ? "You're approving every message before it sends."
                : `Mode: ${modeLabel(config.autonomy)}.`}
            </p>

            <div className="mt-4">
              <div className="flex items-baseline justify-between">
                <span className="font-mono text-[11px] uppercase tracking-widest text-ink-muted">
                  Progress to graduation
                </span>
                <span className="font-mono text-[12px] font-bold text-brand">
                  {config.streak.current} / {config.streak.target}
                </span>
              </div>
              <div className="mt-2 h-2 overflow-hidden rounded-full bg-canvas">
                <div
                  className="h-full rounded-full bg-brand transition-all"
                  style={{ width: `${progressPct}%` }}
                />
              </div>
              <p className="mt-2 text-[12px] text-ink-muted">approve-without-edit streak</p>
            </div>

            <div className="mt-4 flex gap-2">
              <Button
                onClick={() => setModeSheet(true)}
                className="h-12 flex-1 bg-brand text-brand-foreground hover:bg-brand/90"
              >
                Change autonomy mode
              </Button>
              <Button asChild variant="outline" className="h-12 border-brand/30 text-brand">
                <Link to="/app/properties/$id/trust" params={{ id }}>
                  <TrendingUp className="size-4" />
                </Link>
              </Button>
            </div>
          </div>
        </Section>

        {/* Notifications */}
        <Section title="Notifications">
          <SettingsCard>
            <ToggleRow
              label="Send me emergency alerts at any time"
              helper="Wakes your phone, even on silent"
              checked={config.notify.emergencyAnytime}
              onCheckedChange={(v) => {
                update({ notify: { ...config.notify, emergencyAnytime: v } });
                inlineConfirm(v ? "Emergency alerts on" : "Emergency alerts off");
              }}
            />
            <SelectRow
              label="Send urgent alerts within"
              value={config.notify.urgentWindow}
              options={["15 min", "30 min", "1 hr", "2 hrs", "4 hrs"]}
              onChange={(v) => {
                update({ notify: { ...config.notify, urgentWindow: v } });
                inlineConfirm("Urgent window updated");
              }}
            />
            <SelectRow
              label="Send routine messages in a daily digest"
              helper="All routine items, one email"
              value={config.notify.digestTime}
              options={["8:00 AM", "12:00 PM", "5:00 PM", "6:00 PM", "9:00 PM"]}
              onChange={(v) => {
                update({ notify: { ...config.notify, digestTime: v } });
                inlineConfirm("Digest time updated");
              }}
            />
            <ValueRow
              label="Quiet hours for non-emergency"
              value={`${config.notify.quietHours.start} – ${config.notify.quietHours.end}`}
              onEdit={() => inlineConfirm("Editor opens in a sheet (mock)")}
              last
            />
          </SettingsCard>
        </Section>

        {/* House Rules */}
        <Section title="House Rules">
          <SettingsCard>
            <ValueRow
              label="Pets"
              value={config.rules.pets}
              onEdit={() => inlineConfirm("Edit pets")}
            />
            <ValueRow
              label="Smoking"
              value={config.rules.smoking}
              onEdit={() => inlineConfirm("Edit smoking")}
            />
            <ValueRow
              label="Parking spot"
              value={config.rules.parking}
              onEdit={() => inlineConfirm("Edit parking")}
            />
            <ValueRow
              label="Quiet hours for tenants"
              value={`${config.rules.quietHours.start} – ${config.rules.quietHours.end}`}
              onEdit={() => inlineConfirm("Edit quiet hours")}
            />
            <ValueRow
              label="Guests"
              value={config.rules.guests}
              onEdit={() => inlineConfirm("Edit guests")}
              last
            />
          </SettingsCard>
        </Section>

        {/* Lease Facts */}
        <Section title="Lease Facts">
          <SettingsCard>
            <ValueRow
              label="Monthly rent"
              value={config.lease.rent}
              onEdit={() => inlineConfirm("Edit rent")}
            />
            <ValueRow
              label="Rent due day"
              value={config.lease.dueDay}
              onEdit={() => inlineConfirm("Edit due day")}
            />
            <ValueRow
              label="Security deposit"
              value={config.lease.deposit}
              onEdit={() => inlineConfirm("Edit deposit")}
            />
            <ValueRow
              label="Lease end"
              value={
                config.lease.monthToMonth ? (
                  <span className="rounded-full bg-routine-soft px-2 py-0.5 text-[11px] font-bold text-routine">
                    Month-to-month
                  </span>
                ) : (
                  config.lease.end
                )
              }
              onEdit={() => inlineConfirm("Edit lease end")}
              last
            />
          </SettingsCard>
        </Section>

        {/* Vendors */}
        <Section title="Vendors">
          <div className="space-y-2">
            {config.vendors.map((v) => (
              <div
                key={v.id}
                className="flex items-center justify-between rounded-xl border border-border bg-card p-4"
              >
                <div className="min-w-0">
                  <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                    {v.type}
                  </div>
                  <div className="mt-0.5 font-display text-[16px] text-ink">{v.name}</div>
                  <div className="mt-0.5 flex items-center gap-2 text-[13px] text-ink-muted">
                    <Phone className="size-3" /> {v.phone}
                    {v.afterHours && (
                      <span className="rounded-full bg-brand-muted px-1.5 py-0.5 text-[10px] font-bold text-brand">
                        After-hours
                      </span>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => inlineConfirm("Edit vendor (mock)")}
                  className="flex size-10 items-center justify-center rounded-full text-ink-muted hover:bg-brand-muted"
                  aria-label="Edit vendor"
                >
                  <Pencil className="size-4" />
                </button>
              </div>
            ))}
            <AddRow label="Add vendor" onClick={() => inlineConfirm("Add vendor (mock)")} />
          </div>
        </Section>

        {/* Custom FAQ */}
        <Section title="Custom FAQ">
          <div className="space-y-2">
            {config.faq.map((f) => (
              <div key={f.id} className="rounded-xl border border-border bg-card p-4">
                <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                  Q
                </div>
                <p className="mt-1 font-display text-[15px] text-ink">{f.q}</p>
                <div className="mt-3 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                  A
                </div>
                <p className="mt-1 text-[14px] text-ink">{f.a}</p>
              </div>
            ))}
            <AddRow label="Add question" onClick={() => inlineConfirm("Add FAQ (mock)")} />
          </div>
        </Section>

        {/* Severity overrides */}
        <Section title="Severity Overrides">
          {config.overrides.length === 0 ? (
            <p className="px-1 text-[13px] text-ink-muted">
              No overrides. The agent uses default severity rules.
            </p>
          ) : null}
          <div className="mt-2">
            <AddRow label="Add override" onClick={() => inlineConfirm("Add override (mock)")} />
          </div>
        </Section>

        {/* Property number */}
        <Section title="Property Number">
          <div className="rounded-2xl border border-border bg-card p-5">
            <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
              Dedicated number
            </div>
            <p className="mt-1 font-display text-[22px] text-ink">{config.phoneNumber}</p>
            <div className="mt-4 flex flex-col gap-2">
              <Button
                onClick={() => inlineConfirm("Share sheet opened (mock)")}
                className="h-12 bg-brand text-brand-foreground hover:bg-brand/90"
              >
                <Share2 className="size-4" /> Share with a tenant
              </Button>
              <Button
                variant="outline"
                onClick={() => inlineConfirm("Number rotated (mock)")}
                className="h-12 border-border text-ink"
              >
                <RefreshCw className="size-4" /> Get a new number
              </Button>
            </div>
          </div>
        </Section>

        {/* Danger zone */}
        <Section title="Danger Zone" muted>
          <SettingsCard muted>
            <div className="flex min-h-14 items-center justify-between gap-4 px-4 py-3">
              <div className="min-w-0">
                <p className="text-[14px] text-ink-muted">Pause the agent for this property</p>
                <p className="text-[12px] text-ink-muted/80">
                  Incoming texts queue until you resume.
                </p>
              </div>
              <Switch
                checked={paused}
                onCheckedChange={() => setPauseConfirm(true)}
                aria-label="Pause agent"
              />
            </div>
            <Divider />
            <button
              type="button"
              onClick={() => inlineConfirm("CSV export queued (mock)")}
              className="flex min-h-14 w-full items-center justify-between gap-4 px-4 py-3 text-left"
            >
              <span className="text-[14px] text-ink-muted">Export conversation history</span>
              <ChevronRight className="size-4 text-ink-muted/70" />
            </button>
            <Divider />
            <button
              type="button"
              onClick={() => setDeleteConfirm(true)}
              className="flex min-h-14 w-full items-center justify-between gap-4 px-4 py-3 text-left"
            >
              <span className="flex items-center gap-2 text-[14px] font-medium text-emergency">
                <Trash2 className="size-4" /> Delete this property
              </span>
              <ChevronRight className="size-4 text-emergency/70" />
            </button>
          </SettingsCard>
        </Section>
      </div>

      {/* Mode change sheet */}
      <Sheet open={modeSheet} onOpenChange={setModeSheet}>
        <SheetContent side="bottom" className="rounded-t-3xl border-t border-border">
          <SheetHeader className="text-left">
            <SheetTitle className="font-display text-[22px]">Choose autonomy mode</SheetTitle>
            <SheetDescription>
              Reversible any time. Tenants never know the difference.
            </SheetDescription>
          </SheetHeader>
          <div className="mt-4 space-y-2">
            {autonomyModes.map((m) => {
              const active = m.key === config.autonomy;
              return (
                <button
                  key={m.key}
                  type="button"
                  onClick={() => setPendingMode(m.key)}
                  className={cn(
                    "w-full rounded-2xl border p-4 text-left transition",
                    active
                      ? "border-brand bg-brand-muted/60"
                      : "border-border bg-card hover:border-brand/40",
                  )}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-display text-[17px] text-ink">{m.label}</span>
                    {active && (
                      <span className="font-mono text-[10px] font-bold uppercase tracking-widest text-brand">
                        Current
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-[13px] text-ink-muted">{m.description}</p>
                </button>
              );
            })}
          </div>
        </SheetContent>
      </Sheet>

      {/* Confirm mode change */}
      <AlertDialog open={!!pendingMode} onOpenChange={(o) => !o && setPendingMode(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="font-display">
              Switch to {pendingMode ? modeLabel(pendingMode) : ""}?
            </AlertDialogTitle>
            <AlertDialogDescription>
              {autonomyModes.find((m) => m.key === pendingMode)?.description}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmModeChange}
              className="bg-brand text-brand-foreground hover:bg-brand/90"
            >
              Confirm
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Pause confirm */}
      <AlertDialog open={pauseConfirm} onOpenChange={setPauseConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="font-display">
              {paused ? "Resume the agent?" : "Pause the agent?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {paused
                ? "The agent will start drafting replies again."
                : "Tenants can still text. Their messages queue until you resume."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setPaused((p) => !p);
                inlineConfirm(paused ? "Agent resumed" : "Agent paused");
                setPauseConfirm(false);
              }}
            >
              {paused ? "Resume" : "Pause"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete confirm */}
      <AlertDialog open={deleteConfirm} onOpenChange={setDeleteConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="font-display">Delete this property?</AlertDialogTitle>
            <AlertDialogDescription>
              This releases the dedicated number and removes all conversation history. This can't be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-emergency text-destructive-foreground hover:bg-emergency/90"
              onClick={() => {
                inlineConfirm("Property deleted (mock)");
                setDeleteConfirm(false);
              }}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PhoneFrame>
  );
}

// — primitives —

function Section({
  title,
  children,
  muted,
}: {
  title?: string;
  children: React.ReactNode;
  muted?: boolean;
}) {
  return (
    <section className={cn("px-5 pb-5 pt-3", muted && "opacity-80")}>
      {title && (
        <h2 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
          {title}
        </h2>
      )}
      {children}
    </section>
  );
}

function SettingsCard({ children, muted }: { children: React.ReactNode; muted?: boolean }) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-2xl border bg-card",
        muted ? "border-border/70" : "border-border",
      )}
    >
      {children}
    </div>
  );
}

function Divider() {
  return <div className="mx-4 border-t border-border" />;
}

function ToggleRow({
  label,
  helper,
  checked,
  onCheckedChange,
}: {
  label: string;
  helper?: string;
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
}) {
  return (
    <>
      <div className="flex min-h-14 items-center justify-between gap-4 px-4 py-3">
        <div className="min-w-0">
          <p className="text-[14px] text-ink">{label}</p>
          {helper && <p className="mt-0.5 text-[12px] text-ink-muted">{helper}</p>}
        </div>
        <Switch checked={checked} onCheckedChange={onCheckedChange} aria-label={label} />
      </div>
      <Divider />
    </>
  );
}

function SelectRow({
  label,
  helper,
  value,
  options,
  onChange,
  last,
}: {
  label: string;
  helper?: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
  last?: boolean;
}) {
  return (
    <>
      <div className="flex min-h-14 items-center justify-between gap-4 px-4 py-3">
        <div className="min-w-0 flex-1">
          <p className="text-[14px] text-ink">{label}</p>
          {helper && <p className="mt-0.5 text-[12px] text-ink-muted">{helper}</p>}
        </div>
        <Select value={value} onValueChange={onChange}>
          <SelectTrigger className="h-9 w-auto min-w-[110px] border-border bg-canvas font-mono text-[12px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {options.map((o) => (
              <SelectItem key={o} value={o} className="font-mono text-[12px]">
                {o}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {!last && <Divider />}
    </>
  );
}

function ValueRow({
  label,
  value,
  onEdit,
  last,
}: {
  label: string;
  value: React.ReactNode;
  onEdit?: () => void;
  last?: boolean;
}) {
  return (
    <>
      <button
        type="button"
        onClick={onEdit}
        className="flex min-h-14 w-full items-center justify-between gap-4 px-4 py-3 text-left"
      >
        <span className="text-[14px] text-ink">{label}</span>
        <span className="flex items-center gap-2 text-[13px] text-ink-muted">
          {value}
          <ChevronRight className="size-4 text-ink-muted/70" />
        </span>
      </button>
      {!last && <Divider />}
    </>
  );
}

function AddRow({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-14 w-full items-center justify-center gap-2 rounded-xl border border-dashed border-border bg-canvas/50 text-[14px] font-medium text-brand"
    >
      <Plus className="size-4" /> {label}
    </button>
  );
}
