import { createFileRoute } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { GreetingHeader } from "@/components/clarity/GreetingHeader";
import { CountsStrip } from "@/components/clarity/CountsStrip";
import { EmergencyBanner } from "@/components/clarity/EmergencyBanner";
import { DecisionCard } from "@/components/clarity/DecisionCard";
import { UNVERIFIED_SEND_NOTICE } from "@/components/clarity/EditDraftPanel";
import { SkippedCard } from "@/components/clarity/SkippedCard";
import { AllClearState } from "@/components/clarity/AllClearState";
import { useAuth } from "@/auth/AuthProvider";
import { QUEUE_REFETCH_INTERVAL_MS, useQueue } from "@/api/queue";

/**
 * How long past an ambiguous edit-and-send failure a queue read must be
 * before "the draft is still pending" is trusted enough to re-enable Send
 * (F11 — see the effect below).
 *
 * Sized against the SERVER's worst case, not the poll interval. The API's
 * per-case advisory lock retries for `_CASE_LOCK_MAX_WAIT_SECONDS = 30s`
 * (apps/api/app/agent/graph.py) BEFORE the graph resume even begins, so an
 * edit-and-send can legitimately commit ~30s after the request started
 * while the client stamped its failure in the first second. One poll
 * interval was shorter than that ceiling and left a window where a
 * qualifying read still predated the commit. Two intervals clears it with
 * margin; the cost is Send staying disabled a little longer under an
 * on-screen explanation, which is the safe direction by construction.
 */
const UNVERIFIED_SETTLE_MS = 2 * QUEUE_REFETCH_INTERVAL_MS;
import { ApiError, toHouseApiError } from "@/api/errors";
import type { QueueItem } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { formatRelativeTime } from "@/lib/relativeTime";
import {
  buildQueueView,
  pruneSkippedSnapshots,
  secondsRemaining,
  totalUndoSeconds,
  type QueueViewRow,
} from "@/features/queue/queueEntries";
import { useDraftActions } from "@/features/queue/useDraftActions";
import {
  emergencyHeadline,
  emergencySubtext,
  emergencyTenantMessage,
  hasAcknowledgeableNotification,
} from "@/features/emergency/emergencyBanner";
import { useAcknowledge } from "@/features/emergency/useAcknowledge";

