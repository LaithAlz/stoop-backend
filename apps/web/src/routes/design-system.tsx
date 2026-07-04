import { createFileRoute } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";
import { Check, Loader2, Camera, Plus, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Card } from "@/components/ui/card";

import { Wordmark } from "@/components/stoop/Wordmark";
import { SeverityBadge } from "@/components/stoop/SeverityBadge";
import { MessageBubble } from "@/components/stoop/MessageBubble";
import { ApprovalCard } from "@/components/stoop/ApprovalCard";
import { AutonomyPill, type AutonomyMode } from "@/components/stoop/AutonomyPill";
import { MarketingNav } from "@/components/stoop/MarketingNav";
import { MobileTabBar } from "@/components/stoop/MobileTabBar";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/design-system")({
  head: () => ({
    meta: [
      { title: "Stoop. — Design system" },
      {
        name: "description",
        content: "The Heritage Utility design system for Stoop.",
      },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: DesignSystemPage,
});

function Section({
  id,
  eyebrow,
  title,
  description,
  children,
}: {
  id: string;
  eyebrow: string;
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-24 border-t border-border py-20">
      <div className="mb-12 max-w-2xl">
        <p className="mb-3 text-xs font-bold uppercase tracking-[0.2em] text-brand">{eyebrow}</p>
        <h2 className="font-display text-4xl font-bold tracking-tight">{title}</h2>
        {description && <p className="mt-3 text-base text-ink-muted">{description}</p>}
      </div>
      {children}
    </section>
  );
}

function Swatch({ name, value, varName }: { name: string; value: string; varName: string }) {
  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
      <div className="h-20" style={{ background: `var(${varName})` }} />
      <div className="space-y-0.5 px-4 py-3">
        <p className="text-sm font-semibold text-ink">{name}</p>
        <p className="font-mono text-[11px] text-ink-muted">{value}</p>
      </div>
    </div>
  );
}

