/**
 * Case detail — the interleaved timeline from `GET /v1/cases/{id}`
 * (messages as bubbles, audit entries as quiet meta lines, drafts as
 * `DraftBubble`), day dividers, the severity/why plaque. When the case
 * still has a pending draft, the same approve/undo/edit/skip decision the
 * Home queue card offers is available here too (src/features/queue/
 * useDraftActions.ts, shared with src/app/(tabs)/index.tsx).
 */
import { useCallback, useMemo } from "react";
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQueryClient } from "@tanstack/react-query";
import { colors, spacing, type } from "@/theme/tokens";
import { useCase } from "@/api/cases";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { ClassifiedAuditPayload, TimelineDraftEntry, TimelineEntry } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { SeverityPlaque } from "@/components/clarity/SeverityPlaque";
import { EmergencyBanner } from "@/components/clarity/EmergencyBanner";
import { DayDivider } from "@/components/clarity/DayDivider";
import { ThreadMessageRow } from "@/components/clarity/ThreadMessageRow";
import { AuditMetaLine } from "@/components/clarity/AuditMetaLine";
import { DraftBubble } from "@/components/clarity/DraftBubble";
import { MarginNote } from "@/components/clarity/MarginNote";
import { DecisionActions } from "@/components/clarity/DecisionActions";
import { UndoTicket } from "@/components/clarity/UndoTicket";
import { entryFor, secondsRemaining, totalUndoSeconds } from "@/features/queue/queueEntries";
import { useDraftActions } from "@/features/queue/useDraftActions";
import { buildTimelineRows, type TimelineRow } from "@/features/cases/timeline";
import { EditDraftModal } from "@/features/queue/EditDraftModal";

const DEFAULT_WHY = "I sorted this the best I could — no note on file for this one.";

