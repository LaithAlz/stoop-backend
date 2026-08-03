import { cn } from "@/lib/utils";
import type { Severity } from "@/components/stoop/SeverityBadge";

/**
 * The enamel severity plaque from docs/mockups/07-clarity-redesign.html —
 * severity read as a plain word (never a coded tag), styled like the brass
 * plaque by the door. `emergency` / `urgent` / `routine` are the schema-v1
 * enum values (see docs/03-engineering/schema-v1.md); the labels below are
 * Clarity's plain-English display copy, not the stored values.
 */
const plaqueConfig: Record<
  Severity,
  { label: string; variant: "emergency" | "wait" | "whenever" }
> = {
  emergency: { label: "Emergency", variant: "emergency" },
  urgent: { label: "Can't wait", variant: "wait" },
  routine: { label: "Whenever", variant: "whenever" },
};

interface SeverityPlaqueProps {
  severity: Severity | null;
  size?: "default" | "sm";
  className?: string;
}

export function SeverityPlaque({ severity, size = "default", className }: SeverityPlaqueProps) {
  // F3 (safety re-verify, #252): an out-of-vocabulary or absent severity
  // must render nothing rather than throw — an unguarded lookup here took
  // the entire screen down through the root error boundary, and the state
  // that triggers it (classification failed) is precisely when the screen
  // must stay up.
  const cfg = plaqueConfig[severity as Severity] as (typeof plaqueConfig)[Severity] | undefined;
  if (!cfg) return null;
  return (
    <span
      className={cn(
        "clarity-plaque",
        `clarity-plaque--${cfg.variant}`,
        size === "sm" && "clarity-plaque--sm",
        className,
      )}
    >
      {cfg.label}
    </span>
  );
}
