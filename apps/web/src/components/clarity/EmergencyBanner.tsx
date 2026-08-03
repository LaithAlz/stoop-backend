import { Link } from "@tanstack/react-router";
import { TriangleAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  EMERGENCY_ACK_LABEL,
  EMERGENCY_ACK_PENDING_LABEL,
  EMERGENCY_ACK_SUBLABEL,
} from "@/features/emergency/emergencyBanner";

interface EmergencyBannerProps {
  /** Optional — when absent the banner renders as a plain, non-navigating
   *  block instead of a `<Link>`. Every current caller passes one: Home
   *  (src/routes/app.index.tsx, `case_id`) and the conversation thread
   *  (src/routes/app.conversations.$id.tsx, the case's own `id`) both link
   *  to the live `/app/conversations/$id/emergency` route (campaign issue
   *  #234 PR 3). Kept optional rather than required for the rare case a
   *  future caller renders this informationally only (no known caller does
   *  today). */
  conversationId?: string;
  headline: string;
  /** Optional — a caller with nothing more specific than the headline's
   *  own fallback (which already carries the property label) can omit
   *  this rather than repeat it. */
  subtext?: string;
  /** The tenant's own words, rendered inside the banner — the "{name}
   *  said" block gives a banner something to say beyond "someone needs
   *  you now" even when it's also a `<Link>`. Same "{name} said" label
   *  pattern as DecisionCard's tenant-message block. */
  tenantFirstName?: string;
  tenantMessage?: string;
  /** Present only when this card carries a non-null `notification_id`
   *  (api-contracts.md Queue v1.15 amendment) — omit entirely to render
   *  the plain informational banner, e.g. from the conversation thread
   *  (src/routes/app.conversations.$id.tsx), which has no ack surface at
   *  all (`GET /v1/cases/{id}` carries no `notification_id` — v1.15
   *  amendment scopes that field to the queue card only). */
  onAcknowledge?: () => void;
  /** True only while THIS banner's own ack call is in flight — the caller
   *  scopes this per notification id (src/features/emergency/
   *  useAcknowledge.ts's `isAcknowledging`), never a global "something is
   *  acking" flag. */
  acknowledging?: boolean;
  className?: string;
}

/**
 * The one thing on Home that's never buried below the fold — with a
 * `conversationId`, links straight into the emergency takeover
 * (docs/mockups/07 `.em-banner`); without one it's informational + ack
 * only (see the prop's own comment).
 * Rule #1: the emergency line is never paywalled, throttled, or gated, so
 * this banner has no dismiss control. `onAcknowledge` (v1.15 amendment,
 * ported from apps/mobile's #237) is a deliberate, labeled action a
 * landlord must tap on purpose — never a swipe/close gesture on the
 * banner itself — and is a SEPARATE element from the navigation row
 * below, never nested inside the `<Link>` (a `<button>` inside an `<a>`
 * is invalid HTML and breaks screen-reader/keyboard navigation).
 */
export function EmergencyBanner({
  conversationId,
  headline,
  subtext,
  tenantFirstName,
  tenantMessage,
  onAcknowledge,
  acknowledging = false,
  className,
}: EmergencyBannerProps) {
  const content = (
    <>
      <TriangleAlert className="size-[22px] shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <strong className="block font-clarity-serif text-[15px] font-bold leading-snug">
          {headline}
        </strong>
        {subtext && (
          <span className="mt-0.5 block font-clarity-sans text-xs font-semibold opacity-90">
            {subtext}
          </span>
        )}
      </span>
      <span
        className="ml-auto size-2 shrink-0 animate-pulse rounded-full bg-white motion-reduce:animate-none"
        aria-hidden="true"
      />
    </>
  );

  return (
    <div
      className={cn(
        "clarity-emergency-gradient mb-4 overflow-hidden rounded-clarity-md border border-black/25 text-clarity-emergency-ink shadow-clarity-banner",
        className,
      )}
    >
      {conversationId ? (
        <Link
          to="/app/conversations/$id/emergency"
          params={{ id: conversationId }}
          className="flex items-center gap-3 px-4 py-3.5 no-underline"
        >
          {content}
        </Link>
      ) : (
        <div className="flex items-center gap-3 px-4 py-3.5">{content}</div>
      )}
      {tenantMessage && (
        <div className="border-t border-black/15 px-4 py-3">
          {tenantFirstName && (
            <span className="mb-1 block font-clarity-sans text-[11px] font-bold uppercase tracking-[0.02em] opacity-85">
              {tenantFirstName} said
            </span>
          )}
          <p className="font-clarity-sans text-[14px] font-semibold leading-relaxed">
            {tenantMessage}
          </p>
        </div>
      )}
      {onAcknowledge && (
        <button
          type="button"
          onClick={onAcknowledge}
          disabled={acknowledging}
          aria-busy={acknowledging}
          className="flex min-h-11 w-full flex-col items-start gap-0.5 border-t border-black/15 px-4 py-2.5 text-left font-clarity-sans transition-colors duration-150 ease-clarity hover:bg-black/5 disabled:opacity-70 motion-reduce:transition-none"
        >
          <span className="text-[14px] font-extrabold uppercase tracking-[0.02em]">
            {acknowledging ? EMERGENCY_ACK_PENDING_LABEL : EMERGENCY_ACK_LABEL}
          </span>
          <span className="text-xs font-semibold opacity-85">{EMERGENCY_ACK_SUBLABEL}</span>
        </button>
      )}
    </div>
  );
}
