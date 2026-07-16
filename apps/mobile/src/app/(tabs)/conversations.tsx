/**
 * Conversations — the plain SMS history per channel, per mockup exhibit 04
 * ("Conversation thread"). M0 ships the shell only: no data fetching yet
 * (issue #210 scope), so this renders an honest empty state.
 *
 * M1 TODO: list channels via GET /v1/... (docs/03-engineering/
 * api-contracts.md), render ConversationRow / ThreadMessageRow /
 * DayDivider (port apps/web/src/components/clarity versions), and keep
 * the append-only framing from the mockup's footnote ("Nothing here can
 * be edited or removed once it's sent").
 */
import { StyleSheet, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { AppHeader } from "@/components/AppHeader";
import { EmptyState } from "@/components/EmptyState";
import { colors } from "@/theme/tokens";

export default function ConversationsScreen() {
  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <AppHeader title="Conversations" />
      <View style={styles.body}>
        <EmptyState
          icon="chatbubbles-outline"
          title="No conversations yet."
          message="Every text between your tenants and Stoop will be saved here, with dates and times — nothing edited, nothing lost."
        />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  body: { flex: 1 },
});
