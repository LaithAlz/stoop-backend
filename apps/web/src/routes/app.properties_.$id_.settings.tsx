import { createFileRoute, Link } from "@tanstack/react-router";
import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowLeft, Loader2 } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useAuth } from "@/auth/AuthProvider";
import { propertyQueryKey, updateProperty, useProperty } from "@/api/properties";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { Property, UpdatePropertyInput } from "@/api/types";
import {
  backupContactClearAttempted,
  backupContactError,
  buildPropertySettingsPayload,
  propertySettingsFormFromProperty,
  quietHoursClearAttempted,
  quietHoursError,
  type PropertySettingsForm,
} from "@/features/properties/settings";

export const Route = createFileRoute("/app/properties_/$id_/settings")({
  head: ({ params }) => ({
    meta: [{ title: "Property settings — Stoop." }, { name: "robots", content: "noindex" }],
    links: [{ rel: "canonical", href: `/app/properties/${params.id}/settings` }],
  }),
  component: PropertySettingsPage,
});

/**
 * Property settings — `PATCH /v1/properties/{id}` for real (issue #261,
 * the campaign-#234-PR-5 follow-up). This is the real replacement for the
 * mock `app.properties_.$id_.settings.tsx` PR 5 deleted outright: that
 * screen showed autonomy-mode tiers, a house-rules editor, lease facts,
 * vendors, a custom FAQ, notification prefs, and severity overrides — none
 * of it backed by api-contracts.md/schema-v1.md, and one control (autonomy
 * mode) let a landlord believe they'd graduated a property to auto-send
 * while writing only to local `useState`. This rebuild ships exactly the
 * three fields that ARE real, PATCH-able `properties` columns and nothing
 * else:
 *
 * - `backup_contact` — the SECOND phone in the emergency escalation chain
 *   (apps/api/app/agent/emergency_chain.py: T+0/T+5m call the landlord,
 *   T+10m calls this number too) — the redundancy that covers a wrong or
 *   undialable primary number. Phone normalization reuses src/lib/
 *   phone.ts's `toE164`/`phoneLooksValid`-equivalent discipline (moved out
 *   of src/features/account/profileEdit.ts, the five-round-reviewed
 *   reference for this exact "undialable value silently stored" failure
 *   mode) rather than a second implementation.
 * - `quiet_hours` — stored and PATCH-able, but NOT currently read by any
 *   agent node beyond `load_context` (apps/api/app/agent/nodes/
 *   load_context.py loads it into `CaseContext`; nothing downstream of
 *   that consults it — only `house_rules`/`voice_profile` reach the draft
 *   prompt, `apps/api/app/agent/nodes/draft_response.py`). The copy below
 *   says so honestly rather than claiming it changes when Stoop texts.
 * - `house_rules` — genuinely used: injected verbatim into the draft
 *   prompt ("Property house rules (use only what's relevant)").
 *
 * `label`/`address_line1`/`city`/`province`/`postal_code`/`heating_season`
 * are also documented PATCH fields (api-contracts.md's Properties section)
 * but out of scope for #261, which asks for exactly the three above — left
 * out rather than invented, noted in the PR report.
 *
 * Loading/error states follow the property detail page's own pattern
 * (app.properties_.$id.tsx): `isPending` for the first load, a full
 * takeover only when `isError && !data`, the house "Couldn't refresh just
 * now" strip on a background refetch failure otherwise.
 */
function PropertySettingsPage() {
  const { id } = Route.useParams();
  const { session } = useAuth();

  const propertyQuery = useProperty(id, { enabled: Boolean(session) });
  const property = propertyQuery.data;

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-canvas/95 px-4 py-3 backdrop-blur">
        <Link
          to="/app/properties/$id"
          params={{ id }}
          className="flex size-10 items-center justify-center -ml-2"
        >
          <ArrowLeft className="size-5" />
        </Link>
      </header>

      <div className="flex-1 overflow-y-auto pb-24">
        {propertyQuery.isPending ? (
          <div
            role="status"
            aria-live="polite"
            className="flex flex-col items-center gap-3 py-16 text-center"
          >
            <Loader2 className="size-6 animate-spin text-brand motion-reduce:animate-none" />
            <p className="text-sm font-medium text-ink-muted">Loading this property…</p>
          </div>
        ) : propertyQuery.isError && !property ? (
          <div role="alert" className="flex flex-col items-center gap-3 px-6 py-16 text-center">
            <p className="text-sm text-ink-muted">
              {propertyQuery.error instanceof ApiError
                ? toHouseApiError(propertyQuery.error)
                : "Couldn't load this property. Try again."}
            </p>
            <Button
              onClick={() => void propertyQuery.refetch()}
              className="h-11 bg-brand text-brand-foreground hover:bg-brand/90"
            >
              Try again
            </Button>
          </div>
        ) : property ? (
          <>
            {propertyQuery.isError && (
              <div
                role="status"
                className="mx-4 mt-4 rounded-2xl border border-border bg-surface px-4 py-2.5 text-[13px] font-medium text-ink-muted"
              >
                Couldn&apos;t refresh just now — showing the last update.
              </div>
            )}

            <div className="px-5 pb-2 pt-5">
              <p className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                Settings
              </p>
              <h1 className="mt-1 font-display text-[26px] leading-tight tracking-tight text-ink">
                {property.address_line1}, {property.city}
              </h1>
            </div>

            <SettingsForm id={id} property={property} />
          </>
        ) : null}
      </div>
    </PhoneFrame>
  );
}

