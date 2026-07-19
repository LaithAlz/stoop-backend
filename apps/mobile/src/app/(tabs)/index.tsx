/**
 * Home — the real approval queue (issue #210 M1). Fetches `GET /v1/queue`
 * (src/api/queue.ts) and layers the local approve/undo/skip state machine
 * (src/features/queue/useDraftActions.ts + queueEntries.ts) on top — see
 * queueEntries.ts's docstring for why a local overlay exists at all over a
 * server that only ever lists cases still needing action.
 *
 * M0's static "Nothing to show yet." shell is gone; the empty state below
 * only ever renders once a real, successful fetch says the queue is
 * actually empty (never a fake placeholder pretending to be live data).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ActivityIndicator, Alert, FlatList, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { AppHeader } from "@/components/AppHeader";
import { EmptyState } from "@/components/EmptyState";
import { Button } from "@/components/Button";
import { CountsStrip } from "@/components/clarity/CountsStrip";
import { EmergencyBanner } from "@/components/clarity/EmergencyBanner";
import { DecisionCard } from "@/components/clarity/DecisionCard";
import { SkippedCard } from "@/components/clarity/SkippedCard";
import { colors, spacing } from "@/theme/tokens";
import { useQueue } from "@/api/queue";
import { useMe } from "@/api/me";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { QueueItem } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import {
  buildQueueView,
  pruneSkippedSnapshots,
  secondsRemaining,
  totalUndoSeconds,
  type QueueViewRow,
} from "@/features/queue/queueEntries";
import { useDraftActions } from "@/features/queue/useDraftActions";
import { emergencyHeadline, emergencySubtext } from "@/features/emergency/emergencyBanner";
import { EditDraftModal } from "@/features/queue/EditDraftModal";

function timeOfDayGreeting(date: Date): string {
  const hour = date.getHours();
  if (hour < 12) return "morning";
  if (hour < 18) return "afternoon";
  return "evening";
}

export default function HomeScreen() {
  const router = useRouter();
  const queueQuery = useQueue();
  const meQuery = useMe();

  const [skippedSnapshots, setSkippedSnapshots] = useState<Record<string, QueueItem>>({});

  const onNotice = useCallback((message: string) => Alert.alert("Stoop", message), []);
  const onSettled = useCallback(() => void queueQuery.refetch(), [queueQuery]);
  const draftActions = useDraftActions({ onNotice, onSettled });
  const { entries } = draftActions;

  const openCase = useCallback(
    (caseId: string) => {
      router.push({ pathname: "/conversations/[id]", params: { id: caseId } });
    },
    [router],
  );

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

  const name = meQuery.data ? firstName(meQuery.data.full_name) : null;
  const greeting = name
    ? `Good ${timeOfDayGreeting(new Date())}, ${name}.`
    : `Good ${timeOfDayGreeting(new Date())}.`;

  const needYou = queueQuery.data?.counts.total ?? 0;
  const waitingOnTenants = queueQuery.data?.counts.awaiting_tenant ?? 0;

  function handleSkip(item: QueueItem) {
    // M1 senior advisory: snapshots whose skip has since cleared (e.g. the
    // reject call failed and the error handler reset the card) are pruned
    // at this write site — a snapshot with no live "skipped" entry is
    // never rendered (buildQueueView ignores it), so cleaning on the next
    // skip keeps the map bounded without an extra effect/render pass. The
    // just-skipped item is added AFTER the prune (its entry only becomes
    // "skipped" in the dispatch below).
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

  function renderRow({ item: row }: { item: QueueViewRow }) {
    const { item, entry } = row;

    if (entry.status === "skipped") {
      return (
        <SkippedCard
          tenantName={item.tenant_name}
          propertyLabel={item.property_label}
          timestamp={item.received_at}
          onPress={() => openCase(item.case_id)}
        />
      );
    }

    const cardStatus =
      entry.status === "sending" ? "sending" : entry.status === "sent" ? "sent" : "idle";
    const secondsLeft = entry.status === "sending" ? secondsRemaining(entry.undoUntil) : 0;
    const totalSeconds = entry.status === "sending" ? totalUndoSeconds(entry) : 5;
    const ctx = { draftId: item.draft_id, caseId: item.case_id, tenantName: item.tenant_name };

    return (
      <DecisionCard
        item={item}
        status={cardStatus}
        secondsLeft={secondsLeft}
        totalSeconds={totalSeconds}
        staleNotice={draftActions.staleNotices[item.case_id]}
        onApprove={() => draftActions.approve(ctx)}
        onEdit={() => draftActions.openEditor(ctx, item.draft_body)}
        onSkip={() => handleSkip(item)}
        onUndo={() => draftActions.undo(ctx)}
        onOpen={() => openCase(item.case_id)}
      />
    );
  }

  const showAllClear = queueQuery.isSuccess && rows.length === 0 && emergencyItems.length === 0;

  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <AppHeader title={greeting} showLiveIndicator />

      {queueQuery.isLoading ? (
        <View style={styles.centered}>
          <ActivityIndicator color={colors.brand} />
        </View>
      ) : queueQuery.isError ? (
        <View style={styles.centered}>
          <Text style={styles.errorText}>
            {queueQuery.error instanceof ApiError
              ? toHouseApiError(queueQuery.error)
              : "Couldn't load your queue. Try again."}
          </Text>
          <Button
            label="Try again"
            onPress={() => void queueQuery.refetch()}
            testID="queue-retry"
          />
        </View>
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(row) => row.item.draft_id}
          renderItem={renderRow}
          contentContainerStyle={styles.listContent}
          refreshing={queueQuery.isRefetching}
          onRefresh={() => void queueQuery.refetch()}
          ListHeaderComponent={
            <View>
              <CountsStrip needYou={needYou} waitingOnTenants={waitingOnTenants} />
              {emergencyItems.map((item) => (
                <EmergencyBanner
                  key={item.case_id}
                  headline={emergencyHeadline(item)}
                  subtext={emergencySubtext(item)}
                  onPress={() => openCase(item.case_id)}
                />
              ))}
              {emergencyItems.length > 0 ? <View style={styles.headerGap} /> : null}
            </View>
          }
          ListEmptyComponent={
            showAllClear ? (
              <EmptyState
                icon="checkmark-circle-outline"
                title="That's everything."
                message="I'm watching your messages — go enjoy your day. I'll text you if anything needs you."
              />
            ) : null
          }
        />
      )}

      <EditDraftModal
        visible={draftActions.editingContext !== null}
        tenantFirstName={
          draftActions.editingContext ? firstName(draftActions.editingContext.tenantName) : ""
        }
        initialBody={draftActions.editingContext?.body ?? ""}
        submitting={draftActions.isEditSubmitting}
        onCancel={draftActions.cancelEditor}
        onSend={draftActions.submitEdit}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  listContent: {
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.base,
    paddingBottom: spacing.xxl,
    flexGrow: 1,
  },
  headerGap: {
    height: spacing.sm,
  },
  centered: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.base,
    paddingHorizontal: spacing.xl,
  },
  errorText: {
    textAlign: "center",
    color: colors.inkDim,
  },
});
