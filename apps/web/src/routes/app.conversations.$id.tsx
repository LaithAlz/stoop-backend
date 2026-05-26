import { createFileRoute, Link, notFound, useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  MoreHorizontal,
  ChevronDown,
  ChevronUp,
  Check,
  Pencil,
  X,
  Sparkles,
  Image as ImageIcon,
  Phone,
  Clock,
} from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { SeverityBadge } from "@/components/stoop/SeverityBadge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { getConversation, queue } from "@/lib/mock-app";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/app/conversations/$id")({
  head: ({ params }) => ({
    meta: [
      { title: "Conversation — Stoop." },
      { name: "robots", content: "noindex" },
    ],
    links: [{ rel: "canonical", href: `/app/conversations/${params.id}` }],
  }),
  loader: ({ params }) => {
    const item = queue.find((q) => q.id === params.id);
    if (!item) throw notFound();
    return item;
  },
  notFoundComponent: () => (
    <PhoneFrame>
      <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
        <p className="text-sm font-bold uppercase tracking-widest text-ink-muted">404</p>
        <h1 className="mt-2 font-display text-2xl font-bold">Conversation not found.</h1>
        <Link
          to="/app"
          className="mt-6 inline-flex h-12 items-center rounded-xl bg-brand px-5 text-sm font-bold text-brand-foreground"
        >
          Back to queue
        </Link>
      </div>
    </PhoneFrame>
  ),
  errorComponent: ({ reset }) => {
    const router = useRouter();
    return (
      <PhoneFrame>
        <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
          <h1 className="font-display text-xl font-bold">Couldn't load this thread.</h1>
          <button
            onClick={() => {
              router.invalidate();
              reset();
            }}
            className="mt-6 inline-flex h-12 items-center rounded-xl bg-brand px-5 text-sm font-bold text-brand-foreground"
          >
            Try again
          </button>
        </div>
      </PhoneFrame>
    );
  },
  component: ConversationPage,
});

function ConversationPage() {
  const item = Route.useLoaderData();
  const [showReasoning, setShowReasoning] = useState(true);
  const [mode, setMode] = useState<"preview" | "edit" | "sent" | "rejected">("preview");
  const [draftText, setDraftText] = useState(item.draftReply);
  const [showOriginal, setShowOriginal] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectNote, setRejectNote] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "auto" });
  }, []);

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col">
        <header className="flex items-center gap-2 border-b border-border bg-canvas px-3 py-3">
          <Link
            to="/app"
            aria-label="Back to queue"
            className="inline-flex size-11 items-center justify-center rounded-full hover:bg-brand-muted"
          >
            <ArrowLeft className="size-5" aria-hidden="true" />
          </Link>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-bold text-ink">{item.propertyLabel}</p>
            <p className="truncate text-[11px] font-medium uppercase tracking-wider text-ink-muted">
              {item.tenantFirst} · {item.tenantPhoneMasked}
            </p>
          </div>
          <SeverityBadge severity={item.severity} />
          <button
            aria-label="More actions"
            className="inline-flex size-11 items-center justify-center rounded-full hover:bg-brand-muted"
          >
            <MoreHorizontal className="size-5" aria-hidden="true" />
          </button>
        </header>

        <main className="flex-1 overflow-y-auto bg-surface/30 px-4 py-5">
          <DayStamp label="Today" />

          <div className="space-y-4">
            <Tenant text={item.tenantMessage} time="9:42 AM" name={item.tenantFirst} />
            <Agent text="Thanks — can you send a photo so I can see what's happening?" time="9:43 AM" sent />
            {item.hasPhoto && <TenantPhoto time="9:46 AM" />}
            <Tenant
              text="Here you go. Cabinet underneath is soaked."
              time="9:46 AM"
              name={item.tenantFirst}
            />

            {/* Reasoning */}
            <button
              type="button"
              onClick={() => setShowReasoning((s) => !s)}
              className="flex w-full items-center justify-between rounded-2xl border border-border bg-card px-4 py-3 text-left"
            >
              <span className="flex items-center gap-2">
                <Sparkles className="size-4 text-brand" aria-hidden="true" />
                <span className="text-sm font-bold text-ink">Why Stoop drafted this.</span>
              </span>
              {showReasoning ? (
                <ChevronUp className="size-4 text-ink-muted" aria-hidden="true" />
              ) : (
                <ChevronDown className="size-4 text-ink-muted" aria-hidden="true" />
              )}
            </button>

            {showReasoning && <ReasoningPanel />}

            {/* Draft card */}
            {mode === "preview" && (
              <DraftCard
                text={draftText}
                onApprove={() => setMode("sent")}
                onEdit={() => setMode("edit")}
                onReject={() => setRejectOpen(true)}
              />
            )}

            {mode === "edit" && (
              <EditCard
                text={draftText}
                original={item.draftReply}
                showOriginal={showOriginal}
                onToggleOriginal={() => setShowOriginal((s) => !s)}
                onChange={setDraftText}
                onCancel={() => {
                  setDraftText(item.draftReply);
                  setMode("preview");
                }}
                onSend={() => setMode("sent")}
              />
            )}

            {mode === "sent" && (
              <Agent
                text={draftText}
                time="just now"
                sent
                edited={draftText !== item.draftReply}
              />
            )}

            {mode === "rejected" && (
              <div className="rounded-2xl border border-dashed border-border bg-card px-4 py-3 text-center text-xs text-ink-muted">
                Draft dismissed. The agent will keep the thread open and ask you again if the tenant
                follows up.
              </div>
            )}
          </div>

          <div ref={bottomRef} />
        </main>

        <AppTabBar active="queue" queueCount={queue.length} />
      </div>

      <Sheet open={rejectOpen} onOpenChange={setRejectOpen}>
        <SheetContent side="bottom" className="rounded-t-3xl">
          <SheetHeader>
            <SheetTitle className="font-display text-2xl">Tell the agent why?</SheetTitle>
          </SheetHeader>
          <p className="mt-1 text-sm text-ink-muted">
            Optional. A short note helps the next draft land closer to your voice.
          </p>
          <Textarea
            value={rejectNote}
            onChange={(e) => setRejectNote(e.target.value)}
            placeholder="Too formal · wrong vendor · I want to call instead…"
            className="mt-4 min-h-24 text-base"
          />
          <div className="mt-4 grid grid-cols-2 gap-2">
            <Button
              variant="outline"
              className="h-12 font-semibold"
              onClick={() => setRejectOpen(false)}
            >
              Cancel
            </Button>
            <Button
              className="h-12 font-semibold"
              onClick={() => {
                setRejectOpen(false);
                setRejectNote("");
                setMode("rejected");
              }}
            >
              Dismiss draft
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    </PhoneFrame>
  );
}

