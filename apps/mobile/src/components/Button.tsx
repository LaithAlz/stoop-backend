/**
 * Clarity's two button treatments — mockup `.btn` / `.btn.primary`
 * (lines 258-267). M0 only needs these two (sign-in, sign-out); the
 * icon-prefixed ghost variants (Edit/Skip on the approval card) arrive
 * with M1.
 */
import { Pressable, StyleSheet, Text, type PressableProps } from "react-native";
import { colors, radius, touchTarget, type } from "@/theme/tokens";

interface ButtonProps extends Omit<PressableProps, "style"> {
  label: string;
  variant?: "primary" | "ghost";
}

export function Button({ label, variant = "ghost", disabled, ...pressableProps }: ButtonProps) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled: Boolean(disabled) }}
      disabled={disabled}
      style={({ pressed }) => [
        styles.base,
        variant === "primary" ? styles.primary : styles.ghost,
        pressed && styles.pressed,
        disabled ? styles.disabled : null,
      ]}
      {...pressableProps}
    >
      <Text style={variant === "primary" ? styles.labelPrimary : styles.labelGhost}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: {
    minHeight: touchTarget, // "Do" rule: 48px+ targets, mockup line 452
    borderRadius: radius.md,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 16,
  },
  ghost: {
    backgroundColor: colors.panel,
    borderColor: colors.lineStrong,
  },
  primary: {
    backgroundColor: colors.brand,
    borderColor: colors.brandDeep,
    minHeight: 52, // .btn.primary min-height, mockup line 263
  },
  pressed: {
    opacity: 0.85,
  },
  disabled: {
    opacity: 0.5,
  },
  labelGhost: {
    ...type.button,
    color: colors.inkDim,
  },
  labelPrimary: {
    ...type.buttonPrimary,
    color: colors.brandOn,
  },
});
