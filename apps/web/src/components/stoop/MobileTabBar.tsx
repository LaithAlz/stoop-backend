import { Inbox, Home, TrendingUp, Menu, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface Tab {
  key: string;
  label: string;
  icon: LucideIcon;
}

const tabs: Tab[] = [
  { key: "inbox", label: "Inbox", icon: Inbox },
  { key: "homes", label: "Homes", icon: Home },
  { key: "trust", label: "Trust", icon: TrendingUp },
  { key: "menu", label: "Menu", icon: Menu },
];

interface MobileTabBarProps {
  active?: string;
  onChange?: (key: string) => void;
}

export function MobileTabBar({ active = "inbox", onChange }: MobileTabBarProps) {
  return (
    <nav
      aria-label="Primary"
      className="flex h-20 items-stretch justify-around border-t border-border bg-card px-2"
    >
      {tabs.map((tab) => {
        const Icon = tab.icon;
        const isActive = tab.key === active;
        return (
          <button
            key={tab.key}
            type="button"
            aria-current={isActive ? "page" : undefined}
            onClick={() => onChange?.(tab.key)}
            className={cn(
              "flex min-h-14 min-w-14 flex-1 flex-col items-center justify-center gap-1 rounded-lg transition-colors",
              isActive ? "text-brand" : "text-ink-muted hover:text-ink",
            )}
          >
            <Icon className="size-5" aria-hidden="true" />
            <span className="text-[10px] font-bold uppercase tracking-widest">{tab.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
