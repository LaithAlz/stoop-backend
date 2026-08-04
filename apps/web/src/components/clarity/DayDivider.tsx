import { cn } from "@/lib/utils";
import { TimestampChip } from "./TimestampChip";

interface DayDividerProps {
  children: string;
  className?: string;
}

/** #191 F9 (safety review follow-up): converts the already-all-caps label
 *  (`formatDayLabel`, src/lib/relativeTime.ts, shared with apps/mobile and
 *  not changed here) to sentence case before it reaches the DOM. VoiceOver,
 *  and some other assistive tech, read a short string that's literal
 *  ALL-CAPS in the markup as if it were an acronym, spelling "TODAY" out
 *  letter by letter instead of saying the word. `TimestampChip`'s own CSS
 *  `uppercase` still gives the same all-caps "stamp" look on screen; a CSS
 *  text-transform doesn't change the underlying text a screen reader reads
 *  from the DOM node the way a literal uppercase source string does. */
function toReadableCase(label: string): string {
  return label.toLowerCase().replace(/(^|\s)\S/g, (match) => match.toUpperCase());
}

/**
 * The conversation thread's day-group divider, a mono uppercase stamp
 * between two rule lines (docs/mockups/07-clarity-redesign.html
 * `.day-stamp`), reusing the same `TimestampChip` stamp material as
 * every other timestamp in Clarity rather than inventing a new one.
 */
export function DayDivider({ children, className }: DayDividerProps) {
  // #191 item 3 / F9 (safety review follow-up): this used to carry
  // `role="separator"` with an `aria-orientation` and an `aria-label`.
  // Named separators are announced inconsistently across assistive tech
  // (VoiceOver especially), so a screen-reader user could still miss the
  // date depending on which one they run. Plain content that a screen
  // reader reads in normal document order when it passes through doesn't
  // depend on that role being interpreted correctly at all.
  return (
    <div className={cn("my-4 flex items-center gap-2.5", className)}>
      <span className="h-px flex-1 bg-clarity-line-strong" aria-hidden="true" />
      <TimestampChip>{toReadableCase(children)}</TimestampChip>
      <span className="h-px flex-1 bg-clarity-line-strong" aria-hidden="true" />
    </div>
  );
}
