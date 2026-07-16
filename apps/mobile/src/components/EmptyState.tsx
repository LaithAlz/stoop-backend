/**
 * Quiet, specific empty state — not a cheerful illustration. Ports the
 * shape of apps/web/src/components/clarity/AllClearState.tsx (icon circle +
 * serif title + sans body + optional small note) to React Native. Used by
 * every M0 tab placeholder; each screen supplies its own honest copy (rule
 * 8 — plain English, no "triage", nothing implying live data this phase
 * doesn't fetch yet).
 */
import { StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { colors, spacing, type } from "@/theme/tokens";

interface EmptyStateProps {
  icon: keyof typeof Ionicons.glyphMap;
  title: string;
  message: string;
  note?: string;
}

export function EmptyState({ icon, title, message, note }: EmptyStateProps) {
  return (
    <View style={styles.container}>
      <View style={styles.badgeCircle}>
        <Ionicons name={icon} size={26} color={colors.brand} />
      </View>
      <Text style={styles.title}>{title}</Text>
      <Text style={styles.message}>{message}</Text>
      {note ? <Text style={styles.note}>{note}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    alignItems: "center",
    paddingTop: 56, // .all-clear padding: 52px 14px 24px, mockup line 279
    paddingBottom: spacing.xl,
    paddingHorizontal: spacing.md,
  },
  // A circular icon badge is the mockup's own choice (.badge-circle,
  // border-radius:50%, mockup line 280) — distinct from the banned
  // full-pill radius on cards/buttons/badges (rectangular controls).
  badgeCircle: {
    width: 56,
    height: 56,
    borderRadius: 28,
    backgroundColor: colors.brandSoft,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: spacing.base + 2,
  },
  title: {
    ...type.allClearTitle,
    color: colors.ink,
    marginBottom: spacing.sm,
    textAlign: "center",
  },
  message: {
    ...type.body,
    fontSize: 14.5, // .all-clear p, mockup line 284
    color: colors.inkDim,
    textAlign: "center",
    maxWidth: 260, // .all-clear p max-width:26ch, mockup line 284
  },
  note: {
    ...type.footnote,
    color: colors.inkDim,
    opacity: 0.8, // .all-clear .small, mockup line 285
    marginTop: spacing.sm,
    textAlign: "center",
  },
});
