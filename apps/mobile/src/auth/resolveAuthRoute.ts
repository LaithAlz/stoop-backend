/**
 * Pure routing decision for the auth gate — kept separate from
 * src/app/_layout.tsx so it's unit-testable without rendering React Native
 * or Expo Router at all (see src/app/__tests__/auth-gate.test.tsx, which
 * exercises both this function directly and the real root layout via
 * expo-router/testing-library).
 */
export type AuthRoute = "loading" | "tabs" | "sign-in";

export interface AuthRouteState {
  session: unknown;
  initializing: boolean;
}

export function resolveAuthRoute({ session, initializing }: AuthRouteState): AuthRoute {
  if (initializing) return "loading";
  return session ? "tabs" : "sign-in";
}
