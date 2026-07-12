import { createFileRoute, Link, notFound, useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { Check, ChevronLeft } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { SeverityPlaque } from "@/components/clarity/SeverityPlaque";
import { EmergencyBanner } from "@/components/clarity/EmergencyBanner";
import { DayDivider } from "@/components/clarity/DayDivider";
import { ThreadMessageRow } from "@/components/clarity/ThreadMessageRow";
import { DraftBubble } from "@/components/clarity/DraftBubble";
import { DecisionActions } from "@/components/clarity/DecisionActions";
import { MarginNote } from "@/components/clarity/MarginNote";
import { UndoTicket } from "@/components/clarity/UndoTicket";
import {
  getConversation,
  properties,
  queue,
  DEFAULT_WHY,
  type QueueItem,
  type TimelineAuditEntry,
  type TimelineDraftEntry,
  type TimelineMessageEntry,
} from "@/lib/mock-app";

const SEND_WINDOW_SECONDS = 5;

export const Route = createFileRoute("/app/conversations/$id")({
  head: ({ params }) => ({
    meta: [{ title: "Conversation — Stoop." }, { name: "robots", content: "noindex" }],
    links: [{ rel: "canonical", href: `/app/conversations/${params.id}` }],
  }),
  loader: ({ params }) => {
    const item = getConversation(params.id);
    if (!item) throw notFound();
    return item;
  },
  notFoundComponent: () => (
    <PhoneFrame>
      <div className="flex flex-1 flex-col items-center justify-center bg-clarity-bg px-6 text-center">
        <p className="font-clarity-mono text-xs font-bold uppercase tracking-widest text-clarity-ink-dim">
          404
        </p>
        <h1 className="mt-2 font-clarity-serif text-2xl font-semibold text-clarity-ink">
          Conversation not found.
        </h1>
        <Link
          to="/app"
          className="mt-6 inline-flex min-h-12 items-center rounded-clarity-md bg-clarity-brand px-5 font-clarity-sans text-sm font-bold text-clarity-brand-on"
        >
          Back to Home
        </Link>
      </div>
    </PhoneFrame>
  ),
  errorComponent: ConversationErrorComponent,
  component: ConversationPage,
});

function ConversationErrorComponent({ reset }: { reset: () => void }) {
  const router = useRouter();
  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col items-center justify-center bg-clarity-bg px-6 text-center">
        <h1 className="font-clarity-serif text-xl font-semibold text-clarity-ink">
          Couldn't load this thread.
        </h1>
        <button
          type="button"
          onClick={() => {
            router.invalidate();
            reset();
          }}
          className="mt-6 inline-flex min-h-12 items-center rounded-clarity-md bg-clarity-brand px-5 font-clarity-sans text-sm font-bold text-clarity-brand-on"
        >
          Try again
        </button>
      </div>
    </PhoneFrame>
  );
}

function ConversationPage() {
  // The loader above always throws notFound() before returning undefined,
  // so `Route.useLoaderData()` never actually resolves to `undefined` here
  // — narrowed once, right after the hook call, so every access below
  // reads a plain `QueueItem` instead of re-litigating its (type-only,
  // never true at runtime) possible undefined-ness at each site.
  const item = Route.useLoaderData() as QueueItem;
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "auto" });
  }, [item.id]);

  const address = properties.find((p) => p.id === item.propertyId)?.address;
  const messageEntries = item.timeline.filter(
    (e): e is TimelineMessageEntry => e.kind === "message",
  );
  const draftEntry = item.timeline.find((e): e is TimelineDraftEntry => e.kind === "draft");
  const auditEntry = item.timeline.find((e): e is TimelineAuditEntry => e.kind === "audit");
  const why = auditEntry?.payload.summary ?? item.why ?? DEFAULT_WHY;
  const caseOpen = item.status !== "resolved";

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col bg-clarity-bg">
        <ThreadHeader item={item} address={address} showPlaque={caseOpen} />

        <main className="flex-1 overflow-y-auto px-[18px] py-4">
          {item.severity === "emergency" && caseOpen && (
            <EmergencyBanner
              conversationId={item.id}
              headline={item.title}
              subtext={`${item.propertyLabel} · tap to call ${item.tenantFirst} now`}
              className="mb-4"
            />
          )}

          {messageEntries.map((entry, i) => (
            <div key={i}>
              {entry.dayLabel && <DayDivider>{entry.dayLabel}</DayDivider>}
              <ThreadMessageRow entry={entry} tenantFirst={item.tenantFirst} />
            </div>
          ))}

          {draftEntry ? (
            <DraftReply key={item.id} item={item} draft={draftEntry} why={why} />
          ) : (
            auditEntry && <MarginNote>{why}</MarginNote>
          )}

          <p className="mt-4 font-clarity-sans text-xs leading-relaxed text-clarity-ink-dim">
            Nothing here can be edited or removed once it's sent — that's what makes it useful if
            you ever need the record.
          </p>

          <div ref={bottomRef} />
        </main>

        <AppTabBar active="conversations" queueCount={queue.length} />
      </div>
    </PhoneFrame>
  );
}

