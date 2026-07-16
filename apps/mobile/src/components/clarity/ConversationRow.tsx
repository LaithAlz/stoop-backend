/**
 * One row per case on the Conversations tab — ports apps/web/src/
 * components/clarity/ConversationRow.tsx. Not in mockup 07 directly (the
 * Tab IA decision, 2026-07-06); reuses the same enamel-plaque/stamp
 * material as the rest of Clarity rather than inventing new treatment.
 */
import { Pressable, StyleSheet, Text, View } from "react-native";
import { colors, radius, spacing, type } from "@/theme/tokens";
import type { CaseSummary } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { formatRelativeTime } from "@/lib/relativeTime";
import { SeverityPlaque } from "./SeverityPlaque";
import { TimestampChip } from "./TimestampChip";

interface ConversationRowProps {
  item: CaseSummary;
  onPress: () => void;
}

export function ConversationRow({ item, onPress }: ConversationRowProps) {
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.row, pressed && styles.pressed]}
    >
      <View style={styles.headRow}>
        <Text style={styles.name} numberOfLines={1}>
          {firstName(item.tenant_name)}{" "}
          <Text style={styles.propertyLabel}>— {item.property_label}</Text>
        </Text>
        {item.severity && <SeverityPlaque severity={item.severity} size="sm" />}
      </View>
      <Text style={styles.snippet} numberOfLines={2}>
        {item.title ?? "No summary yet."}
      </Text>
      <TimestampChip>{formatRelativeTime(item.last_activity_at)}</TimestampChip>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    gap: spacing.sm,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.surface,
    padding: spacing.base,
    marginBottom: spacing.sm + 4,
  },
  pressed: {
    opacity: 0.85,
  },
  headRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: spacing.sm,
  },
  name: {
    flex: 1,
    ...type.cardTitle,
    fontSize: 15,
    color: colors.ink,
  },
  propertyLabel: {
    fontWeight: "600",
    color: colors.inkDim,
  },
  snippet: {
    ...type.footnote,
    color: colors.inkDim,
  },
});
