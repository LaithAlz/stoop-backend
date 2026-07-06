import { Link } from "@tanstack/react-router";
import { Home, MessageCircle, Building2, User } from "lucide-react";
import { cn } from "@/lib/utils";

type TabKey = "home" | "conversations" | "properties" | "account";

const tabs: {
  key: TabKey;
  label: string;
  icon: typeof Home;
  to: "/app" | "/app/conversations" | "/app/properties" | "/app/account";
}[] = [
  { key: "home", label: "Home", icon: Home, to: "/app" },
  { key: "conversations", label: "Conversations", icon: MessageCircle, to: "/app/conversations" },
  { key: "properties", label: "Properties", icon: Building2, to: "/app/properties" },
  { key: "account", label: "Me", icon: User, to: "/app/account" },
];

interface AppTabBarProps {
  /** Leave unset on screens that don't map to any of the four tabs (e.g.
   * the Activity screen, which is no longer in the tab bar but still has
   * a live route) — no tab gets highlighted in that case. */
  active?: TabKey;
  queueCount?: number;
}

/**
 * Four labeled tabs — Home / Conversations / Properties / Me — matching
 * docs/mockups/07's `.tabbar` (Tab IA adopted 2026-07-06). Icons follow
 * meaning: house→Home, chat→Conversations, building→Properties,
 * person→Me. Shared across every app.* screen.
 */
export function AppTabBar({ active, queueCount = 0 }: AppTabBarProps) {
  return (
    <nav
      aria-label="App tabs"
      className="flex h-[72px] items-stretch justify-around border-t border-clarity-line bg-clarity-bg/95 px-1"
    >
      {tabs.map((t) => {
        const Icon = t.icon;
        const isActive = t.key === active;
        const showBadge = t.key === "home" && queueCount > 0;
        return (
          <Link
            key={t.key}
            to={t.to}
            // Every app screen lives under "/app/...", so without `exact`
            // TanStack Router's own fuzzy active-match would mark the Home
            // tab (to="/app") "active" on every screen — a real duplicate
            // aria-current bug found while testing this route. The four
            // tabs' on/off state is driven entirely by the `active` prop
            // above, not by the router's own path matching.
            activeOptions={{ exact: true }}
            aria-current={isActive ? "page" : undefined}
            className={cn(
              "flex min-h-14 min-w-14 flex-1 flex-col items-center justify-center gap-1 rounded-clarity-sm",
              isActive ? "text-clarity-brand" : "text-clarity-ink-dim",
            )}
          >
            <div className="relative">
              <Icon className="size-[21px]" aria-hidden="true" />
              {showBadge && (
                <span
                  aria-label={`${queueCount} pending`}
                  className="absolute -right-2 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-clarity-brand px-1 font-clarity-mono text-[9px] font-bold text-clarity-brand-on"
                >
                  {queueCount}
                </span>
              )}
            </div>
            <span className="font-clarity-sans text-[10.5px] font-extrabold uppercase tracking-[0.02em]">
              {t.label}
            </span>
          </Link>
        );
      })}
    </nav>
  );
}
