/**
 * The one thing on Home that's never buried below the fold. Ports
 * apps/web/src/components/clarity/EmergencyBanner.tsx (docs/mockups/07
 * `.em-banner`). Rule #1 (CLAUDE.md): the emergency line is never
 * paywalled, throttled, or gated — this banner has no dismiss control.
 * Non-dismissible stays literal: `onAcknowledge` (below) is a deliberate,
 * labeled action a landlord must tap on purpose, never a swipe/close
 * gesture on the banner itself, and tap-to-open-case (`onPress`) is
 * unchanged either way.
 *
 * `headline`/`subtext` are computed by src/features/emergency/
 * emergencyBanner.ts, not inline here — see that module for why the
 * fallback headline never invents incident wording (PR #181's "reported a
 * flood" mistake, called out by api-contracts.md's Queue section). Same
 * module owns the acknowledge button's copy.
 *
 * `onAcknowledge`/`acknowledging` are optional: the case-detail screen
 * (src/app/(tabs)/conversations/[id].tsx) renders this same component with
 * neither prop — `GET /v1/cases/{id}` carries no `notification_id` to ack
 * against (api-contracts.md's Queue v1.15 amendment is queue-card-only), so
 * that surface stays exactly the informational banner it always was. Home
 * (src/app/(tabs)/index.tsx) is the only caller that ever passes them, and
 * only for a card whose `notification_id` is non-null.
 */
import { Pressable, StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { colors, radius, spacing, type } from "@/theme/tokens";
import {
  EMERGENCY_ACK_LABEL,
  EMERGENCY_ACK_PENDING_LABEL,
  EMERGENCY_ACK_SUBLABEL,
} from "@/features/emergency/emergencyBanner";

interface EmergencyBannerProps {
  headline: string;
  subtext: string;
  onPress: () => void;
  /** Present only when this card carries a non-null `notification_id` —
   *  omit entirely to render the plain informational banner. */
  onAcknowledge?: () => void;
  /** True only while THIS banner's own ack call is in flight (the caller
   *  scopes this per notification id — see useAcknowledge's
   *  `isAcknowledging`), never a global "something is acking" flag. */
  acknowledging?: boolean;
}

export function EmergencyBanner({
  headline,
  subtext,
  onPress,
  onAcknowledge,
  acknowledging,
}: EmergencyBannerProps) {
  return (
    <View style={styles.banner}>
      <Pressable
        accessibilityRole="button"
        onPress={onPress}
        style={({ pressed }) => [styles.row, pressed && styles.pressed]}
      >
        <Ionicons name="warning-outline" size={22} color={colors.emergencyInk} />
        <View style={styles.textBlock}>
          <Text style={styles.headline}>{headline}</Text>
          <Text style={styles.subtext}>{subtext}</Text>
        </View>
        <View style={styles.pulseDot} />
      </Pressable>
      {onAcknowledge ? (
        <Pressable
          accessibilityRole="button"
          onPress={onAcknowledge}
          disabled={acknowledging}
          testID="emergency-ack-button"
          style={({ pressed }) => [
            styles.ackButton,
            pressed && styles.pressed,
            acknowledging && styles.ackButtonDisabled,
          ]}
        >
          <Text style={styles.ackLabel}>
            {acknowledging ? EMERGENCY_ACK_PENDING_LABEL : EMERGENCY_ACK_LABEL}
          </Text>
          <Text style={styles.ackSublabel}>{EMERGENCY_ACK_SUBLABEL}</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: "rgba(0,0,0,0.25)",
    backgroundColor: colors.emergency,
    marginBottom: spacing.base,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm + 4,
    paddingHorizontal: spacing.base,
    paddingVertical: 14,
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
  ackButton: {
    borderTopWidth: 1,
    borderTopColor: "rgba(0,0,0,0.15)",
    paddingHorizontal: spacing.base,
    paddingVertical: spacing.sm + 2,
  },
  ackButtonDisabled: {
    opacity: 0.6,
  },
  ackLabel: {
    ...type.button,
    fontSize: 14,
    color: colors.emergencyInk,
  },
  ackSublabel: {
    ...type.meta,
    marginTop: 1,
    color: colors.emergencyInk,
    opacity: 0.85,
  },
});
