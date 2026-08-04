import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { createProperty, propertiesQueryKey } from "@/api/properties";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { CreatePropertyInput } from "@/api/types";

export const Route = createFileRoute("/app/properties_/add")({
  head: () => ({
    meta: [{ title: "Add a property. Stoop." }, { name: "robots", content: "noindex" }],
    links: [{ rel: "canonical", href: "/app/properties/add" }],
  }),
  component: AddPropertyPage,
});

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

/**
 * R1 (safety re-verify, #234 PR 4): every refusal this endpoint documents.
 * Each carries its own house line (src/api/errors.ts) and means, DEFINITELY,
 * that nothing was created — the server runs its compensating number
 * release before answering. Three of them are 5xx-coded
 * (`no_numbers_available` 503, `provisioning_failed` 502,
 * `public_base_url_unconfigured` 500), which is exactly why HTTP status
 * alone can't decide whether a failure was ambiguous here: classifying by
 * status swallowed "try a different area code" — the one instruction that
 * would have worked — and sent the landlord looking for a property that
 * was never created.
 */
const DEFINITE_CREATE_CODES = new Set([
  "no_numbers_available",
  "provisioning_failed",
  "public_base_url_unconfigured",
  "property_limit_reached",
  "duplicate_property",
]);

/**
 * Add a property — the real provisioning flow (`POST /v1/properties`,
 * api-contracts.md v1.12), ported from apps/mobile/src/features/properties/
 * PropertyForm.tsx (campaign issue #234 PR 4) onto plain web form controls.
 * Reached from Properties → Add / "Add your first property" (this PR
 * repoints those links away from `/onboarding`, a deliberately mock,
 * pre-signup demo — see app.properties.tsx's own docstring).
 *
 * Every documented failure surfaces inline via the house-voice map
 * (src/api/errors.ts, already ported in PR 1):
 * - 409 `property_limit_reached` — the account hit its cap; nothing added.
 * - 409 `duplicate_property`   — same address already exists.
 * - 503 `no_numbers_available` — no property row was created; offers the
 *                                 two real remedies (different area code /
 *                                 retry).
 * - 502 `provisioning_failed`  — nothing half-saved (the server releases
 *                                 any purchased number as compensation);
 *                                 safe to just try again.
 *
 * On success this navigates straight to the new property's detail so the
 * landlord sees the number that was just set up.
 */
function AddPropertyPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

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
      // Both the list and any future onboarding gate key off this root.
      void queryClient.invalidateQueries({ queryKey: propertiesQueryKey });
      // L3 (safety review, #234 PR 4): a 2xx whose body somehow carried no
      // id would navigate to `/app/properties/undefined` AFTER a real
      // number purchase — land on the list instead, where the new property
      // actually shows up.
      if (property?.id) {
        void navigate({ to: "/app/properties/$id", params: { id: property.id } });
      } else {
        void navigate({ to: "/app/properties" });
      }
    },
    onError: (error) => {
      // H2 (safety review, #234 PR 4): a status-0/5xx failure here is
      // AMBIGUOUS — this POST buys a real phone number, so "check your
      // connection and try again" can be inviting a retry against a
      // purchase that already succeeded. (The backend dedupes on
      // (landlord, address) and releases the loser's number, so a retry
      // converges to `duplicate_property` rather than double-billing —
      // this is an honesty fix, not a money leak.) Say what's actually
      // known and point at the list, which is the honest read.
      //
      // R1 (safety re-verify): ambiguity is about whether the server TOLD
      // US what happened — not about the status class. Provisioning's own
      // refusals are 5xx-coded (`no_numbers_available` 503,
      // `provisioning_failed` 502, `public_base_url_unconfigured` 500) and
      // are DEFINITE: the server ran its compensating release and knows
      // nothing was saved. Swallowing those cost the landlord the one
      // instruction that works ("try a different area code") and sent them
      // hunting for a property that was never created.
      if (
        error instanceof ApiError &&
        !DEFINITE_CREATE_CODES.has(error.code) &&
        (error.status === 0 || error.status >= 500)
      ) {
        setServerError(
          "That may have gone through. Check your properties list before adding it again.",
        );
        void queryClient.invalidateQueries({ queryKey: propertiesQueryKey });
        return;
      }
      setServerError(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      );
    },
    // Releases L6's latch after every attempt so a genuine retry (a
    // corrected field, say) isn't locked out.
    onSettled: () => {
      submitLatch.current = false;
    },
  });

  const fieldErrors = validate({ label, addressLine1, city, areaCode });
  const valid = Object.keys(fieldErrors).length === 0;

  // L6 (safety review, #234 PR 4): `mutation.isPending` is read from the
  // render closure, so two submits inside one frame (double Enter) both
  // pass the guard and both POST — each buying a number, the second
  // caught by the unique index and released only best-effort. A ref latch
  // is synchronous and can't be raced that way.
  const submitLatch = useRef(false);

  function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitted(true);
    setServerError(null);
    if (!valid || mutation.isPending || submitLatch.current) return;
    submitLatch.current = true;
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
    <PhoneFrame>
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-canvas/95 px-4 py-3 backdrop-blur">
        <Link to="/app/properties" className="flex size-10 items-center justify-center -ml-2">
          <ArrowLeft className="size-5" />
        </Link>
      </header>

      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-5 pb-4 pt-5">
          <h1 className="font-display text-[26px] leading-tight tracking-tight text-ink">
            Add a property
          </h1>
          <p className="mt-1 text-[13px] text-ink-muted">
            It gets its own phone number for tenants to text.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 px-5">
          <div>
            <Label
              htmlFor="prop-label"
              className="text-xs font-bold uppercase tracking-widest text-ink-muted"
            >
              Property nickname
            </Label>
            <Input
              id="prop-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="The Palmerston Duplex"
              className="mt-1 h-12"
              aria-invalid={submitted && Boolean(fieldErrors.label) ? true : undefined}
              aria-describedby={submitted && fieldErrors.label ? "prop-label-err" : undefined}
            />
            {submitted && fieldErrors.label ? (
              <p id="prop-label-err" role="alert" className="mt-1.5 text-xs text-destructive">
                {fieldErrors.label}
              </p>
            ) : (
              <p className="mt-1.5 text-xs text-ink-muted">What you&rsquo;ll see in your queue.</p>
            )}
          </div>

          <div>
            <Label
              htmlFor="prop-address"
              className="text-xs font-bold uppercase tracking-widest text-ink-muted"
            >
              Street address
            </Label>
            <Input
              id="prop-address"
              value={addressLine1}
              onChange={(e) => setAddressLine1(e.target.value)}
              placeholder="41 Palmerston Ave"
              autoComplete="street-address"
              className="mt-1 h-12"
              aria-invalid={submitted && Boolean(fieldErrors.addressLine1) ? true : undefined}
              aria-describedby={
                submitted && fieldErrors.addressLine1 ? "prop-address-err" : undefined
              }
            />
            {submitted && fieldErrors.addressLine1 ? (
              <p id="prop-address-err" role="alert" className="mt-1.5 text-xs text-destructive">
                {fieldErrors.addressLine1}
              </p>
            ) : null}
          </div>

          <div className="flex gap-3">
            <div className="flex-1">
              <Label
                htmlFor="prop-city"
                className="text-xs font-bold uppercase tracking-widest text-ink-muted"
              >
                City
              </Label>
              <Input
                id="prop-city"
                value={city}
                onChange={(e) => setCity(e.target.value)}
                placeholder="Toronto"
                className="mt-1 h-12"
                aria-invalid={submitted && Boolean(fieldErrors.city) ? true : undefined}
                aria-describedby={submitted && fieldErrors.city ? "prop-city-err" : undefined}
              />
              {submitted && fieldErrors.city ? (
                <p id="prop-city-err" role="alert" className="mt-1.5 text-xs text-destructive">
                  {fieldErrors.city}
                </p>
              ) : null}
            </div>
            <div className="w-20">
              <Label
                htmlFor="prop-province"
                className="text-xs font-bold uppercase tracking-widest text-ink-muted"
              >
                Province
              </Label>
              <Input
                id="prop-province"
                value={province}
                onChange={(e) => setProvince(e.target.value.toUpperCase().slice(0, 2))}
                className="mt-1 h-12 text-center"
              />
            </div>
          </div>

          <div>
            <Label
              htmlFor="prop-postal"
              className="text-xs font-bold uppercase tracking-widest text-ink-muted"
            >
              Postal code (optional)
            </Label>
            <Input
              id="prop-postal"
              value={postalCode}
              onChange={(e) => setPostalCode(e.target.value)}
              placeholder="M6G 2K2"
              className="mt-1 h-12"
            />
          </div>

          <div>
            <Label
              htmlFor="prop-area-code"
              className="text-xs font-bold uppercase tracking-widest text-ink-muted"
            >
              Area code for its number (optional)
            </Label>
            <Input
              id="prop-area-code"
              value={areaCode}
              onChange={(e) => setAreaCode(e.target.value.replace(/\D/g, "").slice(0, 3))}
              placeholder="416"
              inputMode="numeric"
              className="mt-1 h-12"
              aria-invalid={submitted && Boolean(fieldErrors.areaCode) ? true : undefined}
              aria-describedby={
                submitted && fieldErrors.areaCode ? "prop-area-code-err" : undefined
              }
            />
            {submitted && fieldErrors.areaCode ? (
              <p id="prop-area-code-err" role="alert" className="mt-1.5 text-xs text-destructive">
                {fieldErrors.areaCode}
              </p>
            ) : (
              <p className="mt-1.5 text-xs text-ink-muted">
                This property gets its own number for tenants to text. We&rsquo;ll look for one in
                this area code first.
              </p>
            )}
          </div>

          {serverError ? (
            <p role="alert" className="text-sm text-destructive">
              {serverError}
            </p>
          ) : null}

          <Button
            type="submit"
            disabled={mutation.isPending || (submitted && !valid)}
            className="h-12 justify-center bg-brand text-brand-foreground hover:bg-brand/90"
          >
            {mutation.isPending ? "Setting up its number…" : "Add property"}
          </Button>
        </form>
      </div>
    </PhoneFrame>
  );
}
