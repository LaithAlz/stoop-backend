/**
 * Wizard step 5 — the payoff: the property's REAL provisioned number (the
 * web wizard shows a mock; this shows whatever `POST /v1/properties`
 * actually assigned) plus the tenant-notice draft to send out. "Share"
 * uses the OS share sheet (React Native's built-in Share — straight into
 * Messages), which is more honest than the web's copy-to-clipboard on a
 * phone. No fake "test round-trip" is simulated here: the number is live,
 * and the honest instruction is to text it or tell your tenants.
 */
import { Alert, Share, StyleSheet, Text, View } from "react-native";
import { useRouter } from "expo-router";
import { useMe } from "@/api/me";
import { Button } from "@/components/Button";
import { StoopNumberCard } from "@/components/clarity/StoopNumberCard";
import { WizardChrome } from "@/features/onboarding/WizardChrome";
import { useOnboarding } from "@/features/onboarding/OnboardingContext";
import { buildDisclosureMessage } from "@/features/onboarding/disclosure";
import { firstName } from "@/lib/tenantName";
import { colors, radius, spacing, type } from "@/theme/tokens";

export default function NumberStep() {
  const router = useRouter();
  const { property } = useOnboarding();
  const meQuery = useMe();

  const finish = () => router.replace("/");

  if (!property) {
    return (
      <WizardChrome
        stepNumber={5}
        title="Almost there."
        subtitle="Add your first property to get its number."
        onBack={() => router.back()}
        onNext={() => router.push("/onboarding/property")}
        nextLabel="Add my property"
      >
        <View />
      </WizardChrome>
    );
  }

  const landlordFirst = meQuery.data?.full_name ? firstName(meQuery.data.full_name) : "";
  const disclosure = property.twilio_number
    ? buildDisclosureMessage(landlordFirst, property.label, property.twilio_number)
    : null;

  async function handleShare() {
    if (!disclosure) return;
    try {
      await Share.share({ message: disclosure });
    } catch {
      Alert.alert("Stoop", "Couldn't open the share sheet. You can send the note yourself.");
    }
  }

  return (
    <WizardChrome
      stepNumber={5}
      title={`${property.label} has its own number.`}
      subtitle="Save it, text it yourself, or send your tenants the note below."
      onBack={() => router.back()}
      onNext={finish}
      nextLabel="Go to your dashboard"
    >
      <StoopNumberCard number={property.twilio_number} />

      {disclosure ? (
        <View style={styles.noticeBox}>
          <Text style={styles.noticeKicker}>Tell your tenants</Text>
          <Text style={styles.noticeBody}>{disclosure}</Text>
          <Button
            label="Share this note"
            variant="ghost"
            onPress={() => void handleShare()}
            testID="share-disclosure"
          />
        </View>
      ) : null}

      <Text style={styles.footnote}>
        Emergency texts reach you free, no matter what. Everything else shows up on Home, sorted and
        drafted.
      </Text>
    </WizardChrome>
  );
}

const styles = StyleSheet.create({
  noticeBox: {
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.surface,
    padding: spacing.base,
    gap: spacing.md,
  },
  noticeKicker: {
    ...type.marginKicker,
    color: colors.inkDim,
  },
  noticeBody: {
    ...type.footnote,
    fontSize: 13.5,
    lineHeight: 20,
    color: colors.ink,
  },
  footnote: {
    ...type.footnote,
    fontSize: 12.5,
    color: colors.inkDim,
    textAlign: "center",
  },
});
