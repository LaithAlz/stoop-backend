/**
 * In-wizard state: the property the wizard just created (with its real,
 * provisioned number), shared across the step screens via context so the
 * tenants/backup/number steps know which property they're working on.
 * Deliberately NOT persisted anywhere — the durable state is the server's
 * (the property, its tenants, the backup contact all live behind the API
 * the moment each step succeeds); see src/features/onboarding/gate.ts on
 * why resume is server-derived.
 */
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import type { Property } from "@/api/types";

interface OnboardingContextValue {
  property: Property | null;
  setProperty: (property: Property) => void;
}

const OnboardingContext = createContext<OnboardingContextValue | undefined>(undefined);

export function OnboardingProvider({ children }: { children: ReactNode }) {
  const [property, setProperty] = useState<Property | null>(null);
  const value = useMemo(() => ({ property, setProperty }), [property]);
  return <OnboardingContext.Provider value={value}>{children}</OnboardingContext.Provider>;
}

export function useOnboarding(): OnboardingContextValue {
  const ctx = useContext(OnboardingContext);
  if (!ctx) {
    throw new Error("useOnboarding must be used within an OnboardingProvider");
  }
  return ctx;
}
