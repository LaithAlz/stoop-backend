/**
 * Small mono, uppercase, letter-spaced "stamp" for timestamps — ports
 * apps/web/src/components/clarity/TimestampChip.tsx (docs/mockups/07
 * `.stamp`) to React Native.
 */
import { StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native";
import { colors, radius, type } from "@/theme/tokens";

interface TimestampChipProps {
  children: string;
  style?: StyleProp<ViewStyle>;
}

export function TimestampChip({ children, style }: TimestampChipProps) {
  return (
    <View style={[styles.chip, style]}>
      <Text style={styles.label}>{children}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  chip: {
    alignSelf: "flex-start",
    borderWidth: 1.5,
    borderColor: colors.brand,
    backgroundColor: colors.brandSoft,
    borderRadius: radius.sm,
    paddingHorizontal: 8,
    paddingVertical: 3,
  },
  label: {
    ...type.stamp,
    color: colors.brand,
  },
});
