/**
 * A quiet, centered meta line for an audit-trail moment in the case-detail
 * timeline (issue #210 M1 brief: "audit entries as quiet meta lines") —
 * new for mobile; no direct web port exists yet (apps/web's own
 * conversation route only extracts `why` from the audit payload today,
 * it doesn't render audit entries as their own rows). Deliberately plain:
 * no icon, no color, no border — the messages and drafts are the content,
 * this is just a footnote about what happened between them.
 */
import { StyleSheet, Text, View } from "react-native";
import { colors, type } from "@/theme/tokens";
import { formatRelativeTime } from "@/lib/relativeTime";

interface AuditMetaLineProps {
  label: string;
  at: string;
}

export function AuditMetaLine({ label, at }: AuditMetaLineProps) {
  return (
    <View style={styles.container}>
      <Text style={styles.text}>
        {label} <Text style={styles.time}>· {formatRelativeTime(at)}</Text>
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    alignItems: "center",
    marginVertical: 8,
  },
  text: {
    ...type.footnote,
    fontSize: 12,
    color: colors.inkDim,
    opacity: 0.85,
    textAlign: "center",
  },
  time: {
    fontWeight: "600",
  },
});
