/**
 * Pure routing decision for the auth gate — ported from
 * apps/mobile/src/auth/resolveAuthRoute.ts (same three-state shape; "tabs"
 * renamed to "app" since the web dashboard has no tab navigator of its
 * own, it's the `/app/*` route tree). Kept separate from
 * src/routes/app.tsx so the decision itself has no router/React
 * dependency.
 */
export type AuthRoute = "loading" | "app" | "sign-in";

export interface AuthRouteState {
  session: unknown;
  initializing: boolean;
}

export function resolveAuthRoute({ session, initializing }: AuthRouteState): AuthRoute {
  if (initializing) return "loading";
  return session ? "app" : "sign-in";
}
