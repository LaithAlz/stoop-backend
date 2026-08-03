import { createFileRoute, Link } from "@tanstack/react-router";
import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ChevronRight, Loader2, LogOut, Mail, Pencil, Sparkles } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import { meQueryKey, useMe, updateMe } from "@/api/me";
import { useQueue } from "@/api/queue";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { LandlordMe, UpdateMeInput } from "@/api/types";
import { useAuth } from "@/auth/AuthProvider";
import { planDisplayName, planStatusNotice } from "@/features/account/plan";
import { buildMeUpdatePayload, phoneLooksValid } from "@/features/account/profileEdit";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/app/account")({
  head: () => ({
    meta: [{ title: "Account — Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: AccountPage,
});

/**
 * Account — the real `GET /v1/me` display and `PATCH /v1/me` profile edit
 * (campaign issue #234 PR 5, the campaign's final PR — replacing
 * src/lib/mock-app.ts's `queue.length` badge count and every hardcoded
 * field this screen used to show). Ported in spirit from apps/mobile's Me
 * tab (src/app/(tabs)/me.tsx, #210 M2) onto this app's existing card-list
 * layout, dropping what mobile itself never claims either:
 *
 * - **Notifications** (push/email digest toggles) — REMOVED. The mock had
 *   two local-state `Switch`es that only ever wrote to `useState` and a
 *   toast; api-contracts.md's "Me" section v1.9 amendment is explicit that
 *   notification prefs/quiet-hours overrides have "NOT implemented" pending
 *   a schema-doc-first decision, and "emergency notifications are not a
 *   settable preference" by construction. Nothing here to wire honestly.
 * - **Billing** (payment method / billing email / receipts) — REMOVED. No
 *   endpoint anywhere in api-contracts.md returns a payment method or
 *   billing email; `POST /v1/billing/portal` (the "Billing (Train 2)"
 *   section) exists for a Stripe-hosted portal but isn't in this PR's scope
 *   (a real wiring of it is its own follow-up, not a card that fakes card
 *   data client-side).
 * - **Security** (change password / 2FA) — REMOVED. This app authenticates
 *   via Supabase magic link only (src/auth/AuthProvider.tsx) — there is no
 *   password to change and no 2FA feature anywhere in this codebase.
 * - **Help center** — REMOVED (was a bare toast, no real destination).
 *   "Email us" survives — a real `mailto:` action, not fabricated.
 * - **Plan** — kept, but reads `subscription_tier`/`price_cohort`/
 *   `subscription_status` from the real `LandlordMe` response through
 *   src/features/account/plan.ts, which only ever prints the canonical
 *   prices (CLAUDE.md rule 8) — never a client-invented plan state.
 *
 * Loading/error states for the profile card follow the house pattern
 * (`isPending` for the first load; a quiet strip on a background refetch
 * failure) — but NOT a full-screen takeover on `isError && !data`, unlike
 * Home/Conversations/Properties: this screen has other, independent
 * actions (sign out, legal links) that must stay reachable even if the
 * profile fetch itself is down, so a failed load renders as a small inline
 * card instead of blanking the whole screen.
 */
function AccountPage() {
  const { session, signOut } = useAuth();
  const meQuery = useMe({ enabled: Boolean(session) });
  // Tab-bar badge reads the queue's own action-needed count, same as every
  // other app.* screen (app.properties.tsx's own pattern) — mock-app.ts's
  // `queue.length` is gone.
  const queueQuery = useQueue({ enabled: Boolean(session) });

  const [editOpen, setEditOpen] = useState(false);
  const [logoutOpen, setLogoutOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  const me = meQuery.data;
  const displayName = me?.full_name || session?.user.email || "Your account";

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 border-b border-border bg-canvas/95 px-5 py-4 backdrop-blur">
        <p className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
          Account
        </p>
        <h1 className="truncate font-display text-[26px] leading-tight tracking-tight text-ink">
          {displayName}
        </h1>
      </header>

      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-5 pt-5">
          {meQuery.isPending ? (
            <div
              role="status"
              aria-live="polite"
              className="flex items-center justify-center gap-3 rounded-2xl border border-border bg-card p-6"
            >
              <Loader2 className="size-5 animate-spin text-brand motion-reduce:animate-none" />
              <p className="text-[13px] font-medium text-ink-muted">Loading your account…</p>
            </div>
          ) : meQuery.isError && !me ? (
            <div role="alert" className="rounded-2xl border border-border bg-card p-5 text-center">
              <p className="text-[13px] text-ink-muted">
                {meQuery.error instanceof ApiError
                  ? toHouseApiError(meQuery.error)
                  : "Couldn't load your account. Try again."}
              </p>
              <Button
                onClick={() => void meQuery.refetch()}
                className="mt-3 h-11 bg-brand text-brand-foreground hover:bg-brand/90"
              >
                Try again
              </Button>
            </div>
          ) : (
            me && (
              <>
                {meQuery.isError && (
                  <div
                    role="status"
                    className="mb-3 rounded-2xl border border-border bg-surface px-4 py-2.5 text-[13px] font-medium text-ink-muted"
                  >
                    Couldn&apos;t refresh just now — showing the last update.
                  </div>
                )}

                <div className="flex items-center gap-3 rounded-2xl border border-border bg-card p-4">
                  <div className="flex size-12 shrink-0 items-center justify-center rounded-full bg-brand text-[16px] font-bold text-brand-foreground">
                    {initials(me.full_name, me.email)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-display text-[16px] text-ink">{me.email}</p>
                    <p className="text-[12px] text-ink-muted">
                      Joined {joinedLabel(me.created_at)}
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setEditOpen(true)}
                    className="h-11 shrink-0 border-border px-3 text-ink"
                  >
                    <Pencil className="size-3.5" /> Edit
                  </Button>
                </div>

                <Section title="Plan">
                  <div className="rounded-2xl border border-brand/20 bg-brand-muted/50 p-5">
                    <div className="flex items-center gap-2 font-mono text-[10px] font-bold uppercase tracking-widest text-brand">
                      <Sparkles className="size-3.5" />{" "}
                      {planDisplayName(me.subscription_tier, me.price_cohort)}
                    </div>
                    {planStatusNotice(me.subscription_status, me.subscription_tier) && (
                      <p className="mt-2 text-[13px] text-destructive">
                        {planStatusNotice(me.subscription_status, me.subscription_tier)}
                      </p>
                    )}
                    <div className="mt-4">
                      <Button
                        asChild
                        className="h-11 w-full bg-brand text-brand-foreground hover:bg-brand/90"
                      >
                        <Link to="/plans">See plans</Link>
                      </Button>
                    </div>
                  </div>
                </Section>
              </>
            )
          )}
        </div>

        <Section title="Support">
          <Card>
            <Row
              icon={Mail}
              label="Email us"
              value="hello@stoop.co"
              onClick={() => {
                window.location.href = "mailto:hello@stoop.co";
              }}
              last
            />
          </Card>
        </Section>

        <Section title="Legal">
          <Card>
            <LinkRow label="Privacy Policy" to="/privacy" />
            <Divider />
            <LinkRow label="Terms of Service" to="/terms" last />
          </Card>
        </Section>

        <Section muted>
          <Card muted>
            <button
              type="button"
              onClick={() => setLogoutOpen(true)}
              className="flex min-h-14 w-full items-center justify-between gap-4 px-4 py-3 text-left"
            >
              <span className="flex items-center gap-3 text-[14px] font-medium text-ink">
                <LogOut className="size-4 text-ink-muted" /> Sign out
              </span>
              <ChevronRight className="size-4 text-ink-muted/70" />
            </button>
          </Card>
          <p className="mt-3 px-1 text-center font-mono text-[10px] uppercase tracking-widest text-ink-muted">
            Stoop. v1.0.0 — Made in the GTA
          </p>
        </Section>
      </div>

      <AppTabBar active="account" queueCount={queueQuery.data?.counts.total ?? 0} />

      {me && <EditProfileDialog open={editOpen} onOpenChange={setEditOpen} current={me} />}

      <AlertDialog open={logoutOpen} onOpenChange={setLogoutOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="font-display">Sign out?</AlertDialogTitle>
            {/* B3 (safety review, #234) — verified this copy against the
                implementation: AuthProvider.signOut() uses `scope: "local"`,
                so this literally only ends the session on THIS browser.
                Other signed-in devices (e.g. the Stoop mobile app) keep
                their own session and keep delivering alerts — this line
                would have been false back when `signOut()` used
                supabase-js's default `scope: "global"`, which revokes the
                session everywhere at once. */}
            <AlertDialogDescription>
              Your agent keeps working while you're signed out. You'll just stop getting alerts on
              this device.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-brand text-brand-foreground hover:bg-brand/90"
              disabled={signingOut}
              onClick={async () => {
                setSigningOut(true);
                // src/routes/app.tsx's route guard picks up the session
                // change and redirects to /sign-in; the PII fence
                // (src/auth/AuthProvider.tsx) clears the query cache on
                // the same SIGNED_OUT event.
                await signOut();
                setLogoutOpen(false);
                setSigningOut(false);
                toast.success("Signed out", { duration: 1500 });
              }}
            >
              Sign out
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PhoneFrame>
  );
}

/** Two-letter initials from a name, falling back to the email's first
 *  letter — never renders blank. `full_name` is a plain, unconstrained,
 *  NULLABLE `text` column (schema-v1.md) sourced from the auth user's
 *  `user_metadata.full_name`, which magic-link sign-in doesn't set — so
 *  null is the ORDINARY case on web, not an edge case (F1). */
function initials(fullName: string | null, email: string | null): string {
  const trimmed = (fullName ?? "").trim();
  if (!trimmed) return (email ?? "").slice(0, 1).toUpperCase() || "?";
  const parts = trimmed.split(/\s+/);
  const first = parts[0]?.[0] ?? "";
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? "") : "";
  return (first + last).toUpperCase() || "?";
}

function joinedLabel(createdAt: string): string {
  const date = new Date(createdAt);
  if (Number.isNaN(date.getTime())) return "recently";
  return date.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

/**
 * Name + phone edit — `PATCH /v1/me`'s two most landlord-relevant fields
 * (mirrors apps/mobile's ProfileEditModal scope). The phone field can't
 * prefill — `GET /v1/me` never returns it (write-only on this contract,
 * api/types.ts's own note) — so the helper text says plainly that blank
 * keeps the current number.
 */
function EditProfileDialog({
  open,
  onOpenChange,
  current,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  current: LandlordMe;
}) {
  // F4 (safety review, #234 PR 5): a dismissal while the PATCH is in
  // flight unmounts EditProfileForm, which tears down its useMutation
  // observer — `onSuccess`/`onError` never run, while the request itself
  // still lands server-side. On the field that decides where emergency
  // calls ring (and which GET /v1/me never echoes back, so the landlord
  // can't check), that is a silent wrong-state in both directions. The
  // dialog is held open until the write resolves.
  const [saving, setSaving] = useState(false);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && saving) return;
        onOpenChange(next);
      }}
    >
      <DialogContent
        className="border-border bg-canvas"
        onEscapeKeyDown={(e) => {
          if (saving) e.preventDefault();
        }}
        onInteractOutside={(e) => {
          if (saving) e.preventDefault();
        }}
      >
        {/* Radix's Dialog.Content only exists in the DOM while `open` is
            true (dialog.tsx has no `forceMount`) — closing this dialog
            unmounts EditProfileForm entirely, so its local state (name,
            phone, submitted, serverError) is already fresh on every open;
            no reset-via-remount `key` needed. */}
        <EditProfileForm
          current={current}
          onClose={() => onOpenChange(false)}
          onSavingChange={setSaving}
        />
      </DialogContent>
    </Dialog>
  );
}

