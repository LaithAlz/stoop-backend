/**
 * Me — account & settings. M1 (issue #210) wires the real `GET /v1/me`
 * (name/email/plan display only — `PATCH /v1/me` profile editing is M2
 * scope) alongside M0's sign-out. `GET /v1/me`'s `full_name` also feeds
 * Home's dynamic greeting (src/app/(tabs)/index.tsx) — both screens read
 * the same React Query cache key (src/api/me.ts's `useMe`).
 */
import { ActivityIndicator, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useAuth } from "@/auth/AuthProvider";
import { AppHeader } from "@/components/AppHeader";
import { Button } from "@/components/Button";
import { colors, radius, spacing, type } from "@/theme/tokens";
import { useMe } from "@/api/me";
import { ApiError, toHouseApiError } from "@/api/errors";
import { planDisplayName } from "@/features/account/plan";

export default function MeScreen() {
  const { session, signOut } = useAuth();
  const meQuery = useMe();

  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <AppHeader title="Me" />
      <View style={styles.body}>
        <View style={styles.card}>
          <Text style={styles.label}>Signed in as</Text>
          <Text style={styles.email}>
            {meQuery.data?.full_name || session?.user.email || "Unknown account"}
          </Text>
          {meQuery.data?.full_name ? (
            <Text style={styles.subtext}>{meQuery.data.email}</Text>
          ) : null}
        </View>

        <View style={styles.card}>
          <Text style={styles.label}>Plan</Text>
          {meQuery.isSuccess ? (
            <Text style={styles.email}>
              {planDisplayName(meQuery.data.subscription_tier, meQuery.data.price_cohort)}
            </Text>
          ) : meQuery.isError ? (
            <Text style={styles.subtext}>
              {meQuery.error instanceof ApiError
                ? toHouseApiError(meQuery.error)
                : "Couldn't load your plan right now."}
            </Text>
          ) : (
            <ActivityIndicator color={colors.brand} style={styles.planSpinner} />
          )}
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
  subtext: {
    ...type.footnote,
    color: colors.inkDim,
  },
  planSpinner: {
    alignSelf: "flex-start",
    marginTop: spacing.xs,
  },
});
