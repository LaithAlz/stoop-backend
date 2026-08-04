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
 * #292: `phone` is validated and sent ONLY when the landlord actually
 * changed it (src/features/tenants/tenantForm.ts's `buildTenantUpdatePayload`
 * / `tenantPhoneUnchanged`), a legacy `tenants.phone` that predates
 * #232/#260's server-side canonicalization must not block an otherwise-
 * unrelated edit (most sharply `vulnerable_occupant`) just because it's
 * along for the ride in the PATCH body. Create mode is unaffected: a new
 * tenant's phone is always required and validated, same as before.
 *
 * Adversarial safety review, 2026-08-04, item 3 (FIX 3, MEDIUM): an
 * unchanged, un-normalizable stored phone skips the blocking `phoneError`
 * (by design, above), but that used to leave the field with zero signal:
 * the neutral "The number they'll text from." helper kept rendering over a
 * number Twilio can never dial and `_lookup_active_tenant` can never match.
 *
 * The re-verify corrected what the stake actually is, and the copy now
 * says it. This is NOT the `unrouted_inbound` path: that dead-letters an
 * unrecognized `To` (schema-v1.md v1.21 point 4), and this tenant's text
 * still routes, because `To` finds the property. What happens instead is
 * that `_lookup_active_tenant` returns None and the `messages` row is
 * written with `tenant_id = NULL`, so the landlord DOES hear something,
 * just unattributed. The real harm runs the other way:
 * `emergency_chain.py` resolves the tenant's number through
 * `messages.tenant_id -> tenants.phone`, so both tenant legs come back
 * `skipped / no_tenant_phone`. This tenant gets no reply and no emergency
 * safety SMS at 2am, and the `vulnerable_occupant` the landlord opened
 * this modal to set can never attach to their message either, which is
 * the irony at the middle of #292. `phoneUnchangedUnreachable`
 * below renders a non-blocking warning in that exact case, mirroring
 * apps/web/src/features/properties/settings.ts's
 * `backupContactPhoneLooksInvalid` pattern (a persistent notice for a
 * stored value, not a blocking form error) on this side of the fence.
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
import type { CreateTenantInput, Tenant, UpdateTenantInput, VulnerableOccupant } from "@/api/types";
import { Button } from "@/components/Button";
import { TextField } from "@/components/TextField";
import { ChipGroup, type ChipOption } from "@/components/clarity/ChipGroup";
import { MarginNote } from "@/components/clarity/MarginNote";
import { phoneErrorMessage, toE164 } from "@/lib/phone";
import { colors, spacing, type } from "@/theme/tokens";
import {
  buildTenantCreatePayload,
  buildTenantUpdatePayload,
  tenantPhoneUnchanged,
} from "./tenantForm";

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

  const trimmedPhone = phone.trim();
  // #292: an edit whose phone field is exactly what was loaded from
  // `tenant` skips validation entirely, a legacy `tenants.phone` that
  // predates #232/#260's server-side canonicalization must not block an
  // otherwise-valid edit to an unrelated field (most sharply
  // `vulnerable_occupant`, which feeds severity classification). Always
  // `false` in create mode, so a new tenant's phone is unaffected.
  const phoneUnchanged = tenantPhoneUnchanged(phone, tenant);
  // #269: same digit-count-only bug as the onboarding backup step, fixed
  // the same way — `tenants.phone` is what the routing match and the
  // draft/reply flow key off (schema-v1.md), so an un-normalized value
  // stored here is silently un-matchable, not just un-dialable. #276: the
  // message itself comes from `phoneErrorMessage`, which names a
  // non-ASCII-digit input specifically instead of restating the "10
  // digits" count rule.
  const phoneError = phoneUnchanged
    ? null
    : trimmedPhone.length === 0
      ? "Add a phone number."
      : phoneErrorMessage(trimmedPhone);
  // Adversarial safety review, 2026-08-04, item 3 (FIX 3): non-blocking
  // signal for the one case `phoneError` deliberately never covers, an
  // unchanged legacy phone Twilio can't dial and the `/sms` webhook can
  // never match (schema-v1.md v1.21 point 4). Never gates Save; only
  // controls which helper text renders below the field. Always `false`
  // when `phoneUnchanged` is `false` (create mode, or a phone the
  // landlord is actively editing), same as `phoneError`'s own scope.
  const phoneUnchangedUnreachable = phoneUnchanged && toE164(trimmedPhone) === null;

  function handleMutationSuccess() {
    void reactQueryClient.invalidateQueries({ queryKey: tenantsQueryKey(propertyId) });
    onClose();
  }

  function handleMutationError(error: unknown) {
    setServerError(
      error instanceof ApiError
        ? toHouseApiError(error)
        : "Something didn't go through. Try again in a moment.",
    );
  }

  // #292: two mutations, not one. `CreateTenantInput.phone` is required
  // (api-contracts.md) while `UpdateTenantInput.phone` is optional, and
  // omitting it on an unchanged edit is the whole point of this fix. A
  // single shared mutation could only keep that distinction with an unsafe
  // cast; splitting by verb keeps both request bodies honestly typed.
  const createMutation = useMutation({
    mutationFn: (input: CreateTenantInput) => createTenant(propertyId, input),
    onSuccess: handleMutationSuccess,
    onError: handleMutationError,
  });

  const updateMutation = useMutation({
    mutationFn: (args: { id: string; input: UpdateTenantInput }) =>
      updateTenant(args.id, args.input),
    onSuccess: handleMutationSuccess,
    onError: handleMutationError,
  });

  const isSaving = createMutation.isPending || updateMutation.isPending;

  function handleSave() {
    setSubmitted(true);
    setServerError(null);
    if (phoneError || isSaving) return;
    const form = { name, phone, unit, vulnerable, notes };
    if (tenant) {
      // #292: omits `phone` entirely when unchanged, see
      // tenantForm.ts's `buildTenantUpdatePayload` docstring. `null` means
      // nothing at all changed; close without a no-op PATCH.
      const payload = buildTenantUpdatePayload(form, tenant);
      if (!payload) {
        onClose();
        return;
      }
      updateMutation.mutate({ id: tenant.id, input: payload });
      return;
    }
    // Create mode: `phoneError` above already guarantees a valid phone;
    // the null check keeps this safe on its own too.
    const payload = buildTenantCreatePayload(form);
    if (!payload) return;
    createMutation.mutate(payload);
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
            ) : phoneUnchangedUnreachable ? (
              // Item 3 (FIX 3): non-blocking, never re-runs `phoneError` and
              // never disables Save. Shown as soon as the modal opens for a
              // tenant whose stored phone doesn't normalize, not only after
              // a submit attempt, since the landlord may never touch this
              // field on this visit.
              <Text style={styles.fieldWarning} testID="tenant-phone-warning">
                I can&rsquo;t text this number as it&rsquo;s saved, so this tenant won&rsquo;t get
                my replies or emergency safety steps. Update it.
              </Text>
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
              label={isSaving ? "Saving…" : tenant ? "Save changes" : "Add tenant"}
              variant="primary"
              disabled={isSaving}
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
  // Item 3 (FIX 3): same visual weight as `fieldError`, apps/web's
  // `backupContactPhoneLooksInvalid` notice reuses its blocking error color
  // for a non-blocking stored-value warning too, this mirrors that choice
  // rather than inventing a third, unreviewed warning color.
  fieldWarning: {
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
