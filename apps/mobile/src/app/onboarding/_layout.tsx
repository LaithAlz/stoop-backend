/**
 * The onboarding wizard's own stack (issue #210 M2) — pushed over the tab
 * shell by the zero-properties gate (src/features/onboarding/gate.ts) or
 * the Properties tab's empty state; the tabs stay mounted underneath, so
 * exiting at any step is just a back navigation. Wrapped in
 * OnboardingProvider so the created property flows to the later steps.
 */
import { Stack } from "expo-router";
import { OnboardingProvider } from "@/features/onboarding/OnboardingContext";
import { colors } from "@/theme/tokens";

export default function OnboardingLayout() {
  return (
    <OnboardingProvider>
      <Stack
        screenOptions={{
          headerShown: false,
          contentStyle: { backgroundColor: colors.bg },
        }}
      >
        <Stack.Screen name="index" />
        <Stack.Screen name="about-you" />
        <Stack.Screen name="property" />
        <Stack.Screen name="tenants" />
        <Stack.Screen name="backup" />
        <Stack.Screen name="number" />
      </Stack>
    </OnboardingProvider>
  );
}
