/**
 * Edit name / emergency-call phone — the Me tab's `PATCH /v1/me` surface
 * (issue #210 M2). Fields are exactly the two this form edits of the four
 * documented ones (src/api/types.ts's `UpdateMeInput`); the payload
 * builder (src/features/account/profileEdit.ts) omits anything blank or
 * unchanged, and never sends a null. The phone field can't prefill —
 * `GET /v1/me` never returns it (write-only on this contract) — so the
 * helper says plainly that blank keeps the current number.
 *
 * Same remount-to-reset pattern as EditDraftModal (`key` on the inner
 * content).
 */
import { useState } from "react";
import { KeyboardAvoidingView, Modal, Platform, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { meQueryKey, updateMe } from "@/api/me";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { LandlordMe, UpdateMeInput } from "@/api/types";
import { Button } from "@/components/Button";
import { TextField } from "@/components/TextField";
import { buildMeUpdatePayload, phoneLooksValid } from "@/features/account/profileEdit";
import { colors, spacing, type } from "@/theme/tokens";

interface ProfileEditModalProps {
  visible: boolean;
  currentName: string | null;
  onClose: () => void;
}

export function ProfileEditModal({ visible, currentName, onClose }: ProfileEditModalProps) {
  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose} transparent={false}>
      <ProfileEditContent key={currentName ?? ""} currentName={currentName} onClose={onClose} />
    </Modal>
  );
}

function ProfileEditContent({ currentName, onClose }: Omit<ProfileEditModalProps, "visible">) {
  const reactQueryClient = useQueryClient();
  const [name, setName] = useState(currentName ?? "");
  const [phone, setPhone] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (input: UpdateMeInput) => updateMe(input),
    onSuccess: (me: LandlordMe) => {
      reactQueryClient.setQueryData(meQueryKey, me);
      onClose();
    },
    onError: (error) => {
      setServerError(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      );
    },
  });

  const phoneError = phoneLooksValid(phone) ? null : "Use a 10-digit phone number.";

  function handleSave() {
    setSubmitted(true);
    setServerError(null);
    if (phoneError || mutation.isPending) return;
    const payload = buildMeUpdatePayload({ name, phone }, { full_name: currentName });
    if (!payload) {
      onClose();
      return;
    }
    mutation.mutate(payload);
  }

  return (
    <SafeAreaView style={styles.safeArea} edges={["top", "bottom"]}>
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <View style={styles.header}>
          <Text style={styles.heading}>Your details</Text>
          <Text style={styles.subheading}>Name and the phone emergency calls ring.</Text>
        </View>

        <View style={styles.body}>
          <TextField
            label="Full name"
            value={name}
            onChangeText={setName}
            autoComplete="name"
            testID="profile-name"
          />

          <View>
            <TextField
              label="Your phone number"
              value={phone}
              onChangeText={setPhone}
              placeholder="(416) 555-0134"
              keyboardType="phone-pad"
              autoComplete="tel"
              testID="profile-phone"
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

          {serverError ? (
            <Text style={styles.serverError} testID="profile-error">
              {serverError}
            </Text>
          ) : null}
        </View>

        <View style={styles.actions}>
          <View style={styles.cancelWrap}>
            <Button label="Cancel" variant="ghost" onPress={onClose} testID="profile-cancel" />
          </View>
          <View style={styles.saveWrap}>
            <Button
              label={mutation.isPending ? "Saving…" : "Save"}
              variant="primary"
              disabled={mutation.isPending}
              onPress={handleSave}
              testID="profile-save"
            />
          </View>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  header: {
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
    paddingBottom: spacing.sm,
    borderBottomWidth: 1,
    borderBottomColor: colors.line,
  },
  heading: {
    ...type.cardTitle,
    fontSize: 19,
    color: colors.ink,
  },
  subheading: {
    ...type.meta,
    fontWeight: "500",
    color: colors.inkDim,
    marginTop: 2,
  },
  body: {
    flex: 1,
    padding: spacing.lg,
    gap: spacing.base,
  },
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
  serverError: {
    ...type.footnote,
    color: colors.emergency,
  },
  actions: {
    flexDirection: "row",
    gap: spacing.sm + 2,
    padding: spacing.lg,
  },
  cancelWrap: {
    width: 110,
  },
  saveWrap: {
    flex: 1,
  },
});
