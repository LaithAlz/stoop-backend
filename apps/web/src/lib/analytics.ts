/**
 * Tiny Plausible analytics helper (ADR-5 — docs/03-engineering/architecture.md):
 * marketing-site-only, anonymous, cookieless, no consent banner required.
 *
 * The Plausible script tag is only injected (see src/routes/__root.tsx)
 * when VITE_PLAUSIBLE_DOMAIN is set at build time. There is no production
 * domain yet, so today this is always a silent no-op — never throws,
 * never blocks the caller (e.g. a form submit).
 *
 * No PII: only ever pass a channel/source string as a prop, never an
 * email, name, or phone number.
 */

type PlausibleEventOptions = {
  props?: Record<string, string | number | boolean>;
};

declare global {
  interface Window {
    plausible?: (event: string, options?: PlausibleEventOptions) => void;
  }
}

export function trackEvent(event: string, options?: PlausibleEventOptions): void {
  try {
    if (typeof window === "undefined") return;
    window.plausible?.(event, options);
  } catch {
    // Analytics must never break the app.
  }
}
