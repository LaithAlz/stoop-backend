import { createFileRoute, Link } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronLeft, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { SeverityPlaque } from "@/components/clarity/SeverityPlaque";
import { EmergencyBanner } from "@/components/clarity/EmergencyBanner";
import { DayDivider } from "@/components/clarity/DayDivider";
import { ThreadMessageRow } from "@/components/clarity/ThreadMessageRow";
import { AuditMetaLine } from "@/components/clarity/AuditMetaLine";
import { DraftBubble } from "@/components/clarity/DraftBubble";
import { StaleDraftBubble } from "@/components/clarity/StaleDraftBubble";
import { DecisionActions } from "@/components/clarity/DecisionActions";
import { MarginNote } from "@/components/clarity/MarginNote";
import { UndoTicket } from "@/components/clarity/UndoTicket";
import { EditDraftPanel } from "@/components/clarity/EditDraftPanel";
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
import { useAuth } from "@/auth/AuthProvider";
import { useCase, resolveCase, caseQueryKey, casesQueryKey } from "@/api/cases";
import { useQueue, queueQueryKey } from "@/api/queue";
import { ApiError, toHouseApiError } from "@/api/errors";
import type {
  CaseDetail,
  ClassifiedAuditPayload,
  TimelineDraftEntry,
  TimelineEntry,
} from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { entryFor, secondsRemaining, totalUndoSeconds } from "@/features/queue/queueEntries";
import { useDraftActions } from "@/features/queue/useDraftActions";
import { emergencyHeadline, emergencySubtext } from "@/features/emergency/emergencyBanner";
import { buildTimelineRows, type TimelineRow } from "@/features/cases/timeline";
import { isEmergencySignal } from "@/features/cases/emergencySignal";
import {
  RESOLVE_CONFIRM_LABEL,
  RESOLVE_CONFIRM_MESSAGE,
  RESOLVE_CONFIRM_TITLE,
  RESOLVE_DONE_NOTICE,
} from "@/features/cases/resolveCase";

export const Route = createFileRoute("/app/conversations/$id")({
  head: ({ params }) => ({
    meta: [{ title: "Conversation — Stoop." }, { name: "robots", content: "noindex" }],
    links: [{ rel: "canonical", href: `/app/conversations/${params.id}` }],
  }),
  // N2 (safety re-verify, #234 PR 3): TanStack Router REUSES the component
  // instance across /conversations/A → /conversations/B (no key on the
  // match unless remountDeps says so — verified in the router source). This
  // screen now holds per-case local state (`pinnedDraft`, useDraftActions'
  // overlay entries, `editingContext`) that must never survive a case
  // switch: case B rendering case A's pinned draft body — or an open editor
  // wired to A's draft id over B's timeline — is an approval-context
  // mismatch on the "nothing sends without landlord approval" path. Not
  // reachable through today's link graph (thread→thread always routes via
  // the list), but that's an accident of the links, not an invariant.
  remountDeps: ({ params }) => params.id,
  component: ConversationPage,
});

/** Matches DecisionCard.tsx's own fallback verbatim (same web app, same
 *  surface family) rather than apps/mobile's differently-worded one — that
 *  cross-platform drift is a pre-existing, already-flagged gap
 *  (DecisionCard.tsx's own comment), not something this PR resolves. */
const DEFAULT_WHY = "I drafted this from your house rules and past replies.";

/**
 * The full conversation thread — wired to `GET /v1/cases/{id}` (campaign
 * issue #234 PR 3, replacing src/lib/mock-app.ts's `getConversation`).
 * Loading/error states follow src/routes/app.index.tsx's exact pattern:
 * `isPending` for the first load, a full takeover ONLY when
 * `isError && !data`, a quiet refresh strip otherwise. The pending draft's
 * approve/undo/skip/edit-and-send reuses src/features/queue/
 * useDraftActions.ts unchanged — the same hook Home uses — per that file's
 * own docstring ("a later PR wiring the conversation thread's own approve/
 * undo/reject can reuse this unchanged").
 *
 * NOTE (api-contracts.md's Cases section, v1.15 amendment on the Queue
 * section): `GET /v1/cases/{id}` carries no `notification_id` — that field
 * is scoped to the queue card only — so this screen's `EmergencyBanner`
 * never gets an `onAcknowledge`. The acknowledge action lives on Home
 * only, the sole surface the contract actually wires it to.
 */
