import { Link } from "@tanstack/react-router";
import { Wordmark } from "./Wordmark";

export function SiteFooter() {
  return (
    <footer className="border-t border-border bg-surface/60 px-6 py-16">
      <div className="mx-auto grid max-w-7xl gap-10 md:grid-cols-[2fr_1fr_1fr]">
        <div className="space-y-4">
          <Wordmark size="md" />
          <p className="max-w-md text-sm leading-relaxed text-ink-muted">
            Built for Ontario landlords. GTA-based. We handle tenant comms — we don't give legal
            advice on tenancy law.
          </p>
        </div>

        <div className="space-y-3">
          <h2 className="text-xs font-bold uppercase tracking-widest text-ink-muted">Product</h2>
          <ul className="space-y-2 text-sm font-medium">
            <li>
              <a href="/#how-it-works" className="hover:text-brand">
                How it works
              </a>
            </li>
            <li>
              <Link to="/plans" className="hover:text-brand">
                Plans
              </Link>
            </li>
            <li>
              <a href="/#faq" className="hover:text-brand">
                FAQ
              </a>
            </li>
            <li>
              <Link to="/sign-in" className="hover:text-brand">
                Sign in
              </Link>
            </li>
          </ul>
        </div>

        <div className="space-y-3">
          <h2 className="text-xs font-bold uppercase tracking-widest text-ink-muted">Legal</h2>
          <ul className="space-y-2 text-sm font-medium">
            <li>
              <Link to="/privacy" className="hover:text-brand">
                Privacy
              </Link>
            </li>
            <li>
              <Link to="/terms" className="hover:text-brand">
                Terms
              </Link>
            </li>
            <li>
              <a href="mailto:hello@stoop.co" className="hover:text-brand">
                Contact
              </a>
            </li>
          </ul>
        </div>
      </div>

      <div className="mx-auto mt-12 flex max-w-7xl flex-col items-start justify-between gap-2 border-t border-border pt-6 text-xs text-ink-muted md:flex-row md:items-center">
        <p>© {new Date().getFullYear()} Stoop. Tenant maintenance, handled for small landlords.</p>
        <p>Made in the GTA.</p>
      </div>
    </footer>
  );
}
