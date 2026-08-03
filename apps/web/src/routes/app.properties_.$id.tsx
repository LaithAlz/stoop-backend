import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowLeft, ChevronRight, Loader2, MessageSquare, Phone } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { SeverityBadge } from "@/components/stoop/SeverityBadge";
import { Button } from "@/components/ui/button";
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
import {
  deleteProperty,
  propertiesQueryKey,
  propertyQueryKey,
  useProperty,
} from "@/api/properties";
import { tenantsQueryKey, useTenants } from "@/api/tenants";
import { revokeTrust } from "@/api/trust";
import { useCasesList } from "@/api/cases";
import { useQueue } from "@/api/queue";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { CaseSummary, Tenant, VulnerableOccupant } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { formatRelativeTime } from "@/lib/relativeTime";
import { backupContactPhoneLooksInvalid } from "@/features/properties/settings";
import {
  NO_NUMBER_BODY,
  NO_NUMBER_TITLE,
  NUMBER_CAPTION,
  formatStoopNumber,
} from "@/features/properties/stoopNumber";
import {
  DELETE_PROPERTY_CONFIRM_LABEL,
  DELETE_PROPERTY_TITLE,
  deletePropertyMessage,
} from "@/features/properties/deleteProperty";
import {
  revokeConfirmation,
  revokeResultNotice,
  TRUST_SECTION_BODY,
  TRUST_SECTION_TITLE,
} from "@/features/trust/revoke";

export const Route = createFileRoute("/app/properties_/$id")({
  head: ({ params }) => ({
    meta: [{ title: "Property — Stoop." }, { name: "robots", content: "noindex" }],
    links: [{ rel: "canonical", href: `/app/properties/${params.id}` }],
  }),
  component: PropertyHub,
});

/** Same wording as apps/mobile's TenantFormModal.VULNERABLE_OPTIONS labels
 *  (already copy-reviewed there) — display-only here since this PR's
 *  detail screen renders the tenant list read-only (no add/edit tenant UI
 *  ships this PR, see the PR report). */
const VULNERABLE_LABELS: Record<VulnerableOccupant, string> = {
  infant: "An infant",
  elderly: "An elderly person",
  medical_device: "On powered medical equipment",
};

/**
 * Property detail — `GET /v1/properties/{id}` (campaign issue #234 PR 4,
 * replacing src/lib/mock-app.ts's `properties`/`queue` and src/lib/
 * mock-property.ts's fictional autonomy-mode/trust-streak data). Loading/
 * error states follow src/routes/app.index.tsx's exact pattern: `isPending`
 * for the first load, a full takeover ONLY when `isError && !data`, a quiet
 * refresh strip otherwise.
 *
 * The old "Manage" section's links to a Settings / Trust dashboard are
 * DROPPED here, not ported: both sub-screens were keyed to mock property
 * ids and 404'd for every real, live property id — the "guaranteed dead
 * end" class PR 3's own safety review flagged for mock conversation
 * links. Their real functional overlap with the live contract — the
 * trust-ladder revoke action — is wired directly on this page instead
 * (below); the rest of what those screens showed (autonomy-mode tiers,
 * house rules editor, lease facts, vendors, FAQ, notification prefs,
 * severity overrides) has no backing in api-contracts.md/schema-v1.md at
 * all. Both files were deleted outright in #234 PR 5 along with
 * mock-property.ts — one of them let a landlord believe they had
 * graduated a property to auto-send when it wrote nothing at all.
 *
 * "Recent conversations" is wired live via `GET /v1/cases?property_id=`
 * (api-contracts.md's Cases section documents this filter param) —
 * replacing the mock queue-filtered list PR 3 left as a non-navigating
 * placeholder (that PR's own note: linking a mock id into the now-live
 * conversation thread route would have been a guaranteed `case_not_found`).
 */
