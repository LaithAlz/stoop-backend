/**
 * The wordmark row + optional "watching your messages" live dot + big serif
 * title — ports apps/web/src/components/clarity/GreetingHeader.tsx and the
 * mockup's `.app-header` (lines 152-162) to React Native. Home uses this
 * with a real "Good {time}, {name}." greeting and the live indicator, the
 * other three tabs reuse the same chrome with a plain section title — those
 * tab-root screens aren't in mockup 07 (it only shows the Home queue, a
 * conversation thread, and a property detail), so this is a faithful
 * extrapolation of the established header, not a new pattern.
 */
import { StyleSheet, Text, View } from "react-native";
import { colors, fonts, spacing, type } from "@/theme/tokens";

interface AppHeaderProps {
  title: string;
  showLiveIndicator?: boolean;
}

export function AppHeader({ title, showLiveIndicator = false }: AppHeaderProps) {
  return (
    <View style={styles.header}>
      <View style={styles.row}>
        <Text style={styles.wordmark}>
          Stoop<Text style={styles.wordmarkDot}>.</Text>
        </Text>
        {showLiveIndicator ? (
          <View style={styles.liveRow}>
            <View style={styles.liveDot} />
            <Text style={styles.liveLabel}>Watching your messages</Text>
          </View>
        ) : null}
      </View>
      <Text style={styles.title}>{title}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  header: {
    borderBottomWidth: 1,
    borderBottomColor: colors.line,
    paddingHorizontal: spacing.base + 4, // .app-header padding: 6px 20px 14px, mockup line 153
    paddingTop: spacing.sm,
    paddingBottom: spacing.md + 2,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    flexWrap: "wrap",
    gap: spacing.sm,
  },
  wordmark: {
    ...type.wordmark,
    color: colors.ink,
  },
  wordmarkDot: {
    color: colors.emergency,
    fontWeight: "900",
  },
  liveRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  liveDot: {
    width: 6,
    height: 6,
    borderRadius: 3,
    backgroundColor: colors.brand,
  },
  liveLabel: {
    fontFamily: fonts.sans,
    fontSize: 11.5,
    fontWeight: "700",
    color: colors.brand,
  },
  title: {
    ...type.greeting,
    color: colors.ink,
    marginTop: spacing.md + 2, // .greeting margin: 14px 0 4px, mockup line 161
    marginBottom: 4,
  },
});
