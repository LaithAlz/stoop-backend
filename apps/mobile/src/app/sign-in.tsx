/**
 * Sign-in — email+password against Supabase Auth (src/lib/supabase.ts),
 * matching the auth model docs/03-engineering/api-contracts.md describes
 * for the API (`Authorization: Bearer <supabase JWT>`). apps/web's own
 * /sign-in route (apps/web/src/routes/sign-in.tsx) is a non-functional
 * mock today — magic-link/social buttons that just call `alert(...)` —
 * so there's nothing real to mirror there; this implements the real thing.
 *
 * Rendered only when signed out — see src/app/_layout.tsx's Stack.Protected
 * guard, driven by src/auth/resolveAuthRoute.ts.
 */
import { useState } from "react";
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useAuth } from "@/auth/AuthProvider";
import { Button } from "@/components/Button";
import { TextField } from "@/components/TextField";
import { colors, radius, spacing, type } from "@/theme/tokens";

export default function SignInScreen() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = email.trim().length > 0 && password.length > 0 && !submitting;

  async function handleSubmit() {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    const { error: signInError } = await signIn(email.trim(), password);
    setSubmitting(false);
    if (signInError) {
      setError(signInError);
    }
    // On success the root layout's auth gate swaps to the tab shell —
    // nothing to navigate to manually here.
  }

  return (
    <SafeAreaView style={styles.safeArea} edges={["top", "bottom"]}>
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <ScrollView
          contentContainerStyle={styles.scrollContent}
          keyboardShouldPersistTaps="handled"
        >
          <Text style={styles.wordmark}>
            Stoop<Text style={styles.wordmarkDot}>.</Text>
          </Text>

          <View style={styles.card}>
            <Text style={styles.heading}>Welcome back.</Text>
            <Text style={styles.subheading}>
              Sign in to sort your queue, edit drafts, and check on your properties.
            </Text>

            <View style={styles.form}>
              <TextField
                label="Email"
                value={email}
                onChangeText={setEmail}
                autoCapitalize="none"
                autoComplete="email"
                keyboardType="email-address"
                textContentType="emailAddress"
                testID="sign-in-email"
              />
              <TextField
                label="Password"
                value={password}
                onChangeText={setPassword}
                secureTextEntry
                autoCapitalize="none"
                autoComplete="password"
                textContentType="password"
                testID="sign-in-password"
              />
              {error ? (
                <Text style={styles.error} testID="sign-in-error">
                  {error}
                </Text>
              ) : null}
              <Button
                label={submitting ? "Signing in…" : "Sign in"}
                variant="primary"
                disabled={!canSubmit}
                onPress={handleSubmit}
                testID="sign-in-submit"
              />
            </View>
          </View>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  scrollContent: {
    flexGrow: 1,
    justifyContent: "center",
    padding: spacing.xl,
    gap: spacing.xl,
  },
  wordmark: {
    ...type.wordmark,
    fontSize: 28,
    color: colors.ink,
    textAlign: "center",
  },
  wordmarkDot: {
    color: colors.emergency,
    fontWeight: "900",
  },
  card: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    borderRadius: radius.lg,
    padding: spacing.xl,
    gap: spacing.md,
  },
  heading: {
    ...type.allClearTitle,
    color: colors.ink,
  },
  subheading: {
    ...type.body,
    fontSize: 14.5,
    color: colors.inkDim,
  },
  form: {
    gap: spacing.base,
    marginTop: spacing.sm,
  },
  error: {
    ...type.footnote,
    color: colors.emergency,
  },
});
