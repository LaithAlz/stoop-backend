/**
 * One row of the full conversation history — the tenant's plain-sans
 * bubble, or Stoop's own already-sent reply in solid brand serif italic.
 * Ports apps/web/src/components/clarity/ThreadMessageRow.tsx
 * (docs/mockups/07 `.thread-bubble-in` / `.thread-bubble-sent`).
 */
import { StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { colors, fonts, radius, spacing, type } from "@/theme/tokens";
import type { TimelineMessageEntry } from "@/api/types";
import { formatRelativeTime } from "@/lib/relativeTime";

interface ThreadMessageRowProps {
  entry: TimelineMessageEntry;
  tenantFirstName: string;
}

function speakerLabel(entry: TimelineMessageEntry, tenantFirstName: string): string {
  if (entry.direction === "outbound") return "Sent by Stoop for you";
  if (entry.party === "vendor") return "Vendor";
  if (entry.party === "landlord") return "You";
  return tenantFirstName;
}

export function ThreadMessageRow({ entry, tenantFirstName }: ThreadMessageRowProps) {
  const isOutbound = entry.direction === "outbound";

  return (
    <View style={[styles.container, isOutbound && styles.outboundContainer]}>
      <View style={[styles.bubble, isOutbound ? styles.outboundBubble : styles.inboundBubble]}>
        <Text style={isOutbound ? styles.outboundText : styles.inboundText}>{entry.body}</Text>
        {entry.media.map((media, index) => (
          <View key={`${media.url}-${index}`} style={styles.mediaChip}>
            <Ionicons name="image-outline" size={16} color={colors.inkDim} />
            <Text style={styles.mediaLabel}>Photo attached</Text>
          </View>
        ))}
      </View>
      <Text style={[styles.meta, isOutbound && styles.metaRight]}>
        {isOutbound ? (
          <Text style={styles.metaBrand}>{speakerLabel(entry, tenantFirstName)}</Text>
        ) : (
          speakerLabel(entry, tenantFirstName)
        )}{" "}
        · {formatRelativeTime(entry.at)}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    maxWidth: "83%",
    marginBottom: 14,
  },
  outboundContainer: {
    alignSelf: "flex-end",
  },
  bubble: {
    borderRadius: radius.lg,
    paddingHorizontal: 15,
    paddingVertical: 13,
  },
  inboundBubble: {
    borderTopLeftRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
  },
  outboundBubble: {
    borderTopRightRadius: radius.sm,
    backgroundColor: colors.brand,
  },
  inboundText: {
    ...type.bubble,
    color: colors.ink,
  },
  outboundText: {
    ...type.bubble,
    fontFamily: fonts.serif,
    fontStyle: "italic",
    color: colors.brandOn,
  },
  mediaChip: {
    marginTop: spacing.sm + 2,
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    alignSelf: "flex-start",
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    paddingVertical: 5,
    paddingHorizontal: 10,
  },
  mediaLabel: {
    ...type.meta,
    color: colors.inkDim,
  },
  meta: {
    ...type.meta,
    marginTop: 6,
    color: colors.inkDim,
  },
  metaRight: {
    textAlign: "right",
  },
  metaBrand: {
    fontWeight: "800",
    color: colors.brand,
  },
});
