/**
 * Onboarding welcome — the RN port of the web wizard's WelcomeStep
 * (apps/web/src/routes/onboarding.tsx), reusing its cleared copy. The
 * pricing sentence is deliberately NOT ported (no new in-app price copy
 * without a store-review decision); the free-Emergency-Line promise is
 * (rule 1 — it's the product's spine). "Exit" pops back to the tabs — the
 * wizard never traps anyone.
 */
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { Button } from "@/components/Button";
import { SeverityPlaque } from "@/components/clarity/SeverityPlaque";
import { colors, radius, spacing, type } from "@/theme/tokens";

export default function OnboardingWelcomeScreen() {
  const router = useRouter();

  return (
    <SafeAreaView style={styles.safeArea} edges={["top", "bottom"]}>
      <View style={styles.header}>
        <Text style={styles.wordmark}>
          Stoop<Text style={styles.wordmarkDot}>.</Text>
        </Text>
        <Pressable
          accessibilityRole="button"
          onPress={() => router.back()}
          style={styles.exitButton}
          hitSlop={8}
          testID="onboarding-exit"
        >
          <Text style={styles.exitLabel}>Exit</Text>
        </Pressable>
      </View>

      <ScrollView style={styles.flex} contentContainerStyle={styles.scrollContent}>
        <Text style={styles.title} accessibilityRole="header">
          Let&rsquo;s get Stoop answering your tenants.
        </Text>
        <Text style={styles.subtitle}>
          Five short steps, about five minutes. You&rsquo;ll end with a live number your tenants can
          text.
        </Text>

        <View style={styles.bucketsBlock}>
          <Text style={styles.bucketsKicker}>
            Every tenant text gets sorted into one of three buckets
          </Text>
          <View style={styles.plaquesRow}>
            <SeverityPlaque severity="emergency" size="sm" />
            <SeverityPlaque severity="urgent" size="sm" />
            <SeverityPlaque severity="routine" size="sm" />
          </View>
          <Text style={styles.bucketsBody}>
            An emergency rings your phone right away. Everything else waits for you, sorted and
            drafted.
          </Text>
        </View>

        <View style={styles.promiseBox}>
          <Ionicons
            name="shield-checkmark-outline"
            size={16}
            color={colors.brand}
            style={styles.promiseIcon}
          />
          <Text style={styles.promiseText}>
            The Emergency Line is free, forever — every message read, real emergencies ring your
            phone.
          </Text>
        </View>
      </ScrollView>

      <View style={styles.footer}>
        <Button
          label="Get started"
          variant="primary"
          onPress={() => router.push("/onboarding/about-you")}
          testID="onboarding-start"
        />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: spacing.base + 4,
    paddingTop: spacing.sm,
  },
  wordmark: {
    ...type.wordmark,
    color: colors.ink,
  },
  wordmarkDot: {
    color: colors.emergency,
    fontWeight: "900",
  },
  exitButton: {
    minHeight: 44,
    justifyContent: "center",
  },
  exitLabel: {
    ...type.meta,
    fontWeight: "700",
    color: colors.inkDim,
  },
  scrollContent: {
    paddingHorizontal: spacing.xl,
    paddingBottom: spacing.xl,
  },
  title: {
    ...type.greeting,
    fontSize: 28,
    lineHeight: 34,
    color: colors.ink,
    marginTop: spacing.xl + 8,
  },
  subtitle: {
    ...type.body,
    fontSize: 14.5,
    lineHeight: 21,
    color: colors.inkDim,
    marginTop: spacing.md,
  },
  bucketsBlock: {
    marginTop: spacing.xl + 4,
  },
  bucketsKicker: {
    ...type.marginKicker,
    color: colors.inkDim,
  },
  plaquesRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: spacing.sm,
    marginTop: spacing.sm + 2,
  },
  bucketsBody: {
    ...type.footnote,
    fontSize: 13,
    lineHeight: 19,
    color: colors.inkDim,
    marginTop: spacing.sm + 2,
  },
  promiseBox: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: spacing.sm + 2,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.surface,
    padding: spacing.base,
    marginTop: spacing.xl + 4,
  },
  promiseIcon: {
    marginTop: 2,
  },
  promiseText: {
    flex: 1,
    ...type.footnote,
    fontSize: 13,
    lineHeight: 19,
    color: colors.inkDim,
  },
  footer: {
    borderTopWidth: 1,
    borderTopColor: colors.line,
    paddingHorizontal: spacing.xl,
    paddingVertical: spacing.base,
  },
});
