import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowLeft, Loader2 } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useAuth } from "@/auth/AuthProvider";
import { propertyQueryKey, updateProperty, useProperty } from "@/api/properties";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { Property, UpdatePropertyInput } from "@/api/types";
import {
  BACKUP_CONTACT_CLEAR_CONFIRM_LABEL,
  BACKUP_CONTACT_CLEAR_MESSAGE,
  backupContactClearAttempted,
  backupContactClearTitle,
  backupContactError,
  backupContactPhoneLooksInvalid,
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
  // B1 (HIGH, safety review): lifted here (not local to SettingsForm) so
  // the header back control below can react to it — the header is the
  // ONE way off this screen, and it must stop being a plain navigable
  // link while a PATCH to `backup_contact` is actually in flight.
  const [saving, setSaving] = useState(false);

  const propertyQuery = useProperty(id, { enabled: Boolean(session) });
  const property = propertyQuery.data;

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-canvas/95 px-4 py-3 backdrop-blur">
        {saving ? (
          // B1: NOT a disabled `<Link>` (Radix/TanStack's Link has no
          // native disabled state and would still be focusable/clickable
          // via keyboard) — swapped for a genuinely inert control for the
          // bounded duration of the save, mirroring app.account.tsx's
          // EditProfileDialog closeButtonClassName approach for the same
          // "can't abandon an in-flight write to a safety-relevant field"
          // problem.
          //
          // LOW (safety review): a bare `<span aria-disabled>` isn't a
          // real control — no role, not focusable, `aria-disabled` has no
          // defined meaning on a non-widget element, so it announces
          // nothing to a keyboard/screen-reader user (who'd otherwise
          // have zero indication anything is even here). A native
          // `disabled` `<button>` IS a real control: correctly pulled out
          // of the tab order (no dead-end focus stop) and, in a browse-
          // mode pass, announced as an unavailable button — the sr-only
          // span says why.
          <button
            type="button"
            disabled
            className="flex size-10 items-center justify-center -ml-2 opacity-50"
          >
            <ArrowLeft className="size-5" aria-hidden="true" />
            <span className="sr-only">Can&rsquo;t leave while saving.</span>
          </button>
        ) : (
          <Link
            to="/app/properties/$id"
            params={{ id }}
            className="flex size-10 items-center justify-center -ml-2"
          >
            <ArrowLeft className="size-5" />
          </Link>
        )}
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
            {/* LOW (safety review): the header's own back arrow still
                targets this same property (which just failed to load),
                so a landlord stuck here had no escape that doesn't retry
                the identical failing request first. */}
            <Link
              to="/app/properties"
              className="text-sm font-medium text-ink-muted underline underline-offset-2"
            >
              Back to properties
            </Link>
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

            <SettingsForm id={id} property={property} saving={saving} onSavingChange={setSaving} />
          </>
        ) : null}
      </div>
    </PhoneFrame>
  );
}

/**
 * Ambiguity check — the canonical three-clause predicate (R3-2/F5, now
 * shared with src/features/queue/useDraftActions.ts and app.account.tsx).
 *
 * R1 (safety re-verify, #261): this file kept the older `status === 0 ||
 * status >= 500` form, which is wrong in BOTH directions. Over-inclusive:
 * `not_configured` and `server_context` also carry status 0 but are
 * thrown BEFORE any `fetch` — telling a landlord their backup contact
 * "may have gone through" for a request that provably never left the
 * browser is the dishonest, silence-inducing direction, on the emergency
 * chain's second phone. Under-inclusive: an unparsable 2xx body throws
 * `ApiError(200, "unknown_error")`, which scored as a DEFINITE failure —
 * but the server answered 2xx, so the write landed.
 */
function isAmbiguousFailure(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return (
    error.code === "network_error" ||
    error.status >= 500 ||
    (error.code === "unknown_error" && error.status >= 200 && error.status < 300)
  );
}

const AMBIGUOUS_NOTICE =
  "That may have gone through — give it a moment to update before trying again.";

