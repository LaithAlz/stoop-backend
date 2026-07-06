import { Link } from "@tanstack/react-router";
import { cn } from "@/lib/utils";
import type { QueueItem } from "@/lib/mock-app";
import { SeverityPlaque } from "./SeverityPlaque";
import { TimestampChip } from "./TimestampChip";

interface ConversationRowProps {
  item: QueueItem;
  className?: string;
}

/**
 * One row per tenant thread on the Conversations tab — tenant name,
 * property, a one-line snippet of the last message, and the severity
 * plaque while the case is still open. A simplified, list-row take on
 * docs/mockups/07-clarity-redesign.html's `.entry` head — this screen
 * isn't in the mockup itself (added per the Tab IA decision, 2026-07-06),
 * so it reuses the same enamel-plaque and stamp material rather than
 * inventing new treatment.
 */
export function ConversationRow({ item, className }: ConversationRowProps) {
  return (
    <Link
      to="/app/conversations/$id"
      params={{ id: item.id }}
      className={cn(
        "flex flex-col gap-2 rounded-clarity-lg border border-clarity-line-strong bg-clarity-surface p-4 no-underline shadow-clarity-card transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-2.5">
        <p className="min-w-0 flex-1 truncate font-clarity-sans text-[15px] font-bold text-clarity-ink">
          {item.tenantFirst}{" "}
          <span className="font-semibold text-clarity-ink-dim">— {item.propertyLabel}</span>
        </p>
        <SeverityPlaque severity={item.severity} size="sm" className="shrink-0" />
      </div>
      <p className="line-clamp-2 font-clarity-sans text-[13.5px] leading-snug text-clarity-ink-dim">
        {item.tenantMessage}
      </p>
      <TimestampChip className="self-start">{item.receivedAgo}</TimestampChip>
    </Link>
  );
}
