/**
 * The one-primary-action row under a drafted reply — Edit / Skip / Approve
 * & send. Ports apps/web/src/components/clarity/DecisionActions.tsx
 * (docs/mockups/07 `.actions`). Shared by the Home queue card and the
 * case-detail timeline's pending draft.
 */
import { Pressable, StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { colors, radius, touchTarget, type } from "@/theme/tokens";

interface DecisionActionsProps {
  onEdit: () => void;
  onSkip: () => void;
  onApprove: () => void;
  disabled?: boolean;
}

export function DecisionActions({ onEdit, onSkip, onApprove, disabled }: DecisionActionsProps) {
  return (
    <View style={styles.row}>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: Boolean(disabled) }}
        disabled={disabled}
        onPress={onEdit}
        style={({ pressed }) => [styles.secondary, pressed && styles.pressed]}
      >
        <Ionicons name="pencil-outline" size={16} color={colors.inkDim} />
        <Text style={styles.secondaryLabel}>Edit</Text>
      </Pressable>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: Boolean(disabled) }}
        disabled={disabled}
        onPress={onSkip}
        style={({ pressed }) => [styles.secondary, pressed && styles.pressed]}
      >
        <Ionicons name="play-skip-forward-outline" size={16} color={colors.inkDim} />
        <Text style={styles.secondaryLabel}>Skip</Text>
      </Pressable>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: Boolean(disabled) }}
        disabled={disabled}
        onPress={onApprove}
        style={({ pressed }) => [styles.primary, pressed && styles.pressed]}
      >
        <Ionicons name="checkmark" size={16} color={colors.brandOn} />
        <Text style={styles.primaryLabel}>Approve &amp; send</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    gap: 10,
    marginTop: 15,
  },
  secondary: {
    minHeight: touchTarget,
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderWidth: 1.5,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    borderRadius: radius.md,
    paddingHorizontal: 14,
  },
  primary: {
    flex: 1,
    minHeight: 52,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    borderWidth: 1.5,
    borderColor: colors.brandDeep,
    backgroundColor: colors.brand,
    borderRadius: radius.md,
    paddingHorizontal: 16,
  },
  pressed: {
    opacity: 0.85,
  },
  secondaryLabel: {
    ...type.button,
    color: colors.inkDim,
  },
  primaryLabel: {
    ...type.buttonPrimary,
    color: colors.brandOn,
  },
});
