/**
 * Wizard step 3 — who's texting in. Real tenants on the real property
 * (`GET/POST /v1/properties/{id}/tenants`) via the same TenantFormModal
 * the property-detail screen uses, so nothing here forks the tenant
 * contract handling (duplicate_phone included). Fully skippable —
 * tenants can always be added later from Properties.
 */
import { useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { useTenants } from "@/api/tenants";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { Tenant } from "@/api/types";
import { Button } from "@/components/Button";
import { MarginNote } from "@/components/clarity/MarginNote";
import { WizardChrome } from "@/features/onboarding/WizardChrome";
import { useOnboarding } from "@/features/onboarding/OnboardingContext";
import { TenantFormModal } from "@/features/tenants/TenantFormModal";
import { firstName } from "@/lib/tenantName";
import { colors, radius, spacing, type } from "@/theme/tokens";

export default function TenantsStep() {
  const router = useRouter();
  const { property } = useOnboarding();
  const tenantsQuery = useTenants(property?.id);

  const [modalOpen, setModalOpen] = useState(false);
  const [editingTenant, setEditingTenant] = useState<Tenant | null>(null);

  const goNext = () => router.push("/onboarding/backup");

  if (!property) {
    // Deep-linked here without a created property — nothing to attach
    // tenants to; send them to the step that creates one.
    return (
      <WizardChrome
        stepNumber={3}
        title="First, your property."
        subtitle="Tenants attach to a property — add yours first."
        onBack={() => router.back()}
        onNext={() => router.push("/onboarding/property")}
        nextLabel="Add my property"
      >
        <View />
      </WizardChrome>
    );
  }

  const tenants = tenantsQuery.data?.items.filter((tenant) => tenant.active) ?? [];

  return (
    <WizardChrome
      stepNumber={3}
      title="Who's texting in?"
      subtitle={`Add the tenants at ${property.label}, or skip and add them later from Properties.`}
      onBack={() => router.back()}
      onSkip={goNext}
      onNext={goNext}
      nextLabel="Continue"
    >
      {tenantsQuery.isLoading ? (
        <ActivityIndicator color={colors.brand} style={styles.spinner} />
      ) : tenantsQuery.isError ? (
        <Text style={styles.errorText}>
          {tenantsQuery.error instanceof ApiError
            ? toHouseApiError(tenantsQuery.error)
            : "Couldn't load the tenants here. Try again."}
        </Text>
      ) : (
        tenants.map((tenant) => (
          <Pressable
            key={tenant.id}
            accessibilityRole="button"
            onPress={() => {
              setEditingTenant(tenant);
              setModalOpen(true);
            }}
            style={({ pressed }) => [styles.tenantRow, pressed && styles.pressed]}
          >
            <View style={styles.tenantText}>
              <Text style={styles.tenantName}>
                {firstName(tenant.name)}
                {tenant.unit ? ` — Unit ${tenant.unit}` : ""}
              </Text>
              <Text style={styles.tenantPhoneNote}>Tap to edit</Text>
            </View>
            <Ionicons name="pencil-outline" size={15} color={colors.inkDim} />
          </Pressable>
        ))
      )}

      <Button
        label={tenants.length === 0 ? "Add a tenant" : "Add another tenant"}
        variant="ghost"
        onPress={() => {
          setEditingTenant(null);
          setModalOpen(true);
        }}
        testID="onboarding-add-tenant"
      />

      <MarginNote>
        If anything ever goes wrong here, I treat it more urgently when someone vulnerable lives in
        the unit.
      </MarginNote>

      <TenantFormModal
        visible={modalOpen}
        propertyId={property.id}
        tenant={editingTenant}
        onClose={() => setModalOpen(false)}
      />
    </WizardChrome>
  );
}

const styles = StyleSheet.create({
  spinner: {
    alignSelf: "flex-start",
  },
  errorText: {
    ...type.footnote,
    color: colors.inkDim,
  },
  tenantRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.md,
    minHeight: 44,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.surface,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm + 2,
  },
  pressed: {
    opacity: 0.85,
  },
  tenantText: {
    flex: 1,
    minWidth: 0,
    gap: 1,
  },
  tenantName: {
    ...type.meta,
    fontWeight: "700",
    color: colors.ink,
  },
  tenantPhoneNote: {
    ...type.footnote,
    fontSize: 11.5,
    color: colors.inkDim,
  },
});