/* --------- bubbles --------- */

function DayStamp({ label }: { label: string }) {
  return (
    <div className="mb-4 flex items-center gap-3">
      <span className="h-px flex-1 bg-border" />
      <span className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
        {label}
      </span>
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}

function Tenant({ text, time, name }: { text: string; time: string; name: string }) {
  return (
    <div className="flex max-w-[85%] flex-col items-start gap-1">
      <div className="rounded-2xl rounded-tl-sm border border-border bg-card px-4 py-3 text-[15px] leading-relaxed text-ink">
        {text}
      </div>
      <span className="ml-1 font-mono text-[10px] font-medium text-ink-muted">
        {name} · {time}
      </span>
    </div>
  );
}

function TenantPhoto({ time }: { time: string }) {
  return (
    <div className="flex max-w-[70%] flex-col items-start gap-1">
      <div className="flex aspect-[4/3] w-56 items-center justify-center rounded-2xl rounded-tl-sm border border-border bg-surface text-ink-muted">
        <ImageIcon className="size-8" aria-hidden="true" />
        <span className="sr-only">Photo from tenant</span>
      </div>
      <span className="ml-1 font-mono text-[10px] font-medium text-ink-muted">
        Photo · {time}
      </span>
    </div>
  );
}

function Agent({
  text,
  time,
  sent,
  edited,
}: {
  text: string;
  time: string;
  sent?: boolean;
  edited?: boolean;
}) {
  return (
    <div className="ml-auto flex max-w-[85%] flex-col items-end gap-1">
      <div className="rounded-2xl rounded-tr-sm bg-brand px-4 py-3 text-[15px] leading-relaxed text-brand-foreground">
        {text}
      </div>
      <div className="mr-1 flex items-center gap-2">
        <span className="inline-flex items-center gap-1 rounded-md border border-brand/30 bg-brand-muted px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-brand">
          <Sparkles className="size-2.5" aria-hidden="true" />
          AI assistant
        </span>
        <span className="font-mono text-[10px] font-medium text-ink-muted">
          {sent ? "Sent" : "Draft"}
          {edited && " · edited from draft"}
          {time && ` · ${time}`}
        </span>
      </div>
    </div>
  );
}

function ReasoningPanel() {
  return (
    <div className="space-y-3 rounded-2xl border border-border bg-canvas p-4">
      <div>
        <p className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
          Severity
        </p>
        <p className="mt-1 text-sm leading-relaxed text-ink">
          <span className="font-bold text-urgent">Urgent.</span> Water damage risk, not active
          flooding. Cabinet wetting can compound within 24h.
        </p>
      </div>
      <div className="rounded-xl border border-border bg-card p-3">
        <p className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">
          Vendor match
        </p>
        <p className="mt-1 text-sm font-bold text-ink">Mike's Plumbing</p>
        <div className="mt-1 flex items-center gap-3 text-xs text-ink-muted">
          <span className="inline-flex items-center gap-1">
            <Phone className="size-3" aria-hidden="true" />
            (905) 555-0142
          </span>
          <span className="inline-flex items-center gap-1">
            <Clock className="size-3" aria-hidden="true" />
            After-hours OK
          </span>
        </div>
        <p className="mt-1 text-[11px] text-ink-muted">Last used 3 weeks ago · Walmer Unit 2</p>
      </div>
    </div>
  );
}

function DraftCard({
  text,
  onApprove,
  onEdit,
  onReject,
}: {
  text: string;
  onApprove: () => void;
  onEdit: () => void;
  onReject: () => void;
}) {
  return (
    <div className="ml-auto flex w-full flex-col gap-2">
      <span className="ml-auto inline-flex items-center gap-1 rounded-md border border-brand/40 bg-canvas px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-brand">
        Draft · awaiting your approval
      </span>
      <div className="rounded-2xl rounded-tr-sm border-2 border-dashed border-brand/50 bg-brand-muted/40 px-4 py-3 text-[15px] leading-relaxed text-ink">
        {text}
      </div>
      <div className="mr-1 flex items-center justify-between text-[11px] text-ink-muted">
        <span>
          Tone: <span className="font-semibold text-ink">Calm + practical</span>
        </span>
        <button onClick={onEdit} className="font-semibold text-brand underline-offset-4 hover:underline">
          Edit instead
        </button>
      </div>
      <div className="mt-2 grid grid-cols-[1fr_auto_auto] gap-2">
        <Button onClick={onApprove} className="h-14 text-base font-bold">
          <Check className="size-4" aria-hidden="true" />
          Approve
        </Button>
        <Button variant="outline" onClick={onEdit} className="h-14 px-4 font-bold">
          <Pencil className="size-4" aria-hidden="true" />
          Edit
        </Button>
        <Button
          variant="outline"
          onClick={onReject}
          className="h-14 border-emergency/30 px-4 font-bold text-emergency hover:bg-emergency-soft"
        >
          <X className="size-4" aria-hidden="true" />
          Reject
        </Button>
      </div>
    </div>
  );
}

function EditCard({
  text,
  original,
  showOriginal,
  onToggleOriginal,
  onChange,
  onCancel,
  onSend,
}: {
  text: string;
  original: string;
  showOriginal: boolean;
  onToggleOriginal: () => void;
  onChange: (v: string) => void;
  onCancel: () => void;
  onSend: () => void;
}) {
  return (
    <div className="ml-auto flex w-full flex-col gap-2">
      <div className="ml-auto flex items-center gap-2">
        <span className="inline-flex items-center gap-1 rounded-md border border-brand/40 bg-canvas px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-brand">
          Editing draft
        </span>
        <button
          onClick={onToggleOriginal}
          className="text-[11px] font-semibold text-ink-muted underline-offset-4 hover:text-brand hover:underline"
        >
          {showOriginal ? "Hide original" : "See original"}
        </button>
      </div>

      {showOriginal && (
        <blockquote className="rounded-xl border border-border bg-surface/60 px-3 py-2 text-xs italic leading-relaxed text-ink-muted">
          {original}
        </blockquote>
      )}

      <Textarea
        value={text}
        onChange={(e) => onChange(e.target.value)}
        className="min-h-32 rounded-2xl rounded-tr-sm border-2 border-brand/40 bg-canvas p-4 text-[15px] leading-relaxed text-ink"
      />

      <div className="mt-1 grid grid-cols-[auto_1fr] gap-2">
        <Button variant="outline" onClick={onCancel} className="h-14 font-bold">
          Cancel
        </Button>
        <Button onClick={onSend} className="h-14 text-base font-bold">
          <Check className="size-4" aria-hidden="true" />
          Send edited version
        </Button>
      </div>
    </div>
  );
}
