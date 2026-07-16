/**
 * The undo control drawn as a physical, perforated ticket strip — not a
 * toast that vanishes. Ports apps/web/src/components/clarity/
 * UndoTicket.tsx (docs/mockups/07 `.ticket`).
 *
 * `secondsLeft`/`totalSeconds` are display-only (the progress bar) — the
 * actual gate on whether Undo still works is the server's `undo_until`,
 * enforced by the DELETE call itself (src/api/drafts.ts's
 * `undoDraftApprove`), never this countdown.
 */
import { Pressable, StyleSheet, Text, View } from "react-native";
import { colors, radius, spacing, touchTarget, type } from "@/theme/tokens";

interface UndoTicketProps {
  secondsLeft: number;
  totalSeconds: number;
  onUndo: () => void;
}

export function UndoTicket({ secondsLeft, totalSeconds, onUndo }: UndoTicketProps) {
  const clamped = Math.max(0, secondsLeft);
  const pct = totalSeconds > 0 ? Math.max(0, Math.min(100, (clamped / totalSeconds) * 100)) : 0;
  const display = `00:${String(clamped).padStart(2, "0")}`;

  return (
    <View style={styles.container}>
      <View style={styles.row}>
        <View style={styles.timeBlock}>
          <Text style={styles.kicker}>Sending</Text>
          <Text style={styles.time}>{display}</Text>
        </View>
        <View style={styles.divider} />
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`Undo — ${clamped} seconds left`}
          onPress={onUndo}
          style={styles.undoButton}
          hitSlop={8}
        >
          <Text style={styles.undoLabel}>Undo</Text>
        </Pressable>
      </View>
      <View style={styles.track}>
        <View style={[styles.fill, { width: `${pct}%` }]} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    marginTop: spacing.md + 3,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.surface,
    paddingHorizontal: spacing.base,
    paddingTop: spacing.md,
    paddingBottom: spacing.md - 2,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
  },
  timeBlock: {
    flex: 1,
    minWidth: 0,
  },
  kicker: {
    ...type.marginKicker,
    color: colors.inkDim,
    marginBottom: 5,
  },
  time: {
    ...type.stamp,
    fontSize: 17,
    color: colors.ink,
  },
  divider: {
    width: 1,
    height: 32,
    borderLeftWidth: 1,
    borderStyle: "dashed",
    borderColor: colors.lineStrong,
  },
  undoButton: {
    minHeight: touchTarget - 4,
    justifyContent: "center",
    paddingHorizontal: spacing.xs,
  },
  undoLabel: {
    ...type.button,
    fontSize: 13.5,
    color: colors.emergency,
    textDecorationLine: "underline",
  },
  track: {
    marginTop: spacing.sm + 1,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.line,
    overflow: "hidden",
  },
  fill: {
    height: "100%",
    borderRadius: 2,
    backgroundColor: colors.brand,
  },
});
