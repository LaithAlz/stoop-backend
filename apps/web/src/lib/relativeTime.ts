/**
 * Plain-English relative timestamps for chrome (never "soon" — concrete
 * over relative is good discipline here too: this always resolves to a
 * specific, bounded phrase, never an open-ended one). Ported from
 * apps/mobile/src/lib/relativeTime.ts (campaign issue #234 PR 2) —
 * `formatRelativeTime` only; that file's `formatDayLabel`/`isSameDay`
 * helpers exist for the case-detail timeline's day dividers, which stay
 * on mock data until a later PR wires `GET /v1/cases/{id}`.
 *
 * This only ever renders client-side on `/app/*` (src/routes/app.tsx's
 * auth guard keeps the queue route's `<Outlet/>` from mounting during
 * SSR), so computing `new Date()` at render time here carries none of
 * GreetingHeader's settle-after-mount hydration risk.
 */
export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso).getTime();
  const diffMs = now.getTime() - then;
  if (!Number.isFinite(diffMs)) return "";
  if (diffMs < 0) return "just now";

  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;

  if (diffMs < minute) return "just now";
  if (diffMs < hour) return `${Math.floor(diffMs / minute)}m ago`;
  if (diffMs < day) return `${Math.floor(diffMs / hour)}h ago`;
  if (diffMs < 7 * day) return `${Math.floor(diffMs / day)}d ago`;

  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
