/**
 * Add/edit a tenant on a property — one modal for both (edit mode when a
 * `tenant` is passed), shared by the property-detail screen and the
 * onboarding wizard's tenants step. Drives the real contract routes
 * (src/api/tenants.ts): `POST /v1/properties/{id}/tenants` /
 * `PATCH /v1/tenants/{id}`. Fields are exactly the documented tenant body
 * ("name?, phone, unit?, vulnerable_occupant?, notes?" — api-contracts.md
 * "Tenants & Vendors"); a 409 `duplicate_phone` surfaces inline as its
 * house line, never a raw error.
 *
 * The vulnerable-occupant question is rubric-load-bearing, not
 * demographics: severity-rubric-v1's vulnerable-occupant modifier raises a
 * heat/power/water failure when someone vulnerable lives in the unit — the
 * inline note says so in plain English (same "Why" note as the web
 * onboarding).
 *
 * Same remount-to-reset pattern as EditDraftModal (`key` on the inner
 * content), so switching between add/edit or reopening never leaks state
 * through a `useEffect`.
 */
import { useState } from "react";
import {
  KeyboardAvoidingView,
  Modal,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createTenant, tenantsQueryKey, updateTenant } from "@/api/tenants";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { CreateTenantInput, Tenant, VulnerableOccupant } from "@/api/types";
import { Button } from "@/components/Button";
import { TextField } from "@/components/TextField";
import { ChipGroup, type ChipOption } from "@/components/clarity/ChipGroup";
import { MarginNote } from "@/components/clarity/MarginNote";
import { colors, spacing, type } from "@/theme/tokens";

/** Display labels for schema-v1's `vulnerable_occupant` values (null =
 *  "No one") — same wording the web onboarding cleared with copy review. */
export const VULNERABLE_OPTIONS: readonly ChipOption<VulnerableOccupant | null>[] = [
  { value: null, label: "No one" },
  { value: "infant", label: "An infant" },
  { value: "elderly", label: "An elderly person" },
  { value: "medical_device", label: "On powered medical equipment" },
];

interface TenantFormModalProps {
  visible: boolean;
  propertyId: string;
  /** Edit mode when set; add mode when null. */
  tenant: Tenant | null;
  onClose: () => void;
}

export function TenantFormModal({ visible, propertyId, tenant, onClose }: TenantFormModalProps) {
  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose} transparent={false}>
      <TenantFormContent
        key={tenant?.id ?? "new"}
        propertyId={propertyId}
        tenant={tenant}
        onClose={onClose}
      />
    </Modal>
  );
}

function TenantFormContent({ propertyId, tenant, onClose }: Omit<TenantFormModalProps, "visible">) {
  const reactQueryClient = useQueryClient();
  const [name, setName] = useState(tenant?.name ?? "");
  const [phone, setPhone] = useState(tenant?.phone ?? "");
  const [unit, setUnit] = useState(tenant?.unit ?? "");
  const [vulnerable, setVulnerable] = useState<VulnerableOccupant | null>(
    tenant?.vulnerable_occupant ?? null,
  );
  const [notes, setNotes] = useState(tenant?.notes ?? "");
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const phoneDigits = phone.replace(/\D/g, "");
  const phoneError = phoneDigits.length < 10 ? "Use a 10-digit phone number." : null;

  const mutation = useMutation({
    mutationFn: (input: CreateTenantInput) =>
      tenant ? updateTenant(tenant.id, input) : createTenant(propertyId, input),
    onSuccess: () => {
      void reactQueryClient.invalidateQueries({ queryKey: tenantsQueryKey(propertyId) });
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

  function handleSave() {
    setSubmitted(true);
    setServerError(null);
    if (phoneError || mutation.isPending) return;
    const input: CreateTenantInput = { phone: phone.trim() };
    if (name.trim()) input.name = name.trim();
    if (unit.trim()) input.unit = unit.trim();
    if (vulnerable) input.vulnerable_occupant = vulnerable;
    if (notes.trim()) input.notes = notes.trim();
    mutation.mutate(input);
  }

  return (
    <SafeAreaView style={styles.safeArea} edges={["top", "bottom"]}>
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <View style={styles.header}>
          <Text style={styles.heading}>{tenant ? "Edit tenant" : "Add a tenant"}</Text>
          <Text style={styles.subheading}>
            {tenant
              ? "Changes apply to their future messages."
              : "So Stoop knows who's texting in from this property."}
          </Text>
        </View>

        <ScrollView
          style={styles.flex}
          contentContainerStyle={styles.body}
          keyboardShouldPersistTaps="handled"
        >
          <TextField
            label="Name"
            value={name}
            onChangeText={setName}
            placeholder="Elena Petrova"
            autoComplete="name"
            testID="tenant-name"
          />

          <View>
            <TextField
              label="Phone"
              value={phone}
              onChangeText={setPhone}
              placeholder="(416) 555-0134"
              keyboardType="phone-pad"
              autoComplete="tel"
              testID="tenant-phone"
            />
            {submitted && phoneError ? (
              <Text style={styles.fieldError}>{phoneError}</Text>
            ) : (
              <Text style={styles.helper}>The number they&rsquo;ll text from.</Text>
            )}
          </View>

          <TextField
            label="Unit (optional)"
            value={unit}
            onChangeText={setUnit}
            placeholder="Unit 2"
            testID="tenant-unit"
          />

          <View style={styles.chipsBlock}>
            <Text style={styles.chipsLabel}>Anyone vulnerable in this unit?</Text>
            <ChipGroup
              options={VULNERABLE_OPTIONS}
              value={vulnerable}
              onChange={setVulnerable}
              accessibilityLabel="Anyone vulnerable in this unit?"
            />
          </View>

          <MarginNote>
            If anything ever goes wrong here, I treat it more urgently when someone vulnerable lives
            in the unit.
          </MarginNote>

          <TextField
            label="Notes (optional)"
            value={notes}
            onChangeText={setNotes}
            multiline
            style={styles.notesInput}
            textAlignVertical="top"
            testID="tenant-notes"
          />

          {serverError ? (
            <Text style={styles.serverError} testID="tenant-form-error">
              {serverError}
            </Text>
          ) : null}
        </ScrollView>

        <View style={styles.actions}>
          <View style={styles.cancelWrap}>
            <Button label="Cancel" variant="ghost" onPress={onClose} testID="tenant-cancel" />
          </View>
          <View style={styles.saveWrap}>
            <Button
              label={mutation.isPending ? "Saving…" : tenant ? "Save changes" : "Add tenant"}
              variant="primary"
              disabled={mutation.isPending}
              onPress={handleSave}
              testID="tenant-save"
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
  chipsBlock: {
    gap: spacing.sm,
  },
  chipsLabel: {
    ...type.meta,
    color: colors.inkDim,
  },
  notesInput: {
    minHeight: 88,
  },
  serverError: {
    ...type.footnote,
    color: colors.emergency,
  },
  actions: {
    flexDirection: "row",
    gap: spacing.sm + 2,
    padding: spacing.lg,
    borderTopWidth: 1,
    borderTopColor: colors.line,
  },
  cancelWrap: {
    width: 110,
  },
  saveWrap: {
    flex: 1,
  },
});
