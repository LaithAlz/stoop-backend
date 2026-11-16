/**
 * Me — account & settings (issue #210 M2): the real `GET /v1/me` display,
 * profile editing via `PATCH /v1/me` (ProfileEditModal — name + the
 * emergency-call phone, the documented fields this form edits), the plan
 * display, the GLOBAL trust revoke (every property at once — the
 * portfolio-wide "turn it all off" the trust contract's `scope: "global"`
 * exists for), and sign-out. The revoke card only renders when the
 * landlord has at least one property: with zero there's nothing to revoke,
 * and the global endpoint still needs a property-scoped path
 * (src/api/trust.ts).
 */
import { useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Linking,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useMutation } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthProvider";
import { AppHeader } from "@/components/AppHeader";
import { Button } from "@/components/Button";
import { colors, radius, spacing, type } from "@/theme/tokens";
import { useMe } from "@/api/me";
import { useFirstPropertyPage } from "@/api/properties";
import { revokeTrust } from "@/api/trust";
import { ApiError, toHouseApiError } from "@/api/errors";
import { planDisplayName } from "@/features/account/plan";
import { ProfileEditModal } from "@/features/account/ProfileEditModal";
import { revokeConfirmation, revokeResultNotice } from "@/features/trust/revoke";
import { usePushPermission } from "@/features/push/usePushPermission";
import { pushStatusLine, resolvePushControlAction } from "@/features/push/pushControl";
import {
  PUSH_ENABLE_BUTTON_LABEL,
  PUSH_EXPLAINER,
  PUSH_OPEN_SETTINGS_BUTTON_LABEL,
  PUSH_REQUEST_FAILED_NOTICE,
  PUSH_SECTION_TITLE,
} from "@/features/push/pushCopy";

// B3-5 (#284): shown when `signOut()` comes back `{ ok: false }`, which
// means "NOT CONFIRMED signed out", not "definitely still signed in".
// Three states reach it: the session is genuinely still live (offline
// `/logout`, or a throw before teardown); the state is UNKNOWN (the
// reject-path session read itself threw, or timed out, or came back with
// an error, all of which AuthProvider deliberately collapses to false);
// and one false negative auth-js hands us, where `_signOut` returns a
// `sessionError` after `_callRefreshToken` already removed the session.
//
// Over-warning is the deliberate direction. The state that must never be
// missed is "still signed in on a device you just handed to someone",
// and an earlier version of this comment (and of that code) claimed a
// precision here that neither has. Same
// house-voice shape as the network_error message src/api/client.ts already
// uses elsewhere in this app, reused rather than inventing new copy for the
// same underlying situation.
const SIGN_OUT_FAILED_NOTICE = "Couldn't reach Stoop. Check your connection and try again.";

