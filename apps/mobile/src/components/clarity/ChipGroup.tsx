/**
 * A single-select chip row — the RN port of the web onboarding's
 * `ChipGroup` (apps/web/src/routes/onboarding.tsx): bordered rectangular
 * chips (radius.md — never a pill, per the mockup "Don't" list), active
 * chip filled with brand green. Radio semantics for screen readers.
 */
import { Pressable, StyleSheet, Text, View } from "react-native";
import { colors, radius, type } from "@/theme/tokens";

export interface ChipOption<V extends string | null> {
  value: V;
  label: string;
}

interface ChipGroupProps<V extends string | null> {
  options: readonly ChipOption<V>[];
  value: V;
  onChange: (value: V) => void;
  accessibilityLabel: string;
}

export function ChipGroup<V extends string | null>({
  options,
  value,
  onChange,
  accessibilityLabel,
}: ChipGroupProps<V>) {
  return (
    <View accessibilityRole="radiogroup" accessibilityLabel={accessibilityLabel} style={styles.row}>
      {options.map((option) => {
        const active = option.value === value;
        return (
          <Pressable
            key={option.label}
            accessibilityRole="radio"
            accessibilityState={{ checked: active }}
            onPress={() => onChange(option.value)}
            style={({ pressed }) => [
              styles.chip,
              active && styles.chipActive,
              pressed && styles.pressed,
            ]}
          >
            <Text style={[styles.label, active && styles.labelActive]}>{option.label}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  chip: {
    minHeight: 44,
    justifyContent: "center",
    borderWidth: 1.5,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    borderRadius: radius.md,
    paddingHorizontal: 14,
  },
  chipActive: {
    borderColor: colors.brandDeep,
    backgroundColor: colors.brand,
  },
  pressed: {
    opacity: 0.85,
  },
  label: {
    ...type.button,
    fontSize: 14,
    color: colors.ink,
  },
  labelActive: {
    color: colors.brandOn,
  },
});
