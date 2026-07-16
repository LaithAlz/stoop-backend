/**
 * The founder-adopted IA (issue #210): Home / Conversations / Properties /
 * Me — four tabs, no more ("Do" list, mockup line 456). Styled from the
 * mockup's `.tabbar`/`.tab` rules (lines 178-189) using expo-router's
 * classic Tabs (not expo-router/unstable-native-tabs) so colors, type, and
 * badge shape stay under our control instead of the OS tab-bar chrome's.
 *
 * This whole group is gated by the root layout's Stack.Protected guard
 * (src/app/_layout.tsx) — it's never rendered for a signed-out user.
 */
import { Tabs } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { colors, type } from "@/theme/tokens";

export default function TabsLayout() {
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.brand,
        tabBarInactiveTintColor: colors.inkDim,
        tabBarStyle: {
          backgroundColor: colors.bg,
          borderTopColor: colors.line,
          borderTopWidth: 1,
        },
        tabBarLabelStyle: type.tabLabel,
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "Home",
          tabBarIcon: ({ color, size }) => (
            <Ionicons name="home-outline" color={color} size={size} />
          ),
        }}
      />
      <Tabs.Screen
        name="conversations"
        options={{
          title: "Conversations",
          tabBarIcon: ({ color, size }) => (
            <Ionicons name="chatbubbles-outline" color={color} size={size} />
          ),
        }}
      />
      <Tabs.Screen
        name="properties"
        options={{
          title: "Properties",
          tabBarIcon: ({ color, size }) => (
            <Ionicons name="business-outline" color={color} size={size} />
          ),
        }}
      />
      <Tabs.Screen
        name="me"
        options={{
          title: "Me",
          tabBarIcon: ({ color, size }) => (
            <Ionicons name="person-outline" color={color} size={size} />
          ),
        }}
      />
    </Tabs>
  );
}
