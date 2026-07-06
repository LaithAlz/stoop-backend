import { cn } from "@/lib/utils";

interface CountsStripProps {
  needYou: number;
  waitingOnTenants: number;
  className?: string;
}

/** The "N need you · N waiting on tenants" line under the greeting. */
export function CountsStrip({ needYou, waitingOnTenants, className }: CountsStripProps) {
  return (
    <p
      className={cn(
        "mt-1.5 flex flex-wrap items-center gap-2 font-clarity-sans text-[13.5px] font-bold text-clarity-ink-dim",
        className,
      )}
    >
      <span>
        <b className="text-clarity-ink">{needYou}</b> need you
      </span>
      <span className="opacity-45" aria-hidden="true">
        ·
      </span>
      <span>
        <b className="text-clarity-ink">{waitingOnTenants}</b> waiting on tenants
      </span>
    </p>
  );
}
