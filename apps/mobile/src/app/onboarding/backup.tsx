/**
 * Wizard step 4 — the backup contact for the emergency escalation chain,
 * saved onto the REAL property (`PATCH /v1/properties/{id}` —
 * `backup_contact` is a property field per api-contracts.md, and the
 * create call in step 2 didn't collect it). Strongly encouraged, never
 * required — skippable like the web step. The explanation copy is the web
 * wizard's own cleared wording (call you → call again → call the backup
 * ten minutes later), with the always-free promise kept intact (rule 1).
 */
import { useState } from "react";
import { Alert, StyleSheet, Text, View } from "react-native";
import { useRouter } from "expo-router";
import { useMutation } from "@tanstack/react-query";
import { updateProperty } from "@/api/properties";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { BackupContact } from "@/api/types";
import { SeverityPlaque } from "@/components/clarity/SeverityPlaque";
import { TextField } from "@/components/TextField";
import { WizardChrome } from "@/features/onboarding/WizardChrome";
import { useOnboarding } from "@/features/onboarding/OnboardingContext";
import { colors, radius, spacing, type } from "@/theme/tokens";

export default function BackupStep() {
  const router = useRouter();
  const { property, setProperty } = useOnboarding();

  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const goNext = () => router.push("/onboarding/number");

  const mutation = useMutation({
    mutationFn: (backupContact: BackupContact) =>
      updateProperty(property?.id ?? "", { backup_contact: backupContact }),
    onSuccess: (updated) => {
      setProperty(updated);
      goNext();
    },
    onError: (error) =>
      Alert.alert(
        "Stoop",
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      ),
  });

  const engaged = name.trim().length > 0 || phone.trim().length > 0;
  const phoneDigits = phone.replace(/\D/g, "");
  const phoneError = engaged && phoneDigits.length < 10 ? "Use a 10-digit phone number." : null;
  const nameError = engaged && !name.trim() ? "Add their name." : null;

  function handleContinue() {
    setSubmitted(true);
    if (mutation.isPending) return;
    if (!engaged || !property) {
      goNext();
      return;
    }
    if (phoneError || nameError) return;
    mutation.mutate({ name: name.trim(), phone: phone.trim() });
  }

  const backupName = name.trim() || "your backup contact";

  return (
    <WizardChrome
      stepNumber={4}
      title="Who do we call if you don't pick up?"
      subtitle="Strongly encouraged, not required — a partner, super, or trusted neighbor."
      onBack={() => router.back()}
      onSkip={goNext}
      onNext={handleContinue}
      nextLabel={mutation.isPending ? "Saving…" : "Continue"}
      nextDisabled={mutation.isPending}
    >
      <View>
        <TextField
          label="Their name (optional)"
          value={name}
          onChangeText={setName}
          placeholder="Jordan (super)"
          testID="backup-name"
        />
        {submitted && nameError ? <Text style={styles.fieldError}>{nameError}</Text> : null}
      </View>

      <View>
        <TextField
          label="Their phone (optional)"
          value={phone}
          onChangeText={setPhone}
          placeholder="(416) 555-0177"
          keyboardType="phone-pad"
          testID="backup-phone"
        />
        {submitted && phoneError ? <Text style={styles.fieldError}>{phoneError}</Text> : null}
      </View>

      <View style={styles.explainBox}>
        <View style={styles.explainHead}>
          <SeverityPlaque severity="emergency" size="sm" />
          <Text style={styles.explainKicker}>Always free</Text>
        </View>
        <Text style={styles.explainBody}>
          When a tenant sends something like this, I call your phone right away — free, no matter
          your plan. If you don&rsquo;t answer, I call again, then call {backupName} ten minutes
          later. Nobody has to wait alone.
        </Text>
      </View>
    </WizardChrome>
  );
}

const styles = StyleSheet.create({
  fieldError: {
    ...type.footnote,
    color: colors.emergency,
    marginTop: spacing.xs,
  },
  explainBox: {
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.surface,
    padding: spacing.base,
    gap: spacing.md,
  },
  explainHead: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
  },
  explainKicker: {
    ...type.marginKicker,
    color: colors.inkDim,
  },
  explainBody: {
    ...type.footnote,
    fontSize: 13,
    lineHeight: 19,
    color: colors.inkDim,
  },
});
