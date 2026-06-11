import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { Menu, X, Apple, Smartphone } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Wordmark } from "./Wordmark";
import { cn } from "@/lib/utils";

const APP_STORE_URL = "https://apps.apple.com/app/stoop";
const PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=co.stoop.app";

type NavLink =
  | { label: string; kind: "anchor"; href: string }
  | { label: string; kind: "route"; to: "/" | "/plans" | "/design-system" | "/sign-in" };

const links: NavLink[] = [
  { label: "How it works", kind: "anchor", href: "/#how-it-works" },
  { label: "Plans", kind: "route", to: "/plans" },
  { label: "FAQ", kind: "anchor", href: "/#faq" },
  { label: "Sign in", kind: "route", to: "/sign-in" },
];

function GetTheAppButtons({ stacked = false }: { stacked?: boolean }) {
  return (
    <div className={cn("flex gap-2", stacked && "flex-col")}>
      <Button asChild className="h-11 px-4 font-semibold">
        <a href={APP_STORE_URL} rel="noopener" aria-label="Download Stoop on the App Store">
          <Apple className="size-4" aria-hidden="true" />
          App Store
        </a>
      </Button>
      <Button asChild variant="outline" className="h-11 px-4 font-semibold">
        <a href={PLAY_STORE_URL} rel="noopener" aria-label="Get Stoop on Google Play">
          <Smartphone className="size-4" aria-hidden="true" />
          Play Store
        </a>
      </Button>
    </div>
  );
}

export function MarketingNav() {
  const [open, setOpen] = useState(false);

  return (
    <nav
      aria-label="Primary"
      className="sticky top-0 z-40 border-b border-border bg-canvas/85 backdrop-blur-md"
    >
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6">
        <Link to="/" className="flex items-center" aria-label="Stoop. home">
          <Wordmark size="sm" />
        </Link>

        <ul className="hidden items-center gap-8 md:flex">
          {links.map((l) =>
            l.kind === "route" ? (
              <li key={l.label}>
                <Link
                  to={l.to}
                  className="text-sm font-medium text-ink-muted transition-colors hover:text-brand"
                  activeProps={{ className: "text-brand" }}
                >
                  {l.label}
                </Link>
              </li>
            ) : (
              <li key={l.label}>
                <a
                  href={l.href}
                  className="text-sm font-medium text-ink-muted transition-colors hover:text-brand"
                >
                  {l.label}
                </a>
              </li>
            ),
          )}
        </ul>

        <div className="hidden md:block">
          <Button asChild className="h-11 px-5 font-semibold">
            <Link to="/founding">Get early access</Link>
          </Button>
        </div>

        <button
          type="button"
          aria-expanded={open}
          aria-controls="mobile-menu"
          aria-label={open ? "Close menu" : "Open menu"}
          onClick={() => setOpen((v) => !v)}
          className="inline-flex size-11 items-center justify-center rounded-lg border border-border bg-canvas md:hidden"
        >
          {open ? (
            <X className="size-5" aria-hidden="true" />
          ) : (
            <Menu className="size-5" aria-hidden="true" />
          )}
        </button>
      </div>

      {open && (
        <div id="mobile-menu" className="border-t border-border bg-canvas md:hidden">
          <ul className="mx-auto flex max-w-7xl flex-col gap-1 px-6 py-4">
            {links.map((l) => (
              <li key={l.label}>
                {l.kind === "route" ? (
                  <Link
                    to={l.to}
                    onClick={() => setOpen(false)}
                    className="block min-h-11 rounded-lg px-3 py-3 text-base font-semibold text-ink hover:bg-brand-muted"
                  >
                    {l.label}
                  </Link>
                ) : (
                  <a
                    href={l.href}
                    onClick={() => setOpen(false)}
                    className="block min-h-11 rounded-lg px-3 py-3 text-base font-semibold text-ink hover:bg-brand-muted"
                  >
                    {l.label}
                  </a>
                )}
              </li>
            ))}
            <li className="pt-3">
              <Button asChild className="h-11 w-full font-semibold">
                <Link to="/founding" onClick={() => setOpen(false)}>
                  Get early access
                </Link>
              </Button>
            </li>
          </ul>
        </div>
      )}
    </nav>
  );
}

export { APP_STORE_URL, PLAY_STORE_URL, GetTheAppButtons };
