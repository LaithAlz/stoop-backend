/**
 * Wizard step 1 — who's on the other end: name + the phone emergency
 * calls ring. Drives the REAL `PATCH /v1/me` (the web wizard's account
 * step is a mock). Name prefills from `GET /v1/me`; phone can't (the
 * contract never returns it — write-only field), so blank means "keep
 * what's on file" and the payload builder
 * (src/features/account/profileEdit.ts) guarantees a blank field is
 * omitted, never sent as null.
 */
import { useState } from "react";
import { Alert, StyleSheet, Text, View } from "react-native";
import { useRouter } from "expo-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { meQueryKey, updateMe, useMe } from "@/api/me";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { UpdateMeInput } from "@/api/types";
import { TextField } from "@/components/TextField";
import { WizardChrome } from "@/features/onboarding/WizardChrome";
import { buildMeUpdatePayload, phoneLooksValid } from "@/features/account/profileEdit";
import { colors, spacing, type } from "@/theme/tokens";

export default function AboutYouStep() {
  const router = useRouter();
  const reactQueryClient = useQueryClient();
  const meQuery = useMe();

  const [name, setName] = useState<string | null>(null);
  const [phone, setPhone] = useState("");
  const [submitted, setSubmitted] = useState(false);

  // Prefill once the read lands, without clobbering anything typed first.
  const nameValue = name ?? meQuery.data?.full_name ?? "";

  const mutation = useMutation({
    mutationFn: (input: UpdateMeInput) => updateMe(input),
    onSuccess: (me) => {
      reactQueryClient.setQueryData(meQueryKey, me);
      router.push("/onboarding/property");
    },
    onError: (error) =>
      Alert.alert(
        "Stoop",
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      ),
  });

  const phoneError = phoneLooksValid(phone) ? null : "Use a 10-digit phone number.";

  function handleContinue() {
    setSubmitted(true);
    if (phoneError || mutation.isPending) return;
    const payload = buildMeUpdatePayload(
      { name: nameValue, phone },
      { full_name: meQuery.data?.full_name ?? null },
    );
    if (!payload) {
      router.push("/onboarding/property");
      return;
    }
    mutation.mutate(payload);
  }

  return (
    <WizardChrome
      stepNumber={1}
      title="Tell us who you are."
      subtitle="So Stoop knows who's on the other end of every text."
      onBack={() => router.back()}
      onSkip={() => router.push("/onboarding/property")}
      onNext={handleContinue}
      nextLabel={mutation.isPending ? "Saving…" : "Continue"}
      nextDisabled={mutation.isPending}
    >
      <TextField
        label="Full name"
        value={nameValue}
        onChangeText={setName}
        placeholder="Sarah Chen"
        autoComplete="name"
        testID="about-you-name"
      />

      <View>
        <TextField
          label="Your phone number"
          value={phone}
          onChangeText={setPhone}
          placeholder="(416) 555-0134"
          keyboardType="phone-pad"
          autoComplete="tel"
          testID="about-you-phone"
        />
        {submitted && phoneError ? (
          <Text style={styles.fieldError}>{phoneError}</Text>
        ) : (
          <Text style={styles.helper}>
            This is where emergency calls ring, day or night. Leave it blank to keep the number
            already on file.
          </Text>
        )}
      </View>
    </WizardChrome>
  );
}

const styles = StyleSheet.create({
  helper: {
    ...type.footnote,
    color: colors.inkDim,
    marginTop: spacing.xs,
  },
  fieldError: {
    ...type.footnote,
    color: colors.emergency,
    marginTop: spacing.xs,
  },
});
