/** The "N need you · N waiting on tenants" line under the greeting. Ports
 *  apps/web/src/components/clarity/CountsStrip.tsx. */
import { StyleSheet, Text, View } from "react-native";
import { colors, type } from "@/theme/tokens";

interface CountsStripProps {
  needYou: number;
  waitingOnTenants: number;
}

export function CountsStrip({ needYou, waitingOnTenants }: CountsStripProps) {
  return (
    <View style={styles.row}>
      <Text style={styles.text}>
        <Text style={styles.bold}>{needYou}</Text> need you
      </Text>
      <Text style={styles.dot}>·</Text>
      <Text style={styles.text}>
        <Text style={styles.bold}>{waitingOnTenants}</Text> waiting on tenants
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    flexWrap: "wrap",
    gap: 8,
    marginTop: 6,
  },
  text: {
    ...type.counts,
    color: colors.inkDim,
  },
  bold: {
    color: colors.ink,
  },
  dot: {
    color: colors.inkDim,
    opacity: 0.45,
  },
});
