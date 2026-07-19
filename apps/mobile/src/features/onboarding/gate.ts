/**
 * The onboarding entry gate (issue #210 M2): the wizard is offered when —
 * and only when — a REAL, successful `GET /v1/properties` read says the
 * landlord has zero properties. Never a feature flag, never a stored
 * "finished onboarding" bit that could disagree with reality (CLAUDE.md
 * rule 7's spirit: flags don't gate behavior that data can drive).
 *
 * Session semantics: the gate auto-opens the wizard at most ONCE per
 * app session (the module-scope flag below) — after that, skipping is
 * respected and the tabs stay fully usable; the Properties tab's own
 * "Add your first property" empty state remains the standing entry. A
 * fresh launch with still-zero properties offers the wizard again (an
 * account with nothing set up genuinely has nothing else to show). The
 * flag resets on sign-out (src/auth/AuthProvider.tsx) so a different
 * landlord on the same device gets their own gate decision.
 *
 * "Resumable" is server-derived, not client-persisted: the wizard's own
 * steps navigate forward with the created property's real id, and every
 * artifact it creates (the property, its number, tenants) lives on the
 * server — if the app dies mid-wizard, the Properties tab picks up
 * exactly where the data actually is.
 */

let offeredThisSession = false;

export function hasOfferedOnboarding(): boolean {
  return offeredThisSession;
}

export function markOnboardingOffered(): void {
  offeredThisSession = true;
}

/** Called on sign-out (and from tests). */
export function resetOnboardingOffer(): void {
  offeredThisSession = false;
}

export interface OnboardingGateState {
  /** True only when `GET /v1/properties` actually succeeded — a loading or
   *  failed fetch must never open the wizard (an error is not "zero
   *  properties"). */
  fetched: boolean;
  itemCount: number;
  alreadyOffered: boolean;
}

export function shouldOfferOnboarding(state: OnboardingGateState): boolean {
  return state.fetched && state.itemCount === 0 && !state.alreadyOffered;
}