function EditProfileForm({
  current,
  onClose,
  onSavingChange,
}: {
  current: LandlordMe;
  onClose: () => void;
  onSavingChange: (saving: boolean) => void;
}) {
  const queryClient = useQueryClient();
  // F1: `?? ""` — a null `full_name` (the ordinary magic-link case) would
  // otherwise make this an uncontrolled input and throw on `.trim()` in
  // the payload builder.
  const [name, setName] = useState(current.full_name ?? "");
  const [phone, setPhone] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  // Same double-submit latch as src/routes/app.properties_.add.tsx (L6) —
  // a ref is synchronous where `mutation.isPending` read from the render
  // closure isn't.
  const submitLatch = useRef(false);

  const mutation = useMutation({
    mutationFn: (input: UpdateMeInput) => updateMe(input),
    onSuccess: (me) => {
      queryClient.setQueryData(meQueryKey, me);
      toast.success("Saved", { duration: 1500 });
      onClose();
    },
    onError: (error, variables) => {
      // H2-style ambiguity check (src/routes/app.properties_.$id.tsx) — a
      // status-0/5xx failure here doesn't prove the write didn't land.
      if (error instanceof ApiError && (error.status === 0 || error.status >= 500)) {
        // F5 (safety review, #234 PR 5): "refresh to check" is impossible
        // advice for a phone edit — GET /v1/me deliberately never returns
        // `phone`, so no refresh can confirm it. PATCH is idempotent, so
        // re-sending the same number is the safe move and the honest
        // instruction.
        setServerError(
          "phone" in variables
            ? "We couldn't confirm your number saved. Save it again — sending the same number twice is harmless."
            : "That may have gone through — refresh to check before trying again.",
        );
        void queryClient.invalidateQueries({ queryKey: meQueryKey });
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
      onSavingChange(false);
    },
  });

  // Copy-guardian (#234 PR 5, round 2): the message has to describe what
  // `phoneLooksValid` actually accepts — the F2/F3 fixes widened it past
  // plain 10 digits, and an error narrower than the rule tells a landlord
  // their valid number is wrong.
  const phoneError = phoneLooksValid(phone)
    ? null
    : "Use 10 digits, or 11 starting with 1 for the country code.";

  function handleSave(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitted(true);
    setServerError(null);
    if (phoneError || mutation.isPending || submitLatch.current) return;
    // F7 (safety review, #234 PR 5): a deliberately-blanked name produced
    // no payload, so the dialog just closed with the old name still in the
    // header — a silent no-op that reads as success. Say what happened
    // instead of pretending it worked.
    if (name.trim().length === 0 && (current.full_name ?? "").length > 0) {
      setServerError("Your name can't be blank.");
      return;
    }
    const payload = buildMeUpdatePayload({ name, phone }, { full_name: current.full_name });
    if (!payload) {
      onClose();
      return;
    }
    submitLatch.current = true;
    onSavingChange(true);
    mutation.mutate(payload);
  }

  return (
    <form onSubmit={handleSave}>
      <DialogHeader>
        <DialogTitle className="font-display">Your details</DialogTitle>
        <DialogDescription>Name and the phone emergency calls ring.</DialogDescription>
      </DialogHeader>

      <div className="mt-4 flex flex-col gap-4">
        <div>
          <Label
            htmlFor="me-name"
            className="text-xs font-bold uppercase tracking-widest text-ink-muted"
          >
            Full name
          </Label>
          <Input
            id="me-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoComplete="name"
            className="mt-1 h-12"
          />
        </div>

        <div>
          <Label
            htmlFor="me-phone"
            className="text-xs font-bold uppercase tracking-widest text-ink-muted"
          >
            Your phone number
          </Label>
          <Input
            id="me-phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="(416) 555-0134"
            inputMode="tel"
            autoComplete="tel"
            className="mt-1 h-12"
            aria-invalid={submitted && Boolean(phoneError) ? true : undefined}
            aria-describedby={submitted && phoneError ? "me-phone-err" : "me-phone-help"}
          />
          {submitted && phoneError ? (
            <p id="me-phone-err" role="alert" className="mt-1.5 text-xs text-destructive">
              {phoneError}
            </p>
          ) : (
            <p id="me-phone-help" className="mt-1.5 text-xs text-ink-muted">
              This is where emergency calls ring, day or night. Leave it blank to keep the number
              already on file.
            </p>
          )}
        </div>

        {serverError ? (
          <p role="alert" className="text-sm text-destructive">
            {serverError}
          </p>
        ) : null}
      </div>

      <DialogFooter className="mt-6">
        <Button
          type="button"
          variant="outline"
          // F4: can't abandon an in-flight write to the emergency-call
          // number — the request lands either way and the result handlers
          // would be gone.
          disabled={mutation.isPending}
          className="h-11 border-border text-ink"
          onClick={onClose}
        >
          Cancel
        </Button>
        <Button
          type="submit"
          disabled={mutation.isPending}
          className="h-11 bg-brand text-brand-foreground hover:bg-brand/90"
        >
          {mutation.isPending ? "Saving…" : "Save"}
        </Button>
      </DialogFooter>
    </form>
  );
}

function Section({
  title,
  muted,
  children,
}: {
  title?: string;
  muted?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className={cn("px-5 pb-5 pt-3", muted && "opacity-80")}>
      {title && (
        <h2 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
          {title}
        </h2>
      )}
      {children}
    </section>
  );
}

function Card({ children, muted }: { children: React.ReactNode; muted?: boolean }) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-2xl border bg-card",
        muted ? "border-border/70" : "border-border",
      )}
    >
      {children}
    </div>
  );
}

