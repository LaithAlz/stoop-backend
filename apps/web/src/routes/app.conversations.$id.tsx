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
  const pendingDraft = caseDetail?.timeline.find(isPendingDraft);
  const draftId = pendingDraft?.id;
  const draftBody = pendingDraft?.body;
  const draftEntry = draftId
    ? entryFor(draftActions.entries, draftId)
    : { status: "idle" as const };
  const isEditingThisDraft = draftId ? draftActions.editingContext?.draftId === draftId : false;

  // Once the local approve overlay settles on "sent", the server's own
  // timeline (a real outbound `message` entry replacing the drafted one,
  // per src/features/cases/timeline.ts's docstring) is the honest next
  // read — refetch instead of waiting for whatever next triggers a fetch.
  useEffect(() => {
    if (draftEntry.status === "sent") {
      void queryClient.invalidateQueries({ queryKey: caseQueryKey(id) });
    }
  }, [draftEntry.status, queryClient, id]);

  const isClassifiedAudit = (
    entry: TimelineEntry,
  ): entry is Extract<TimelineEntry, { kind: "audit" }> =>
    entry.kind === "audit" && entry.action === "classified";
  const classifiedEntry = caseDetail?.timeline.find(isClassifiedAudit);
  const why =
    (classifiedEntry?.payload as ClassifiedAuditPayload | undefined)?.summary ?? DEFAULT_WHY;

  const caseIsOpen = caseDetail ? caseDetail.status !== "resolved" : false;

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
          ) : caseQuery.isError && !caseDetail ? (
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
          ) : caseDetail ? (
            <>
              {caseQuery.isError && (
                <div
                  role="status"
                  className="mb-3.5 rounded-clarity-md border border-clarity-line-strong bg-clarity-panel px-4 py-2.5 font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim"
                >
                  Couldn&apos;t refresh just now — showing the last update.
                </div>
              )}

              {caseDetail.severity === "emergency" && caseIsOpen && (
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

              {draftId && draftBody !== undefined ? (
                <div className="mt-2">
                  {isEditingThisDraft ? (
                    <EditDraftPanel
                      tenantName={tenantFirst}
                      initialBody={draftBody}
                      submitting={draftActions.isEditSubmitting}
                      onCancel={() => draftActions.cancelEditor()}
                      onSend={(body) => draftActions.submitEdit(body)}
                    />
                  ) : (
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
                  )}
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
          ) : null}
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
  return null; // "draft" rows render via the pinned footer below, not inline.
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
        {caseDetail?.severity && showPlaque && <SeverityPlaque severity={caseDetail.severity} />}
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