/**
 * H2-shaped ambiguity check, same branch as app.properties_.$id.tsx and
 * src/routes/app.account.tsx's edit form: a status-0/5xx failure doesn't
 * prove the PATCH didn't land server-side.
 */
function isAmbiguousFailure(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 0 || error.status >= 500);
}

const AMBIGUOUS_NOTICE =
  "That may have gone through — give it a moment to update before trying again.";

function SettingsForm({ id, property }: { id: string; property: Property }) {
  const queryClient = useQueryClient();

  const [form, setForm] = useState<PropertySettingsForm>(() =>
    propertySettingsFormFromProperty(property),
  );
  const [current, setCurrent] = useState<Property>(property);
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  // L6-shaped latch (src/routes/app.properties_.add.tsx) — synchronous,
  // unlike a `mutation.isPending` read from the render closure, so two
  // submits inside one frame can't both PATCH.
  const submitLatch = useRef(false);

  const mutation = useMutation({
    mutationFn: (input: UpdatePropertyInput) => updateProperty(id, input),
    onSuccess: (updated) => {
      queryClient.setQueryData(propertyQueryKey(id), updated);
      setCurrent(updated);
      // Re-seed from the server's own echo (normalized phone, trimmed
      // text) rather than leaving the raw, pre-normalization input on
      // screen — same "show what was actually saved" discipline as
      // app.account.tsx's profile edit closing on success.
      setForm(propertySettingsFormFromProperty(updated));
      setServerError(null);
      toast.success("Saved", { duration: 1500 });
    },
    onError: (error) => {
      if (isAmbiguousFailure(error)) {
        setServerError(AMBIGUOUS_NOTICE);
        void queryClient.invalidateQueries({ queryKey: propertyQueryKey(id) });
        return;
      }
      setServerError(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      );
    },
    onSettled: () => {
      submitLatch.current = false;
    },
  });

  const backupError = backupContactError(form);
  const quietError = quietHoursError(form);
  const backupCleared = backupContactClearAttempted(form, current);
  const quietCleared = quietHoursClearAttempted(form, current);

  function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitted(true);
    setServerError(null);
    if (backupError || quietError || mutation.isPending || submitLatch.current) return;
    const payload = buildPropertySettingsPayload(form, current);
    if (!payload) {
      // Nothing changed (or the only change was a blank-both "clear" the
      // form already flags via backupCleared/quietCleared above) — say so
      // instead of a Save button that visibly does nothing.
      toast("Nothing to update.", { duration: 1500 });
      return;
    }
    submitLatch.current = true;
    mutation.mutate(payload);
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-6 px-5 pb-8 pt-4">
      {/* Backup contact */}
      <section className="flex flex-col gap-3 rounded-2xl border border-border bg-card p-4">
        <div>
          <h2 className="font-display text-[16px] text-ink">Backup contact</h2>
          <p className="mt-1 text-[13px] leading-relaxed text-ink-muted">
            The second number I call during a real emergency, in case yours is ever wrong, off, or
            you just don&rsquo;t pick up. I call you first, every time — if there&rsquo;s no answer,
            I call this number about ten minutes later. Optional, but strongly recommended.
          </p>
        </div>

        <div>
          <Label
            htmlFor="settings-backup-name"
            className="text-xs font-bold uppercase tracking-widest text-ink-muted"
          >
            Their name
          </Label>
          <Input
            id="settings-backup-name"
            value={form.backupName}
            onChange={(e) => setForm((f) => ({ ...f, backupName: e.target.value }))}
            placeholder="Jordan (super)"
            autoComplete="name"
            disabled={mutation.isPending}
            className="mt-1 h-11"
            aria-invalid={submitted && Boolean(backupError) ? true : undefined}
            aria-describedby={submitted && backupError ? "settings-backup-err" : undefined}
          />
        </div>

        <div>
          <Label
            htmlFor="settings-backup-phone"
            className="text-xs font-bold uppercase tracking-widest text-ink-muted"
          >
            Their phone number
          </Label>
          <Input
            id="settings-backup-phone"
            value={form.backupPhone}
            onChange={(e) => setForm((f) => ({ ...f, backupPhone: e.target.value }))}
            placeholder="(416) 555-0177"
            inputMode="tel"
            autoComplete="tel"
            disabled={mutation.isPending}
            className="mt-1 h-11"
            aria-invalid={submitted && Boolean(backupError) ? true : undefined}
            aria-describedby={submitted && backupError ? "settings-backup-err" : undefined}
          />
        </div>

        {submitted && backupError ? (
          <p id="settings-backup-err" role="alert" className="text-xs text-destructive">
            {backupError}
          </p>
        ) : backupCleared ? (
          <p className="text-xs text-ink-muted">
            Backup contact can&rsquo;t be cleared from this form — leaving both fields blank keeps
            the one already on file.
          </p>
        ) : null}
      </section>

      {/* Quiet hours */}
      <section className="flex flex-col gap-3 rounded-2xl border border-border bg-card p-4">
        <div>
          <h2 className="font-display text-[16px] text-ink">Quiet hours</h2>
          <p className="mt-1 text-[13px] leading-relaxed text-ink-muted">
            For your own reference. It never delays a real emergency — that always reaches you right
            away, any time.
          </p>
        </div>

        <div className="flex gap-3">
          <div className="flex-1">
            <Label
              htmlFor="settings-quiet-start"
              className="text-xs font-bold uppercase tracking-widest text-ink-muted"
            >
              Starts
            </Label>
            <Input
              id="settings-quiet-start"
              type="time"
              value={form.quietStart}
              onChange={(e) => setForm((f) => ({ ...f, quietStart: e.target.value }))}
              disabled={mutation.isPending}
              className="mt-1 h-11"
              aria-invalid={submitted && Boolean(quietError) ? true : undefined}
              aria-describedby={submitted && quietError ? "settings-quiet-err" : undefined}
            />
          </div>
          <div className="flex-1">
            <Label
              htmlFor="settings-quiet-end"
              className="text-xs font-bold uppercase tracking-widest text-ink-muted"
            >
              Ends
            </Label>
            <Input
              id="settings-quiet-end"
              type="time"
              value={form.quietEnd}
              onChange={(e) => setForm((f) => ({ ...f, quietEnd: e.target.value }))}
              disabled={mutation.isPending}
              className="mt-1 h-11"
              aria-invalid={submitted && Boolean(quietError) ? true : undefined}
              aria-describedby={submitted && quietError ? "settings-quiet-err" : undefined}
            />
          </div>
        </div>

        {submitted && quietError ? (
          <p id="settings-quiet-err" role="alert" className="text-xs text-destructive">
            {quietError}
          </p>
        ) : quietCleared ? (
          <p className="text-xs text-ink-muted">
            Quiet hours can&rsquo;t be cleared from this form — leaving both fields blank keeps the
            hours already on file.
          </p>
        ) : null}
      </section>

      {/* House rules */}
      <section className="flex flex-col gap-3 rounded-2xl border border-border bg-card p-4">
        <div>
          <h2 className="font-display text-[16px] text-ink">House rules</h2>
          <p className="mt-1 text-[13px] leading-relaxed text-ink-muted">
            I use this to answer routine questions myself, in your voice — parking, garbage day,
            anything specific to this property.
          </p>
        </div>
        <div>
          <Label htmlFor="settings-house-rules" className="sr-only">
            House rules
          </Label>
          <Textarea
            id="settings-house-rules"
            value={form.houseRules}
            onChange={(e) => setForm((f) => ({ ...f, houseRules: e.target.value }))}
            placeholder="Visitor parking is behind the building, 48 hours max. Garbage day is Thursday."
            disabled={mutation.isPending}
            rows={5}
            className="min-h-28 resize-y"
          />
        </div>
      </section>

      {serverError ? (
        <p role="alert" className="text-sm text-destructive">
          {serverError}
        </p>
      ) : null}

      <Button
        type="submit"
        disabled={mutation.isPending}
        className="h-12 justify-center bg-brand text-brand-foreground hover:bg-brand/90"
      >
        {mutation.isPending ? "Saving…" : "Save settings"}
      </Button>
    </form>
  );
}
