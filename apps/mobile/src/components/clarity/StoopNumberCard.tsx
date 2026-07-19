/**
 * The property's own Stoop number, drawn as the web onboarding's
 * "Dedicated SMS number" ticket (brand-soft ground, brand border, big
 * serif digits) — the single most important fact on a property: it's what
 * tenants text. When the number is null the card tells the truth instead
 * (see src/features/properties/stoopNumber.ts — no invented state, no
 * "coming shortly").
 */
import { StyleSheet, Text, View } from "react-native";
import { colors, radius, spacing, type } from "@/theme/tokens";
import {
  formatStoopNumber,
  NO_NUMBER_BODY,
  NO_NUMBER_TITLE,
  NUMBER_CAPTION,
} from "@/features/properties/stoopNumber";

interface StoopNumberCardProps {
  number: string | null;
}

export function StoopNumberCard({ number }: StoopNumberCardProps) {
  if (!number) {
    return (
      <View style={styles.missingCard}>
        <Text style={styles.missingTitle}>{NO_NUMBER_TITLE}</Text>
        <Text style={styles.missingBody}>{NO_NUMBER_BODY}</Text>
      </View>
    );
  }

  return (
    <View style={styles.card}>
      <Text style={styles.kicker}>Dedicated SMS number</Text>
      <Text style={styles.number} testID="stoop-number">
        {formatStoopNumber(number)}
      </Text>
      <Text style={styles.caption}>{NUMBER_CAPTION}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.brandBorder,
    backgroundColor: colors.brandSoft,
    alignItems: "center",
    paddingVertical: spacing.lg,
    paddingHorizontal: spacing.base,
  },
  kicker: {
    ...type.marginKicker,
    color: colors.inkDim,
  },
  number: {
    fontFamily: type.greeting.fontFamily,
    fontSize: 26,
    fontWeight: "700",
    color: colors.ink,
    marginTop: spacing.sm,
  },
  caption: {
    ...type.footnote,
    color: colors.inkDim,
    marginTop: spacing.xs,
  },
  missingCard: {
    borderRadius: radius.md,
    borderWidth: 1,
    borderStyle: "dashed",
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    paddingVertical: spacing.base,
    paddingHorizontal: spacing.base,
    gap: spacing.xs,
  },
  missingTitle: {
    ...type.cardTitle,
    color: colors.ink,
  },
  missingBody: {
    ...type.footnote,
    color: colors.inkDim,
  },
});
