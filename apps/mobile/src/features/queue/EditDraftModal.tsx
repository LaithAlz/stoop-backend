/**
 * The edit-and-send modal editor (issue #210 M1 scope item 2) — prefilled
 * with the current draft body, `POST /v1/drafts/{id}/edit-and-send` on
 * Send. Reuses the M0 `TextField`/`Button` components rather than
 * inventing new form controls.
 *
 * The inner content is remounted (via `key={initialBody}`) rather than
 * reset through a `useEffect` — React's own guidance for "state that should
 * reset when a prop changes" is a fresh component instance, not an effect
 * that calls `setState` synchronously (which eslint-plugin-react-hooks'
 * `set-state-in-effect` rule flags for exactly the cascading-render reason
 * described there).
 */
import { useState } from "react";
import { KeyboardAvoidingView, Modal, Platform, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { colors, spacing, type } from "@/theme/tokens";
import { Button } from "@/components/Button";
import { TextField } from "@/components/TextField";

interface EditDraftModalProps {
  visible: boolean;
  tenantFirstName: string;
  initialBody: string;
  submitting: boolean;
  onCancel: () => void;
  onSend: (body: string) => void;
}

export function EditDraftModal({
  visible,
  tenantFirstName,
  initialBody,
  submitting,
  onCancel,
  onSend,
}: EditDraftModalProps) {
  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onCancel} transparent={false}>
      <EditDraftModalContent
        key={initialBody}
        tenantFirstName={tenantFirstName}
        initialBody={initialBody}
        submitting={submitting}
        onCancel={onCancel}
        onSend={onSend}
      />
    </Modal>
  );
}

function EditDraftModalContent({
  tenantFirstName,
  initialBody,
  submitting,
  onCancel,
  onSend,
}: Omit<EditDraftModalProps, "visible">) {
  const [body, setBody] = useState(initialBody);
  const canSend = body.trim().length > 0 && !submitting;

  return (
    <SafeAreaView style={styles.safeArea} edges={["top", "bottom"]}>
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <View style={styles.header}>
          <Text style={styles.heading}>Editing your reply</Text>
          <Text style={styles.subheading}>to {tenantFirstName}</Text>
        </View>

        <View style={styles.body}>
          <TextField
            label="Your reply"
            value={body}
            onChangeText={setBody}
            multiline
            style={styles.input}
            textAlignVertical="top"
            testID="edit-draft-input"
          />
        </View>

        <View style={styles.actions}>
          <View style={styles.cancelWrap}>
            <Button label="Cancel" variant="ghost" onPress={onCancel} testID="edit-draft-cancel" />
          </View>
          <View style={styles.sendWrap}>
            <Button
              label={submitting ? "Sending…" : "Send edited version"}
              variant="primary"
              disabled={!canSend}
              onPress={() => onSend(body.trim())}
              testID="edit-draft-send"
            />
          </View>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  header: {
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
    paddingBottom: spacing.sm,
    borderBottomWidth: 1,
    borderBottomColor: colors.line,
  },
  heading: {
    ...type.cardTitle,
    fontSize: 19,
    color: colors.ink,
  },
  subheading: {
    ...type.meta,
    color: colors.inkDim,
    marginTop: 2,
  },
  body: {
    flex: 1,
    padding: spacing.lg,
  },
  input: {
    minHeight: 160,
  },
  actions: {
    flexDirection: "row",
    gap: spacing.sm + 2,
    padding: spacing.lg,
  },
  cancelWrap: {
    width: 110,
  },
  sendWrap: {
    flex: 1,
  },
});
