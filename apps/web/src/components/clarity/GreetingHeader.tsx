import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** "Good morning / afternoon / evening" — same three-way split the mockup's
 * static "Good morning, Laith." implies for a screen you open all day. */
function timeOfDayGreeting(date: Date): string {
  const hour = date.getHours();
  if (hour < 12) return "morning";
  if (hour < 18) return "afternoon";
  return "evening";
}

interface GreetingHeaderProps {
  name: string;
  /** Override the "Good {x}" word; defaults to the current time of day. */
  greeting?: string;
  /** Defaults to the mockup's live-status copy; override only if the
   * connection is actually degraded (see docs/02-product/emergency-prefilter.md). */
  watchingLabel?: string;
  children?: ReactNode;
  className?: string;
}

/**
 * The wordmark row + "watching your messages" live dot + "Good morning,
 * {name}." headline. Renders any children (typically <CountsStrip />)
 * directly beneath the greeting, matching docs/mockups/07's app-header.
 */
export function GreetingHeader({
  name,
  greeting,
  watchingLabel = "Watching your messages",
  children,
  className,
}: GreetingHeaderProps) {
  const word = greeting ?? timeOfDayGreeting(new Date());
  return (
    <header className={cn("border-b border-clarity-line px-5 pb-3.5 pt-4", className)}>
      <div className="flex items-center justify-between gap-3">
        <span className="font-clarity-serif text-xl font-bold tracking-tight text-clarity-ink">
          Stoop<span className="text-clarity-emergency">.</span>
        </span>
        <span className="inline-flex items-center gap-1.5 font-clarity-sans text-[11.5px] font-bold text-clarity-brand">
          <i aria-hidden="true" className="size-1.5 rounded-full bg-clarity-brand" />
          {watchingLabel}
        </span>
      </div>
      <h1 className="mt-3.5 font-clarity-serif text-[27px] font-semibold leading-[1.2] tracking-tight text-clarity-ink">
        Good {word}, {name}.
      </h1>
      {children}
    </header>
  );
}
