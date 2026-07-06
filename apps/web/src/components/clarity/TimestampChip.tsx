import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Small mono, uppercase, letter-spaced "stamp" used for timestamps and
 * other wayfinding text (docs/mockups/07-clarity-redesign.html `.stamp`).
 */
interface TimestampChipProps {
  children: ReactNode;
  className?: string;
}

export function TimestampChip({ children, className }: TimestampChipProps) {
  return (
    <span
      className={cn(
        "inline-block whitespace-nowrap rounded-clarity-sm border-[1.5px] border-clarity-brand bg-clarity-brand/5 px-2 py-[3px] font-clarity-mono text-[10.5px] font-bold uppercase tracking-[0.05em] text-clarity-brand",
        className,
      )}
    >
      {children}
    </span>
  );
}