export default function CaseDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const reactQueryClient = useQueryClient();
  const caseQuery = useCase(id);

  const onNotice = useCallback((message: string) => Alert.alert("Stoop", message), []);
  const onSettled = useCallback(() => {
    void reactQueryClient.invalidateQueries({ queryKey: ["case", id] });
    void reactQueryClient.invalidateQueries({ queryKey: ["queue"] });
  }, [reactQueryClient, id]);
  const draftActions = useDraftActions({ onNotice, onSettled });

  const caseDetail = caseQuery.data;
  const tenantFirst = firstName(caseDetail?.tenant.name);

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

  const isClassifiedAudit = (
    entry: TimelineEntry,
  ): entry is Extract<TimelineEntry, { kind: "audit" }> =>
    entry.kind === "audit" && entry.action === "classified";
  const classifiedEntry = caseDetail?.timeline.find(isClassifiedAudit);
  const why =
    (classifiedEntry?.payload as ClassifiedAuditPayload | undefined)?.summary ?? DEFAULT_WHY;

  const caseIsOpen = caseDetail ? caseDetail.status !== "resolved" : false;

  function renderTimelineRow({ item: row }: { item: TimelineRow }) {
    if (row.kind === "day-divider") return <DayDivider>{row.label}</DayDivider>;
    if (row.kind === "message")
      return <ThreadMessageRow entry={row.entry} tenantFirstName={tenantFirst} />;
    if (row.kind === "audit") return <AuditMetaLine label={row.label} at={row.entry.at} />;
    return null; // "draft" rows render via the pinned footer below, not inline.
  }

  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <View style={styles.header}>
        <View style={styles.headRow}>
          <Pressable
            accessibilityRole="button"
            onPress={() => router.back()}
            style={styles.backButton}
            hitSlop={8}
          >
            <Ionicons name="chevron-back" size={16} color={colors.inkDim} />
            <Text style={styles.backLabel}>Conversations</Text>
          </Pressable>
          {caseDetail?.severity && caseIsOpen && <SeverityPlaque severity={caseDetail.severity} />}
        </View>
        <Text style={styles.title}>
          {tenantFirst}
          {caseDetail?.tenant.unit ? ` — Unit ${caseDetail.tenant.unit}` : ""}
        </Text>
        <Text style={styles.subtitle}>every message, saved with dates and times</Text>
      </View>

      {caseQuery.isLoading ? (
        <View style={styles.centered}>
          <ActivityIndicator color={colors.brand} />
        </View>
      ) : caseQuery.isError || !caseDetail ? (
        <View style={styles.centered}>
          <Text style={styles.errorText}>
            {caseQuery.error instanceof ApiError
              ? toHouseApiError(caseQuery.error)
              : "Couldn't load this conversation. Try again."}
          </Text>
        </View>
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(row) => row.key}
          renderItem={renderTimelineRow}
          contentContainerStyle={styles.listContent}
          ListHeaderComponent={
            caseDetail.severity === "emergency" && caseIsOpen ? (
              <EmergencyBanner
                headline={
                  caseDetail.title ?? `${tenantFirst} needs you now — ${caseDetail.property.label}`
                }
                subtext={`${caseDetail.property.label} · tap to see what's happening`}
                onPress={() => {}}
              />
            ) : null
          }
          ListFooterComponent={
            draftId && draftBody !== undefined ? (
              <View style={styles.draftFooter}>
                <DraftBubble
                  label={
                    draftEntry.status === "sending"
                      ? `On its way to ${tenantFirst}`
                      : "I'd like to reply"
                  }
                  body={draftBody}
                />
                {draftActions.staleNotices[caseDetail.id] ? (
                  <Text style={styles.staleNotice}>{draftActions.staleNotices[caseDetail.id]}</Text>
                ) : null}
                {draftEntry.status === "sending" ? (
                  <UndoTicket
                    secondsLeft={secondsRemaining(draftEntry.undoUntil)}
                    totalSeconds={totalUndoSeconds(draftEntry)}
                    onUndo={() =>
                      draftActions.undo({
                        draftId,
                        caseId: caseDetail.id,
                        tenantName: caseDetail.tenant.name ?? "",
                      })
                    }
                  />
                ) : draftEntry.status === "sent" ? (
                  <Text style={styles.sentNote}>Sent.</Text>
                ) : draftEntry.status !== "skipped" ? (
                  <>
                    <MarginNote>{why}</MarginNote>
                    <DecisionActions
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
                    />
                  </>
                ) : (
                  <Text style={styles.sentNote}>No reply sent — case still open</Text>
                )}
              </View>
            ) : (
              <Text style={styles.appendOnlyNote}>
                Nothing here can be edited or removed once it&rsquo;s sent — that&rsquo;s what makes
                it useful if you ever need the record.
              </Text>
            )
          }
        />
      )}

      <EditDraftModal
        visible={draftActions.editingContext !== null}
        tenantFirstName={tenantFirst}
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
  header: {
    borderBottomWidth: 1,
    borderBottomColor: colors.line,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
    paddingBottom: spacing.md,
  },
  headRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.sm,
  },
  backButton: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    minHeight: 40,
  },
  backLabel: {
    ...type.button,
    fontSize: 13,
    color: colors.inkDim,
  },
  title: {
    ...type.cardTitle,
    fontSize: 19,
    color: colors.ink,
    marginTop: spacing.sm,
  },
  subtitle: {
    ...type.meta,
    fontWeight: "500",
    color: colors.inkDim,
    marginTop: 2,
  },
  listContent: {
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.base,
    paddingBottom: spacing.xxl,
    flexGrow: 1,
  },
  centered: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: spacing.xl,
  },
  errorText: {
    textAlign: "center",
    color: colors.inkDim,
  },
  draftFooter: {
    marginTop: spacing.sm,
  },
  staleNotice: {
    ...type.footnote,
    color: colors.brand,
    marginTop: spacing.sm,
  },
  sentNote: {
    ...type.meta,
    color: colors.whenever,
    marginTop: spacing.md,
  },
  appendOnlyNote: {
    ...type.footnote,
    color: colors.inkDim,
    marginTop: spacing.base,
  },
});
