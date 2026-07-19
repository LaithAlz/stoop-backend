/**
 * Wizard step 2 — the first property, via the shared PropertyForm: the
 * REAL `POST /v1/properties` provisioning call (the web wizard fakes this
 * with a timer and a mock number; here the number on the next screens is
 * whatever Twilio actually assigned). Every documented failure surfaces
 * inline in the form (cap / duplicate / no numbers / provisioning
 * failure — src/api/errors.ts).
 *
 * Resume honesty: if the wizard already created a property this visit
 * (context) — e.g. the landlord tapped Back from the tenants step — this
 * step shows what exists instead of offering to create it twice (the
 * server would 409 `duplicate_property` anyway; we just don't pretend).
 */
import { StyleSheet, Text, View } from "react-native";
import { useRouter } from "expo-router";
import { StoopNumberCard } from "@/components/clarity/StoopNumberCard";
import { WizardChrome } from "@/features/onboarding/WizardChrome";
import { useOnboarding } from "@/features/onboarding/OnboardingContext";
import { PropertyForm } from "@/features/properties/PropertyForm";
import { colors, spacing, type } from "@/theme/tokens";

export default function PropertyStep() {
  const router = useRouter();
  const { property, setProperty } = useOnboarding();

  if (property) {
    return (
      <WizardChrome
        stepNumber={2}
        title="Your first property is set."
        subtitle={`${property.label} already has its own number — nothing to redo here.`}
        onBack={() => router.back()}
        onNext={() => router.push("/onboarding/tenants")}
      >
        <StoopNumberCard number={property.twilio_number} />
      </WizardChrome>
    );
  }

  return (
    <WizardChrome
      stepNumber={2}
      title="Your first property."
      subtitle="Just one to start. You can add more later from Properties."
      onBack={() => router.back()}
    >
      <PropertyForm
        submitLabel="Reserve its number"
        onCreated={(created) => {
          setProperty(created);
          router.push("/onboarding/tenants");
        }}
      />
      <View style={styles.noteWrap}>
        <Text style={styles.note}>
          Adding a property reserves a real phone number for it — that takes a moment.
        </Text>
      </View>
    </WizardChrome>
  );
}

const styles = StyleSheet.create({
  noteWrap: {
    marginTop: -spacing.sm,
  },
  note: {
    ...type.footnote,
    fontSize: 12,
    color: colors.inkDim,
  },
});
