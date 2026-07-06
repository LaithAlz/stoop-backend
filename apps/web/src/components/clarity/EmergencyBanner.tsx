import { Link } from "@tanstack/react-router";
import { TriangleAlert } from "lucide-react";
import { cn } from "@/lib/utils";

interface EmergencyBannerProps {
  conversationId: string;
  headline: string;
  subtext: string;
  className?: string;
}

/**
 * The one thing on Home that's never buried below the fold — links
 * straight into the emergency takeover (docs/mockups/07 `.em-banner`).
 * Rule #1: the emergency line is never paywalled, throttled, or gated,
 * so this banner has no dismiss control and no "later" affordance.
 */
export function EmergencyBanner({
  conversationId,
  headline,
  subtext,
  className,
}: EmergencyBannerProps) {
  return (
    <Link
      to="/app/conversations/$id/emergency"
      params={{ id: conversationId }}
      className={cn(
        "clarity-emergency-gradient mb-4 flex items-center gap-3 rounded-clarity-md border border-black/25 px-4 py-3.5 text-clarity-emergency-ink no-underline shadow-clarity-banner",
        className,
      )}
    >
      <TriangleAlert className="size-[22px] shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <strong className="block font-clarity-serif text-[15px] font-bold leading-snug">
          {headline}
        </strong>
        <span className="mt-0.5 block font-clarity-sans text-xs font-semibold opacity-90">
          {subtext}
        </span>
      </span>
      <span
        className="ml-auto size-2 shrink-0 animate-pulse rounded-full bg-white motion-reduce:animate-none"
        aria-hidden="true"
      />
    </Link>
  );
}
