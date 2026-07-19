/**
 * The one create-property form — shared by Properties → Add
 * (src/app/(tabs)/properties/add.tsx) and the onboarding wizard's property
 * step, so the provisioning flow and its failure handling exist exactly
 * once. Drives the REAL `POST /v1/properties` (api-contracts.md v1.12):
 * creating a property purchases and wires its own Twilio number, so the
 * submit copy says what's actually happening ("Setting up its number…"),
 * and every documented failure lands as its honest house line
 * (src/api/errors.ts):
 *
 * - 409 `property_limit_reached` — the account hit its cap; nothing added.
 * - 409 `duplicate_property`   — same address already exists; the line
 *                                 points at the Properties list.
 * - 503 `no_numbers_available` — no property row was created; the line
 *                                 offers the two real remedies (different
 *                                 area code / retry).
 * - 502 `provisioning_failed`  — nothing half-saved (the server releases
 *                                 any purchased number as compensation);
 *                                 safe to just try again.
 */
import { useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createProperty, propertiesQueryKey } from "@/api/properties";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { CreatePropertyInput, Property } from "@/api/types";
import { Button } from "@/components/Button";
import { TextField } from "@/components/TextField";
import { colors, spacing, type } from "@/theme/tokens";

interface PropertyFormProps {
  submitLabel: string;
  onCreated: (property: Property) => void;
}

interface FieldErrors {
  label?: string;
  addressLine1?: string;
  city?: string;
  areaCode?: string;
}

function validate(fields: {
  label: string;
  addressLine1: string;
  city: string;
  areaCode: string;
}): FieldErrors {
  const errors: FieldErrors = {};
  if (!fields.label.trim()) errors.label = "Give it a short nickname.";
  if (!fields.addressLine1.trim()) errors.addressLine1 = "Add the street address.";
  if (!fields.city.trim()) errors.city = "Add the city.";
  const areaDigits = fields.areaCode.replace(/\D/g, "");
  if (fields.areaCode.trim() && areaDigits.length !== 3) {
    errors.areaCode = "An area code is 3 digits.";
  }
  return errors;
}

export function PropertyForm({ submitLabel, onCreated }: PropertyFormProps) {
  const reactQueryClient = useQueryClient();
  const [label, setLabel] = useState("");
  const [addressLine1, setAddressLine1] = useState("");
  const [city, setCity] = useState("");
  const [province, setProvince] = useState("ON");
  const [postalCode, setPostalCode] = useState("");
  const [areaCode, setAreaCode] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (input: CreatePropertyInput) => createProperty(input),
    onSuccess: (property) => {
      // Both the tab list and the onboarding gate key off this root.
      void reactQueryClient.invalidateQueries({ queryKey: propertiesQueryKey });
      onCreated(property);
    },
    onError: (error) => {
      setServerError(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      );
    },
  });

  const fieldErrors = validate({ label, addressLine1, city, areaCode });
  const valid = Object.keys(fieldErrors).length === 0;

  function handleSubmit() {
    setSubmitted(true);
    setServerError(null);
    if (!valid || mutation.isPending) return;
    const input: CreatePropertyInput = {
      label: label.trim(),
      address_line1: addressLine1.trim(),
      city: city.trim(),
    };
    if (province.trim()) input.province = province.trim().toUpperCase();
    if (postalCode.trim()) input.postal_code = postalCode.trim();
    const areaDigits = areaCode.replace(/\D/g, "");
    if (areaDigits.length === 3) input.area_code = areaDigits;
    mutation.mutate(input);
  }

  return (
    <View style={styles.form}>
      <View>
        <TextField
          label="Property nickname"
          value={label}
          onChangeText={setLabel}
          placeholder="The Palmerston Duplex"
          testID="property-label"
        />
        {submitted && fieldErrors.label ? (
          <Text style={styles.fieldError}>{fieldErrors.label}</Text>
        ) : (
          <Text style={styles.helper}>What you&rsquo;ll see in your queue.</Text>
        )}
      </View>

      <View>
        <TextField
          label="Street address"
          value={addressLine1}
          onChangeText={setAddressLine1}
          placeholder="41 Palmerston Ave"
          autoComplete="street-address"
          testID="property-address"
        />
        {submitted && fieldErrors.addressLine1 ? (
          <Text style={styles.fieldError}>{fieldErrors.addressLine1}</Text>
        ) : null}
      </View>

      <View style={styles.rowFields}>
        <View style={styles.cityField}>
          <TextField
            label="City"
            value={city}
            onChangeText={setCity}
            placeholder="Toronto"
            testID="property-city"
          />
          {submitted && fieldErrors.city ? (
            <Text style={styles.fieldError}>{fieldErrors.city}</Text>
          ) : null}
        </View>
        <View style={styles.provinceField}>
          <TextField
            label="Province"
            value={province}
            onChangeText={(text) => setProvince(text.toUpperCase().slice(0, 2))}
            autoCapitalize="characters"
            testID="property-province"
          />
        </View>
      </View>

      <TextField
        label="Postal code (optional)"
        value={postalCode}
        onChangeText={setPostalCode}
        placeholder="M6G 2K2"
        autoCapitalize="characters"
        testID="property-postal"
      />

      <View>
        <TextField
          label="Area code for its number (optional)"
          value={areaCode}
          onChangeText={setAreaCode}
          placeholder="416"
          keyboardType="number-pad"
          maxLength={3}
          testID="property-area-code"
        />
        {submitted && fieldErrors.areaCode ? (
          <Text style={styles.fieldError}>{fieldErrors.areaCode}</Text>
        ) : (
          <Text style={styles.helper}>
            This property gets its own number for tenants to text. We&rsquo;ll look for one in this
            area code first.
          </Text>
        )}
      </View>

      {serverError ? (
        <Text style={styles.serverError} testID="property-form-error">
          {serverError}
        </Text>
      ) : null}

      <Button
        label={mutation.isPending ? "Setting up its number…" : submitLabel}
        variant="primary"
        disabled={mutation.isPending || (submitted && !valid)}
        onPress={handleSubmit}
        testID="property-submit"
      />
    </View>
  );
}

const styles = StyleSheet.create({
  form: {
    gap: spacing.base,
  },
  rowFields: {
    flexDirection: "row",
    gap: spacing.md,
  },
  cityField: {
    flex: 1,
  },
  provinceField: {
    width: 88,
  },
  helper: {
    ...type.footnote,
    color: colors.inkDim,
    marginTop: spacing.xs,
  },
  fieldError: {
    ...type.footnote,
    color: colors.emergency,
    marginTop: spacing.xs,
  },
  serverError: {
    ...type.footnote,
    color: colors.emergency,
  },
});