function PropertyHub() {
  const { id } = Route.useParams();
  const { session } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const propertyQuery = useProperty(id, { enabled: Boolean(session) });
  const tenantsQuery = useTenants(id, { enabled: Boolean(session) });
  const recentCasesQuery = useCasesList({
    propertyId: id,
    limit: 5,
    enabled: Boolean(session) && Boolean(id),
  });
  const queueQuery = useQueue({ enabled: Boolean(session) });

  const [revokeConfirmOpen, setRevokeConfirmOpen] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);

  // H2 (safety review, #234 PR 4): a status-0 (`network_error`) or 5xx
  // failure is AMBIGUOUS — the request may well have applied server-side
  // and only the response was lost. Reporting it as a definite failure is
  // the dishonest direction on both of these actions (one severs a
  // building's tenant line, the other turns auto-send off). Same branch
  // and the same house line the approve loop already uses
  // (src/features/queue/useDraftActions.ts).
  const isAmbiguousFailure = (error: unknown) =>
    error instanceof ApiError && (error.status === 0 || error.status >= 500);
  const AMBIGUOUS_NOTICE =
    "That may have gone through — give it a moment to update before trying again.";

  const revokeMutation = useMutation({
    mutationFn: () => revokeTrust(id, "property"),
    onSuccess: (result) => {
      toast(revokeResultNotice(result.scope, result.revoked_count));
      setRevokeConfirmOpen(false);
    },
    onError: (error) => {
      if (isAmbiguousFailure(error)) {
        toast(AMBIGUOUS_NOTICE);
        void queryClient.invalidateQueries({ queryKey: propertyQueryKey(id) });
        setRevokeConfirmOpen(false);
        return;
      }
      toast(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      );
      // L5 (safety review): a DEFINITE revoke failure means auto-send is
      // still ON — closing the dialog reads as success to anyone who
      // misses the toast, so the dialog stays open for a real refusal.
    },
  });

  // No optimistic removal — the property only leaves the list once the
  // server confirms the delete (same "server confirms first" discipline as
  // the approve/undo path — src/features/queue/useDraftActions.ts).
  const deleteMutation = useMutation({
    mutationFn: () => deleteProperty(id),
    onSuccess: () => {
      // L1 (safety review): drop THIS property's cached detail/tenant
      // reads rather than invalidating them — a prefix invalidate refetches
      // the still-mounted detail, 404s, and paints "Couldn't refresh just
      // now" over a fully-rendered deleted property.
      queryClient.removeQueries({ queryKey: propertyQueryKey(id) });
      queryClient.removeQueries({ queryKey: tenantsQueryKey(id) });
      // R2 (safety re-verify): NOT awaited. react-query awaits onSuccess
      // inside the same try that routes into onError, so a rejected or
      // interrupted navigation would report "Something didn't go through"
      // for a delete that already severed the building's line. Invalidate
      // first so the list is stale either way, then navigate.
      void queryClient.invalidateQueries({ queryKey: propertiesQueryKey });
      void navigate({ to: "/app/properties" });
    },
    onError: (error) => {
      // LOW (safety review, #258 follow-up): invalidated unconditionally
      // now, not just on the ambiguous branch — `property_not_found` in
      // particular means the row is ALREADY gone (e.g. deleted from
      // another tab/device a moment earlier), a "delete actually
      // succeeded, this attempt just reported a definite failure" case
      // the list should still reconcile against. Harmless on every other
      // failure code too (just a background refetch of an unchanged list).
      void queryClient.invalidateQueries({ queryKey: propertiesQueryKey });
      if (isAmbiguousFailure(error)) {
        // H2: on DELETE specifically, "may have gone through" means the
        // building's line may already be severed — send them to the list,
        // which is the honest read, instead of asserting failure.
        toast(AMBIGUOUS_NOTICE);
        setDeleteConfirmOpen(false);
        return;
      }
      toast(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      );
      setDeleteConfirmOpen(false);
    },
  });

  const property = propertyQuery.data;
  const tenants: Tenant[] = (tenantsQuery.data?.items ?? []).filter((t) => t.active);
  // L4 (#258 follow-up): the RAW row count, not the active-only `tenants`
  // list above — a soft-deleted tenant row (`active = false`) still blocks
  // the delete via `tenants.property_id`'s `ON DELETE RESTRICT`
  // (schema-v1.md), so it still counts as a known blocker here even
  // though it's hidden from the Tenants panel above.
  const allTenantCount = tenantsQuery.data?.items.length ?? 0;
  const recentCases: CaseSummary[] = recentCasesQuery.data?.pages[0]?.items ?? [];

  const revokeCopy = revokeConfirmation("property");

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-canvas/95 px-4 py-3 backdrop-blur">
        <Link to="/app/properties" className="flex size-10 items-center justify-center -ml-2">
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

            <div className="px-5 pb-4 pt-5">
              <p className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                {property.label}
              </p>
              <h1 className="mt-1 font-display text-[26px] leading-tight tracking-tight text-ink">
                {property.address_line1}, {property.city}
              </h1>
              <p className="mt-2 flex items-center gap-1.5 font-mono text-[12px] text-ink-muted">
                <Phone className="size-3" />
                {property.twilio_number ? (
                  formatStoopNumber(property.twilio_number)
                ) : (
                  <span className="italic">{NO_NUMBER_TITLE}</span>
                )}
              </p>
              {/* H3 (safety review, #234 PR 4): a null `twilio_number`
                  means tenants texting this property reach NOBODY — there
                  is no emergency line for this building and no
                  re-provision endpoint in the contract. The ported helper
                  already carries the honest wording for both states; a
                  bare "No Stoop number yet" (with its implied "coming
                  soon") was the only thing rendering. */}
              {property.twilio_number ? (
                <p className="mt-1 text-[12px] text-ink-muted">{NUMBER_CAPTION}</p>
              ) : (
                <p className="mt-1 text-[12px] font-medium text-urgent">{NO_NUMBER_BODY}</p>
              )}
              {property.open_case_count > 0 && (
                <p className="mt-1 text-[12px] font-medium text-urgent">
                  {property.open_case_count === 1
                    ? "1 open case at this property."
                    : `${property.open_case_count} open cases at this property.`}
                </p>
              )}
            </div>

            {/* Settings — backup_contact/quiet_hours/house_rules (issue
                #261), the real replacement for the deleted mock "Manage"
                section's Settings link (this file's own docstring). */}
            <section className="px-4 pt-4">
              <h2 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                Settings
              </h2>
              <Link
                to="/app/properties/$id/settings"
                params={{ id }}
                className="flex min-h-11 items-center justify-between gap-4 rounded-2xl border border-border bg-card px-4 py-4 transition hover:border-brand/30"
              >
                <div className="min-w-0">
                  <p className="text-[14px] font-medium text-ink">
                    Backup contact, quiet hours &amp; house rules
                  </p>
                  <p className="mt-0.5 text-[12px] text-ink-muted">
                    {property.backup_contact
                      ? `Backup contact: ${property.backup_contact.name}`
                      : "No backup contact set yet"}
                  </p>
                  {/* M1 (safety review, #261 follow-up): this caption used
                      to name the backup contact without ever checking
                      `.phone` — asserting a redundancy for the emergency
                      chain's second number that may not actually be
                      dialable. */}
                  {backupContactPhoneLooksInvalid(property.backup_contact) && (
                    <p className="mt-0.5 text-[12px] font-medium text-urgent">
                      Their number doesn&rsquo;t look valid — I may not be able to reach them in an
                      emergency.
                    </p>
                  )}
                </div>
                <ChevronRight className="size-4 shrink-0 text-ink-muted/70" />
              </Link>
            </section>

            {/* Tenants */}
            <section className="px-4 pt-4">
              <h2 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                Tenants
              </h2>
              {/* H1 (safety review, #234 PR 4): the error branch below is
                  gated on `!tenantsQuery.data`, and a failed BACKGROUND
                  refetch shows this strip instead of replacing the panel.
                  Blanking it would take the unit numbers and the
                  vulnerable-occupant flags ("On powered medical
                  equipment") off screen because a refresh blipped — the
                  wrong failure direction on the one panel that says which
                  unit needs help first. */}
              {tenantsQuery.isError && tenantsQuery.data && (
                <div
                  role="status"
                  className="mb-2 rounded-2xl border border-border bg-surface px-4 py-2.5 text-[13px] font-medium text-ink-muted"
                >
                  Couldn&apos;t refresh just now — showing the last update.
                </div>
              )}
              <div className="overflow-hidden rounded-2xl border border-border bg-card">
                {tenantsQuery.isPending ? (
                  <div className="flex items-center justify-center px-4 py-8">
                    <Loader2 className="size-5 animate-spin text-brand motion-reduce:animate-none" />
                  </div>
                ) : tenantsQuery.isError && !tenantsQuery.data ? (
                  <p className="px-4 py-5 text-[13px] text-ink-muted">
                    {tenantsQuery.error instanceof ApiError
                      ? toHouseApiError(tenantsQuery.error)
                      : "Couldn't load the tenants here. Try refreshing."}
                  </p>
                ) : tenants.length === 0 ? (
                  // Deliberately SHORTER than mobile's version ("… Add them
                  // so Stoop knows who's texting in.") — copy-guardian
                  // ruling, #234 PR 4: this screen ships no tenant-add UI
                  // yet, and an instruction pointing at an affordance that
                  // doesn't exist here is a dead promise. Restore mobile's
                  // full line when the tenant-add flow lands.
                  <p className="px-4 py-5 text-[13px] text-ink-muted">No tenants on file yet.</p>
                ) : (
                  tenants.map((tenant, i) => (
                    <div key={tenant.id}>
                      {i > 0 && <div className="border-t border-border" />}
                      <div className="flex min-h-14 items-center justify-between gap-3 px-4 py-3">
                        <div className="min-w-0 flex-1">
                          <p className="text-[14px] font-medium text-ink">
                            {/* Fallback via the same seam mobile uses
                                (firstName → "Your tenant") — copy-guardian
                                caught "Unnamed tenant" as an unreviewed
                                drift from that blessed register. Called
                                unconditionally: `tenants.name` is
                                unconstrained nullable text, and `??` alone
                                would let an empty string render a blank
                                name line. */}
                            {firstName(tenant.name)}
                            {tenant.unit ? ` — Unit ${tenant.unit}` : ""}
                          </p>
                          <p className="mt-0.5 font-mono text-[12px] text-ink-muted">
                            {tenant.phone}
                          </p>
                          {tenant.vulnerable_occupant && (
                            <p className="mt-0.5 text-[12px] text-ink-muted">
                              {VULNERABLE_LABELS[tenant.vulnerable_occupant]}
                            </p>
                          )}
                        </div>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </section>

            {/* Trust ladder */}
            <section className="px-4 pt-5">
              <h2 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                {TRUST_SECTION_TITLE}
              </h2>
              <div className="rounded-2xl border border-border bg-card p-4">
                <p className="text-[13px] leading-relaxed text-ink-muted">{TRUST_SECTION_BODY}</p>
                <Button
                  variant="outline"
                  disabled={revokeMutation.isPending}
                  onClick={() => setRevokeConfirmOpen(true)}
                  className="mt-3 h-11 border-border text-ink"
                >
                  {revokeMutation.isPending ? "Turning off…" : "Turn off automatic sending here"}
                </Button>
              </div>
            </section>

            {/* Recent conversations */}
            <section className="px-4 pt-6">
              <h2 className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                Recent conversations
              </h2>
              {recentCasesQuery.isPending ? (
                <div className="flex items-center justify-center rounded-2xl border border-border bg-card py-8">
                  <Loader2 className="size-5 animate-spin text-brand motion-reduce:animate-none" />
                </div>
              ) : recentCasesQuery.isError && recentCases.length === 0 ? (
                <p className="rounded-2xl border border-border bg-card px-4 py-5 text-[13px] text-ink-muted">
                  {recentCasesQuery.error instanceof ApiError
                    ? toHouseApiError(recentCasesQuery.error)
                    : "Couldn't load conversations here. Try refreshing."}
                </p>
              ) : recentCases.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-border bg-canvas/50 px-5 py-8 text-center">
                  <MessageSquare className="mx-auto size-5 text-ink-muted" />
                  <p className="mt-2 text-[13px] text-ink-muted">No conversations yet.</p>
                </div>
              ) : (
                <ul className="space-y-2">
                  {recentCases.map((c) => (
                    <li key={c.id}>
                      <Link
                        to="/app/conversations/$id"
                        params={{ id: c.id }}
                        className="block rounded-2xl border border-border bg-card p-4 transition hover:border-brand/30"
                      >
                        <div className="flex items-center gap-2">
                          <span className="font-display text-[15px] text-ink">
                            {firstName(c.tenant_name)}
                          </span>
                          {c.severity && <SeverityBadge severity={c.severity} />}
                        </div>
                        <p className="mt-1 line-clamp-2 text-[13px] text-ink-muted">
                          {c.title ?? "No summary yet."}
                        </p>
                        <p className="mt-1 font-mono text-[10px] uppercase tracking-widest text-ink-muted">
                          {formatRelativeTime(c.last_activity_at)}
                        </p>
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* Danger zone */}
            <section className="mt-6 px-4">
              <button
                type="button"
                onClick={() => setDeleteConfirmOpen(true)}
                disabled={deleteMutation.isPending}
                className="inline-flex min-h-11 items-center text-[14px] font-medium text-destructive disabled:opacity-60"
              >
                {deleteMutation.isPending ? "Deleting…" : "Delete this property"}
              </button>
              <p className="mt-1 text-[12px] text-ink-muted">
                Deleting is permanent. Its number is released after a 24-hour hold, and open cases
                or saved history will block the delete.
              </p>
            </section>
          </>
        ) : null}
      </div>

      <AppTabBar active="properties" queueCount={queueQuery.data?.counts.total ?? 0} />

      <AlertDialog open={revokeConfirmOpen} onOpenChange={setRevokeConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="font-display">{revokeCopy.title}</AlertDialogTitle>
            <AlertDialogDescription>{revokeCopy.message}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revokeMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={revokeMutation.isPending}
              onClick={(e) => {
                e.preventDefault();
                revokeMutation.mutate();
              }}
            >
              {revokeMutation.isPending ? "Turning off…" : revokeCopy.confirmLabel}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="font-display">{DELETE_PROPERTY_TITLE}</AlertDialogTitle>
            <AlertDialogDescription>
              {deletePropertyMessage({
                tenantCount: allTenantCount,
                openCaseCount: property?.open_case_count ?? 0,
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleteMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={(e) => {
                e.preventDefault();
                deleteMutation.mutate();
              }}
            >
              {deleteMutation.isPending ? "Deleting…" : DELETE_PROPERTY_CONFIRM_LABEL}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PhoneFrame>
  );
}
