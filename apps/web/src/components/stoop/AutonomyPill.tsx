import { Eye, Sparkles, Zap, Infinity as InfinityIcon, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export type AutonomyMode = "shadow" | "auto-routine" | "auto-urgent" | "full-auto";

const modes: Record<AutonomyMode, { label: string; icon: LucideIcon; hint: string }> = {
  shadow: { label: "Shadow", icon: Eye, hint: "Approve every draft" },
  "auto-routine": { label: "Auto-Routine", icon: Sparkles, hint: "Routine sends itself" },
  "auto-urgent": { label: "Auto-Urgent", icon: Zap, hint: "Urgent sends itself" },
  "full-auto": { label: "Full Auto", icon: InfinityIcon, hint: "Stoop handles everything" },
};

interface AutonomyPillProps {
  mode: AutonomyMode;
  active?: boolean;
  className?: string;
}

export function AutonomyPill({ mode, active = false, className }: AutonomyPillProps) {
  const cfg = modes[mode];
  const Icon = cfg.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-bold uppercase tracking-wider transition-colors",
        active
          ? "bg-brand text-brand-foreground border-brand"
          : "bg-canvas text-ink-muted border-border hover:border-brand/40",
        className,
      )}
    >
      <Icon className="size-3.5 shrink-0" aria-hidden="true" />
      <span>{cfg.label}</span>
    </span>
  );
}

export { modes as autonomyModes };
