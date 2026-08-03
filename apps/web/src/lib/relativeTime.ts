/**
 * Plain-English relative timestamps for chrome (never "soon" — concrete
 * over relative is good discipline here too: this always resolves to a
 * specific, bounded phrase, never an open-ended one). Ported from
 * apps/mobile/src/lib/relativeTime.ts (campaign issue #234 PR 2, PR 3 adds
 * `formatDayLabel`/`isSameDay` — the case-detail timeline's day dividers,
 * now that `GET /v1/cases/{id}` is wired, src/features/cases/timeline.ts).
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

/** Day-divider label — "TODAY" / "YESTERDAY" / a short date, matching the
 *  mockup's uppercase mono "day stamp" (docs/mockups/07 `.day-stamp`).
 *  Ported verbatim from apps/mobile/src/lib/relativeTime.ts. */
export function formatDayLabel(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diffDays = Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);

  if (diffDays === 0) return "TODAY";
  if (diffDays === 1) return "YESTERDAY";
  return date
    .toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
    .toUpperCase();
}

/** Same calendar day check the day-divider grouping in the case-detail
 *  timeline uses. */
export function isSameDay(a: string, b: string): boolean {
  const da = new Date(a);
  const db = new Date(b);
  return (
    da.getFullYear() === db.getFullYear() &&
    da.getMonth() === db.getMonth() &&
    da.getDate() === db.getDate()
  );
}
