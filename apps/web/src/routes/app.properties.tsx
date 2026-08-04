import { createFileRoute, Link } from "@tanstack/react-router";
import { Home, Plus, ChevronRight, Loader2 } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/auth/AuthProvider";
import { usePropertiesList } from "@/api/properties";
import { useQueue } from "@/api/queue";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { Property } from "@/api/types";
import { NO_NUMBER_TITLE, formatStoopNumber } from "@/features/properties/stoopNumber";

export const Route = createFileRoute("/app/properties")({
  head: () => ({
    meta: [{ title: "Properties. Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: PropertiesPage,
});

/**
 * Properties — the real list from `GET /v1/properties` (campaign issue
 * #234 PR 4, replacing src/lib/mock-app.ts's `properties`). Cursor-paginated
 * per api-contracts.md's Conventions section; loading/error/empty states
 * follow src/routes/app.index.tsx's exact pattern from PR 2: `isPending`
 * for the first load, a full takeover ONLY when `isError && !data`, and the
 * same quiet refresh strip on a background refetch failure that still has
 * data to show.
 *
 * Each row shows the property's own Stoop number (the number tenants
 * text — honest when a property has none, api-contracts.md v1.12's
 * previously-null case) and its `open_case_count` (a real `Property` field)
 * — NOT the mock's fictional "autonomy mode" pill (Shadow / Auto-Routine /
 * ...): no read endpoint anywhere in api-contracts.md exposes trust/
 * autonomy STATE (see src/features/trust/revoke.ts's own note), so that
 * pill had nothing real to read from and is dropped rather than faked
 * next to live data.
 *
 * "Add a property" now goes to the real, live provisioning flow
 * (/app/properties/add — this PR) instead of `/onboarding`, which is a
 * deliberately mock, pre-signup marketing demo (onboarding.tsx's own
 * docstring: "no real auth/API calls" — apps/mobile's own onboarding
 * property step even notes "the web wizard fakes this with a timer and a
 * mock number"). A signed-in landlord tapping "Add" here needs a REAL
 * property, so this screen no longer links there.
 */
function PropertiesPage() {
  const { session } = useAuth();
  const propertiesQuery = usePropertiesList({ enabled: Boolean(session) });
  // Tab-bar badge reads the queue's own action-needed count, same as every
  // other app.* screen (app.conversations.index.tsx's own pattern).
  const queueQuery = useQueue({ enabled: Boolean(session) });

  const items: Property[] = propertiesQuery.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <PhoneFrame>
      <header className="sticky top-0 z-10 border-b border-border bg-canvas/95 px-5 py-4 backdrop-blur">
        <div className="flex items-center justify-between">
          <div>
            <p className="font-mono text-[10px] font-bold uppercase tracking-widest text-ink-muted">
              Properties
            </p>
            {/* M2 (safety review, #234 PR 4): `items.length` counts LOADED
                pages only — it read "20 properties" for 45, and a
                confident "0 properties" above both the loading state and
                the error takeover. The count only appears once the list
                is genuinely complete. */}
            <h1 className="font-display text-[26px] leading-tight tracking-tight text-ink">
              {!propertiesQuery.isPending && propertiesQuery.data && !propertiesQuery.hasNextPage
                ? `${items.length} ${items.length === 1 ? "property" : "properties"}`
                : "Properties"}
            </h1>
          </div>
          <Button
            size="sm"
            className="h-10 bg-brand text-brand-foreground hover:bg-brand/90"
            asChild
          >
            <Link to="/app/properties/add">
              <Plus className="size-4" /> Add
            </Link>
          </Button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {propertiesQuery.isPending ? (
          <div
            role="status"
            aria-live="polite"
            className="flex flex-col items-center gap-3 py-16 text-center"
          >
            <Loader2 className="size-6 animate-spin text-brand motion-reduce:animate-none" />
            <p className="text-sm font-medium text-ink-muted">Loading your properties…</p>
          </div>
        ) : propertiesQuery.isError && !propertiesQuery.data ? (
          <div role="alert" className="flex flex-col items-center gap-3 py-16 text-center">
            <p className="text-sm text-ink-muted">
              {propertiesQuery.error instanceof ApiError
                ? toHouseApiError(propertiesQuery.error)
                : "Couldn't load your properties. Try again."}
            </p>
            <Button
              onClick={() => void propertiesQuery.refetch()}
              className="h-11 bg-brand text-brand-foreground hover:bg-brand/90"
            >
              Try again
            </Button>
          </div>
        ) : (
          <>
            {propertiesQuery.isError && (
              <div
                role="status"
                className="mb-3 rounded-2xl border border-border bg-surface px-4 py-2.5 text-[13px] font-medium text-ink-muted"
              >
                Couldn&apos;t refresh just now. Showing the last update.
              </div>
            )}

            {items.length === 0 ? (
              <div className="flex flex-col items-center gap-4 rounded-2xl border border-dashed border-border bg-canvas/50 px-5 py-12 text-center">
                <div className="flex size-12 items-center justify-center rounded-full bg-brand-muted text-brand">
                  <Home className="size-6" />
                </div>
                <div>
                  <p className="font-display text-[18px] text-ink">No properties yet.</p>
                  <p className="mt-1 text-[13px] text-ink-muted">
                    Each property you add gets its own phone number for tenants to text.
                    They&rsquo;ll all show up here.
                  </p>
                </div>
                <Button className="h-11 bg-brand text-brand-foreground hover:bg-brand/90" asChild>
                  <Link to="/app/properties/add">Add your first property</Link>
                </Button>
              </div>
            ) : (
              <ul className="space-y-3">
                {items.map((p) => (
                  <li key={p.id}>
                    <Link
                      to="/app/properties/$id"
                      params={{ id: p.id }}
                      className="block rounded-2xl border border-border bg-card p-4 transition hover:border-brand/30"
                    >
                      <div className="flex items-start gap-3">
                        <div className="mt-0.5 flex size-10 shrink-0 items-center justify-center rounded-xl bg-brand-muted text-brand">
                          <Home className="size-5" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="font-display text-[17px] leading-tight text-ink">
                            {p.label}
                          </div>
                          <div className="mt-0.5 truncate text-[13px] text-ink-muted">
                            {p.address_line1}, {p.city}
                          </div>
                          <div className="mt-2 flex flex-wrap items-center gap-2">
                            <span
                              className={
                                p.twilio_number
                                  ? "rounded-full bg-brand-muted px-2 py-0.5 font-mono text-[10px] font-bold text-brand"
                                  : "rounded-full bg-canvas px-2 py-0.5 font-mono text-[10px] italic text-ink-muted"
                              }
                            >
                              {p.twilio_number
                                ? formatStoopNumber(p.twilio_number)
                                : NO_NUMBER_TITLE}
                            </span>
                            {p.open_case_count > 0 && (
                              <span className="rounded-full bg-urgent-soft px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest text-urgent">
                                {p.open_case_count === 1
                                  ? "1 open case"
                                  : `${p.open_case_count} open cases`}
                              </span>
                            )}
                          </div>
                        </div>
                        <ChevronRight className="size-4 shrink-0 text-ink-muted/70" />
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}

            {propertiesQuery.hasNextPage && (
              <div className="mt-4 flex flex-col items-center gap-2">
                {propertiesQuery.isFetchNextPageError && (
                  <p role="alert" className="text-[13px] font-medium text-ink-muted">
                    {propertiesQuery.error instanceof ApiError
                      ? toHouseApiError(propertiesQuery.error)
                      : "Couldn't load more. Try again."}
                  </p>
                )}
                <Button
                  variant="outline"
                  onClick={() => void propertiesQuery.fetchNextPage()}
                  disabled={propertiesQuery.isFetchingNextPage}
                  className="h-10 border-border text-ink"
                >
                  {propertiesQuery.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              </div>
            )}

            {items.length > 0 && (
              <Link
                to="/app/properties/add"
                className="mt-4 flex min-h-14 items-center justify-center gap-2 rounded-2xl border border-dashed border-border bg-canvas/50 text-[14px] font-medium text-brand"
              >
                <Plus className="size-4" /> Add another property
              </Link>
            )}
          </>
        )}
      </div>

      <AppTabBar active="properties" queueCount={queueQuery.data?.counts.total ?? 0} />
    </PhoneFrame>
  );
}
