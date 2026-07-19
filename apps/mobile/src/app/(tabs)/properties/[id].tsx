/**
 * Property detail (issue #210 M2) — `GET /v1/properties/{id}` plus the
 * property's tenants (`GET /v1/properties/{id}/tenants`, unpaginated per
 * the contract). Leads with the property's own Stoop number (the number
 * tenants text — StoopNumberCard is honest when it's null). Below: the
 * tenant list with add/edit (TenantFormModal), the trust-ladder section
 * (REVOKE ACTION ONLY — no read contract for trust state exists; see
 * src/api/trust.ts), and the confirm-gated delete with the 24-hour
 * number-release hold explained in plain English.
 */
import { useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { colors, radius, spacing, type } from "@/theme/tokens";
import { deleteProperty, propertiesQueryKey, useProperty } from "@/api/properties";
import { useTenants } from "@/api/tenants";
import { revokeTrust } from "@/api/trust";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { Tenant } from "@/api/types";
import { Button } from "@/components/Button";
import { StoopNumberCard } from "@/components/clarity/StoopNumberCard";
import { firstName } from "@/lib/tenantName";
import { TenantFormModal, VULNERABLE_OPTIONS } from "@/features/tenants/TenantFormModal";
import {
  revokeConfirmation,
  revokeResultNotice,
  TRUST_SECTION_BODY,
  TRUST_SECTION_TITLE,
} from "@/features/trust/revoke";
import {
  DELETE_PROPERTY_CONFIRM_LABEL,
  DELETE_PROPERTY_MESSAGE,
  DELETE_PROPERTY_TITLE,
} from "@/features/properties/deleteProperty";

function vulnerableLabel(tenant: Tenant): string | null {
  if (!tenant.vulnerable_occupant) return null;
  const option = VULNERABLE_OPTIONS.find((o) => o.value === tenant.vulnerable_occupant);
  return option ? option.label : null;
}

export default function PropertyDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const reactQueryClient = useQueryClient();
  const propertyQuery = useProperty(id);
  const tenantsQuery = useTenants(id);

  const [tenantModalOpen, setTenantModalOpen] = useState(false);
  const [editingTenant, setEditingTenant] = useState<Tenant | null>(null);

  const property = propertyQuery.data;

  const revokeMutation = useMutation({
    mutationFn: () => revokeTrust(id as string, "property"),
    onSuccess: (result) =>
      Alert.alert("Stoop", revokeResultNotice(result.scope, result.revoked_count)),
    onError: (error) =>
      Alert.alert(
        "Stoop",
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      ),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteProperty(id as string),
    onSuccess: () => {
      void reactQueryClient.invalidateQueries({ queryKey: propertiesQueryKey });
      router.back();
    },
    onError: (error) =>
      Alert.alert(
        "Stoop",
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      ),
  });

  function confirmRevoke() {
    const copy = revokeConfirmation("property");
    Alert.alert(copy.title, copy.message, [
      { text: "Cancel", style: "cancel" },
      { text: copy.confirmLabel, style: "destructive", onPress: () => revokeMutation.mutate() },
    ]);
  }

  function confirmDelete() {
    Alert.alert(DELETE_PROPERTY_TITLE, DELETE_PROPERTY_MESSAGE, [
      { text: "Cancel", style: "cancel" },
      {
        text: DELETE_PROPERTY_CONFIRM_LABEL,
        style: "destructive",
        onPress: () => deleteMutation.mutate(),
      },
    ]);
  }

  const tenants = tenantsQuery.data?.items.filter((tenant) => tenant.active) ?? [];

  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <View style={styles.header}>
        <Pressable
          accessibilityRole="button"
          onPress={() => router.back()}
          style={styles.backButton}
          hitSlop={8}
        >
          <Ionicons name="chevron-back" size={16} color={colors.inkDim} />
          <Text style={styles.backLabel}>Properties</Text>
        </Pressable>
        <Text style={styles.title} numberOfLines={1}>
          {property?.label ?? "Property"}
        </Text>
        {property ? (
          <Text style={styles.subtitle} numberOfLines={1}>
            {property.address_line1}, {property.city}
            {property.province ? `, ${property.province}` : ""}
            {property.postal_code ? ` ${property.postal_code}` : ""}
          </Text>
        ) : null}
      </View>

      {propertyQuery.isLoading ? (
        <View style={styles.centered}>
          <ActivityIndicator color={colors.brand} />
        </View>
      ) : propertyQuery.isError || !property ? (
        <View style={styles.centered}>
          <Text style={styles.errorText}>
            {propertyQuery.error instanceof ApiError
              ? toHouseApiError(propertyQuery.error)
              : "Couldn't load this property. Try again."}
          </Text>
          <Button
            label="Try again"
            onPress={() => void propertyQuery.refetch()}
            testID="property-retry"
          />
        </View>
      ) : (
        <ScrollView contentContainerStyle={styles.scrollContent}>
          <StoopNumberCard number={property.twilio_number} />

          {property.open_case_count > 0 ? (
            <Text style={styles.openCases}>
              {property.open_case_count === 1
                ? "1 open case at this property."
                : `${property.open_case_count} open cases at this property.`}
            </Text>
          ) : null}

          <View style={styles.section}>
            <Text style={styles.sectionTitle}>Tenants</Text>
            {tenantsQuery.isLoading ? (
              <ActivityIndicator color={colors.brand} style={styles.tenantsSpinner} />
            ) : tenantsQuery.isError ? (
              <Text style={styles.sectionBody}>
                {tenantsQuery.error instanceof ApiError
                  ? toHouseApiError(tenantsQuery.error)
                  : "Couldn't load the tenants here. Pull to refresh."}
              </Text>
            ) : tenants.length === 0 ? (
              <Text style={styles.sectionBody}>
                No tenants on file yet. Add them so Stoop knows who&rsquo;s texting in.
              </Text>
            ) : (
              tenants.map((tenant) => {
                const vulnerable = vulnerableLabel(tenant);
                return (
                  <Pressable
                    key={tenant.id}
                    accessibilityRole="button"
                    onPress={() => {
                      setEditingTenant(tenant);
                      setTenantModalOpen(true);
                    }}
                    style={({ pressed }) => [styles.tenantRow, pressed && styles.pressed]}
                    testID={`tenant-row-${tenant.id}`}
                  >
                    <View style={styles.tenantText}>
                      <Text style={styles.tenantName}>
                        {firstName(tenant.name)}
                        {tenant.unit ? ` — Unit ${tenant.unit}` : ""}
                      </Text>
                      {vulnerable ? (
                        <Text style={styles.tenantVulnerable}>{vulnerable}</Text>
                      ) : null}
                    </View>
                    <Ionicons name="pencil-outline" size={15} color={colors.inkDim} />
                  </Pressable>
                );
              })
            )}
            <Button
              label="Add a tenant"
              variant="ghost"
              onPress={() => {
                setEditingTenant(null);
                setTenantModalOpen(true);
              }}
              testID="add-tenant"
            />
          </View>

          <View style={styles.section}>
            <Text style={styles.sectionTitle}>{TRUST_SECTION_TITLE}</Text>
            <Text style={styles.sectionBody}>{TRUST_SECTION_BODY}</Text>
            <Button
              label={revokeMutation.isPending ? "Turning off…" : "Turn off automatic sending here"}
              variant="ghost"
              disabled={revokeMutation.isPending}
              onPress={confirmRevoke}
              testID="revoke-trust"
            />
          </View>

          <View style={styles.dangerSection}>
            <Pressable
              accessibilityRole="button"
              onPress={confirmDelete}
              disabled={deleteMutation.isPending}
              style={styles.deleteButton}
              testID="delete-property"
            >
              <Text style={styles.deleteLabel}>
                {deleteMutation.isPending ? "Deleting…" : "Delete this property"}
              </Text>
            </Pressable>
            <Text style={styles.deleteNote}>
              Deleting is permanent. Its number is released after a 24-hour hold, and open cases or
              saved history will block the delete.
            </Text>
          </View>
        </ScrollView>
      )}

      <TenantFormModal
        visible={tenantModalOpen}
        propertyId={id ?? ""}
        tenant={editingTenant}
        onClose={() => setTenantModalOpen(false)}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  header: {
    borderBottomWidth: 1,
    borderBottomColor: colors.line,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
    paddingBottom: spacing.md,
  },
  backButton: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    minHeight: 40,
  },
  backLabel: {
    ...type.button,
    fontSize: 13,
    color: colors.inkDim,
  },
  title: {
    ...type.cardTitle,
    fontSize: 19,
    color: colors.ink,
    marginTop: spacing.sm,
  },
  subtitle: {
    ...type.meta,
    fontWeight: "500",
    color: colors.inkDim,
    marginTop: 2,
  },
  centered: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.base,
    paddingHorizontal: spacing.xl,
  },
  errorText: {
    textAlign: "center",
    color: colors.inkDim,
  },
  scrollContent: {
    padding: spacing.lg,
    paddingBottom: spacing.xxl,
    gap: spacing.lg,
  },
  openCases: {
    ...type.meta,
    color: colors.wait,
  },
  section: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    borderRadius: radius.lg,
    padding: spacing.lg,
    gap: spacing.md,
  },
  sectionTitle: {
    ...type.cardTitle,
    color: colors.ink,
  },
  sectionBody: {
    ...type.footnote,
    fontSize: 13.5,
    lineHeight: 20,
    color: colors.inkDim,
  },
  tenantsSpinner: {
    alignSelf: "flex-start",
  },
  tenantRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.md,
    minHeight: 44,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.panel,
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
  tenantVulnerable: {
    ...type.footnote,
    fontSize: 12,
    color: colors.inkDim,
  },
  dangerSection: {
    marginTop: spacing.sm,
    gap: spacing.sm,
  },
  deleteButton: {
    minHeight: 44,
    justifyContent: "center",
  },
  deleteLabel: {
    ...type.button,
    color: colors.emergency,
  },
  deleteNote: {
    ...type.footnote,
    fontSize: 12,
    color: colors.inkDim,
  },
});
