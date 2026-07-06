import type { ReactNode } from "react";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

interface HandledNoteProps {
  children: ReactNode;
  className?: string;
}

/** The "I handled this myself" note — a plain dashed box, not a hidden log. */
export function HandledNote({ children, className }: HandledNoteProps) {
  return (
    <div
      className={cn(
        "mt-3.5 flex items-start gap-3 rounded-clarity-lg border border-dashed border-clarity-line-strong p-4",
        className,
      )}
    >
      <Check className="mt-0.5 size-[18px] shrink-0 text-clarity-whenever" aria-hidden="true" />
      <p className="font-clarity-sans text-[13.5px] leading-relaxed text-clarity-ink-dim">
        {children}
      </p>
    </div>
  );
}
