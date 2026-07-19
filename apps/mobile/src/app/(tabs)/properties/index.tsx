/**
 * Properties — the real list from `GET /v1/properties` (issue #210 M2),
 * cursor-paginated per api-contracts.md's Conventions section. Each row
 * leads with the property's own Stoop number (the number tenants text —
 * the most load-bearing fact about a property) and is honest when a
 * property has none. The empty state is the standing onboarding entry:
 * "Add your first property" starts the same wizard the zero-properties
 * gate offers after sign-in.
 */
import { useMemo } from "react";
import { ActivityIndicator, FlatList, Pressable, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { AppHeader } from "@/components/AppHeader";
import { Button } from "@/components/Button";
import { EmptyState } from "@/components/EmptyState";
import { colors, radius, spacing, type } from "@/theme/tokens";
import { usePropertiesList } from "@/api/properties";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { Property } from "@/api/types";
import { formatStoopNumber } from "@/features/properties/stoopNumber";

function PropertyRow({ property, onPress }: { property: Property; onPress: () => void }) {
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.row, pressed && styles.pressed]}
    >
      <View style={styles.rowText}>
        <Text style={styles.rowTitle} numberOfLines={1}>
          {property.label}
        </Text>
        <Text style={styles.rowAddress} numberOfLines={1}>
          {property.address_line1}, {property.city}
        </Text>
        <Text style={property.twilio_number ? styles.rowNumber : styles.rowNoNumber}>
          {property.twilio_number
            ? formatStoopNumber(property.twilio_number)
            : "No Stoop number yet"}
        </Text>
        {property.open_case_count > 0 ? (
          <Text style={styles.rowCases}>
            {property.open_case_count === 1
              ? "1 open case"
              : `${property.open_case_count} open cases`}
          </Text>
        ) : null}
      </View>
      <Ionicons name="chevron-forward" size={16} color={colors.inkDim} />
    </Pressable>
  );
}

export default function PropertiesScreen() {
  const router = useRouter();
  const propertiesQuery = usePropertiesList();

  const items = useMemo<Property[]>(
    () => propertiesQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [propertiesQuery.data],
  );

  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <AppHeader title="Properties" />

      {propertiesQuery.isLoading ? (
        <View style={styles.centered}>
          <ActivityIndicator color={colors.brand} />
        </View>
      ) : propertiesQuery.isError ? (
        <View style={styles.centered}>
          <Text style={styles.errorText}>
            {propertiesQuery.error instanceof ApiError
              ? toHouseApiError(propertiesQuery.error)
              : "Couldn't load your properties. Try again."}
          </Text>
          <Button
            label="Try again"
            onPress={() => void propertiesQuery.refetch()}
            testID="properties-retry"
          />
        </View>
      ) : (
        <FlatList
          data={items}
          keyExtractor={(property) => property.id}
          renderItem={({ item }) => (
            <PropertyRow
              property={item}
              onPress={() => router.push({ pathname: "/properties/[id]", params: { id: item.id } })}
            />
          )}
          contentContainerStyle={styles.listContent}
          refreshing={propertiesQuery.isRefetching && !propertiesQuery.isFetchingNextPage}
          onRefresh={() => void propertiesQuery.refetch()}
          onEndReachedThreshold={0.4}
          onEndReached={() => {
            if (propertiesQuery.hasNextPage && !propertiesQuery.isFetchingNextPage) {
              void propertiesQuery.fetchNextPage();
            }
          }}
          ListHeaderComponent={
            items.length > 0 ? (
              <View style={styles.addButtonWrap}>
                <Button
                  label="Add a property"
                  variant="ghost"
                  onPress={() => router.push("/properties/add")}
                  testID="add-property"
                />
              </View>
            ) : null
          }
          ListFooterComponent={
            propertiesQuery.isFetchingNextPage ? (
              <ActivityIndicator style={styles.footerSpinner} color={colors.brand} />
            ) : null
          }
          ListEmptyComponent={
            <View>
              <EmptyState
                icon="business-outline"
                title="No properties yet."
                message="Each property you add gets its own phone number for tenants to text. They'll all show up here."
              />
              <View style={styles.emptyAction}>
                <Button
                  label="Add your first property"
                  variant="primary"
                  onPress={() => router.push("/onboarding")}
                  testID="add-first-property"
                />
              </View>
            </View>
          }
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  listContent: {
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.base,
    paddingBottom: spacing.xxl,
    flexGrow: 1,
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
  addButtonWrap: {
    marginBottom: spacing.base,
  },
  footerSpinner: {
    marginVertical: spacing.base,
  },
  emptyAction: {
    paddingHorizontal: spacing.xl,
    marginTop: spacing.sm,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.surface,
    padding: spacing.base,
    marginBottom: spacing.sm + 4,
  },
  pressed: {
    opacity: 0.85,
  },
  rowText: {
    flex: 1,
    minWidth: 0,
    gap: 2,
  },
  rowTitle: {
    ...type.cardTitle,
    color: colors.ink,
  },
  rowAddress: {
    ...type.footnote,
    color: colors.inkDim,
  },
  rowNumber: {
    ...type.stamp,
    fontSize: 12.5,
    color: colors.brand,
    marginTop: 2,
  },
  rowNoNumber: {
    ...type.footnote,
    fontStyle: "italic",
    color: colors.inkDim,
    marginTop: 2,
  },
  rowCases: {
    ...type.meta,
    color: colors.wait,
    marginTop: 2,
  },
});