export default function MeScreen() {
  const { session, signOut } = useAuth();
  const meQuery = useMe();
  const firstPageQuery = useFirstPropertyPage();
  const push = usePushPermission();

  const [editOpen, setEditOpen] = useState(false);

  const firstPropertyId = firstPageQuery.data?.items[0]?.id;

  const pushAction = resolvePushControlAction(push.state);

  async function handlePushButton() {
    if (pushAction === "open-settings") {
      // iOS after a prior denial (or Android after "don't ask again"): an
      // in-app prompt is a guaranteed no-op, so send them to the OS
      // setting instead of a dead button (pushControl.resolvePushControl
      // Action encodes exactly when this applies).
      await Linking.openSettings();
      return;
    }
    const result = await push.requestPermission();
    // 'unsupported' after an explicit tap means the native call couldn't
    // run at all (e.g. a simulator with no push capability) — say so
    // honestly rather than leaving the button looking broken.
    if (result.status === "unsupported") {
      Alert.alert("Stoop", PUSH_REQUEST_FAILED_NOTICE);
    }
  }

  const revokeMutation = useMutation({
    mutationFn: () => revokeTrust(firstPropertyId as string, "global"),
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

  function confirmGlobalRevoke() {
    const copy = revokeConfirmation("global");
    Alert.alert(copy.title, copy.message, [
      { text: "Cancel", style: "cancel" },
      { text: copy.confirmLabel, style: "destructive", onPress: () => revokeMutation.mutate() },
    ]);
  }

  // B3-5 (#284): `signOut()` used to be fire-and-forget here, so an
  // offline "Sign out" (auth-js's `_signOut` skips clearing the local
  // session when its own `/logout` call fails - see AuthProvider.tsx's
  // `signOut` docstring) left the landlord still signed in with no signal
  // that anything went wrong. Silent on success (matches every other
  // action on this screen); only speaks up on the honest failure.
  //
  // FIX 1 (#284 adversarial review): `try`/`catch` here is defense in
  // depth, not the primary fix - AuthProvider.signOut already wraps
  // supabase.auth.signOut()'s own reject path (an unguarded SecureStore
  // read/write in auth-js) and resolves `{ ok: false }` instead of
  // throwing. But this button's `onPress={() => void handleSignOut()}`
  // discards whatever this function returns, so if `signOut()` were ever to
  // throw anyway - a future auth-js change, a bug in the wrapper above -
  // this is the last place standing between that and the exact "tap Sign
  // out, see nothing happen, still fully signed in" failure this finding
  // exists to close. Same house-voice notice as the honest `{ ok: false }`
  // case, since from the landlord's side they're the same failure.
  async function handleSignOut() {
    try {
      const result = await signOut();
      if (!result.ok) {
        Alert.alert("Stoop", SIGN_OUT_FAILED_NOTICE);
      }
    } catch {
      Alert.alert("Stoop", SIGN_OUT_FAILED_NOTICE);
    }
  }

  return (
    <SafeAreaView style={styles.safeArea} edges={["top"]}>
      <AppHeader title="Me" />
      <ScrollView contentContainerStyle={styles.body}>
        <View style={styles.card}>
          <Text style={styles.label}>Signed in as</Text>
          <Text style={styles.email}>
            {meQuery.data?.full_name || session?.user.email || "Unknown account"}
          </Text>
          {meQuery.data?.full_name ? (
            <Text style={styles.subtext}>{meQuery.data.email}</Text>
          ) : null}
          <View style={styles.cardAction}>
            <Button
              label="Edit name & phone"
              variant="ghost"
              onPress={() => setEditOpen(true)}
              testID="edit-profile"
            />
          </View>
        </View>

        <View style={styles.card}>
          <Text style={styles.label}>Plan</Text>
          {meQuery.isSuccess ? (
            <Text style={styles.email}>
              {planDisplayName(meQuery.data.subscription_tier, meQuery.data.price_cohort)}
            </Text>
          ) : meQuery.isError ? (
            <Text style={styles.subtext}>
              {meQuery.error instanceof ApiError
                ? toHouseApiError(meQuery.error)
                : "Couldn't load your plan right now."}
            </Text>
          ) : (
            <ActivityIndicator color={colors.brand} style={styles.planSpinner} />
          )}
        </View>

        <View style={styles.card}>
          <Text style={styles.label}>{PUSH_SECTION_TITLE}</Text>
          {push.loading ? (
            <ActivityIndicator color={colors.brand} style={styles.planSpinner} />
          ) : (
            <Text style={styles.pushStatus} testID="push-status">
              {pushStatusLine(push.state)}
            </Text>
          )}
          <Text style={styles.cardBody}>{PUSH_EXPLAINER}</Text>
          {pushAction !== "none" ? (
            <View style={styles.cardAction}>
              <Button
                label={
                  pushAction === "open-settings"
                    ? PUSH_OPEN_SETTINGS_BUTTON_LABEL
                    : PUSH_ENABLE_BUTTON_LABEL
                }
                variant="ghost"
                disabled={push.requesting}
                onPress={() => void handlePushButton()}
                testID="push-permission-action"
              />
            </View>
          ) : null}
        </View>

        {firstPropertyId ? (
          <View style={styles.card}>
            <Text style={styles.label}>Automatic sending</Text>
            <Text style={styles.cardBody}>
              At properties where Stoop has earned it, routine replies can go out without waiting.
              One tap here turns that off everywhere. Every reply comes back to you.
            </Text>
            <View style={styles.cardAction}>
              <Button
                label={
                  revokeMutation.isPending
                    ? "Turning off…"
                    : "Turn off automatic sending everywhere"
                }
                variant="ghost"
                disabled={revokeMutation.isPending}
                onPress={confirmGlobalRevoke}
                testID="revoke-trust-global"
              />
            </View>
          </View>
        ) : null}

        <Button
          label="Sign out"
          variant="ghost"
          onPress={() => void handleSignOut()}
          testID="sign-out"
        />
      </ScrollView>

      <ProfileEditModal
        visible={editOpen}
        currentName={meQuery.data?.full_name ?? null}
        onClose={() => setEditOpen(false)}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  body: {
    padding: spacing.lg, // .app-main padding 16-18px, mockup line 169
    paddingBottom: spacing.xxl,
    gap: spacing.base,
  },
  card: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    borderRadius: radius.lg,
    padding: spacing.lg, // .entry padding: 18px, mockup line 209
    gap: spacing.xs,
  },
  label: {
    ...type.meta,
    color: colors.inkDim,
  },
  email: {
    ...type.cardTitle,
    color: colors.ink,
  },
  subtext: {
    ...type.footnote,
    color: colors.inkDim,
  },
  cardBody: {
    ...type.footnote,
    fontSize: 13,
    lineHeight: 19,
    color: colors.inkDim,
  },
  pushStatus: {
    ...type.cardTitle,
    fontSize: 15,
    color: colors.ink,
  },
  cardAction: {
    marginTop: spacing.sm,
  },
  planSpinner: {
    alignSelf: "flex-start",
    marginTop: spacing.xs,
  },
});
