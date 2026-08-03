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
import type { TimelineEntry, TimelineMessageEntry } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { formatRelativeTime } from "@/lib/relativeTime";
import { emergencyHeadline } from "@/features/emergency/emergencyBanner";

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
 * non-null — the OLD mock always showed "Dispatch Mike's Plumbing (24/7)"
 * regardless of whether any vendor was actually engaged; that was
 * fabricated. `CaseDetailVendor` (api-contracts.md's Cases section, v1.16
 * amendment) has no `working_hours` field either, so this never claims a
 * vendor is available "24/7".
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
  const latestStoopReply = useMemo(
    () =>
      caseDetail
        ? latestMessage(caseDetail.timeline, (m) => m.direction === "outbound")
        : undefined,
    [caseDetail],
  );

  const tenantFirst = firstName(caseDetail?.tenant.name);
  // `CaseDetailTenant.phone`/`CaseDetailVendor.phone` are typed optional
  // (src/api/types.ts's own comment: a best-effort read of an undocumented
  // GET shape, api-contracts.md's v1.16 amendment) even though the schema
  // itself has both as NOT NULL in practice — guarded defensively rather
  // than asserted.
  const tenantPhone = caseDetail?.tenant.phone;
  const tenantPhoneDigits = tenantPhone ? tenantPhone.replace(/\D/g, "") : "";
  const vendorPhone = caseDetail?.vendor?.phone;
  const vendorPhoneDigits = vendorPhone ? vendorPhone.replace(/\D/g, "") : "";
  const isStillActiveEmergency = caseDetail
    ? caseDetail.severity === "emergency" && caseDetail.status !== "resolved"
    : false;

  return (
    <PhoneFrame tone="dark">
      <div className="flex flex-1 flex-col text-white" style={{ backgroundColor: "#0f1311" }}>
        <header className="bg-emergency px-5 pb-6 pt-4">
          <div className="flex items-center justify-between">
            <Link
              to="/app/conversations/$id"
              params={{ id }}
              aria-label="Back to conversation"
              className="inline-flex size-11 items-center justify-center rounded-full bg-white/15 text-white hover:bg-white/25"
            >
              <ArrowLeft className="size-5" aria-hidden="true" />
            </Link>
            <span className="inline-flex items-center gap-2 rounded-full bg-white/15 px-3 py-1.5 text-[11px] font-bold uppercase tracking-widest text-white">
              <AlertOctagon className="size-3.5" aria-hidden="true" />
              <span className="inline-flex size-2 animate-pulse rounded-full bg-white motion-reduce:animate-none" />
              Emergency{caseDetail ? ` · ${formatRelativeTime(caseDetail.opened_at)}` : ""}
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

              <a
                href={`tel:+${tenantPhoneDigits}`}
                className="mt-5 flex items-center justify-between rounded-2xl bg-white/15 px-4 py-3 text-white hover:bg-white/25"
              >
                <span>
                  <span className="block text-[11px] font-bold uppercase tracking-widest text-white/80">
                    Tenant
                  </span>
                  <span className="block text-base font-bold">
                    {tenantFirst} · {tenantPhone ?? "no phone on file"}
                  </span>
                </span>
                <span className="inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-widest">
                  <Phone className="size-4" aria-hidden="true" />
                  Tap to call
                </span>
              </a>
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
          ) : caseQuery.isError && !caseDetail ? (
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
          ) : caseDetail ? (
            <>
              {!isStillActiveEmergency && (
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
          ) : null}
        </main>

        {caseDetail && (
          <div className="space-y-2 border-t border-white/10 bg-black/30 p-4 pb-6">
            <a
              href={`tel:+${tenantPhoneDigits}`}
              className="flex min-h-[60px] w-full items-center justify-center gap-2 rounded-2xl bg-white text-lg font-bold uppercase tracking-wide text-ink hover:bg-white/95"
            >
              <Phone className="size-5" aria-hidden="true" />
              Call {tenantFirst} now
            </a>
            <a
              href={`sms:+${tenantPhoneDigits}`}
              className="flex min-h-[60px] w-full items-center justify-center gap-2 rounded-2xl border border-white/20 bg-white/[0.06] text-base font-bold text-white hover:bg-white/15"
            >
              <MessageSquare className="size-5" aria-hidden="true" />
              Text {tenantFirst}
            </a>
            {caseDetail.vendor && (
              <a
                href={`tel:+${vendorPhoneDigits}`}
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
