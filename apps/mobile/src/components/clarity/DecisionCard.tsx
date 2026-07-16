/**
 * One decision, full stop — tenant's text, Stoop's drafted reply, the
 * plain-English reason why, and exactly one primary action. Ports
 * apps/web/src/components/clarity/DecisionCard.tsx (docs/mockups/07
 * `.entry`) to React Native for the Home approval queue.
 */
import { Pressable, StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { colors, radius, spacing, type } from "@/theme/tokens";
import type { QueueItem } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { formatRelativeTime } from "@/lib/relativeTime";
import { SeverityPlaque } from "./SeverityPlaque";
import { TimestampChip } from "./TimestampChip";
import { DraftBubble } from "./DraftBubble";
import { MarginNote } from "./MarginNote";
import { DecisionActions } from "./DecisionActions";
import { UndoTicket } from "./UndoTicket";

const DEFAULT_WHY = "I sorted this the best I could — open the full view for the details.";

/** "sending" shows the live undo ticket; "sent" is the brief window after
 *  the local countdown hits zero but before the next queue refetch has
 *  confirmed the card is gone — no actions, no ticket (the server's
 *  `undo_until` has passed too, so an Undo tap would just 409). */
type DecisionCardStatus = "idle" | "sending" | "sent";

interface DecisionCardProps {
  item: QueueItem;
  status: DecisionCardStatus;
  secondsLeft?: number;
  totalSeconds?: number;
  staleNotice?: string;
  onApprove: () => void;
  onEdit: () => void;
  onSkip: () => void;
  onUndo: () => void;
  onOpen: () => void;
}

export function DecisionCard({
  item,
  status,
  secondsLeft = 0,
  totalSeconds = 5,
  staleNotice,
  onApprove,
  onEdit,
  onSkip,
  onUndo,
  onOpen,
}: DecisionCardProps) {
  const tenantFirst = firstName(item.tenant_name);

  return (
    <View style={styles.card}>
      <View style={styles.headRow}>
        <SeverityPlaque severity={item.severity} />
        <Pressable accessibilityRole="button" onPress={onOpen} style={styles.fullView} hitSlop={8}>
          <Text style={styles.fullViewLabel}>Full view</Text>
          <Ionicons name="chevron-forward" size={12} color={colors.inkDim} />
        </Pressable>
      </View>

      <Text style={styles.metaLine}>
        <Text style={styles.metaBold}>{tenantFirst}</Text>
        {" — "}
        {item.property_label}
        {"  "}
        <TimestampChip>{formatRelativeTime(item.received_at)}</TimestampChip>
      </Text>

      <View style={styles.inbound}>
        <Text style={styles.inboundLabel}>{tenantFirst} said</Text>
        <Text style={styles.inboundBody}>{item.tenant_message}</Text>
        {item.has_media && (
          <View style={styles.mediaChip}>
            <Ionicons name="image-outline" size={16} color={colors.inkDim} />
            <Text style={styles.mediaLabel}>{item.media_note ?? "Sent a photo"}</Text>
          </View>
        )}
      </View>

      <DraftBubble
        label={status === "sending" ? `On its way to ${tenantFirst}` : "I'd like to reply"}
        body={item.draft_body}
      />

      {staleNotice ? <Text style={styles.staleNotice}>{staleNotice}</Text> : null}

      {status === "sending" && (
        <UndoTicket secondsLeft={secondsLeft} totalSeconds={totalSeconds} onUndo={onUndo} />
      )}
      {status === "sent" && <Text style={styles.sentNote}>Sent.</Text>}
      {status === "idle" && (
        <>
          <MarginNote>{item.why ?? DEFAULT_WHY}</MarginNote>
          <DecisionActions onApprove={onApprove} onEdit={onEdit} onSkip={onSkip} />
        </>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.surface,
    padding: 18,
    marginBottom: spacing.md + 2,
  },
  headRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.sm,
    marginBottom: spacing.sm + 2,
  },
  fullView: {
    flexDirection: "row",
    alignItems: "center",
    gap: 3,
    minHeight: 32,
  },
  fullViewLabel: {
    ...type.button,
    fontSize: 12,
    color: colors.inkDim,
  },
  metaLine: {
    ...type.meta,
    fontWeight: "600",
    color: colors.inkDim,
    marginBottom: spacing.sm + 4,
  },
  metaBold: {
    fontWeight: "800",
    color: colors.ink,
  },
  inbound: {
    borderRadius: radius.lg,
    borderTopLeftRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    paddingHorizontal: 15,
    paddingVertical: 13,
  },
  inboundLabel: {
    ...type.marginKicker,
    color: colors.inkDim,
    marginBottom: 6,
  },
  inboundBody: {
    ...type.bubble,
    color: colors.ink,
  },
  mediaChip: {
    marginTop: spacing.sm + 2,
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    paddingVertical: 5,
    paddingHorizontal: 10,
  },
  mediaLabel: {
    ...type.meta,
    color: colors.inkDim,
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
});
