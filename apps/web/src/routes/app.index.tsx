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
import { SkippedCard } from "@/components/clarity/SkippedCard";
import { AllClearState } from "@/components/clarity/AllClearState";
import { useAuth } from "@/auth/AuthProvider";
import { useQueue } from "@/api/queue";
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
 * mock-app.ts is intentionally NOT imported here anymore — conversations/
 * properties/account stay on it until their own PRs land (see the PR
 * report). The empty state below only ever renders once a real,
 * successful fetch says the queue is actually empty.
 */
function AppQueuePage() {
  const { session } = useAuth();
  // Defense-in-depth session gate — see src/api/queue.ts's docstring for
  // why this is a SECOND, independent gate on top of the route guard that
  // already keeps this component from ever mounting unauthenticated.
  const queueQuery = useQueue({ enabled: Boolean(session) });

  const [skippedSnapshots, setSkippedSnapshots] = useState<Record<string, QueueItem>>({});

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

  const items = useMemo(() => queueQuery.data?.items ?? [], [queueQuery.data]);
  // Rule #1: the emergency line is never paywalled, throttled, or gated —
  // every emergency renders its own banner, never just the first one.
  const emergencyItems = useMemo(
    () => items.filter((item) => item.severity === "emergency"),
    [items],
  );
  const decisionItems = useMemo(
    () => items.filter((item) => item.severity !== "emergency"),
    [items],
  );
  const rows = useMemo(
    () => buildQueueView(decisionItems, entries, skippedSnapshots),
    [decisionItems, entries, skippedSnapshots],
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

  const showAllClear = queueQuery.isSuccess && rows.length === 0 && emergencyItems.length === 0;

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col bg-clarity-bg">
        <GreetingHeader>
          <CountsStrip needYou={needYou} waitingOnTenants={waitingOnTenants} />
        </GreetingHeader>

        <main className="flex-1 overflow-y-auto px-[18px] py-4">
          {queueQuery.isLoading ? (
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
          ) : queueQuery.isError ? (
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
              {emergencyItems.map((item) => (
                <EmergencyBanner
                  key={item.case_id}
                  conversationId={item.case_id}
                  headline={emergencyHeadline(item)}
                  subtext={emergencySubtext(item)}
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
}: {
  row: QueueViewRow;
  draftActions: ReturnType<typeof useDraftActions>;
  onSkip: (item: QueueItem) => void;
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
  const secondsLeft = entry.status === "sending" ? secondsRemaining(entry.undoUntil) : 0;
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
      tenantName={firstName(item.tenant_name)}
      propertyLabel={propertyLabel}
      timestamp={formatRelativeTime(item.received_at)}
      tenantMessage={item.tenant_message}
      photoNote={item.has_media ? (item.media_note ?? "Sent a photo") : undefined}
      draftMessage={item.draft_body}
      why={item.why}
      conversationId={item.case_id}
      status={cardStatus}
      secondsLeft={secondsLeft}
      totalSeconds={totalSeconds}
      staleNotice={draftActions.staleNotices[item.case_id]}
      editSubmitting={draftActions.isEditSubmitting}
      onApprove={() => draftActions.approve(ctx)}
      onEdit={() => draftActions.openEditor(ctx, item.draft_body)}
      onSkip={() => onSkip(item)}
      onUndo={() => draftActions.undo(ctx)}
      onCancelEdit={() => draftActions.cancelEditor()}
      onSubmitEdit={(body) => draftActions.submitEdit(body)}
    />
  );
}
