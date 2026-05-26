import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import {
  ChevronRight,
  CreditCard,
  Bell,
  Shield,
  HelpCircle,
  LogOut,
  Sparkles,
} from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
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
import { queue } from "@/lib/mock-app";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/app/account")({
  head: () => ({
    meta: [
      { title: "Account — Stoop." },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: AccountPage,
});

function AccountPage() {
  const [push, setPush] = useState(true);
  const [email, setEmail] = useState(true);
  const [logoutOpen, setLogoutOpen] = useState(false);

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 border-b border-border bg-canvas/95 px-5 py-4 backdrop-blur">
        <p className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
          Account
        </p>
        <h1 className="font-display text-[26px] leading-tight tracking-tight text-ink">
          Sarah Chen
        </h1>
      </header>

      <div className="flex-1 overflow-y-auto pb-24">
        {/* Profile card */}
        <div className="px-5 pt-5">
          <div className="flex items-center gap-3 rounded-2xl border border-border bg-card p-4">
            <div className="flex size-12 items-center justify-center rounded-full bg-brand text-[16px] font-bold text-brand-foreground">
              SC
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate font-display text-[16px] text-ink">sarah@northfield.ca</p>
              <p className="text-[12px] text-ink-muted">3 properties · joined Oct 2024</p>
            </div>
          </div>
        </div>

        {/* Plan */}
        <Section title="Plan">
          <div className="rounded-2xl border border-brand/20 bg-brand-muted/50 p-5">
            <div className="flex items-center gap-2 font-mono text-[10px] font-bold uppercase tracking-widest text-brand">
              <Sparkles className="size-3.5" /> Pro · annual
            </div>
            <p className="mt-2 font-display text-[20px] text-ink">
              $39 / mo · billed yearly
            </p>
            <p className="mt-1 text-[13px] text-ink-muted">
              Up to 5 properties. Renews August 14, 2026.
            </p>
            <div className="mt-4 flex gap-2">
              <Button
                asChild
                className="h-11 flex-1 bg-brand text-brand-foreground hover:bg-brand/90"
              >
                <Link to="/plans">Change plan</Link>
              </Button>
              <Button variant="outline" className="h-11 border-border" onClick={() => toast("Receipts emailed", { duration: 1500 })}>
                Receipts
              </Button>
            </div>
          </div>
        </Section>

        {/* Billing */}
        <Section title="Billing">
          <Card>
            <Row
              icon={CreditCard}
              label="Payment method"
              value="Visa ending 4242"
              onClick={() => toast("Update card (mock)")}
            />
            <Divider />
            <Row
              icon={CreditCard}
              label="Billing email"
              value="sarah@northfield.ca"
              onClick={() => toast("Edit email (mock)")}
              last
            />
          </Card>
        </Section>

        {/* Notifications */}
        <Section title="Notifications">
          <Card>
            <ToggleRow
              icon={Bell}
              label="Push notifications"
              helper="Emergencies bypass silent mode"
              checked={push}
              onCheckedChange={(v) => {
                setPush(v);
                toast.success(v ? "Push on" : "Push off", { duration: 1500 });
              }}
            />
            <ToggleRow
              icon={Bell}
              label="Daily digest email"
              helper="Routine items, 6:00 PM"
              checked={email}
              onCheckedChange={(v) => {
                setEmail(v);
                toast.success(v ? "Digest on" : "Digest off", { duration: 1500 });
              }}
              last
            />
          </Card>
        </Section>

        {/* Security */}
        <Section title="Security">
          <Card>
            <Row icon={Shield} label="Change password" onClick={() => toast("Password flow (mock)")} />
            <Divider />
            <Row icon={Shield} label="Two-factor auth" value="Off" onClick={() => toast("2FA setup (mock)")} last />
          </Card>
        </Section>

        {/* Support */}
        <Section title="Support">
          <Card>
            <Row icon={HelpCircle} label="Help center" onClick={() => toast("Help (mock)")} />
            <Divider />
            <Row icon={HelpCircle} label="Email us" value="hello@stoop.co" onClick={() => (window.location.href = "mailto:hello@stoop.co")} last />
          </Card>
        </Section>

        {/* Legal */}
        <Section title="Legal">
          <Card>
            <LinkRow label="Privacy Policy" to="/privacy" />
            <Divider />
            <LinkRow label="Terms of Service" to="/terms" last />
          </Card>
        </Section>

        {/* Sign out + danger */}
        <Section muted>
          <Card muted>
            <button
              type="button"
              onClick={() => setLogoutOpen(true)}
              className="flex min-h-14 w-full items-center justify-between gap-4 px-4 py-3 text-left"
            >
              <span className="flex items-center gap-3 text-[14px] font-medium text-ink">
                <LogOut className="size-4 text-ink-muted" /> Sign out
              </span>
              <ChevronRight className="size-4 text-ink-muted/70" />
            </button>
          </Card>
          <p className="mt-3 px-1 text-center font-mono text-[10px] uppercase tracking-widest text-ink-muted">
            Stoop. v1.0.0 — Made in the GTA
          </p>
        </Section>
      </div>

      <AppTabBar active="account" queueCount={queue.length} />

      <AlertDialog open={logoutOpen} onOpenChange={setLogoutOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="font-display">Sign out?</AlertDialogTitle>
            <AlertDialogDescription>
              Your agent keeps working while you're signed out. You'll just stop getting alerts on this device.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-brand text-brand-foreground hover:bg-brand/90"
              onClick={() => {
                setLogoutOpen(false);
                toast.success("Signed out", { duration: 1500 });
              }}
            >
              Sign out
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PhoneFrame>
  );
}

function Section({
  title,
  muted,
  children,
}: {
  title?: string;
  muted?: boolean;
  children: React.ReactNode;
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

function Card({ children, muted }: { children: React.ReactNode; muted?: boolean }) {
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

function Row({
  icon: Icon,
  label,
  value,
  onClick,
  last,
}: {
  icon: typeof Bell;
  label: string;
  value?: string;
  onClick?: () => void;
  last?: boolean;
}) {
  return (
    <>
      <button
        type="button"
        onClick={onClick}
        className="flex min-h-14 w-full items-center justify-between gap-4 px-4 py-3 text-left"
      >
        <span className="flex items-center gap-3 text-[14px] text-ink">
          <Icon className="size-4 text-ink-muted" />
          {label}
        </span>
        <span className="flex items-center gap-2 text-[13px] text-ink-muted">
          {value}
          <ChevronRight className="size-4 text-ink-muted/70" />
        </span>
      </button>
      {!last && <Divider />}
    </>
  );
}

function LinkRow({ label, to, last }: { label: string; to: "/privacy" | "/terms"; last?: boolean }) {
  return (
    <>
      <Link to={to} className="flex min-h-14 w-full items-center justify-between gap-4 px-4 py-3">
        <span className="text-[14px] text-ink">{label}</span>
        <ChevronRight className="size-4 text-ink-muted/70" />
      </Link>
      {!last && <Divider />}
    </>
  );
}

function ToggleRow({
  icon: Icon,
  label,
  helper,
  checked,
  onCheckedChange,
  last,
}: {
  icon: typeof Bell;
  label: string;
  helper?: string;
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  last?: boolean;
}) {
  return (
    <>
      <div className="flex min-h-14 items-center justify-between gap-4 px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <Icon className="size-4 shrink-0 text-ink-muted" />
          <div className="min-w-0">
            <p className="text-[14px] text-ink">{label}</p>
            {helper && <p className="mt-0.5 text-[12px] text-ink-muted">{helper}</p>}
          </div>
        </div>
        <Switch checked={checked} onCheckedChange={onCheckedChange} aria-label={label} />
      </div>
      {!last && <Divider />}
    </>
  );
}
