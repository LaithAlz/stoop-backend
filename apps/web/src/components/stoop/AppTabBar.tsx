import { Link } from "@tanstack/react-router";
import { Inbox, Home, Activity, User } from "lucide-react";
import { cn } from "@/lib/utils";

type TabKey = "queue" | "properties" | "activity" | "account";

const tabs: {
  key: TabKey;
  label: string;
  icon: typeof Inbox;
  to: "/app" | "/app/properties" | "/app/activity" | "/app/account";
}[] = [
  { key: "queue", label: "Queue", icon: Inbox, to: "/app" },
  { key: "properties", label: "Properties", icon: Home, to: "/app/properties" },
  { key: "activity", label: "Activity", icon: Activity, to: "/app/activity" },
  { key: "account", label: "Account", icon: User, to: "/app/account" },
];

interface AppTabBarProps {
  active?: TabKey;
  queueCount?: number;
}

export function AppTabBar({ active = "queue", queueCount = 0 }: AppTabBarProps) {
  return (
    <nav
      aria-label="App tabs"
      className="flex h-[72px] items-stretch justify-around border-t border-border bg-card px-1"
    >
      {tabs.map((t) => {
        const Icon = t.icon;
        const isActive = t.key === active;
        const showBadge = t.key === "queue" && queueCount > 0;
        return (
          <Link
            key={t.key}
            to={t.to}
            aria-current={isActive ? "page" : undefined}
            className={cn(
              "flex min-h-14 min-w-14 flex-1 flex-col items-center justify-center gap-1 rounded-lg",
              isActive ? "text-brand" : "text-ink-muted",
            )}
          >
            <div className="relative">
              <Icon className="size-5" aria-hidden="true" />
              {showBadge && (
                <span
                  aria-label={`${queueCount} pending`}
                  className="absolute -right-2 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-brand px-1 text-[10px] font-bold text-brand-foreground"
                >
                  {queueCount}
                </span>
              )}
            </div>
            <span className="text-[10px] font-bold uppercase tracking-widest">{t.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
