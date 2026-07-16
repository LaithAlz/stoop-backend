/**
 * The conversation thread's day-group divider — ports apps/web/src/
 * components/clarity/DayDivider.tsx (docs/mockups/07 `.day-stamp`), reusing
 * `TimestampChip` rather than inventing a new stamp material.
 */
import { StyleSheet, View } from "react-native";
import { colors } from "@/theme/tokens";
import { TimestampChip } from "./TimestampChip";

interface DayDividerProps {
  children: string;
}

export function DayDivider({ children }: DayDividerProps) {
  return (
    <View style={styles.row} accessibilityRole="none">
      <View style={styles.line} />
      <TimestampChip>{children}</TimestampChip>
      <View style={styles.line} />
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    marginVertical: 16,
  },
  line: {
    flex: 1,
    height: 1,
    backgroundColor: colors.lineStrong,
  },
});
