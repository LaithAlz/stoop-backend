import type { ReactNode } from "react";
export function useAuth() {
  return {
    session: { access_token: "tok", user: { id: "landlord-1" } } as any,
    initializing: false,
    initTimedOut: false,
    retryInit: () => {},
    signOut: async () => {},
  };
}
export function AuthProvider({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
