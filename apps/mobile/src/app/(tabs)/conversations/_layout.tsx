/**
 * Conversations gets its own nested stack so tapping a row (or a case link
 * from Home) pushes the detail screen on top of the list without leaving
 * the tab bar — the standard expo-router "stack inside a tab" shape.
 */
import { Stack } from "expo-router";
import { colors } from "@/theme/tokens";

export default function ConversationsStackLayout() {
  return (
    <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: colors.bg } }}>
      <Stack.Screen name="index" />
      <Stack.Screen name="[id]" />
    </Stack>
  );
}
