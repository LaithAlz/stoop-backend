/**
 * Module-scope store for the #252 ambiguous-edit-and-send guard, hoisted
 * above any one route (issue #279).
 *
 * `useDraftActions` (this directory's own hook) is instantiated PER ROUTE:
 * Home (src/routes/app.index.tsx) and the conversation thread
 * (src/routes/app.conversations.$id.tsx) each construct their own
 * instance, with their own `useReducer`/`useState`. Before #279, the flag
 * this guard raises lived in one of those instances' local `useState`
 * (`unverifiedSendIds`), so a flag raised by an edit-and-send on ONE route
 * was invisible to the other (the thread never wired the guard at all,
 * zero references to `isSendUnverified` on that route), which meant an
 * ambiguous failure there left Send fully enabled, and a flag raised on
 * Home before navigating to the thread for the SAME draft would vanish
 * the instant Home's hook instance unmounted.
 *
 * This module is the single shared source every `useDraftActions`
 * instance now reads and writes through, so a flag raised on either
 * surface is visible, and resolvable, on both. Plain module state plus
 * `useSyncExternalStore`, not TanStack Query's cache: nothing here is a
 * server read's result (it is client-only bookkeeping about a request
 * whose outcome is still unknown), so piggybacking on the query cache
 * would either need a `queryFn` that lies about fetching or a permanently
 * `enabled: false` query with no natural invalidation story. This says
 * exactly what it is.
 */
import { useSyncExternalStore } from "react";

/** Draft id -> the CLIENT `Date.now()` epoch millisecond its edit-and-send
 *  hit an ambiguous failure at. Directly comparable to a TanStack Query
 *  `dataUpdatedAt` (same clock, same units), see
 *  `useResolveUnverifiedSends.ts`'s own resolution rule. */
export type UnverifiedSendMap = ReadonlyMap<string, number>;

let state: UnverifiedSendMap = new Map();
const listeners = new Set<() => void>();

function setState(next: UnverifiedSendMap): void {
  state = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Plain (non-hook) read of the current map, the same module-scope
 *  `state` every `useUnverifiedSendIds()` caller reads, exported so the
 *  cross-route sharing itself is directly checkable (module import, no
 *  React renderer needed) rather than only inferable from reading the
 *  hook's implementation. */
export function getUnverifiedSendSnapshot(): UnverifiedSendMap {
  return state;
}

/** Raises the flag for `draftId`. Idempotent-by-overwrite: a second
 *  ambiguous failure on the same draft (unlikely, Send is disabled while
 *  the flag is up, but not impossible if a landlord's own retry races
 *  this) simply re-stamps the failure time, which only ever makes the
 *  settle window this flag is checked against start over, the safe
 *  direction. */
export function markUnverifiedSend(draftId: string, failedAt: number): void {
  const next = new Map(state);
  next.set(draftId, failedAt);
  setState(next);
}

/**
 * Clears the flag for `draftId` if it was set. Returns whether it WAS
 * flagged before this call: the one-time resolution notice
 * (`useDraftActions.ts`'s `resolveUnverifiedSend`) is gated on this so a
 * caller iterating a stale snapshot can't double-fire it, and so a route
 * that never raised the flag itself (it was raised on the OTHER route)
 * still gets to show the "this draft isn't waiting to send anymore"
 * notice exactly once, wherever the resolving read happens to land.
 */
export function clearUnverifiedSend(draftId: string): boolean {
  if (!state.has(draftId)) return false;
  const next = new Map(state);
  next.delete(draftId);
  setState(next);
  return true;
}

/**
 * Resets the store to empty. BLOCKER 4 (safety review, #291/#279): no
 * longer test-only, despite the name this used to have
 * (`__resetUnverifiedSendStoreForTests`). This module is process-global
 * (this file's own docstring above explains why it has to be, over a
 * per-route `useState`), which means the flag it holds is cross-account
 * state sitting in a singleton. src/auth/AuthProvider.tsx now calls this
 * from the SAME two places it already clears the React Query cache for
 * the identical reason (its own PII-fence comments): an explicit
 * sign-out, and a detected identity change (a different landlord signing
 * in on the same browser/tab). Left un-reset, a flag landlord A raised
 * survives their sign-out and resolves against landlord B's first queue
 * read in the same tab. No tenant data crosses (the notice string is
 * static), but it's a cross-account artifact on a safety control, and it
 * silently discards A's own guard at the same moment. Still exported
 * plainly (not renamed back to a dunder-prefixed test helper) so it stays
 * obviously callable from both app code and this file's own harness.
 */
export function resetUnverifiedSendStore(): void {
  state = new Map();
}

export function useUnverifiedSendIds(): UnverifiedSendMap {
  return useSyncExternalStore(subscribe, getUnverifiedSendSnapshot, getUnverifiedSendSnapshot);
}
