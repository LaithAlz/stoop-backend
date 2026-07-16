/**
 * Me — account & settings. M0 ships the shell plus the one real control
 * this phase has: sign out. Shows the signed-in email (already visible to
 * the signed-in user — display only; never logged, CLAUDE.md rule 5).
 *
 * M2 TODO: profile editing via GET/PATCH /v1/me, notification preferences,
 * plan details. Plan copy must follow CLAUDE.md rule 8 (free Emergency
 * Line / $10 Full Plan / $5 early-access grandfathered).
 */
import { StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useAuth } from "@/auth/AuthProvider";
import { AppHeader } from "@/components/AppHeader";
import { Button } from "@/components/Button";
import { colors, radius, spacing, type } from "@/theme/tokens";

export default function MeScreen() {
  const { session, signOut } = useAuth();

  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <AppHeader title="Me" />
      <View style={styles.body}>
        <View style={styles.card}>
          <Text style={styles.label}>Signed in as</Text>
          <Text style={styles.email}>{session?.user.email ?? "Unknown account"}</Text>
        </View>

        <View style={styles.card}>
          <Text style={styles.settingsNote}>
            Your profile, notification choices, and plan details will live here soon.
          </Text>
        </View>

        <Button label="Sign out" variant="ghost" onPress={() => void signOut()} testID="sign-out" />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  body: {
    flex: 1,
    padding: spacing.lg, // .app-main padding 16-18px, mockup line 169
    gap: spacing.base,
  },
  card: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    borderRadius: radius.lg,
    padding: spacing.lg, // .entry padding: 18px, mockup line 209
    gap: spacing.xs,
  },
  label: {
    ...type.meta,
    color: colors.inkDim,
  },
  email: {
    ...type.cardTitle,
    color: colors.ink,
  },
  settingsNote: {
    ...type.body,
    fontSize: 14.5,
    color: colors.inkDim,
  },
});
