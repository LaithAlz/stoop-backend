import { Link } from "@tanstack/react-router";
import { cn } from "@/lib/utils";
import type { CaseSummary } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { formatRelativeTime } from "@/lib/relativeTime";
import { SeverityPlaque } from "./SeverityPlaque";
import { TimestampChip } from "./TimestampChip";

interface ConversationRowProps {
  item: CaseSummary;
  className?: string;
}

/**
 * One row per case on the Conversations tab — tenant name, property, the
 * agent-written case title as a one-line snippet, and the severity plaque
 * while the case still has one. A simplified, list-row take on
 * docs/mockups/07-clarity-redesign.html's `.entry` head — this screen
 * isn't in the mockup itself (added per the Tab IA decision, 2026-07-06),
 * so it reuses the same enamel-plaque and stamp material rather than
 * inventing new treatment.
 *
 * Wired to `GET /v1/cases`'s `CaseSummary` shape (campaign issue #234
 * PR 3, replacing src/lib/mock-app.ts's `QueueItem`) — `CaseSummary` has
 * no `tenant_message` field (that's a `GET /v1/queue`-only field), so the
 * snippet reads `title` instead, same as apps/mobile's own port of this
 * component.
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
          {firstName(item.tenant_name)}
          <span className="font-semibold text-clarity-ink-dim">, {item.property_label}</span>
        </p>
        {item.severity && (
          <SeverityPlaque severity={item.severity} size="sm" className="shrink-0" />
        )}
      </div>
      <p className="line-clamp-2 font-clarity-sans text-[13.5px] leading-snug text-clarity-ink-dim">
        {item.title ?? "No summary yet."}
      </p>
      <TimestampChip className="self-start">
        {formatRelativeTime(item.last_activity_at)}
      </TimestampChip>
    </Link>
  );
}