export const Route = createFileRoute("/app/")({
  head: () => ({
    meta: [{ title: "Home — Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: AppQueuePage,
});

/**
 * Home — the real approval queue (issue #234 PR 2). Fetches `GET /v1/queue`
 * (src/api/queue.ts) and layers the local approve/undo/skip state machine
 * (src/features/queue/useDraftActions.ts + queueEntries.ts) on top — see
 * queueEntries.ts's docstring for why a local overlay exists at all over a
 * server that only ever lists cases still needing action. Ported from
 * apps/mobile/src/app/(tabs)/index.tsx's same wiring.
 *
 * mock-app.ts is intentionally NOT imported here — the conversation routes
 * are live too as of campaign issue #234 PR 3 (src/api/cases.ts), so every
 * card/banner below links straight into them; properties/account stay on
 * mock-app.ts until their own PRs land (see the PR report). The empty
 * state below only ever renders once a real, successful fetch says the
 * queue is actually empty.
 */
function AppQueuePage() {
  const { session } = useAuth();
  // Defense-in-depth session gate — see src/api/queue.ts's docstring for
  // why this is a SECOND, independent gate on top of the route guard that
  // already keeps this component from ever mounting unauthenticated.
  const queueQuery = useQueue({ enabled: Boolean(session) });

  const [skippedSnapshots, setSkippedSnapshots] = useState<Record<string, QueueItem>>({});
  // A7 (safety review, #234 PR 2): the item whose editor is open, captured
  // at open time — buildQueueView pins it so a background poll that drops
  // the row can't unmount the editor mid-type. Cleared as soon as the
  // editor closes (the effect below).
  const [editingSnapshot, setEditingSnapshot] = useState<QueueItem | null>(null);

  const onNotice = useCallback((message: string) => toast(message), []);
  const onSettled = useCallback(() => void queueQuery.refetch(), [queueQuery]);
  const draftActions = useDraftActions({ onNotice, onSettled });
  const { entries } = draftActions;
  const acknowledge = useAcknowledge({ onNotice });

  // Once the server confirms a "sent" card is really gone from the queue,
  // drop its local entry too — otherwise it just sits inert forever.
  useEffect(() => {
    if (!queueQuery.data) return;
    const freshIds = new Set(queueQuery.data.items.map((item) => item.draft_id));
    for (const [draftId, entry] of Object.entries(entries)) {
      if (entry.status === "sent" && !freshIds.has(draftId)) {
        draftActions.dispatch({ type: "cleared", draftId });
      }
    }
    // draftActions.dispatch is stable (useReducer) — entries is the only
    // real dependency here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queueQuery.data, entries]);

  // R3-1 (safety review round 3 follow-up, issue #252): resolve any
  // edit-and-send left `unverifiedSendIds` by useDraftActions.ts's
  // ambiguous-failure branch against THIS successful queue read — still
  // listed as a card means the edit never applied (re-enable Send); gone
  // means it did (close the editor + notice if it's still open on it).
  //
  // F1 (safety re-verify, #252): resolve ONLY against a read that
  // completed after the failure. Without the `dataUpdatedAt` comparison
  // this effect fired on the very next commit — when `queueQuery.data` is
  // still the last successful payload from BEFORE the send, which of
  // course still lists the draft — so it resolved "still pending",
  // re-enabled Send about one frame later, and the guard did nothing at
  // all. `isFetching` alone isn't sufficient; the generation is.
  //
  // F11 (safety re-verify round 2, #252): the two directions need
  // DIFFERENT evidence, because the server request outlives the client's
  // error. `POST /v1/drafts/{id}/edit-and-send` synchronously resumes the
  // LangGraph thread under a per-case lock — hundreds of ms to seconds —
  // and the ambiguous triggers (edge 504, client timeout, dropped
  // connection) all leave the origin still working. So a read completing
  // 200ms after the failure can honestly report the draft still `pending`
  // while the origin commits a second later. Resolving "still pending" on
  // that read re-enables Send permanently, and the retype-and-resend
  // lands on the idempotent 200 that discards the new body.
  //   gone          → definitive on the FIRST post-failure read (the
  //                   editor closes either way; a resend can only 409 or
  //                   hit the idempotent 200).
  //   still pending → only trustworthy once a full poll interval has
  //                   elapsed past the failure, by which time an
  //                   in-flight commit has long landed. Costs one extra
  //                   poll of dead Send under the explanatory line —
  //                   the safe direction, by construction.
  useEffect(() => {
    if (!queueQuery.data) return;
    const freshIds = new Set(queueQuery.data.items.map((item) => item.draft_id));
    for (const [draftId, failedAt] of draftActions.unverifiedSendIds) {
      if (queueQuery.dataUpdatedAt <= failedAt) continue;
      const stillPending = freshIds.has(draftId);
      if (stillPending && queueQuery.dataUpdatedAt <= failedAt + UNVERIFIED_SETTLE_MS) {
        continue;
      }
      draftActions.resolveUnverifiedSend(draftId, stillPending);
    }
    // `draftActions.resolveUnverifiedSend` is a useCallback over
    // [onNotice, unverifiedSendIds] — both already covered by the deps
    // below, so listing the whole `draftActions` object would only add
    // churn. (It was `[onNotice]` alone until F12 added the membership
    // read; keeping this comment truthful matters, since every defect
    // found in this file so far has been a comment asserting a guarantee
    // the code no longer had.)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queueQuery.data, queueQuery.dataUpdatedAt, draftActions.unverifiedSendIds]);

  const items = useMemo(() => queueQuery.data?.items ?? [], [queueQuery.data]);
  // Rule #1: the emergency line is never paywalled, throttled, or gated —
  // every emergency renders its own banner, never just the first one.
  //
  // F10 (safety re-verify round 2, #252): severity is NOT the only
  // emergency signal available here. `notification_id` is populated only
  // when the case has an UNACKNOWLEDGED emergency-call notification
  // (app/routers/queue.py's third LATERAL) — an authoritative "this
  // landlord's phone is ringing about this case" flag, and the Home
  // analogue of the `emergency_triggered` audit row the thread already
  // keys on. Without it, a card whose severity is null (a defensive
  // classification miss) or written LOWER than emergency (the clamp
  // failure this same PR fixed on the thread's plaque) rendered as an
  // ordinary decision card: no banner, and — worse — no acknowledge
  // button at all, while the escalation chain was still calling. The
  // dashboard has to agree with the phone.
  const isEmergencyCard = (item: QueueItem) =>
    item.severity === "emergency" || item.notification_id !== null;
  const emergencyItems = useMemo(() => items.filter(isEmergencyCard), [items]);
  const decisionItems = useMemo(() => items.filter((item) => !isEmergencyCard(item)), [items]);
  const editingContext = draftActions.editingContext;
  useEffect(() => {
    if (!editingContext) setEditingSnapshot(null);
  }, [editingContext]);

  const rows = useMemo(
    () =>
      buildQueueView(
        decisionItems,
        entries,
        skippedSnapshots,
        editingContext ? editingSnapshot : null,
      ),
    [decisionItems, entries, skippedSnapshots, editingContext, editingSnapshot],
  );

  const needYou = queueQuery.data?.counts.total ?? 0;
  const waitingOnTenants = queueQuery.data?.counts.awaiting_tenant ?? 0;

  function handleSkip(item: QueueItem) {
    setSkippedSnapshots((prev) => ({
      ...pruneSkippedSnapshots(prev, entries),
      [item.draft_id]: item,
    }));
    draftActions.skip({
      draftId: item.draft_id,
      caseId: item.case_id,
      tenantName: item.tenant_name,
    });
  }

  function handleOpenEditor(item: QueueItem) {
    setEditingSnapshot(item);
    draftActions.openEditor(
      { draftId: item.draft_id, caseId: item.case_id, tenantName: item.tenant_name },
      item.draft_body,
    );
  }

  // `Boolean(data)`, not `isSuccess` — during a failed background refetch
  // (B1 below) the last successful payload is still what's on screen, and
  // an empty one still honestly means "all clear as of the last update"
  // (the refresh strip right above says exactly that).
  const showAllClear = Boolean(queueQuery.data) && rows.length === 0 && emergencyItems.length === 0;

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col bg-clarity-bg">
        <GreetingHeader>
          <CountsStrip needYou={needYou} waitingOnTenants={waitingOnTenants} />
        </GreetingHeader>

        <main className="flex-1 overflow-y-auto px-[18px] py-4">
          {/* A9 (safety review, #234 PR 2): `isPending`, not `isLoading` —
              a query that's gated off (`enabled: false`, e.g. the brief
              session-settling gap) has isLoading false but has never
              fetched, and used to fall through to the content branch and
              render an EMPTY queue as if the server had said "all clear".
              isPending is true until a first result actually exists. */}
          {queueQuery.isPending ? (
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
                Loading your queue…
              </p>
            </div>
          ) : queueQuery.isError && !queueQuery.data ? (
            <div role="alert" className="flex flex-col items-center gap-3 py-16 text-center">
              <p className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                {queueQuery.error instanceof ApiError
                  ? toHouseApiError(queueQuery.error)
                  : "Couldn't load your queue. Try again."}
              </p>
              <button
                type="button"
                onClick={() => void queueQuery.refetch()}
                className="inline-flex min-h-12 items-center justify-center rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand px-5 font-clarity-sans text-[15px] font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
              >
                Try again
              </button>
            </div>
          ) : (
            <>
              {/* B1 (safety review, #234 PR 2): the full error takeover
                  above only ever renders when NO data has ever loaded.
                  TanStack Query keeps the last successful payload through
                  a failed background refetch — blanking the whole screen
                  (including a live emergency banner and its ack button)
                  over a transient refetch error is the wrong failure
                  direction, so a refetch failure renders as this quiet
                  strip over the preserved data instead. The banners/cards
                  below carry a `conversationId` (the card's `case_id`) so
                  they link into the live conversation routes (campaign
                  issue #234 PR 3). */}
              {emergencyItems.map((item) => (
                <EmergencyBanner
                  key={item.case_id}
                  conversationId={item.case_id}
                  headline={emergencyHeadline(item)}
                  subtext={emergencySubtext(item)}
                  tenantFirstName={firstName(item.tenant_name)}
                  tenantMessage={emergencyTenantMessage(item)}
                  onAcknowledge={
                    hasAcknowledgeableNotification(item)
                      ? () => acknowledge.acknowledge(item.notification_id)
                      : undefined
                  }
                  acknowledging={
                    hasAcknowledgeableNotification(item)
                      ? acknowledge.isAcknowledging(item.notification_id)
                      : false
                  }
                />
              ))}

              {queueQuery.isError && (
                <div
                  role="status"
                  className="mb-3.5 rounded-clarity-md border border-clarity-line-strong bg-clarity-panel px-4 py-2.5 font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim"
                >
                  Couldn&apos;t refresh just now — showing the last update.
                </div>
              )}

              {showAllClear ? (
                <AllClearState message="I'm watching your messages — go enjoy your day. I'll text you if anything needs you." />
              ) : (
                rows.length > 0 && (
                  <div className="space-y-3.5">
                    {rows.map((row) => (
                      <QueueRow
                        key={row.item.draft_id}
                        row={row}
                        draftActions={draftActions}
                        onSkip={handleSkip}
                        onOpenEditor={handleOpenEditor}
                      />
                    ))}
                  </div>
                )
              )}
            </>
          )}
        </main>

        <AppTabBar active="home" queueCount={needYou} />
      </div>
    </PhoneFrame>
  );
}

function QueueRow({
  row,
  draftActions,
  onSkip,
  onOpenEditor,
}: {
  row: QueueViewRow;
  draftActions: ReturnType<typeof useDraftActions>;
  onSkip: (item: QueueItem) => void;
  onOpenEditor: (item: QueueItem) => void;
}) {
  const { item, entry } = row;

  if (entry.status === "skipped") {
    return (
      <SkippedCard
        conversationId={item.case_id}
        tenantName={firstName(item.tenant_name)}
        propertyLabel={item.property_label}
        timestamp={formatRelativeTime(item.received_at)}
      />
    );
  }

  const isEditingThis = draftActions.editingContext?.draftId === item.draft_id;
  const cardStatus =
    entry.status === "sending"
      ? "sending"
      : entry.status === "sent"
        ? "sent"
        : isEditingThis
          ? "editing"
          : "pending";
  const secondsLeft = entry.status === "sending" ? secondsRemaining(entry.undoExpiresAtClient) : 0;
  const totalSeconds = entry.status === "sending" ? totalUndoSeconds(entry) : 5;
  const ctx = { draftId: item.draft_id, caseId: item.case_id, tenantName: item.tenant_name };
  // Live `unit` is a bare label ("2", "4", "A") — composed with the
  // property label the same way the mock's `unitLabel`/`propertyLabel`
  // pair was, no new copy invented.
  const propertyLabel = item.unit
    ? `Unit ${item.unit}, ${item.property_label}`
    : item.property_label;

  return (
    <DecisionCard
      severity={item.severity}
      conversationId={item.case_id}
      tenantName={firstName(item.tenant_name)}
      propertyLabel={propertyLabel}
      timestamp={formatRelativeTime(item.received_at)}
      tenantMessage={item.tenant_message ?? ""}
      photoNote={item.has_media ? (item.media_note ?? "Sent a photo") : undefined}
      draftMessage={item.draft_body}
      why={item.why}
      status={cardStatus}
      secondsLeft={secondsLeft}
      totalSeconds={totalSeconds}
      // F7 (safety re-verify round 2, #252): with the guard actually
      // holding, Cancel is the only live control in the editor — so it
      // becomes the path of least resistance, and behind it the card
      // used to return to a full action row with Approve enabled and no
      // marking at all. One tap there sends the ORIGINAL, un-edited body:
      // exactly the wording the landlord opened the editor to fix. The
      // card now carries the same explanation and the same block.
      staleNotice={
        draftActions.staleNotices[item.case_id] ??
        (draftActions.isSendUnverified(item.draft_id) ? UNVERIFIED_SEND_NOTICE : undefined)
      }
      editSubmitting={draftActions.isEditSubmitting}
      sendUnverified={draftActions.isSendUnverified(item.draft_id)}
      actionsBusy={
        draftActions.isBusy(item.draft_id) || draftActions.isSendUnverified(item.draft_id)
      }
      onApprove={() => draftActions.approve(ctx)}
      onEdit={() => onOpenEditor(item)}
      onSkip={() => onSkip(item)}
      onUndo={() => draftActions.undo(ctx)}
      onCancelEdit={() => draftActions.cancelEditor()}
      onSubmitEdit={(body) => draftActions.submitEdit(body)}
    />
  );
}
