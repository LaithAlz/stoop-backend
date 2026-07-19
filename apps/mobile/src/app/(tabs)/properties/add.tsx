/**
 * Add a property — the real provisioning flow (`POST /v1/properties`,
 * api-contracts.md v1.12) via the shared PropertyForm, which owns the
 * documented failure paths (cap, duplicate, no numbers, provisioning
 * failure). On success this navigates straight to the new property's
 * detail so the landlord sees the number that was just set up.
 */
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { colors, spacing, type } from "@/theme/tokens";
import { PropertyForm } from "@/features/properties/PropertyForm";

export default function AddPropertyScreen() {
  const router = useRouter();

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
        <Text style={styles.title}>Add a property</Text>
        <Text style={styles.subtitle}>It gets its own phone number for tenants to text.</Text>
      </View>

      <ScrollView contentContainerStyle={styles.scrollContent} keyboardShouldPersistTaps="handled">
        <PropertyForm
          submitLabel="Add property"
          onCreated={(property) =>
            router.replace({ pathname: "/properties/[id]", params: { id: property.id } })
          }
        />
      </ScrollView>
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
  scrollContent: {
    padding: spacing.lg,
    paddingBottom: spacing.xxl,
  },
});
