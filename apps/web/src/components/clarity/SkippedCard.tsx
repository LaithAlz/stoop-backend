import { Link } from "@tanstack/react-router";
import { cn } from "@/lib/utils";
import { TimestampChip } from "./TimestampChip";

interface SkippedCardProps {
  /** B3 (safety review, #234 PR 2): optional — when absent the card is a
   *  plain, non-navigating block. Home omits it while the conversation
   *  routes still read mock-app.ts (a live case UUID would 404 there);
   *  PR 3 restores the link. */
  conversationId?: string;
  tenantName: string;
  propertyLabel: string;
  timestamp: string;
  className?: string;
}

/**
 * What a skipped draft becomes. Skip dismisses the draft, not the case —
 * the card collapses into this muted, de-emphasized "waiting" state
 * instead of vanishing (founder decision, 2026-07-06). With a
 * `conversationId`, tapping it still opens the full conversation; there's
 * no action button here because there's nothing queued to approve anymore.
 */
export function SkippedCard({
  conversationId,
  tenantName,
  propertyLabel,
  timestamp,
  className,
}: SkippedCardProps) {
  const body = (
    <>
      <span className="min-w-0 flex-1 font-clarity-sans text-[13.5px] leading-snug text-clarity-ink-dim">
        <b className="block font-bold text-clarity-ink">
          {tenantName} — {propertyLabel}
        </b>
        No reply sent — case still open
      </span>
      <TimestampChip className="shrink-0 border-clarity-line-strong bg-transparent text-clarity-ink-dim">
        {timestamp}
      </TimestampChip>
    </>
  );

  const frame = cn(
    "flex min-h-11 items-center justify-between gap-3 rounded-clarity-lg border border-dashed border-clarity-line-strong bg-clarity-bg px-[18px] py-3.5 opacity-75",
    className,
  );

  if (!conversationId) {
    return <div className={frame}>{body}</div>;
  }

  return (
    <Link
      to="/app/conversations/$id"
      params={{ id: conversationId }}
      className={cn(
        frame,
        "no-underline transition-opacity duration-150 ease-clarity hover:opacity-100 motion-reduce:transition-none",
      )}
    >
      {body}
    </Link>
  );
}
