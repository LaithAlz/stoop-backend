/**
 * The reasoning line, styled as marginalia next to the draft — visible by
 * default, never behind a "why?" toggle. Ports apps/web/src/components/
 * clarity/MarginNote.tsx (docs/mockups/07 `.margin-note`).
 */
import { StyleSheet, Text, View } from "react-native";
import { colors, spacing, type } from "@/theme/tokens";

interface MarginNoteProps {
  kicker?: string;
  children: string;
}

export function MarginNote({ kicker = "Why", children }: MarginNoteProps) {
  return (
    <View style={styles.container}>
      <Text style={styles.kicker}>{kicker}</Text>
      <Text style={styles.body}>{children}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    marginTop: spacing.md + 2,
    marginBottom: spacing.base,
    marginLeft: spacing.md,
    marginRight: 2,
    borderLeftWidth: 2,
    borderLeftColor: colors.brand,
    paddingLeft: spacing.base,
    paddingVertical: 2,
  },
  kicker: {
    ...type.marginKicker,
    color: colors.brand,
    marginBottom: 5,
  },
  body: {
    ...type.marginBody,
    color: colors.inkDim,
  },
});
