/**
 * Stoop's own drafted reply, not yet delivered — dashed brand border,
 * serif italic. Ports apps/web/src/components/clarity/DraftBubble.tsx
 * (docs/mockups/07 `.bubble-out` / `.thread-bubble-pending`). Shared by
 * the Home queue card and the case-detail timeline.
 */
import { StyleSheet, Text, View } from "react-native";
import { colors, fonts, radius, spacing, type } from "@/theme/tokens";

interface DraftBubbleProps {
  /** "I'd like to reply" (pending), "On its way to {tenant}" (sending), or
   *  "Replaced" (stale) — the only labels this bubble uses. */
  label: string;
  body: string;
}

export function DraftBubble({ label, body }: DraftBubbleProps) {
  return (
    <View style={styles.bubble}>
      <Text style={styles.label}>{label}</Text>
      <Text style={styles.body}>{body}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  bubble: {
    borderWidth: 1.5,
    borderStyle: "dashed",
    borderColor: colors.brandBorder,
    backgroundColor: colors.brandSoft,
    borderRadius: radius.lg,
    borderTopRightRadius: radius.sm,
    padding: spacing.md + 1,
    marginTop: spacing.sm,
  },
  label: {
    ...type.marginKicker,
    color: colors.brand,
    marginBottom: spacing.xs + 2,
  },
  body: {
    ...type.bubble,
    fontFamily: fonts.serif,
    fontStyle: "italic",
    color: colors.ink,
  },
});
