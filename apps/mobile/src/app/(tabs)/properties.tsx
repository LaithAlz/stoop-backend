/**
 * Properties — per-property view incl. the trust ladder story, per mockup
 * exhibit 05 ("Property trust, told as a story"). M0 ships the shell only:
 * no data fetching yet (issue #210 scope), so this renders an honest empty
 * state.
 *
 * M2 TODO: list properties (GET /v1/properties), provisioning flow, trust
 * ladder progress + revoke controls (new permissions default OFF, always
 * the landlord's call — mockup exhibit 05, CLAUDE.md rule 3).
 */
import { StyleSheet, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { AppHeader } from "@/components/AppHeader";
import { EmptyState } from "@/components/EmptyState";
import { colors } from "@/theme/tokens";

export default function PropertiesScreen() {
  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <AppHeader title="Properties" />
      <View style={styles.body}>
        <EmptyState
          icon="business-outline"
          title="No properties yet."
          message="Each property you add gets its own phone number for tenants to text. They'll all show up here."
        />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  body: { flex: 1 },
});
