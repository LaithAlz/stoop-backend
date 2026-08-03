import { createFileRoute, Link } from "@tanstack/react-router";
import { useMemo } from "react";
import {
  ArrowLeft,
  AlertOctagon,
  Loader2,
  Phone,
  MessageSquare,
  Wrench,
  Image as ImageIcon,
} from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { useAuth } from "@/auth/AuthProvider";
import { useCase } from "@/api/cases";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { TimelineEntry, TimelineMessageEntry, VulnerableOccupant } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { formatRelativeTime } from "@/lib/relativeTime";
import { emergencyHeadline } from "@/features/emergency/emergencyBanner";
import { hasEmergencyTrigger } from "@/features/cases/emergencySignal";

export const Route = createFileRoute("/app/conversations/$id_/emergency")({
  head: () => ({
    meta: [{ title: "Emergency — Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: EmergencyPage,
});

function latestMessage(
  timeline: TimelineEntry[],
  predicate: (entry: TimelineMessageEntry) => boolean,
): TimelineMessageEntry | undefined {
  let found: TimelineMessageEntry | undefined;
  for (const entry of timeline) {
    if (entry.kind === "message" && predicate(entry)) found = entry;
  }
  return found;
}

/** M3 (safety review, #234 PR 3 fix round): same blessed wording as
 *  apps/mobile's TenantFormModal.tsx `VULNERABLE_OPTIONS` — the ONE field
 *  that can change a 2am drive-over-vs-call-911 decision, rendered here
 *  verbatim, no invented severity language wrapped around it. */
const VULNERABLE_LABELS: Record<VulnerableOccupant, string> = {
  infant: "An infant",
  elderly: "An elderly person",
  medical_device: "On powered medical equipment",
};

/**
 * The emergency takeover — the one screen dark styling is allowed on
 * (per this app's design contract). Wired to `GET /v1/cases/{id}`
 * (campaign issue #234 PR 3, replacing src/lib/mock-app.ts's hardcoded
 * "Active ceiling flood above the bedroom…" copy — api-contracts.md's
 * Queue section, v1.1 amendment: "`title` is the emergency-banner
 * headline… the dashboard must not hardcode incident wording (PR #181
 * shipped a hardcoded 'reported a flood' once; never again)"). Every field
 * below comes from the case's own real data — no invented situation
 * details, no invented vendor.
 *
 * The vendor dispatch action only renders when `caseDetail.vendor` is
 * non-null AND has a phone on file — the OLD mock always showed "Dispatch
 * Mike's Plumbing (24/7)" regardless of whether any vendor was actually
 * engaged; that was fabricated. `CaseDetailVendor` (api-contracts.md's
 * Cases section, v1.16 amendment) has no `working_hours` field either, so
 * this never claims a vendor is available "24/7".
 */
function EmergencyPage() {
  const { id } = Route.useParams();
  const { session } = useAuth();
  const caseQuery = useCase(id, { enabled: Boolean(session) });
  const caseDetail = caseQuery.data;

  const latestTenantMessage = useMemo(
    () =>
      caseDetail
        ? latestMessage(
            caseDetail.timeline,
            (m) => m.direction === "inbound" && m.party === "tenant",
          )
        : undefined,
    [caseDetail],
  );
  // M2 (safety review, #234 PR 3 fix round): "outbound" alone also matches
  // a reply Stoop sent to a VENDOR (`drafts.recipient`, schema-v1.md) —
  // showing that under "Stoop already replied" here would read as if the
  // TENANT got the safety instructions, when they may not have. Scoped to
  // `party === "tenant"` so this only ever shows what the tenant was
  // actually sent.
  const latestStoopReply = useMemo(
    () =>
      caseDetail
        ? latestMessage(
            caseDetail.timeline,
            (m) => m.direction === "outbound" && m.party === "tenant",
          )
        : undefined,
    [caseDetail],
  );

  const tenantFirst = firstName(caseDetail?.tenant.name);
  // H2 (safety review, #234 PR 3 fix round): the old `.replace(/\D/g, "")`
  // + `tel:+${digits}` synthesized a country code onto whatever the
  // landlord typed for `tenants.phone` (never E.164-normalized anywhere —
  // #232) — a landlord-typed "416-555-0134" became `tel:+4165550134`, a
  // dead tone at 2am. The stored string is passed through to `tel:`/`sms:`
  // as-is instead; a browser/OS dialer already understands a plain
  // 10-digit local number exactly as well as it understands a mistakenly
  // "+"-prefixed one, and this way we never invent digits that aren't
  // actually on file.
  const tenantPhone = caseDetail?.tenant.phone?.trim() || undefined;
  const vendorPhone = caseDetail?.vendor?.phone?.trim() || undefined;
  // N3 (safety re-verify): `#`/`?` inside a stored phone ("416-555-0134
  // #12") would start a URI fragment/query and silently truncate the
  // href. Strip ONLY those two — not encodeURIComponent, which escapes
  // `+` to `%2B` and some dialers mishandle that. Display keeps the raw
  // string; only the href is sanitized.
  const telHref = (phone: string) => `tel:${phone.replace(/[#?]/g, "")}`;
  const smsHref = (phone: string) => `sms:${phone.replace(/[#?]/g, "")}`;
  const vulnerableLabel = caseDetail?.tenant.vulnerable_occupant
    ? VULNERABLE_LABELS[caseDetail.tenant.vulnerable_occupant]
    : null;

  // H1 (safety review, #234 PR 3 fix round — verified backend-side): a
  // `null` severity is NOT proof this was never an emergency — it's the
  // state before `classify_severity` runs, and the permanent state in
  // degraded mode when classification fails outright; the case row itself
  // (and Tier-0's phone/SMS chain) can exist before severity is ever
  // written at all (emergency-prefilter.md). Reading `severity === null`
  // as "stood down" would show "This case isn't an active emergency
  // anymore." on a LIVE Tier-0 fire while the landlord's phone is still
  // ringing — the client must never re-introduce a de-escalation the
  // backend itself doesn't perform. `null` is therefore clamped to
  // "still active" here, full stop — this screen has nothing softer to
  // fall back to display than the takeover chrome itself.
  // Round-3 residual: the `emergency_triggered` audit row is honored here
  // too — a case whose written severity ended up BELOW emergency despite a
  // Tier-0 trigger means the backend's own never-de-escalate clamp failed
  // (see src/features/cases/emergencySignal.ts); this screen must fail
  // toward the alarm, never toward "stood down".
  const activeEmergency = caseDetail
    ? caseDetail.status !== "resolved" &&
      (caseDetail.severity === "emergency" ||
        caseDetail.severity === null ||
        hasEmergencyTrigger(caseDetail))
    : false;

  return (
    <PhoneFrame tone="dark">
      <div className="flex flex-1 flex-col text-white" style={{ backgroundColor: "#0f1311" }}>
        <header className="bg-emergency px-5 pb-6 pt-4">
          <div className="flex items-center justify-between">
            {/* LOW (safety review, #234 PR 3 fix round): this used to
                point at the conversation thread — but the "Stop the
                calls" acknowledge button lives on Home's banner
                (src/routes/app.index.tsx; GET /v1/cases/{id} carries no
                notification_id to ack against, so the thread has none
                either). Sending the landlord to the thread first is one
                extra tap AWAY from the one action that silences the
                escalation chain; Home is the shorter path back to it. */}
            <Link
              to="/app"
              aria-label="Back to Home"
              className="inline-flex size-11 items-center justify-center rounded-full bg-white/15 text-white hover:bg-white/25"
            >
              <ArrowLeft className="size-5" aria-hidden="true" />
            </Link>
            <span className="inline-flex items-center gap-2 rounded-full bg-white/15 px-3 py-1.5 text-[11px] font-bold uppercase tracking-widest text-white">
              <AlertOctagon className="size-3.5" aria-hidden="true" />
              <span className="inline-flex size-2 animate-pulse rounded-full bg-white motion-reduce:animate-none" />
              {/* M1 (safety review, #234 PR 3 fix round): `opened_at` is
                  when the CASE opened, not when this escalation fired — a
                  case opened 3 days ago that just escalated read
                  "Emergency · 3d ago" instead of reflecting the actual
                  trigger. The latest real tenant message is the closest
                  honest signal this payload carries for "when". */}
              Emergency
              {caseDetail
                ? ` · ${formatRelativeTime(latestTenantMessage?.at ?? caseDetail.opened_at)}`
                : ""}
            </span>
            <span className="w-11" />
          </div>

          {caseDetail && (
            <>
              <div className="mt-5">
                <p className="text-[11px] font-bold uppercase tracking-widest text-white/80">
                  Property
                </p>
                <h1 className="mt-1 font-display text-[26px] font-bold leading-tight tracking-tight text-white">
                  {caseDetail.property.address_line1}
                  {caseDetail.tenant.unit ? `, Unit ${caseDetail.tenant.unit}` : ""}
                  <br />
                  {caseDetail.property.city}
                </h1>
              </div>

              {tenantPhone ? (
                <a
                  href={telHref(tenantPhone)}
                  className="mt-5 flex items-center justify-between rounded-2xl bg-white/15 px-4 py-3 text-white hover:bg-white/25"
                >
                  <span>
                    <span className="block text-[11px] font-bold uppercase tracking-widest text-white/80">
                      Tenant
                    </span>
                    <span className="block text-base font-bold">
                      {tenantFirst} · {tenantPhone}
                    </span>
                  </span>
                  <span className="inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-widest">
                    <Phone className="size-4" aria-hidden="true" />
                    Tap to call
                  </span>
                </a>
              ) : (
                <div className="mt-5 rounded-2xl bg-white/15 px-4 py-3 text-white">
                  <span className="block text-[11px] font-bold uppercase tracking-widest text-white/80">
                    Tenant
                  </span>
                  <span className="block text-base font-bold">
                    {tenantFirst} · no phone on file
                  </span>
                </div>
              )}

              {vulnerableLabel && (
                // N5 (safety re-verify): the bare form-option label ("An
                // infant") directly under the tenant line read as
                // describing the tenant — the lead-in anchors it as
                // who's-in-the-unit data the landlord entered.
                <p className="mt-3 text-sm font-bold text-white">
                  <span className="mr-2 text-[11px] font-bold uppercase tracking-widest text-white/80">
                    In the unit
                  </span>
                  {vulnerableLabel}
                </p>
              )}
            </>
          )}
        </header>

        <main className="flex-1 overflow-y-auto px-5 py-6">
          {caseQuery.isPending ? (
            <div
              role="status"
              aria-live="polite"
              className="flex flex-col items-center gap-3 py-16 text-center"
            >
              <Loader2
                className="size-6 animate-spin text-white motion-reduce:animate-none"
                aria-hidden="true"
              />
              <p className="text-sm font-medium text-white/80">Loading this emergency…</p>
            </div>
          ) : !caseDetail ? (
            // H3 (safety review, #234 PR 3 fix round): belt-and-braces —
            // renders the SAME error block for a genuine fetch error and
            // for the "not pending, not error, but still no data" gap a
            // fixed src/api/client.ts should no longer produce. Previously
            // this fell through to a bare `null` — a blank dark screen on
            // the one screen that can least afford to render nothing.
            <div role="alert" className="flex flex-col items-center gap-3 py-16 text-center">
              <p className="text-sm font-medium text-white/80">
                {caseQuery.error instanceof ApiError
                  ? toHouseApiError(caseQuery.error)
                  : "Couldn't load this alert. Try again."}
              </p>
              <button
                type="button"
                onClick={() => void caseQuery.refetch()}
                className="mt-2 inline-flex h-12 items-center rounded-xl bg-white px-5 text-sm font-bold text-ink"
              >
                Try again
              </button>
            </div>
          ) : (
            <>
              {/* N6 (safety re-verify): same non-destructive refresh strip
                  the thread and list carry — a failing background refetch
                  on THIS screen must not show stale data silently. */}
              {caseQuery.isError && (
                <div
                  role="status"
                  className="mb-4 rounded-2xl border border-white/15 bg-white/[0.04] px-4 py-2.5 text-[13px] font-semibold text-white/80"
                >
                  Couldn&apos;t refresh just now — showing the last update.
                </div>
              )}
              {!activeEmergency && (
                <div className="mb-6 rounded-2xl border border-white/15 bg-white/[0.04] p-4">
                  <p className="text-sm leading-relaxed text-white/90">
                    This case isn&rsquo;t an active emergency anymore. The details below are what
                    Stoop had on file at the time.
                  </p>
                </div>
              )}

              <p className="text-[11px] font-bold uppercase tracking-widest text-white/60">
                Situation
              </p>
              <p className="mt-2 font-display text-2xl font-bold leading-snug text-white">
                {emergencyHeadline({
                  title: caseDetail.title,
                  tenant_name: caseDetail.tenant.name ?? "",
                  property_label: caseDetail.property.label,
                })}
              </p>

              {latestTenantMessage && (
                <div className="mt-6 rounded-2xl border border-white/15 bg-white/[0.04] p-4">
                  <p className="text-[10px] font-bold uppercase tracking-widest text-white/60">
                    From {tenantFirst} · {formatRelativeTime(latestTenantMessage.at)}
                  </p>
                  <p className="mt-2 text-[17px] leading-relaxed text-white">
                    &ldquo;{latestTenantMessage.body}&rdquo;
                  </p>
                </div>
              )}

              {latestTenantMessage && latestTenantMessage.media.length > 0 && (
                <div className="mt-4">
                  <div className="flex aspect-[4/3] w-full items-center justify-center rounded-2xl border border-white/15 bg-white/[0.04] text-white/40">
                    <ImageIcon className="size-10" aria-hidden="true" />
                    <span className="sr-only">Photo from {tenantFirst}</span>
                  </div>
                  <p className="mt-2 font-mono text-[10px] font-medium uppercase tracking-widest text-white/50">
                    Photo attached · {formatRelativeTime(latestTenantMessage.at)}
                  </p>
                </div>
              )}

              {latestStoopReply && (
                <div className="mt-6 rounded-2xl border border-white/10 bg-white/[0.04] p-4">
                  <p className="text-[10px] font-bold uppercase tracking-widest text-white/60">
                    Stoop already replied
                  </p>
                  <p className="mt-2 text-sm leading-relaxed text-white/90">
                    &ldquo;{latestStoopReply.body}&rdquo;
                  </p>
                </div>
              )}
            </>
          )}
        </main>

        {caseDetail && (
          <div className="space-y-2 border-t border-white/10 bg-black/30 p-4 pb-6">
            {tenantPhone ? (
              <>
                <a
                  href={telHref(tenantPhone)}
                  className="flex min-h-[60px] w-full items-center justify-center gap-2 rounded-2xl bg-white text-lg font-bold uppercase tracking-wide text-ink hover:bg-white/95"
                >
                  <Phone className="size-5" aria-hidden="true" />
                  Call {tenantFirst} now
                </a>
                <a
                  href={smsHref(tenantPhone)}
                  className="flex min-h-[60px] w-full items-center justify-center gap-2 rounded-2xl border border-white/20 bg-white/[0.06] text-base font-bold text-white hover:bg-white/15"
                >
                  <MessageSquare className="size-5" aria-hidden="true" />
                  Text {tenantFirst}
                </a>
              </>
            ) : (
              // H2 (safety review, #234 PR 3 fix round): no phone on file
              // — a full-prominence "Call {first} now" with href="tel:+"
              // was a silent dead tap. Plain, honest line instead of a
              // broken-looking action.
              <p className="flex min-h-[60px] w-full items-center justify-center rounded-2xl border border-white/20 bg-white/[0.06] px-4 text-center text-sm font-semibold text-white/80">
                No phone on file for {tenantFirst} — open the conversation to reply.
              </p>
            )}
            {caseDetail.vendor && vendorPhone && (
              <a
                href={telHref(vendorPhone)}
                className="flex min-h-[60px] w-full items-center justify-center gap-2 rounded-2xl border border-white/20 bg-white/[0.06] text-base font-bold text-white hover:bg-white/15"
              >
                <Wrench className="size-5" aria-hidden="true" />
                Call {caseDetail.vendor.name ?? "the vendor"}
                {caseDetail.vendor.trade ? ` — ${caseDetail.vendor.trade}` : ""}
              </a>
            )}
          </div>
        )}
      </div>
    </PhoneFrame>
  );
}