function ThreadHeader({
  item,
  address,
  showPlaque,
}: {
  item: QueueItem;
  address?: string;
  showPlaque: boolean;
}) {
  return (
    <header className="border-b border-clarity-line px-5 pb-3.5 pt-4">
      <div className="flex items-center justify-between gap-2">
        <Link
          to="/app"
          className="inline-flex min-h-11 items-center gap-1 py-1 font-clarity-sans text-[13px] font-bold text-clarity-ink-dim hover:text-clarity-ink"
        >
          <ChevronLeft className="size-[15px]" aria-hidden="true" />
          Home
        </Link>
        {showPlaque && <SeverityPlaque severity={item.severity} />}
      </div>
      <h1 className="mt-2 font-clarity-serif text-[19px] font-semibold leading-[1.3] tracking-tight text-clarity-ink">
        {item.tenantFirst} — {item.unitLabel}
      </h1>
      <p className="mt-0.5 font-clarity-sans text-[12.5px] text-clarity-ink-dim">
        {address ? `${address} · ` : ""}every message, saved with dates and times
      </p>
    </header>
  );
}

type DraftMode = "pending" | "editing" | "sending" | "sent" | "skipped";

/**
 * The single pending draft at the foot of the thread — Stoop's dashed
 * "I'd like to reply" bubble, the always-visible margin note, and the
 * Edit / Skip / Approve & send row (the same decision Home's
 * `DecisionCard` renders, now the last thing in the full history rather
 * than its own card). Keyed by `item.id` from the parent so navigating
 * to a different conversation resets this state instead of carrying it
 * over.
 */
function DraftReply({
  item,
  draft,
  why,
}: {
  item: QueueItem;
  draft: TimelineDraftEntry;
  why: string;
}) {
  const [mode, setMode] = useState<DraftMode>("pending");
  const [body, setBody] = useState(draft.body);
  const [showOriginal, setShowOriginal] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState(SEND_WINDOW_SECONDS);

  useEffect(() => {
    if (mode !== "sending") return;
    const timer = setInterval(() => {
      setSecondsLeft((s) => {
        if (s <= 1) {
          setMode("sent");
          return 0;
        }
        return s - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [mode]);

  function handleApprove() {
    setSecondsLeft(SEND_WINDOW_SECONDS);
    setMode("sending");
  }

  function handleUndo() {
    setSecondsLeft(SEND_WINDOW_SECONDS);
    setMode("pending");
  }

  if (mode === "sent") {
    return (
      <ThreadMessageRow
        entry={{
          kind: "message",
          direction: "outbound",
          party: "tenant",
          body,
          media: [],
          at: "just now",
        }}
        tenantFirst={item.tenantFirst}
      />
    );
  }

  if (mode === "skipped") {
    return (
      <div className="rounded-clarity-lg border border-dashed border-clarity-line-strong bg-clarity-bg px-[18px] py-3.5 text-center font-clarity-sans text-[13px] text-clarity-ink-dim">
        No reply sent — case still open
      </div>
    );
  }

  if (mode === "editing") {
    return (
      <div className="ml-auto flex max-w-[83%] flex-col gap-2">
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center gap-1 rounded-clarity-sm border border-clarity-brand-border bg-clarity-bg px-2 py-0.5 font-clarity-sans text-[10px] font-bold uppercase tracking-widest text-clarity-brand">
            Editing draft
          </span>
          <button
            type="button"
            onClick={() => setShowOriginal((s) => !s)}
            className="inline-flex min-h-11 items-center font-clarity-sans text-[11.5px] font-bold text-clarity-ink-dim underline-offset-4 hover:text-clarity-brand hover:underline"
          >
            {showOriginal ? "Hide original" : "See original"}
          </button>
        </div>

        {showOriginal && (
          <blockquote className="rounded-clarity-md border border-clarity-line-strong bg-clarity-surface px-3 py-2 font-clarity-serif text-xs italic leading-relaxed text-clarity-ink-dim">
            {draft.body}
          </blockquote>
        )}

        <label htmlFor={`draft-edit-${item.id}`} className="sr-only">
          Edit your reply to {item.tenantFirst}
        </label>
        <textarea
          id={`draft-edit-${item.id}`}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          className="min-h-32 w-full rounded-clarity-lg rounded-tr-clarity-sm border-[1.5px] border-clarity-brand-border bg-clarity-brand-soft p-4 font-clarity-serif text-[15.5px] italic leading-relaxed text-clarity-ink"
        />

        <div className="mt-1 grid grid-cols-[auto_1fr] gap-2.5">
          <button
            type="button"
            onClick={() => {
              setBody(draft.body);
              setMode("pending");
            }}
            className="inline-flex min-h-[52px] items-center justify-center rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-4 font-clarity-sans text-[15px] font-extrabold text-clarity-ink-dim transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleApprove}
            className="flex min-h-[52px] items-center justify-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans text-base font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
          >
            <Check className="size-4" aria-hidden="true" />
            Send edited version
          </button>
        </div>
      </div>
    );
  }

  const isSending = mode === "sending";
  return (
    <div>
      <div className="ml-auto max-w-[83%]">
        <DraftBubble
          label={isSending ? `On its way to ${item.tenantFirst}` : "I'd like to reply"}
          body={body}
        />
      </div>
      {isSending ? (
        <UndoTicket
          secondsLeft={secondsLeft}
          totalSeconds={SEND_WINDOW_SECONDS}
          onUndo={handleUndo}
        />
      ) : (
        <>
          <MarginNote>{why}</MarginNote>
          <DecisionActions
            onEdit={() => setMode("editing")}
            onSkip={() => setMode("skipped")}
            onApprove={handleApprove}
          />
        </>
      )}
    </div>
  );
}
