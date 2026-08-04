import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { Loader2, MessageCircle } from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { ConversationRow } from "@/components/clarity/ConversationRow";
import { useAuth } from "@/auth/AuthProvider";
import { useCasesList } from "@/api/cases";
import { useQueue } from "@/api/queue";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { CaseStatus, CaseSummary } from "@/api/types";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/app/conversations/")({
  head: () => ({
    meta: [{ title: "Conversations. Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: ConversationsIndexPage,
});

const STATUS_FILTERS: { value: CaseStatus | undefined; label: string }[] = [
  { value: undefined, label: "All" },
  { value: "awaiting_approval", label: "Waiting on you" },
  { value: "awaiting_tenant", label: "Waiting on tenant" },
  { value: "resolved", label: "Resolved" },
];

/**
 * The Conversations tab's destination (added per the Tab IA decision,
 * 2026-07-06 — previously only a conversation's detail route existed).
 * Wired to `GET /v1/cases` (src/api/cases.ts, campaign issue #234 PR 3,
 * replacing src/lib/mock-app.ts's `queue`) — every case, most-recently
 * active first, filterable by the contract's own `status` query param.
 * Loading/error/empty states follow src/routes/app.index.tsx's exact
 * pattern from PR 2: `isPending` (not `isLoading`) for the first load, a
 * full takeover ONLY when `isError && !data`, and the same quiet refresh
 * strip on a background refetch failure that still has data to show.
 */
function ConversationsIndexPage() {
  const { session } = useAuth();
  const [status, setStatus] = useState<CaseStatus | undefined>(undefined);
  const casesQuery = useCasesList({ status, enabled: Boolean(session) });
  // Home's tab-bar badge reads the queue's own action-needed count — kept
  // consistent with every other screen's tab bar rather than this route's
  // own (differently-scoped) case count.
  const queueQuery = useQueue({ enabled: Boolean(session) });

  // LOW (safety review, #234 PR 3 fix round): `GET /v1/cases`'s sort key
  // (`last_activity_at`) is a MUTABLE cursor per api-contracts.md's own A3
  // amendment — a case bumped by new activity between page fetches can
  // "skip ahead" and land on more than one already-fetched page. Deduping
  // by id keeps the list from showing the same case twice. N4 (re-verify):
  // FIRST write wins — page 1 is the most recently fetched snapshot of a
  // bumped case; letting a later (staler) page overwrite its fields kept
  // the fresh position but regressed the data. Re-fetching page 1 is the
  // amendment's own documented remedy for a stale cursor.
  const items = useMemo(() => {
    const byId = new Map<string, CaseSummary>();
    for (const page of casesQuery.data?.pages ?? []) {
      for (const item of page.items) {
        if (!byId.has(item.id)) byId.set(item.id, item);
      }
    }
    return Array.from(byId.values());
  }, [casesQuery.data]);

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col bg-clarity-bg">
        <header className="border-b border-clarity-line px-5 pb-3.5 pt-4">
          <h1 className="font-clarity-serif text-[27px] font-semibold leading-[1.2] tracking-tight text-clarity-ink">
            Conversations
          </h1>
          <p className="mt-1 font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim">
            Every tenant thread, saved with dates and times.
          </p>

          <div role="group" aria-label="Filter by status" className="mt-3.5 flex flex-wrap gap-2">
            {STATUS_FILTERS.map((filter) => {
              const active = filter.value === status;
              return (
                <button
                  key={filter.label}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setStatus(filter.value)}
                  className={cn(
                    "inline-flex min-h-11 items-center rounded-clarity-md border-[1.5px] px-3.5 font-clarity-sans text-[13px] font-bold transition-colors duration-150 ease-clarity motion-reduce:transition-none",
                    active
                      ? "border-clarity-brand-deep bg-clarity-brand text-clarity-brand-on"
                      : "border-clarity-line-strong bg-clarity-panel text-clarity-ink-dim hover:text-clarity-ink",
                  )}
                >
                  {filter.label}
                </button>
              );
            })}
          </div>
        </header>

        <main className="flex-1 overflow-y-auto px-[18px] py-4">
          {casesQuery.isPending ? (
            <div
              role="status"
              aria-live="polite"
              className="flex flex-col items-center gap-3 py-16 text-center"
            >
              <Loader2
                className="size-6 animate-spin text-clarity-brand motion-reduce:animate-none"
                aria-hidden="true"
              />
              <p className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                Loading your conversations…
              </p>
            </div>
          ) : casesQuery.isError && !casesQuery.data ? (
            <div role="alert" className="flex flex-col items-center gap-3 py-16 text-center">
              <p className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                {casesQuery.error instanceof ApiError
                  ? toHouseApiError(casesQuery.error)
                  : "Couldn't load your conversations. Try again."}
              </p>
              <button
                type="button"
                onClick={() => void casesQuery.refetch()}
                className="inline-flex min-h-12 items-center justify-center rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand px-5 font-clarity-sans text-[15px] font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
              >
                Try again
              </button>
            </div>
          ) : (
            <>
              {/* LOW (safety review, #234 PR 3 fix round): a failed
                  `fetchNextPage()` also flips this query's own `isError` —
                  excluded here (`!casesQuery.isFetchNextPageError`) so a
                  load-more failure shows ONLY its own message below the
                  list, not this unrelated "couldn't refresh" strip too. */}
              {casesQuery.isError && !casesQuery.isFetchNextPageError && (
                <div
                  role="status"
                  className="mb-3.5 rounded-clarity-md border border-clarity-line-strong bg-clarity-panel px-4 py-2.5 font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim"
                >
                  Couldn&apos;t refresh just now. Showing the last update.
                </div>
              )}

              {items.length === 0 ? (
                <EmptyConversations filtered={status !== undefined} />
              ) : (
                <ul className="space-y-3">
                  {items.map((item) => (
                    <li key={item.id}>
                      <ConversationRow item={item} />
                    </li>
                  ))}
                </ul>
              )}

              {casesQuery.hasNextPage && (
                <div className="mt-4 flex flex-col items-center gap-2">
                  {casesQuery.isFetchNextPageError && (
                    <p
                      role="alert"
                      className="font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim"
                    >
                      {casesQuery.error instanceof ApiError
                        ? toHouseApiError(casesQuery.error)
                        : "Couldn't load more. Try again."}
                    </p>
                  )}
                  <button
                    type="button"
                    onClick={() => void casesQuery.fetchNextPage()}
                    disabled={casesQuery.isFetchingNextPage}
                    aria-busy={casesQuery.isFetchingNextPage}
                    className="inline-flex min-h-11 items-center justify-center rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-4 font-clarity-sans text-[13.5px] font-bold text-clarity-ink-dim disabled:opacity-60"
                  >
                    {casesQuery.isFetchingNextPage ? "Loading…" : "Load more"}
                  </button>
                </div>
              )}
            </>
          )}
        </main>

        <AppTabBar active="conversations" queueCount={queueQuery.data?.counts.total ?? 0} />
      </div>
    </PhoneFrame>
  );
}

function EmptyConversations({ filtered }: { filtered: boolean }) {
  return (
    <div className="px-3.5 pb-6 pt-12 text-center">
      <div className="mx-auto mb-4 flex size-14 items-center justify-center rounded-full bg-clarity-brand-soft text-clarity-brand">
        <MessageCircle className="size-6" aria-hidden="true" />
      </div>
      <h2 className="mb-2 font-clarity-serif text-xl font-semibold text-clarity-ink">
        {filtered ? "Nothing here yet." : "No conversations yet."}
      </h2>
      <p className="mx-auto max-w-[30ch] font-clarity-sans text-sm leading-relaxed text-clarity-ink-dim">
        {filtered
          ? "No conversations match this filter right now."
          : "Every text between your tenants and Stoop will be saved here, with dates and times: nothing edited, nothing lost."}
      </p>
    </div>
  );
}
