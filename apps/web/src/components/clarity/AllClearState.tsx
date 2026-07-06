import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

interface AllClearStateProps {
  message: string;
  lastChecked?: string;
  className?: string;
}

/** Quiet, specific empty state — not a cheerful illustration. */
export function AllClearState({ message, lastChecked, className }: AllClearStateProps) {
  return (
    <div className={cn("px-3.5 pb-6 pt-12 text-center", className)}>
      <div className="mx-auto mb-4 flex size-14 items-center justify-center rounded-full bg-clarity-brand-soft text-clarity-brand">
        <Check className="size-6" aria-hidden="true" />
      </div>
      <h2 className="mb-2 font-clarity-serif text-xl font-semibold text-clarity-ink">
        That's everything.
      </h2>
      <p className="mx-auto max-w-[26ch] font-clarity-sans text-sm leading-relaxed text-clarity-ink-dim">
        {message}
      </p>
      {lastChecked && (
        <p className="mt-2 font-clarity-sans text-xs text-clarity-ink-dim/80">{lastChecked}</p>
      )}
    </div>
  );
}
