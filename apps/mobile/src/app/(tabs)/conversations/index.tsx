/**
 * Conversations — every case, cursor-paginated per docs/03-engineering/
 * api-contracts.md's "Conventions" section (`GET /v1/cases`). Ports the
 * shape of apps/web/src/components/clarity/ConversationRow.tsx.
 */
import { useMemo } from "react";
import { ActivityIndicator, FlatList, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { AppHeader } from "@/components/AppHeader";
import { EmptyState } from "@/components/EmptyState";
import { Button } from "@/components/Button";
import { ConversationRow } from "@/components/clarity/ConversationRow";
import { colors, spacing } from "@/theme/tokens";
import { useCasesList } from "@/api/cases";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { CaseSummary } from "@/api/types";

export default function ConversationsScreen() {
  const router = useRouter();
  const casesQuery = useCasesList();

  const items = useMemo<CaseSummary[]>(
    () => casesQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [casesQuery.data],
  );

  function openCase(id: string) {
    router.push({ pathname: "/conversations/[id]", params: { id } });
  }

  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <AppHeader title="Conversations" />

      {casesQuery.isLoading ? (
        <View style={styles.centered}>
          <ActivityIndicator color={colors.brand} />
        </View>
      ) : casesQuery.isError ? (
        <View style={styles.centered}>
          <Text style={styles.errorText}>
            {casesQuery.error instanceof ApiError
              ? toHouseApiError(casesQuery.error)
              : "Couldn't load your conversations. Try again."}
          </Text>
          <Button
            label="Try again"
            onPress={() => void casesQuery.refetch()}
            testID="cases-retry"
          />
        </View>
      ) : (
        <FlatList
          data={items}
          keyExtractor={(item) => item.id}
          renderItem={({ item }) => (
            <ConversationRow item={item} onPress={() => openCase(item.id)} />
          )}
          contentContainerStyle={styles.listContent}
          refreshing={casesQuery.isRefetching && !casesQuery.isFetchingNextPage}
          onRefresh={() => void casesQuery.refetch()}
          onEndReachedThreshold={0.4}
          onEndReached={() => {
            if (casesQuery.hasNextPage && !casesQuery.isFetchingNextPage) {
              void casesQuery.fetchNextPage();
            }
          }}
          ListFooterComponent={
            casesQuery.isFetchingNextPage ? (
              <ActivityIndicator style={styles.footerSpinner} color={colors.brand} />
            ) : null
          }
          ListEmptyComponent={
            <EmptyState
              icon="chatbubbles-outline"
              title="No conversations yet."
              message="Every text between your tenants and Stoop will be saved here, with dates and times — nothing edited, nothing lost."
            />
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
  footerSpinner: {
    marginVertical: spacing.base,
  },
});
