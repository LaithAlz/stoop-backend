/**
 * The five-step wizard's shared scaffolding — the RN port of the web
 * onboarding's `WizardChrome` (apps/web/src/routes/onboarding.tsx): back
 * arrow / progress dots / optional skip in the header, "STEP N OF 5"
 * kicker, serif title, scrollable body, and an optional pinned Continue
 * button. Steps that own their submit (the property step's provisioning
 * call) omit `onNext` and render their own primary button inline.
 */
import type { ReactNode } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { Button } from "@/components/Button";
import { colors, spacing, type } from "@/theme/tokens";

export const TOTAL_WIZARD_STEPS = 5;

interface WizardChromeProps {
  stepNumber: number;
  title: string;
  subtitle?: string;
  onBack: () => void;
  onSkip?: () => void;
  skipLabel?: string;
  onNext?: () => void;
  nextLabel?: string;
  nextDisabled?: boolean;
  children: ReactNode;
}

export function WizardChrome({
  stepNumber,
  title,
  subtitle,
  onBack,
  onSkip,
  skipLabel = "Skip for now",
  onNext,
  nextLabel = "Continue",
  nextDisabled,
  children,
}: WizardChromeProps) {
  return (
    <SafeAreaView style={styles.safeArea} edges={["top", "bottom"]}>
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <View style={styles.header}>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Go back"
            onPress={onBack}
            style={styles.backButton}
            hitSlop={8}
          >
            <Ionicons name="chevron-back" size={20} color={colors.inkDim} />
          </Pressable>
          <View style={styles.dots} accessibilityElementsHidden importantForAccessibility="no">
            {Array.from({ length: TOTAL_WIZARD_STEPS }, (_, i) => i + 1).map((n) => (
              <View
                key={n}
                style={[
                  styles.dot,
                  n < stepNumber && styles.dotDone,
                  n === stepNumber && styles.dotActive,
                ]}
              />
            ))}
          </View>
          {onSkip ? (
            <Pressable
              accessibilityRole="button"
              onPress={onSkip}
              style={styles.skipButton}
              hitSlop={8}
              testID="wizard-skip"
            >
              <Text style={styles.skipLabel}>{skipLabel}</Text>
            </Pressable>
          ) : (
            <View style={styles.headerSpacer} />
          )}
        </View>

        <ScrollView
          style={styles.flex}
          contentContainerStyle={styles.scrollContent}
          keyboardShouldPersistTaps="handled"
        >
          <Text style={styles.kicker}>
            Step {stepNumber} of {TOTAL_WIZARD_STEPS}
          </Text>
          <Text style={styles.title} accessibilityRole="header">
            {title}
          </Text>
          {subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}
          <View style={styles.body}>{children}</View>
        </ScrollView>

        {onNext ? (
          <View style={styles.footer}>
            <Button
              label={nextLabel}
              variant="primary"
              disabled={nextDisabled}
              onPress={onNext}
              testID="wizard-next"
            />
          </View>
        ) : null}
      </KeyboardAvoidingView>
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
    paddingHorizontal: spacing.base,
    paddingTop: spacing.sm,
    paddingBottom: spacing.xs,
  },
  backButton: {
    width: 44,
    height: 44,
    alignItems: "flex-start",
    justifyContent: "center",
  },
  dots: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.line,
  },
  dotDone: {
    backgroundColor: colors.brand,
  },
  dotActive: {
    width: 22,
    backgroundColor: colors.brand,
  },
  skipButton: {
    minHeight: 44,
    justifyContent: "center",
  },
  skipLabel: {
    ...type.meta,
    fontWeight: "700",
    color: colors.inkDim,
  },
  headerSpacer: {
    width: 44,
  },
  scrollContent: {
    paddingHorizontal: spacing.xl,
    paddingBottom: spacing.xl,
  },
  kicker: {
    ...type.marginKicker,
    color: colors.brand,
    marginTop: spacing.sm,
  },
  title: {
    ...type.greeting,
    fontSize: 25,
    lineHeight: 30,
    color: colors.ink,
    marginTop: spacing.sm,
  },
  subtitle: {
    ...type.body,
    fontSize: 14.5,
    lineHeight: 21,
    color: colors.inkDim,
    marginTop: spacing.sm,
  },
  body: {
    marginTop: spacing.xl,
    gap: spacing.lg,
  },
  footer: {
    borderTopWidth: 1,
    borderTopColor: colors.line,
    paddingHorizontal: spacing.xl,
    paddingVertical: spacing.base,
    backgroundColor: colors.bg,
  },
});
