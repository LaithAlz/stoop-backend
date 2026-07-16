/**
 * The enamel severity plaque — ports apps/web/src/components/clarity/
 * SeverityPlaque.tsx. `emergency`/`urgent`/`routine` are the schema-v1
 * enum values (docs/03-engineering/schema-v1.md); the labels below are
 * Clarity's plain-English display copy, not the stored values.
 *
 * Color reading of theme/tokens.ts's severity triples: the base color
 * (`whenever`/`wait`/`emergency`) is the plaque's own background, `Deep`
 * is its border, and `Ink` (a light, near-white tint despite the name) is
 * the text color printed ON that background — the same relationship
 * `brandOn` has to `brand` elsewhere in tokens.ts.
 */
import { StyleSheet, Text, View } from "react-native";
import { colors, radius, type } from "@/theme/tokens";
import type { Severity } from "@/api/types";

const plaqueConfig: Record<Severity, { label: string; bg: string; border: string; text: string }> =
  {
    emergency: {
      label: "Emergency",
      bg: colors.emergency,
      border: colors.emergencyDeep,
      text: colors.emergencyInk,
    },
    urgent: {
      label: "Can't wait",
      bg: colors.wait,
      border: colors.waitDeep,
      text: colors.waitInk,
    },
    routine: {
      label: "Whenever",
      bg: colors.whenever,
      border: colors.wheneverDeep,
      text: colors.wheneverInk,
    },
  };

interface SeverityPlaqueProps {
  severity: Severity;
  size?: "default" | "sm";
}

export function SeverityPlaque({ severity, size = "default" }: SeverityPlaqueProps) {
  const cfg = plaqueConfig[severity];
  return (
    <View
      style={[
        styles.base,
        size === "sm" && styles.sm,
        { backgroundColor: cfg.bg, borderColor: cfg.border },
      ]}
    >
      <Text style={[size === "sm" ? type.plaqueSm : type.plaque, { color: cfg.text }]}>
        {cfg.label}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  base: {
    alignSelf: "flex-start",
    borderRadius: radius.plaque,
    borderWidth: 1.5,
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  sm: {
    paddingHorizontal: 7,
    paddingVertical: 3,
  },
});
