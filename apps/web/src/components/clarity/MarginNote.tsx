import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * The reasoning line, styled as marginalia next to the draft — a plain
 * italic-serif sentence with a double rule, visible by default, never
 * tucked behind a "why?" disclosure toggle (docs/mockups/07 `.margin-note`).
 */
interface MarginNoteProps {
  kicker?: string;
  children: ReactNode;
  linkHref?: string;
  linkLabel?: string;
  className?: string;
}

export function MarginNote({
  kicker = "Why",
  children,
  linkHref,
  linkLabel,
  className,
}: MarginNoteProps) {
  return (
    <div
      className={cn(
        "clarity-margin-note m-[14px_2px_16px_12px] border-l-2 border-clarity-brand py-0.5 pl-4",
        className,
      )}
    >
      <span className="mb-[5px] block font-clarity-sans text-[10px] font-extrabold uppercase tracking-[0.1em] text-clarity-brand">
        {kicker}
      </span>
      <p className="font-clarity-serif text-[14.5px] italic leading-relaxed text-clarity-ink-dim">
        {children}
      </p>
      {linkHref && linkLabel && (
        <a
          href={linkHref}
          className="mt-2 inline-block min-h-11 py-2 font-clarity-sans text-[13px] font-bold not-italic text-clarity-brand underline-offset-2 hover:underline"
        >
          {linkLabel}
        </a>
      )}
    </div>
  );
}
