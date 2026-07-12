import { cn } from "@/lib/utils";
import { TimestampChip } from "./TimestampChip";

interface DayDividerProps {
  children: string;
  className?: string;
}

/**
 * The conversation thread's day-group divider — a mono uppercase stamp
 * between two rule lines (docs/mockups/07-clarity-redesign.html
 * `.day-stamp`), reusing the same `TimestampChip` stamp material as
 * every other timestamp in Clarity rather than inventing a new one.
 */
export function DayDivider({ children, className }: DayDividerProps) {
  return (
    <div
      role="separator"
      aria-orientation="horizontal"
      className={cn("my-4 flex items-center gap-2.5", className)}
    >
      <span className="h-px flex-1 bg-clarity-line-strong" aria-hidden="true" />
      <TimestampChip>{children}</TimestampChip>
      <span className="h-px flex-1 bg-clarity-line-strong" aria-hidden="true" />
    </div>
  );
}