function DesignSystemPage() {
  const [autonomy, setAutonomy] = useState<AutonomyMode>("shadow");
  const [errorDemo, setErrorDemo] = useState("not-an-email");
  const [chips, setChips] = useState<string[]>(["Plumbing", "HVAC"]);
  const allChips = ["Plumbing", "HVAC", "Electrical", "Appliance", "Locks", "Pest"];

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <MarketingNav />

      {/* Page header */}
      <header className="border-b border-border bg-canvas px-6 py-20">
        <div className="mx-auto max-w-7xl space-y-6">
          <p className="text-xs font-bold uppercase tracking-[0.2em] text-brand">
            Design system · v1
          </p>
          <h1 className="max-w-3xl text-balance font-display text-6xl font-bold leading-[0.95] tracking-tight">
            Heritage utility. The visual language of <Wordmark size="xl" />
          </h1>
          <p className="max-w-2xl text-lg leading-relaxed text-ink-muted">
            A serif-led, forest-and-canvas system for a trusted operator tool. Every surface in the
            Stoop. marketing site and mobile app inherits from this page.
          </p>
        </div>
      </header>

      <div className="mx-auto max-w-7xl px-6">
        {/* Wordmark */}
        <Section
          id="wordmark"
          eyebrow="01 — Identity"
          title="The wordmark"
          description="The mark is Stoop. — the period is part of the mark and always rendered in emergency red as a small operator signal."
        >
          <div className="grid items-end gap-10 rounded-3xl border border-border bg-card p-10 md:grid-cols-4">
            <div className="space-y-2">
              <Wordmark size="favicon" />
              <p className="text-xs font-medium uppercase tracking-wider text-ink-muted">Favicon</p>
            </div>
            <div className="space-y-2">
              <Wordmark size="sm" />
              <p className="text-xs font-medium uppercase tracking-wider text-ink-muted">Small</p>
            </div>
            <div className="space-y-2">
              <Wordmark size="lg" />
              <p className="text-xs font-medium uppercase tracking-wider text-ink-muted">Large</p>
            </div>
            <div className="space-y-2">
              <Wordmark size="xl" />
              <p className="text-xs font-medium uppercase tracking-wider text-ink-muted">Display</p>
            </div>
          </div>
        </Section>

        {/* Typography */}
        <Section
          id="typography"
          eyebrow="02 — Typography"
          title="Fraunces & Plus Jakarta Sans"
          description="An editorial serif paired with a humanist sans. No Inter, no Roboto, no system defaults for display."
        >
          <div className="grid gap-6 rounded-3xl border border-border bg-card p-10 md:grid-cols-[2fr_1fr]">
            <div className="space-y-6">
              <div>
                <span className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                  Display · Fraunces 700
                </span>
                <p className="font-display text-6xl font-bold leading-tight tracking-tight">
                  Maintenance, handled.
                </p>
              </div>
              <div>
                <span className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                  H1 · Fraunces 700 · 48
                </span>
                <p className="font-display text-5xl font-bold tracking-tight">
                  Built for small landlords
                </p>
              </div>
              <div>
                <span className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                  H2 · Fraunces 600 · 32
                </span>
                <p className="font-display text-3xl font-semibold italic">
                  Approve. Edit. Trust grows.
                </p>
              </div>
              <div>
                <span className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                  H3 · Plus Jakarta 700 · 20
                </span>
                <p className="text-xl font-bold">Section heading</p>
              </div>
              <div>
                <span className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                  Body · Plus Jakarta 400 · 16
                </span>
                <p className="max-w-xl text-base leading-relaxed">
                  Tenants text a property phone number. Stoop classifies severity, asks clarifying
                  questions, gathers photos, and drafts a reply in your voice.
                </p>
              </div>
              <div>
                <span className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                  Small · 13
                </span>
                <p className="text-[13px] text-ink-muted">
                  Received 4 minutes ago · Unit 4B · 128 Wythe Avenue
                </p>
              </div>
              <div>
                <span className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                  Label · uppercase 10
                </span>
                <p className="text-[10px] font-bold uppercase tracking-[0.18em]">Approve & send</p>
              </div>
            </div>

            <div className="space-y-3 rounded-2xl border border-border bg-surface/60 p-6">
              <p className="text-xs font-bold uppercase tracking-widest text-ink-muted">
                Scale tokens
              </p>
              <ul className="space-y-2 font-mono text-[11px] text-ink-muted">
                <li>display · 72 / 0.95</li>
                <li>h1 · 48 / 1.05</li>
                <li>h2 · 32 / 1.15</li>
                <li>h3 · 20 / 1.25</li>
                <li>body · 16 / 1.6</li>
                <li>small · 13 / 1.5</li>
                <li>label · 10 / 1.2 · ucase</li>
              </ul>
            </div>
          </div>
        </Section>

        {/* Color */}
        <Section
          id="color"
          eyebrow="03 — Color"
          title="Light mode is the system"
          description="Dark tokens are reserved exclusively for the emergency screen — the one place the app inverts."
        >
          <div className="grid gap-4 md:grid-cols-3 lg:grid-cols-6">
            <Swatch name="Canvas" value="#FDFCFB" varName="--canvas" />
            <Swatch name="Surface" value="#F3F4F3" varName="--surface" />
            <Swatch name="Brand · Forest" value="#2D4A3E" varName="--brand" />
            <Swatch name="Brand muted" value="#E9EDEA" varName="--brand-muted" />
            <Swatch name="Ink" value="#1A1C19" varName="--ink" />
            <Swatch name="Ink muted" value="#5E635E" varName="--ink-muted" />
          </div>
        </Section>

        {/* Severity */}
        <Section
          id="severity"
          eyebrow="04 — Severity"
          title="Color, icon, and label — always together"
          description="Severity is never conveyed by color alone. Each level carries a Lucide icon and an explicit text label."
        >
          <div className="grid gap-4 md:grid-cols-3">
            <SeverityBadge severity="emergency" variant="row" />
            <SeverityBadge severity="urgent" variant="row" />
            <SeverityBadge severity="routine" variant="row" />
          </div>
          <div className="mt-6 flex flex-wrap gap-3">
            <SeverityBadge severity="emergency" />
            <SeverityBadge severity="urgent" />
            <SeverityBadge severity="routine" />
          </div>
        </Section>

        {/* Buttons */}
        <Section
          id="buttons"
          eyebrow="05 — Buttons"
          title="Action variants"
          description="Primary actions are 56px on mobile. Press states, focus rings, loading and disabled are all on."
        >
          <div className="space-y-8 rounded-3xl border border-border bg-card p-8">
            <div className="flex flex-wrap items-center gap-4">
              <Button>Primary</Button>
              <Button variant="secondary">Secondary</Button>
              <Button variant="outline">Outline</Button>
              <Button variant="ghost">Ghost</Button>
              <Button variant="destructive">Danger</Button>
            </div>
            <div className="flex flex-wrap items-center gap-4">
              <Button size="sm">Small</Button>
              <Button>Default</Button>
              <Button size="lg">Large</Button>
              <Button className="h-14 px-8 text-base">Mobile primary (56px)</Button>
            </div>
            <div className="flex flex-wrap items-center gap-4">
              <Button disabled>
                <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                Sending
              </Button>
              <Button disabled variant="outline">
                Disabled
              </Button>
              <Button>
                <Check className="size-4" aria-hidden="true" />
                Approve & send
              </Button>
            </div>
          </div>
        </Section>

        {/* Forms */}
        <Section
          id="forms"
          eyebrow="06 — Forms"
          title="Form elements with real labels"
          description="Every field uses a real <label>. Placeholders never carry meaning."
        >
          <div className="grid gap-10 rounded-3xl border border-border bg-card p-8 md:grid-cols-2">
            <div className="space-y-6">
              <div className="space-y-2">
                <Label htmlFor="ds-property">Property nickname</Label>
                <Input id="ds-property" placeholder="e.g. 128 Wythe Ave" />
              </div>

              <div className="space-y-2">
                <Label htmlFor="ds-email" className="text-emergency">
                  Email
                </Label>
                <Input
                  id="ds-email"
                  type="email"
                  value={errorDemo}
                  onChange={(e) => setErrorDemo(e.target.value)}
                  aria-invalid
                  aria-describedby="ds-email-err"
                  className="border-emergency focus-visible:ring-emergency/40"
                />
                <p id="ds-email-err" className="text-xs font-medium text-emergency">
                  That doesn't look like a valid email.
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="ds-vendor">Preferred plumber</Label>
                <Select>
                  <SelectTrigger id="ds-vendor">
                    <SelectValue placeholder="Choose a vendor" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="a">Westside Plumbing</SelectItem>
                    <SelectItem value="b">Pipeworks Co.</SelectItem>
                    <SelectItem value="c">On-call rotation</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label htmlFor="ds-note">Note for the agent</Label>
                <Textarea
                  id="ds-note"
                  placeholder="Add context Stoop should know when replying."
                  rows={4}
                />
              </div>
            </div>

            <div className="space-y-8">
              <div className="space-y-3">
                <Label>Categories the agent should handle</Label>
                <div className="flex flex-wrap gap-2">
                  {allChips.map((chip) => {
                    const active = chips.includes(chip);
                    return (
                      <button
                        key={chip}
                        type="button"
                        onClick={() =>
                          setChips((c) => (active ? c.filter((x) => x !== chip) : [...c, chip]))
                        }
                        className={cn(
                          "inline-flex min-h-11 items-center gap-1.5 rounded-full border px-4 py-2 text-sm font-semibold transition-colors",
                          active
                            ? "border-brand bg-brand text-brand-foreground"
                            : "border-border bg-canvas text-ink-muted hover:border-brand/40 hover:text-ink",
                        )}
                        aria-pressed={active}
                      >
                        {active ? (
                          <Check className="size-3.5" aria-hidden="true" />
                        ) : (
                          <Plus className="size-3.5" aria-hidden="true" />
                        )}
                        {chip}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="flex items-center justify-between rounded-2xl border border-border bg-surface/60 p-4">
                <div>
                  <Label htmlFor="ds-toggle" className="cursor-pointer">
                    Send me emergency alerts at any time
                  </Label>
                  <p className="text-xs text-ink-muted">
                    Overrides quiet hours for life-safety issues.
                  </p>
                </div>
                <Switch id="ds-toggle" defaultChecked />
              </div>

              <div className="space-y-3">
                <Label>Quiet hours</Label>
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1">
                    <span className="text-xs font-medium text-ink-muted">From</span>
                    <Input type="time" defaultValue="22:00" />
                  </div>
                  <div className="space-y-1">
                    <span className="text-xs font-medium text-ink-muted">Until</span>
                    <Input type="time" defaultValue="07:00" />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </Section>

        {/* Cards */}
        <Section
          id="cards"
          eyebrow="07 — Cards"
          title="Card variants"
          description="From quiet metric tiles to severity-tinted draft cards."
        >
          <div className="grid gap-6 lg:grid-cols-3">
            <Card className="p-6">
              <p className="text-xs font-bold uppercase tracking-widest text-ink-muted">Default</p>
              <p className="mt-2 font-display text-2xl font-bold">128 Wythe Ave</p>
              <p className="text-sm text-ink-muted">4 units · 2 active threads</p>
            </Card>

            <Card className="border-emergency/30 bg-emergency-soft p-6">
              <SeverityBadge severity="emergency" />
              <p className="mt-3 font-display text-2xl font-bold text-emergency">Water leak</p>
              <p className="text-sm text-ink">Unit 3B — Ceiling, master bath</p>
            </Card>

            <Card className="border-dashed border-brand/40 bg-brand-muted/40 p-6">
              <span className="inline-flex items-center gap-1 rounded-md bg-canvas px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-brand">
                Draft pending
              </span>
              <p className="mt-3 text-sm leading-relaxed text-ink">
                "I'm dispatching a plumber now. They should arrive within 2 hours."
              </p>
              <div className="mt-4 flex gap-2">
                <Button size="sm" variant="outline">
                  Edit
                </Button>
                <Button size="sm">Approve</Button>
              </div>
            </Card>

            <Card className="p-6">
              <p className="text-xs font-bold uppercase tracking-widest text-ink-muted">
                This week
              </p>
              <p className="mt-2 font-display text-5xl font-bold">12</p>
              <p className="text-sm text-ink-muted">Messages auto-handled · 92% approval rate</p>
            </Card>

            <Card className="p-6">
              <p className="text-xs font-bold uppercase tracking-widest text-ink-muted">
                Trust progress
              </p>
              <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-surface">
                <div className="h-full w-[64%] rounded-full bg-brand" />
              </div>
              <p className="mt-2 text-sm text-ink-muted">
                64% to <span className="font-semibold text-ink">Auto-Routine</span>
              </p>
            </Card>

            <Card className="border-routine/30 bg-routine-soft p-6">
              <SeverityBadge severity="routine" />
              <p className="mt-3 font-display text-2xl font-bold text-routine">Trash schedule</p>
              <p className="text-sm text-ink">Resolved · agent answered in under 1 minute</p>
            </Card>
          </div>
        </Section>

        {/* Message bubbles */}
        <Section
          id="bubbles"
          eyebrow="08 — Conversation"
          title="Message bubbles"
          description="Tenant inbound, agent outbound, draft pending, and photo bubbles. The AI assistant tag is always visible whenever the agent speaks."
        >
          <div className="grid gap-10 md:grid-cols-2">
            <div className="space-y-5 rounded-3xl border border-border bg-card p-6">
              <MessageBubble
                variant="tenant"
                text="Hey, the sink in the kitchen is backing up. Water is starting to pool on the floor."
                timestamp="10:14 AM"
              />
              <MessageBubble variant="photo" timestamp="10:14 AM" />
              <MessageBubble
                variant="agent"
                text="I've reached our on-call plumber. They'll be there within 90 minutes. I'll text once they're 10 minutes out."
                timestamp="10:16 AM"
              />
            </div>

            <div className="space-y-5 rounded-3xl border border-dashed border-brand/40 bg-brand-muted/30 p-6">
              <MessageBubble
                variant="tenant"
                text="When is trash pickup this week? It's a holiday Monday."
                timestamp="9:02 AM"
              />
              <MessageBubble
                variant="draft"
                text="Pickup is delayed one day this week — it'll be Tuesday morning. Please have bins out by 7am."
                timestamp="9:02 AM"
              />
              <div className="flex gap-3">
                <Button variant="outline" className="flex-1">
                  Edit draft
                </Button>
                <Button className="flex-1">Approve & send</Button>
              </div>
            </div>
          </div>
        </Section>

        {/* Autonomy pills */}
        <Section
          id="autonomy"
          eyebrow="09 — Autonomy"
          title="The trust ladder"
          description="Landlords graduate Stoop. from shadow mode toward full autonomy as they approve more drafts unedited."
        >
          <div className="space-y-6 rounded-3xl border border-border bg-card p-8">
            <div className="flex flex-wrap gap-3">
              {(["shadow", "auto-routine", "auto-urgent", "full-auto"] as AutonomyMode[]).map(
                (m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setAutonomy(m)}
                    className="rounded-full focus-visible:outline-none"
                  >
                    <AutonomyPill mode={m} active={autonomy === m} />
                  </button>
                ),
              )}
            </div>
            <p className="text-sm text-ink-muted">
              Tap a pill to preview the active state. Selected:{" "}
              <span className="font-semibold text-ink">{autonomy}</span>
            </p>
          </div>
        </Section>

        {/* Approval card */}
        <Section
          id="approval"
          eyebrow="10 — Approval queue"
          title="The card that runs the app"
          description="Tenant message, AI-drafted reply with the always-visible AI assistant tag, severity, and 56px primary action."
        >
          <div className="mx-auto max-w-md">
            <ApprovalCard
              unit="Unit 4B"
              property="128 Wythe Ave"
              receivedAgo="4m ago"
              severity="urgent"
              tenantMessage="The kitchen sink is overflowing. I've turned the valve under the sink but water is still coming up from the drain."
              draftReply="I'm so sorry — I've reached our on-call plumber and they'll be there within 90 minutes. Please clear the area under the sink and place towels around the base. I'll text once they're 10 minutes out."
            />
          </div>
        </Section>

        {/* Navigation */}
        <Section
          id="nav"
          eyebrow="11 — Navigation"
          title="Marketing nav & in-app tab bar"
          description="No hamburgers, no icon-only nav. Every tab carries a text label."
        >
          <div className="space-y-10">
            <div className="overflow-hidden rounded-3xl border border-border bg-canvas">
              <MarketingNav />
              <div className="px-6 py-10 text-sm text-ink-muted">
                Top nav uses the Stoop. wordmark on the left, plain-English links in the middle, and
                trial CTA on the right.
              </div>
            </div>

            <div className="mx-auto w-[375px] overflow-hidden rounded-[2.5rem] border-[8px] border-card bg-card shadow-xl">
              <div className="flex h-40 items-end bg-surface/60 p-6">
                <p className="font-display text-xl font-bold">Bottom tab bar</p>
              </div>
              <MobileTabBar />
            </div>
          </div>
        </Section>

        {/* Icons */}
        <Section
          id="icons"
          eyebrow="12 — Iconography"
          title="Lucide, paired with labels"
          description="No emojis as structural UI. Icons sit next to a text label everywhere except back arrows and close X."
        >
          <div className="grid gap-3 rounded-3xl border border-border bg-card p-6 sm:grid-cols-3 md:grid-cols-4">
            {[
              { Icon: Check, label: "Approve" },
              { Icon: Camera, label: "Add photo" },
              { Icon: Plus, label: "Add property" },
              { Icon: X, label: "Close" },
            ].map(({ Icon, label }) => (
              <div
                key={label}
                className="flex items-center gap-3 rounded-xl border border-border bg-canvas px-4 py-3"
              >
                <Icon className="size-5 text-brand" aria-hidden="true" />
                <span className="text-sm font-semibold">{label}</span>
              </div>
            ))}
          </div>
        </Section>
      </div>

      <footer className="mt-10 border-t border-border bg-surface/60 px-6 py-12">
        <div className="mx-auto flex max-w-7xl items-center justify-between">
          <Wordmark size="sm" />
          <p className="text-xs font-medium uppercase tracking-widest text-ink-muted">
            Heritage utility · v1
          </p>
        </div>
      </footer>
    </div>
  );
}
