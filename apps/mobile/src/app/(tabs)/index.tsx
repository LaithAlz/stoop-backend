/**
 * Home — the approval queue, per mockup exhibit 01 ("Home — the queue,
 * needing you"). M0 ships the shell only: no data fetching yet (issue
 * #210 scope), so this renders the honest "nothing to show yet" state
 * rather than a fake "0 need you" count — there's no queue endpoint call
 * behind it to make that number true.
 *
 * M1 TODO: fetch GET /v1/queue (docs/03-engineering/api-contracts.md),
 * render EmergencyBanner + DecisionCard + CountsStrip above this empty
 * state (port apps/web/src/components/clarity/{EmergencyBanner,
 * DecisionCard,CountsStrip}.tsx), and only fall back to this view once
 * the queue really is empty.
 */
import { StyleSheet, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { AppHeader } from "@/components/AppHeader";
import { EmptyState } from "@/components/EmptyState";
import { colors } from "@/theme/tokens";

function timeOfDayGreeting(date: Date): string {
  const hour = date.getHours();
  if (hour < 12) return "morning";
  if (hour < 18) return "afternoon";
  return "evening";
}

export default function HomeScreen() {
  // The mockup greets by first name ("Good morning, Laith.") — but M0 has
  // no /v1/me fetch yet, so there's no real display name to greet with. A
  // name derived from the email local-part reads wrong ("Good morning,
  // Allaithalzoubi2."), so until M1 wires /v1/me the greeting stands alone.
  const greeting = `Good ${timeOfDayGreeting(new Date())}.`;

  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <AppHeader title={greeting} showLiveIndicator />
      <View style={styles.body}>
        <EmptyState
          icon="home-outline"
          title="Nothing to show yet."
          message="This is where anything that needs you will show up — one decision at a time, never a table to scan."
          note="Connect your first property to turn this on."
        />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  body: { flex: 1 },
});
