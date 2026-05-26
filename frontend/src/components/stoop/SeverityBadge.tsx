import { AlertOctagon, Clock, Wrench, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export type Severity = "emergency" | "urgent" | "routine";

const severityConfig: Record<
  Severity,
  { label: string; icon: LucideIcon; classes: string; dot: string }
> = {
  emergency: {
    label: "Emergency",
    icon: AlertOctagon,
    classes: "bg-emergency-soft text-emergency border-emergency/20",
    dot: "bg-emergency",
  },
  urgent: {
    label: "Urgent",
    icon: Clock,
    classes: "bg-urgent-soft text-urgent border-urgent/20",
    dot: "bg-urgent",
  },
  routine: {
    label: "Routine",
    icon: Wrench,
    classes: "bg-routine-soft text-routine border-routine/20",
    dot: "bg-routine",
  },
};

interface SeverityBadgeProps {
  severity: Severity;
  variant?: "pill" | "row";
  className?: string;
}

export function SeverityBadge({ severity, variant = "pill", className }: SeverityBadgeProps) {
  const cfg = severityConfig[severity];
  const Icon = cfg.icon;

  if (variant === "row") {
    return (
      <div
        className={cn(
          "flex items-center gap-3 rounded-lg border px-3 py-2.5",
          cfg.classes,
          className,
        )}
      >
        <Icon className="size-4 shrink-0" aria-hidden="true" />
        <span className="text-sm font-bold uppercase tracking-wider">{cfg.label}</span>
      </div>
    );
  }

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider",
        cfg.classes,
        className,
      )}
    >
      <Icon className="size-3 shrink-0" aria-hidden="true" />
      <span>{cfg.label}</span>
    </span>
  );
}

export { severityConfig };
