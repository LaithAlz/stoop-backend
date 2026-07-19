/**
 * Properties gets its own nested stack (list → detail → add) so pushing a
 * property keeps the tab bar in place — same shape as the conversations
 * stack (src/app/(tabs)/conversations/_layout.tsx).
 */
import { Stack } from "expo-router";
import { colors } from "@/theme/tokens";

export default function PropertiesStackLayout() {
  return (
    <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: colors.bg } }}>
      <Stack.Screen name="index" />
      <Stack.Screen name="[id]" />
      <Stack.Screen name="add" />
    </Stack>
  );
}
