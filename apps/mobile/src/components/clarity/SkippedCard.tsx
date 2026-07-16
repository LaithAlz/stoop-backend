/**
 * What a skipped draft becomes. Skip dismisses the draft, not the case —
 * the card collapses into this muted, de-emphasized "waiting" state
 * instead of vanishing (founder decision, 2026-07-06). Ports
 * apps/web/src/components/clarity/SkippedCard.tsx.
 */
import { Pressable, StyleSheet, Text, View } from "react-native";
import { colors, radius, spacing, type } from "@/theme/tokens";
import { firstName } from "@/lib/tenantName";
import { formatRelativeTime } from "@/lib/relativeTime";
import { TimestampChip } from "./TimestampChip";

interface SkippedCardProps {
  tenantName: string;
  propertyLabel: string;
  timestamp: string;
  onPress: () => void;
}

export function SkippedCard({ tenantName, propertyLabel, timestamp, onPress }: SkippedCardProps) {
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.container, pressed && styles.pressed]}
    >
      <View style={styles.textBlock}>
        <Text style={styles.title}>
          {firstName(tenantName)} — {propertyLabel}
        </Text>
        <Text style={styles.subtitle}>No reply sent — case still open</Text>
      </View>
      <TimestampChip>{formatRelativeTime(timestamp)}</TimestampChip>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  container: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.md,
    minHeight: 44,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderStyle: "dashed",
    borderColor: colors.lineStrong,
    backgroundColor: colors.bg,
    paddingHorizontal: 18,
    paddingVertical: 14,
    marginBottom: spacing.md + 2,
    opacity: 0.8,
  },
  pressed: {
    opacity: 1,
  },
  textBlock: {
    flex: 1,
    minWidth: 0,
  },
  title: {
    ...type.meta,
    fontWeight: "700",
    color: colors.ink,
    marginBottom: 2,
  },
  subtitle: {
    ...type.meta,
    fontWeight: "500",
    color: colors.inkDim,
  },
});