function SettingsForm({
  id,
  property,
  saving,
  onSavingChange,
}: {
  id: string;
  property: Property;
  // LOW (safety review): the PARENT's copy of "is a save in flight",
  // passed back down (not just written via `onSavingChange`) so a
  // hypothetical mid-flight remount of this component — a fresh
  // `useMutation`/`submitLatch` with `isPending`/`.current` both reset to
  // their initial `false` — still can't fire a second, concurrent PATCH.
  // `saving` (parent state) survives a remount of THIS component the way
  // nothing local to it can.
  saving: boolean;
  onSavingChange: (saving: boolean) => void;
}) {
  const queryClient = useQueryClient();

  const [form, setForm] = useState<PropertySettingsForm>(() =>
    propertySettingsFormFromProperty(property),
  );
  const [current, setCurrent] = useState<Property>(property);
  // M2 (safety review): whether the landlord has typed anything since
  // mount or the last successful save. Gates the re-seed effect below —
  // never overwrite an in-progress, not-yet-saved edit, even to deliver
  // fresher server data.
  const [dirty, setDirty] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  // #268: gates the real backup-contact-removal confirm dialog below —
  // Save never sends `backup_contact: null` without this having been
  // explicitly confirmed first (see buildPropertySettingsPayload's
  // `confirmedClear` option).
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  // L6-shaped latch (src/routes/app.properties_.add.tsx) — synchronous,
  // unlike a `mutation.isPending` read from the render closure, so two
  // submits inside one frame can't both PATCH.
  const submitLatch = useRef(false);

  // F3 (MEDIUM, safety review): `current` is the diff BASELINE for
  // `buildPropertySettingsPayload`/`backupContactClearAttempted`/
  // `quietHoursClearAttempted`/`storedBackupPhoneInvalid` — it never
  // holds anything the landlord is mid-typing (that's `form`, gated on
  // `!dirty` below), so gating it on `!dirty` too was wrong: `dirty` goes
  // true on the very first keystroke and stays true until a successful
  // save, which means a SINGLE `!dirty`-gated effect skipped exactly the
  // scenario its own original comment cited (the ambiguous-failure
  // `invalidateQueries` below, which by definition only runs after a
  // submit, which by definition only happens after typing) — `current`
  // was stale as a diff baseline in precisely the case that mattered, and
  // this also produced a post-save re-seed flash (the unconditional
  // `setCurrent`/`setForm` in `onSuccess` below would render, then this
  // effect would re-fire and briefly reassert the same values a second
  // time). Unconditional on `[property]` — always safe, since `current`
  // is never live-edited text.
  useEffect(() => {
    setCurrent(property);
  }, [property]);

  // M2 (MEDIUM, safety review): without this, `form` was seeded ONCE from
  // the `property` prop (the `useState` lazy initializer above) and never
  // again — a background refetch (window refocus, another tab's edit, or
  // this screen's OWN ambiguous-failure invalidate below) could deliver
  // fresher data that the screen would never actually show, making the
  // ambiguous notice's "give it a moment to update" false on a screen
  // structurally incapable of updating. Only re-seeds while `!dirty` — an
  // edit already in progress is never silently overwritten.
  useEffect(() => {
    if (dirty) return;
    setForm(propertySettingsFormFromProperty(property));
  }, [property, dirty]);

  function updateForm(patch: Partial<PropertySettingsForm>) {
    setDirty(true);
    setForm((f) => ({ ...f, ...patch }));
  }

  const mutation = useMutation({
    mutationFn: (input: UpdatePropertyInput) => updateProperty(id, input),
    // F1 (HIGH, safety review): without this, react-query's default
    // `networkMode: "online"` (src/api/queryClient.ts sets none) means an
    // OFFLINE attempt never calls `mutationFn` at all — the mutation just
    // sits paused, `onError`/`onSettled` never fire, `onSavingChange(false)`
    // (only ever called from `onSettled`) never runs, and `saving` latches
    // true forever: every field disabled, the Save button stuck on
    // "Saving…", and the header back control (below) reduced to an inert,
    // unfocusable button with no way off the screen until connectivity
    // returns. `"always"` makes an offline attempt actually run
    // `mutationFn`, which reaches `apiRequest`'s `fetch` and throws
    // immediately into the existing ambiguous-failure branch — genuinely
    // bounded, unlike a paused-forever mutation.
    networkMode: "always",
    onSuccess: (updated) => {
      queryClient.setQueryData(propertyQueryKey(id), updated);
      setCurrent(updated);
      // Re-seed from the server's own echo (normalized phone, trimmed
      // text) rather than leaving the raw, pre-normalization input on
      // screen — same "show what was actually saved" discipline as
      // app.account.tsx's profile edit closing on success.
      setForm(propertySettingsFormFromProperty(updated));
      setDirty(false);
      setServerError(null);
      toast.success("Saved", { duration: 1500 });
    },
    onError: (error) => {
      const message = isAmbiguousFailure(error)
        ? AMBIGUOUS_NOTICE
        : error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.";
      setServerError(message);
      // B1 (HIGH, safety review): local `serverError` state is lost the
      // instant this component unmounts — e.g. the landlord taps the back
      // arrow right after Save — while the PATCH itself still lands
      // server-side, silently. A toast is rendered at the app root
      // (src/routes/__root.tsx's `<Toaster>`), so it survives navigation
      // the way local state structurally can't.
      toast.error(message);
      if (isAmbiguousFailure(error)) {
        void queryClient.invalidateQueries({ queryKey: propertyQueryKey(id) });
      }
    },
    onSettled: () => {
      submitLatch.current = false;
      onSavingChange(false);
    },
  });

  const backupError = backupContactError(form);
  const quietError = quietHoursError(form);
  const backupCleared = backupContactClearAttempted(form, current);
  const quietCleared = quietHoursClearAttempted(form, current);
  // M1 (MEDIUM, safety review): checked against `current` (the last
  // known-good STORED value), not the live `form` text — a separate
  // concern from `backupError`, which only validates what's being typed
  // right now.
  const storedBackupPhoneInvalid = backupContactPhoneLooksInvalid(current.backup_contact);
  // LOW (safety review): the guard/disabled checks below read `busy`, not
  // bare `mutation.isPending` — see the `saving` prop doc comment above.
  const busy = saving || mutation.isPending;

  // Shared by both the direct-save path and the post-confirm path below —
  // never called twice for the same submit (guarded by submitLatch either
  // way it's reached).
  function submitPayload(options: { confirmedClear?: boolean } = {}) {
    const payload = buildPropertySettingsPayload(form, current, options);
    if (!payload) {
      // Nothing changed (or the only change was a blank-both "clear" the
      // form already flags via backupCleared/quietCleared above) — say so
      // instead of a Save button that visibly does nothing.
      toast("Nothing to update.", { duration: 1500 });
      return;
    }
    submitLatch.current = true;
    onSavingChange(true);
    mutation.mutate(payload);
  }

  function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitted(true);
    setServerError(null);
    if (backupError || quietError || busy || submitLatch.current) return;
    // #268: a blank-both backup contact on a previously-set one is a real
    // removal, not an ordinary edit — confirm the specific consequence
    // before sending anything, rather than folding it into a generic Save.
    if (backupCleared) {
      setClearConfirmOpen(true);
      return;
    }
    submitPayload();
  }

  function handleConfirmClearBackupContact() {
    setClearConfirmOpen(false);
    submitPayload({ confirmedClear: true });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-6 px-5 pb-8 pt-4">
      {/* Backup contact */}
      <section className="flex flex-col gap-3 rounded-2xl border border-border bg-card p-4">
        <div>
          <h2 className="font-display text-[16px] text-ink">Backup contact</h2>
          {/* M3/F4 (safety review): discloses what apps/api/app/agent/
              emergency_chain.py's `render_backup_alert_sms` actually sends
              the backup contact at T+10m (and every repeat) — what
              happened at this property (interpolated below as
              `current.label`: `render_backup_alert_sms`'s own
              `property_label` parameter is sourced from `p.label` — the
              nickname the landlord chose, NOT `address_line1`; F4 caught
              this copy saying "the address" instead), that the landlord
              hasn't answered, the tenant's name, and an ask to either call
              that tenant or tap the ack link. Also discloses the
              15-minute landlord/backup alternating cycle
              (`ESCALATION_REPEAT_INTERVAL_MINUTES`) that continues until
              someone acknowledges. A landlord consenting on a third
              party's behalf needs to know what that person is actually
              being asked to do, not just that they're contacted
              (copy-guardian FAIL on the first version, commit 9ec310b). */}
          <p className="mt-1 text-[13px] leading-relaxed text-ink-muted">
            The second number I call during a real emergency, in case yours is ever wrong, off, or
            you just don&rsquo;t pick up. I call you first, every time — if there&rsquo;s no answer,
            I call this number about ten minutes later. They&rsquo;ll also get a text saying what
            happened at {current.label}, that you haven&rsquo;t answered, and your tenant&rsquo;s
            name — asking them to call your tenant or tap a link to say they&rsquo;ve got it. I keep
            alternating between you and them every fifteen minutes until one of you does. Optional,
            but strongly recommended.
          </p>
        </div>

        {/* M1 (MEDIUM, safety review): a stored-but-undialable number was
            otherwise invisible until this form happened to be opened and
            re-submitted — this warns as soon as the section loads. */}
        {storedBackupPhoneInvalid && (
          <p role="alert" className="text-[13px] font-medium text-urgent">
            The phone number on file for your backup contact doesn&rsquo;t look valid — I may not be
            able to reach them in an emergency. Fix it below.
          </p>
        )}

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
            onChange={(e) => updateForm({ backupName: e.target.value })}
            placeholder="Jordan (super)"
            autoComplete="name"
            disabled={busy}
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
            onChange={(e) => updateForm({ backupPhone: e.target.value })}
            placeholder="(416) 555-0177"
            inputMode="tel"
            autoComplete="tel"
            disabled={busy}
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
          // #268: a real clear now exists (PATCH backup_contact: null,
          // api-contracts.md's v1.25 amendment) — this used to be a dead
          // end ("can't be cleared, contact support"). Save still asks for
          // an explicit confirmation (the dialog below) before anything is
          // sent; this line just previews that Save will ask.
          <p className="text-xs text-ink-muted">
            Saving will remove {current.backup_contact?.name} as your backup contact — I&rsquo;ll
            ask you to confirm first.
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
              onChange={(e) => updateForm({ quietStart: e.target.value })}
              disabled={busy}
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
              onChange={(e) => updateForm({ quietEnd: e.target.value })}
              disabled={busy}
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
            onChange={(e) => updateForm({ houseRules: e.target.value })}
            placeholder="Visitor parking is behind the building, 48 hours max. Garbage day is Thursday."
            disabled={busy}
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
        disabled={busy}
        className="h-12 justify-center bg-brand text-brand-foreground hover:bg-brand/90"
      >
        {busy ? "Saving…" : "Save settings"}
      </Button>

      {/* #268 — Radix portals this out to document.body at render time, so
          declaring it here (inside the <form> JSX) never nests real <button>
          DOM inside the form's own subtree; its Cancel/Confirm actions can't
          accidentally trigger a native form submit. */}
      <AlertDialog open={clearConfirmOpen} onOpenChange={setClearConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="font-display">
              {backupContactClearTitle(current.backup_contact?.name ?? "them")}
            </AlertDialogTitle>
            <AlertDialogDescription>{BACKUP_CONTACT_CLEAR_MESSAGE}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={(e) => {
                e.preventDefault();
                handleConfirmClearBackupContact();
              }}
            >
              {busy ? "Removing…" : BACKUP_CONTACT_CLEAR_CONFIRM_LABEL}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </form>
  );
}
