import { createFileRoute, Outlet, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { Loader2 } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { useAuth } from "@/auth/AuthProvider";
import { resolveAuthRoute } from "@/auth/resolveAuthRoute";

export const Route = createFileRoute("/app")({
  component: AppLayout,
});

/**
 * Route guard for every `/app/*` screen (issue #234 PR 1). A layout guard
 * rather than `beforeLoad` on purpose: the Supabase session lives in
 * `localStorage` (src/lib/supabase.ts), which only exists in the browser —
 * `beforeLoad` also runs during SSR, where there's no session to read
 * either way. Rendering the same "loading" shell on the server and on the
 * client's first paint (both start from `initializing: true`, see
 * AuthProvider) and only deciding to redirect once the real session
 * resolves, client-side, in an effect, avoids a hydration mismatch — same
 * pattern as GreetingHeader's clock-safe render (apps/web/AGENTS.md's
 * SSR/hydration lesson).
 */
function AppLayout() {
  const { session, initializing } = useAuth();
  const navigate = useNavigate();
  const authRoute = resolveAuthRoute({ session, initializing });

  useEffect(() => {
    if (authRoute === "sign-in") {
      void navigate({ to: "/sign-in", replace: true });
    }
  }, [authRoute, navigate]);

  if (authRoute !== "app") {
    return <AppLoadingShell />;
  }

  return <Outlet />;
}

function AppLoadingShell() {
  return (
    <PhoneFrame>
      <div
        role="status"
        aria-live="polite"
        className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center"
      >
        <Loader2
          className="size-6 animate-spin text-brand motion-reduce:animate-none"
          aria-hidden="true"
        />
        <p className="text-sm font-medium text-ink-muted">Checking your account…</p>
      </div>
    </PhoneFrame>
  );
}