function Divider() {
  return <div className="mx-4 border-t border-border" />;
}

function Row({
  icon: Icon,
  label,
  value,
  onClick,
  last,
}: {
  icon: typeof Mail;
  label: string;
  value?: string;
  onClick?: () => void;
  last?: boolean;
}) {
  return (
    <>
      <button
        type="button"
        onClick={onClick}
        className="flex min-h-14 w-full items-center justify-between gap-4 px-4 py-3 text-left"
      >
        <span className="flex items-center gap-3 text-[14px] text-ink">
          <Icon className="size-4 text-ink-muted" />
          {label}
        </span>
        <span className="flex items-center gap-2 text-[13px] text-ink-muted">
          {value}
          <ChevronRight className="size-4 text-ink-muted/70" />
        </span>
      </button>
      {!last && <Divider />}
    </>
  );
}

function LinkRow({
  label,
  to,
  last,
}: {
  label: string;
  to: "/privacy" | "/terms";
  last?: boolean;
}) {
  return (
    <>
      <Link to={to} className="flex min-h-14 w-full items-center justify-between gap-4 px-4 py-3">
        <span className="text-[14px] text-ink">{label}</span>
        <ChevronRight className="size-4 text-ink-muted/70" />
      </Link>
      {!last && <Divider />}
    </>
  );
}