function ConversationPage() {
  const { id } = Route.useParams();
  const { session } = useAuth();
  const queryClient = useQueryClient();
  const caseQuery = useCase(id, { enabled: Boolean(session) });
  const queueQuery = useQueue({ enabled: Boolean(session) });
  const bottomRef = useRef<HTMLDivElement>(null);

  const onNotice = useCallback((message: string) => toast(message), []);
  const onSettled = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: caseQueryKey(id) });
    void queryClient.invalidateQueries({ queryKey: queueQueryKey });
  }, [queryClient, id]);
  const draftActions = useDraftActions({ onNotice, onSettled });

  const caseDetail = caseQuery.data;
  const tenantFirst = firstName(caseDetail?.tenant.name);

  useEffect(() => {
    if (caseDetail) bottomRef.current?.scrollIntoView({ behavior: "auto" });
    // Deliberately keyed on the case id only, not the whole `caseDetail`
    // object — a background refetch (new timeline entries, a stale-draft
    // notice) must not yank the landlord back to the bottom mid-read; only
    // navigating to a genuinely different conversation should.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseDetail?.id]);

  const rows = useMemo<TimelineRow[]>(
    () => (caseDetail ? buildTimelineRows(caseDetail.timeline) : []),
    [caseDetail],
  );

  const isPendingDraft = (entry: TimelineEntry): entry is TimelineDraftEntry =>
    entry.kind === "draft" && entry.status === "pending";
  const livePendingDraft = caseDetail?.timeline.find(isPendingDraft);

  // M4 (safety review, #234 PR 3 fix round): `livePendingDraft` is looked
  // up fresh from `caseDetail.timeline` on every render — the moment an
  // approve/skip actually applies server-side, that draft's `status`
  // moves off `"pending"`, so a background refetch mid-action (React
  // Query's `staleTime`/`refetchOnWindowFocus`) makes `livePendingDraft`
  // disappear even while the LOCAL overlay (`draftActions.entries`) is
  // still honestly "sending" with a live 5s undo countdown on screen.
  // Pinning the id/body the first time we see them — and continuing to
  // use the pin for as long as the local entry for that id is non-idle —
  // keeps the footer rendered through that window instead of blanking it
  // out from under an active Undo ticket. Mirrors Home's own pinned-item
  // pattern for the identical reason (src/features/queue/queueEntries.ts's
  // `pinnedEditingItem`).
  const [pinnedDraft, setPinnedDraft] = useState<{ id: string; body: string } | null>(null);
  useEffect(() => {
    if (livePendingDraft) {
      setPinnedDraft({ id: livePendingDraft.id, body: livePendingDraft.body });
    }
    // `livePendingDraft` is a NEW object from `.find()` every render —
    // depending on the two primitive fields this effect actually reads
    // (rather than the object reference) is deliberate, not an omission.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [livePendingDraft?.id, livePendingDraft?.body]);

  const pinnedEntry = pinnedDraft ? entryFor(draftActions.entries, pinnedDraft.id) : undefined;
  // N1 (safety re-verify, #234 PR 3): the pin only ever needs to survive
  // the UNDO WINDOW ("sending"). Keeping it for any non-idle status meant
  // that after the countdown expired ("sent") the footer kept rendering
  // the same body as a "I'd like to reply" DraftBubble with "Sent."
  // under it — the landlord's reply shown twice, the second copy dressed
  // as still-queued, on the screen whose whole promise is the record. A
  // skipped draft deliberately falls back to the timeline's own "You
  // skipped this reply — case stayed open." audit line (the honest
  // thread-native representation; Home's muted skip CARD is a queue
  // ruling, not a thread one).
  const keepPinned = Boolean(!livePendingDraft && pinnedDraft && pinnedEntry?.status === "sending");

  const draftId = livePendingDraft?.id ?? (keepPinned ? pinnedDraft?.id : undefined);
  const draftBody = livePendingDraft?.body ?? (keepPinned ? pinnedDraft?.body : undefined);
  const draftEntry = draftId
    ? entryFor(draftActions.entries, draftId)
    : { status: "idle" as const };

  // M5 (safety review, #234 PR 3 fix round): the editor, once open, must
  // render from `useDraftActions`' OWN pinned `editingContext` (draft id +
  // body captured at open time) — never from the live/pinned `draftId`
  // above. A tenant reply arriving mid-type makes the OLD draft `stale`
  // and drafts a NEW one with a different id; deriving the editor's
  // "am I still open for this draft" from the live `draftId` would flip
  // to false the instant that happens and unmount `EditDraftPanel` with
  // whatever the landlord had just typed still in it.
  const editingContext = draftActions.editingContext;

  // Once the local approve overlay settles on "sent", the server's own
  // timeline (a real outbound `message` entry replacing the drafted one,
  // per src/features/cases/timeline.ts's docstring) is the honest next
  // read — refetch instead of waiting for whatever next triggers a fetch.
  // Watches the PINNED entry, not `draftEntry`: N1's `keepPinned` drops to
  // false the instant the entry leaves "sending", which zeroes `draftId`
  // and would make `draftEntry` skip straight to "idle" without this
  // effect ever seeing "sent". Clearing the pin here also retires it for
  // good once the send is final.
  useEffect(() => {
    if (pinnedEntry?.status === "sent") {
      void queryClient.invalidateQueries({ queryKey: caseQueryKey(id) });
      setPinnedDraft(null);
    }
  }, [pinnedEntry?.status, queryClient, id]);

  const isClassifiedAudit = (
    entry: TimelineEntry,
  ): entry is Extract<TimelineEntry, { kind: "audit" }> =>
    entry.kind === "audit" && entry.action === "classified";
  const classifiedEntry = caseDetail?.timeline.find(isClassifiedAudit);
  // M6 (safety review, #234 PR 3 fix round): `payload` is an opaque jsonb
  // blob (ClassifiedAuditPayload is a read-time cast, never a guaranteed
  // shape — see that type's own comment in src/api/types.ts) — a
  // non-string `summary` reaching JSX here would throw at render time and
  // white-screen the whole thread. Guarded rather than trusted.
  const rawWhy = (classifiedEntry?.payload as ClassifiedAuditPayload | undefined)?.summary;
  const why = typeof rawWhy === "string" ? rawWhy : DEFAULT_WHY;

  const caseIsOpen = caseDetail ? caseDetail.status !== "resolved" : false;
  // H1 (safety review, #234 PR 3 fix round): never let a `null` severity
  // (transient pre-classification, or permanent degraded mode — see
  // src/features/cases/emergencySignal.ts's full writeup) suppress a real
  // Tier-0 emergency signal the timeline otherwise carries.
  const showEmergencyBanner = Boolean(caseDetail && caseIsOpen && isEmergencySignal(caseDetail));

  const [resolveConfirmOpen, setResolveConfirmOpen] = useState(false);
  const resolveMutation = useMutation({
    mutationFn: () => resolveCase(id),
    onSuccess: () => {
      toast(RESOLVE_DONE_NOTICE);
      onSettled();
      // A resolve moves this case out of every open-case list — the
      // Conversations tab's own filters must not keep showing it as open
      // until the next fetch.
      void queryClient.invalidateQueries({ queryKey: casesQueryKey() });
      setResolveConfirmOpen(false);
    },
    onError: (error) => {
      onNotice(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      );
      setResolveConfirmOpen(false);
    },
  });

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col bg-clarity-bg">
        <ThreadHeader caseDetail={caseDetail} tenantFirst={tenantFirst} showPlaque={caseIsOpen} />

        <main className="flex-1 overflow-y-auto px-[18px] py-4">
          {caseQuery.isPending ? (
            <div
              role="status"
              aria-live="polite"
              className="flex flex-col items-center gap-3 py-16 text-center"
            >
              <Loader2
                className="size-6 animate-spin text-clarity-brand motion-reduce:animate-none"
                aria-hidden="true"
              />
              <p className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                Loading this conversation…
              </p>
            </div>
          ) : !caseDetail ? (
            // H3 (safety review, #234 PR 3 fix round): this branch is now
            // "no data, whatever the reason" (a genuine fetch error, OR —
            // belt-and-braces — the defensive case a fixed
            // src/api/client.ts should no longer be able to produce: a 2xx
            // that somehow resolved to no usable body). Previously this
            // was `caseQuery.isError && !caseDetail`, with a bare `null`
            // trailing branch below for the "not error, but also no data"
            // gap — a blank screen instead of a readable one.
            <div role="alert" className="flex flex-col items-center gap-3 py-16 text-center">
              <p className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                {caseQuery.error instanceof ApiError
                  ? toHouseApiError(caseQuery.error)
                  : "Couldn't load this conversation. Try again."}
              </p>
              <button
                type="button"
                onClick={() => void caseQuery.refetch()}
                className="inline-flex min-h-12 items-center justify-center rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand px-5 font-clarity-sans text-[15px] font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
              >
                Try again
              </button>
            </div>
          ) : (
            <>
              {caseQuery.isError && (
                <div
                  role="status"
                  className="mb-3.5 rounded-clarity-md border border-clarity-line-strong bg-clarity-panel px-4 py-2.5 font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim"
                >
                  Couldn&apos;t refresh just now — showing the last update.
                </div>
              )}

              {showEmergencyBanner && (
                <EmergencyBanner
                  conversationId={caseDetail.id}
                  headline={emergencyHeadline({
                    title: caseDetail.title,
                    tenant_name: caseDetail.tenant.name ?? "",
                    property_label: caseDetail.property.label,
                  })}
                  subtext={emergencySubtext({ property_label: caseDetail.property.label })}
                  className="mb-4"
                />
              )}

              {rows.map((row) => renderTimelineRow(row, tenantFirst))}

              {editingContext ? (
                <div className="mt-2">
                  <EditDraftPanel
                    tenantName={tenantFirst}
                    initialBody={editingContext.body}
                    submitting={draftActions.isEditSubmitting}
                    onCancel={() => draftActions.cancelEditor()}
                    onSend={(body) => draftActions.submitEdit(body)}
                  />
                  {draftActions.staleNotices[caseDetail.id] && (
                    <p className="mt-2.5 text-right font-clarity-sans text-[13px] font-semibold text-clarity-brand">
                      {draftActions.staleNotices[caseDetail.id]}
                    </p>
                  )}
                </div>
              ) : draftId && draftBody !== undefined ? (
                <div className="mt-2">
                  <DraftFooter
                    tenantFirst={tenantFirst}
                    draftBody={draftBody}
                    draftEntry={draftEntry}
                    why={why}
                    staleNotice={draftActions.staleNotices[caseDetail.id]}
                    isBusy={draftActions.isBusy(draftId)}
                    onApprove={() =>
                      draftActions.approve({
                        draftId,
                        caseId: caseDetail.id,
                        tenantName: caseDetail.tenant.name ?? "",
                      })
                    }
                    onEdit={() =>
                      draftActions.openEditor(
                        {
                          draftId,
                          caseId: caseDetail.id,
                          tenantName: caseDetail.tenant.name ?? "",
                        },
                        draftBody,
                      )
                    }
                    onSkip={() =>
                      draftActions.skip({
                        draftId,
                        caseId: caseDetail.id,
                        tenantName: caseDetail.tenant.name ?? "",
                      })
                    }
                    onUndo={() =>
                      draftActions.undo({
                        draftId,
                        caseId: caseDetail.id,
                        tenantName: caseDetail.tenant.name ?? "",
                      })
                    }
                  />
                </div>
              ) : (
                <p className="mt-4 font-clarity-sans text-xs leading-relaxed text-clarity-ink-dim">
                  Nothing here can be edited or removed once it&rsquo;s sent — that&rsquo;s what
                  makes it useful if you ever need the record.
                </p>
              )}

              {caseIsOpen && (
                <div className="mt-6 flex flex-col items-start gap-1">
                  <button
                    type="button"
                    onClick={() => setResolveConfirmOpen(true)}
                    disabled={resolveMutation.isPending}
                    className="inline-flex min-h-11 items-center gap-1.5 font-clarity-sans text-sm font-extrabold text-clarity-brand disabled:opacity-60"
                  >
                    <Check className="size-4" aria-hidden="true" />
                    {resolveMutation.isPending ? "Resolving…" : RESOLVE_CONFIRM_LABEL}
                  </button>
                  <p className="font-clarity-sans text-xs text-clarity-ink-dim">
                    Closes the case. A drafted reply that hasn&rsquo;t sent won&rsquo;t go out.
                  </p>
                </div>
              )}

              <div ref={bottomRef} />
            </>
          )}
        </main>

        <AppTabBar active="conversations" queueCount={queueQuery.data?.counts.total ?? 0} />
      </div>

      <AlertDialog open={resolveConfirmOpen} onOpenChange={setResolveConfirmOpen}>
        <AlertDialogContent className="border-clarity-line-strong bg-clarity-surface">
          <AlertDialogHeader>
            <AlertDialogTitle className="font-clarity-serif text-clarity-ink">
              {RESOLVE_CONFIRM_TITLE}
            </AlertDialogTitle>
            <AlertDialogDescription className="font-clarity-sans text-clarity-ink-dim">
              {RESOLVE_CONFIRM_MESSAGE}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="border-[1.5px] border-clarity-line-strong bg-clarity-panel font-clarity-sans font-extrabold text-clarity-ink-dim">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => resolveMutation.mutate()}
              disabled={resolveMutation.isPending}
              className="border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans font-extrabold text-clarity-brand-on hover:bg-clarity-brand"
            >
              {resolveMutation.isPending ? "Resolving…" : RESOLVE_CONFIRM_LABEL}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PhoneFrame>
  );
}

function renderTimelineRow(row: TimelineRow, tenantFirst: string) {
  if (row.kind === "day-divider") return <DayDivider key={row.key}>{row.label}</DayDivider>;
  if (row.kind === "message")
    return <ThreadMessageRow key={row.key} entry={row.entry} tenantFirst={tenantFirst} />;
  if (row.kind === "audit")
    return <AuditMetaLine key={row.key} label={row.label} at={row.entry.at} />;
  // "draft" rows: a live PENDING draft renders via the pinned footer below,
  // never inline (that would duplicate the one actionable bubble at the
  // foot of the thread). A `stale` draft has no footer — nothing else
  // duplicates it — so it renders here as a muted, non-sendable bubble
  // (issue #256: conversation-model.md's stale-draft rule, "kept in the
  // audit trail, never shown as sendable again").
  if (row.kind === "draft" && row.entry.status === "stale") {
    return <StaleDraftBubble key={row.key} className="ml-auto max-w-[83%]" body={row.entry.body} />;
  }
  return null;
}

function ThreadHeader({
  caseDetail,
  tenantFirst,
  showPlaque,
}: {
  caseDetail: CaseDetail | undefined;
  tenantFirst: string;
  showPlaque: boolean;
}) {
  // H1 (safety review, #234 PR 3 fix round): a `null` severity must not
  // render as "no plaque" (which reads as "nothing to flag here") when
  // the timeline actually carries an `emergency_triggered` audit row —
  // see src/features/cases/emergencySignal.ts's full writeup.
  //
  // #256 comment item 1 (safety re-verify): the ORIGINAL version of this
  // let a real, non-null `severity` always win over the Tier-0 signal —
  // correct for the ordinary "null until classified" case, but wrong for
  // the clamp-failure case emergencySignal.ts's own writeup documents: a
  // Tier-0 trigger fired AND the backend's never-de-escalate clamp still
  // somehow wrote a severity below "emergency". That combination has to
  // read as an active emergency HERE too — the banner right below this
  // header already does (`showEmergencyBanner` uses the same
  // `isEmergencySignal`, unconditionally), so the plaque one line above it
  // must never show a calmer word ("Routine") directly over an emergency
  // banner. `isEmergencySignal` wins outright now; the written severity is
  // only consulted once that signal is false.
  const plaqueSeverity =
    caseDetail && isEmergencySignal(caseDetail) ? "emergency" : (caseDetail?.severity ?? null);

  return (
    <header className="border-b border-clarity-line px-5 pb-3.5 pt-4">
      <div className="flex items-center justify-between gap-2">
        <Link
          to="/app/conversations"
          className="inline-flex min-h-11 items-center gap-1 py-1 font-clarity-sans text-[13px] font-bold text-clarity-ink-dim hover:text-clarity-ink"
        >
          <ChevronLeft className="size-[15px]" aria-hidden="true" />
          Conversations
        </Link>
        {plaqueSeverity && showPlaque && <SeverityPlaque severity={plaqueSeverity} />}
      </div>
      <h1 className="mt-2 font-clarity-serif text-[19px] font-semibold leading-[1.3] tracking-tight text-clarity-ink">
        {caseDetail
          ? `${tenantFirst}${caseDetail.tenant.unit ? ` — Unit ${caseDetail.tenant.unit}` : ""}`
          : "Conversation"}
      </h1>
      <p className="mt-0.5 font-clarity-sans text-[12.5px] text-clarity-ink-dim">
        {caseDetail ? `${caseDetail.property.address_line1} · ` : ""}every message, saved with dates
        and times
      </p>
    </header>
  );
}

/**
 * The single pending draft at the foot of the thread — Stoop's dashed
 * "I'd like to reply" bubble, the always-visible margin note, and the
 * Edit / Skip / Approve & send row (the same decision Home's
 * `DecisionCard` renders, now the last thing in the full history rather
 * than its own card).
 */
function DraftFooter({
  tenantFirst,
  draftBody,
  draftEntry,
  why,
  staleNotice,
  isBusy,
  onApprove,
  onEdit,
  onSkip,
  onUndo,
}: {
  tenantFirst: string;
  draftBody: string;
  draftEntry: ReturnType<typeof entryFor>;
  why: string;
  staleNotice?: string;
  isBusy: boolean;
  onApprove: () => void;
  onEdit: () => void;
  onSkip: () => void;
  onUndo: () => void;
}) {
  const isSending = draftEntry.status === "sending";
  const isSent = draftEntry.status === "sent";
  const isSkipped = draftEntry.status === "skipped";
  const secondsLeft =
    draftEntry.status === "sending" ? secondsRemaining(draftEntry.undoExpiresAtClient) : 0;
  const totalSeconds = draftEntry.status === "sending" ? totalUndoSeconds(draftEntry) : 5;

  if (isSkipped) {
    return (
      <p className="rounded-clarity-lg border border-dashed border-clarity-line-strong bg-clarity-bg px-[18px] py-3.5 text-center font-clarity-sans text-[13px] text-clarity-ink-dim">
        No reply sent — case still open
      </p>
    );
  }

  return (
    <div>
      <DraftBubble
        className="ml-auto max-w-[83%]"
        label={isSending ? `On its way to ${tenantFirst}` : "I'd like to reply"}
        body={draftBody}
      />
      {staleNotice && (
        <p className="mt-2.5 text-right font-clarity-sans text-[13px] font-semibold text-clarity-brand">
          {staleNotice}
        </p>
      )}
      {isSending ? (
        <UndoTicket
          secondsLeft={secondsLeft}
          totalSeconds={totalSeconds}
          onUndo={onUndo}
          undoDisabled={isBusy}
        />
      ) : isSent ? (
        <p className="mt-3.5 text-right font-clarity-sans text-[13px] font-semibold text-clarity-whenever">
          Sent.
        </p>
      ) : (
        <>
          <MarginNote>{why}</MarginNote>
          <DecisionActions
            onEdit={onEdit}
            onSkip={onSkip}
            onApprove={onApprove}
            disabled={isBusy}
          />
        </>
      )}
    </div>
  );
}
