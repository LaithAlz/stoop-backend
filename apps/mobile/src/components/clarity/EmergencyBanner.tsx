/**
 * The one thing on Home that's never buried below the fold. Ports
 * apps/web/src/components/clarity/EmergencyBanner.tsx (docs/mockups/07
 * `.em-banner`). Rule #1 (CLAUDE.md): the emergency line is never
 * paywalled, throttled, or gated — this banner has no dismiss control.
 *
 * `headline`/`subtext` are computed by src/features/emergency/
 * emergencyBanner.ts, not inline here — see that module for why the
 * fallback headline never invents incident wording (PR #181's "reported a
 * flood" mistake, called out by api-contracts.md's Queue section).
 */
import { Pressable, StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { colors, radius, spacing, type } from "@/theme/tokens";

interface EmergencyBannerProps {
  headline: string;
  subtext: string;
  onPress: () => void;
}

export function EmergencyBanner({ headline, subtext, onPress }: EmergencyBannerProps) {
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.banner, pressed && styles.pressed]}
    >
      <Ionicons name="warning-outline" size={22} color={colors.emergencyInk} />
      <View style={styles.textBlock}>
        <Text style={styles.headline}>{headline}</Text>
        <Text style={styles.subtext}>{subtext}</Text>
      </View>
      <View style={styles.pulseDot} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm + 4,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: "rgba(0,0,0,0.25)",
    backgroundColor: colors.emergency,
    paddingHorizontal: spacing.base,
    paddingVertical: 14,
    marginBottom: spacing.base,
  },
  pressed: {
    opacity: 0.9,
  },
  textBlock: {
    flex: 1,
    minWidth: 0,
  },
  headline: {
    ...type.cardTitle,
    fontSize: 15,
    color: colors.emergencyInk,
  },
  subtext: {
    ...type.meta,
    marginTop: 2,
    color: colors.emergencyInk,
    opacity: 0.9,
  },
  pulseDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: "#FFFFFF",
  },
});
