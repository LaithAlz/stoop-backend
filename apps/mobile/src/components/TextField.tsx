/**
 * A labeled text input in Clarity's material — bordered panel surface,
 * 48px+ touch target (mockup "Do" list, line 452). No direct mockup
 * exhibit for form fields (07 doesn't show a sign-in screen), so this
 * reuses the plaque/card border language (colors.lineStrong, radius.md)
 * rather than inventing a new one.
 */
import { StyleSheet, Text, TextInput, View, type TextInputProps } from "react-native";
import { colors, radius, spacing, touchTarget, type } from "@/theme/tokens";

interface TextFieldProps extends TextInputProps {
  label: string;
}

export function TextField({ label, style, ...inputProps }: TextFieldProps) {
  return (
    <View style={styles.container}>
      <Text style={styles.label}>{label}</Text>
      <TextInput
        style={[styles.input, style]}
        placeholderTextColor={colors.inkDim}
        {...inputProps}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    gap: spacing.xs,
  },
  label: {
    ...type.meta,
    color: colors.inkDim,
  },
  input: {
    ...type.body,
    minHeight: touchTarget,
    borderWidth: 1.5,
    borderColor: colors.lineStrong,
    borderRadius: radius.md,
    paddingHorizontal: spacing.md,
    color: colors.ink,
    backgroundColor: colors.panel,
  },
});
