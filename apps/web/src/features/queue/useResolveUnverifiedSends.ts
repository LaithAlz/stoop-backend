/**
 * The #252 unverified-send resolution rule, shared by every caller as of
 * issue #279. Previously lived inline in src/routes/app.index.tsx (Home)
 * only — the conversation thread (src/routes/app.conversations.$id.tsx)
 * never wired it at all, so an ambiguous edit-and-send failure there
 * raised the flag (useDraftActions.ts) and nothing ever resolved it, and
 * Send stayed enabled through the exact window the flag exists to close.
 *
 * `GET /v1/queue` is the resolution source on BOTH routes, not just Home:
 * it lists every one of the landlord's cases still `awaiting_approval`
 * with a `pending` draft, unscoped to any one conversation, so it is a
 * valid "is this draft still pending" read for a flag raised on EITHER
 * surface. The conversation thread already fetches it today (for the tab
 * bar's queue-count badge — src/routes/app.conversations.$id.tsx), so
 * reusing it here needs no second query.
 *
 * Extracted into one hook, not copied twice, because this rule already
 * took two safety rounds to get right and both earlier, wrong attempts
 * SHIPPED and were inert — see the two comment blocks (F1, F11) below.
 * A second hand-copied version on the thread route would have been a
 * third chance to drift.
 */
import { useEffect } from "react";
import { QUEUE_REFETCH_INTERVAL_MS } from "@/api/queue";
import type { QueueResponse } from "@/api/types";

/**
 * How long past an ambiguous edit-and-send failure a queue read must be
 * before "the draft is still pending" is trusted enough to re-enable Send.
 *
 * Sized against the SERVER's worst case, not the client's poll interval.
 * The API's per-case advisory lock retries for
 * `_CASE_LOCK_MAX_WAIT_SECONDS = 30s` (apps/api/app/agent/graph.py)
 * BEFORE the graph resume even begins, so an edit-and-send can
 * legitimately commit ~30s after the request started while the client
 * stamped its failure in the first second. One poll interval was shorter
 * than that ceiling and left a window where a qualifying read still
 * predated the commit. Two intervals clears it with margin; the cost is
 * Send staying disabled a little longer under an on-screen explanation,
 * which is the safe direction by construction.
 */
export const UNVERIFIED_SETTLE_MS = 2 * QUEUE_REFETCH_INTERVAL_MS;

export interface UseResolveUnverifiedSendsOptions {
  /** The queue read both routes already fetch — Home as its primary data
   *  source, the thread for its tab-bar badge count. */
  data: QueueResponse | undefined;
  /** TanStack Query's own `dataUpdatedAt` for that same read — the CLIENT
   *  clock moment this particular payload was received, directly
   *  comparable to `markUnverifiedSend`'s `failedAt` stamp (same clock,
   *  same units; see unverifiedSendStore.ts). */
  dataUpdatedAt: number;
  /** `useDraftActions().unverifiedSendIds` — sourced from the shared
   *  module-scope store as of #279, so this is the SAME map regardless of
   *  which route's `useDraftActions` instance is asking. */
  unverifiedSendIds: ReadonlyMap<string, number>;
  /** `useDraftActions().resolveUnverifiedSend`. */
  resolveUnverifiedSend: (draftId: string, stillPending: boolean) => void;
}

export interface DueUnverifiedResolution {
  draftId: string;
  /** Whether the draft was still present in the fresh read — `true`
   *  means the edit-and-send never applied (re-enable Send); `false`
   *  means it's gone (the notice + editor-close branch). */
  stillPending: boolean;
}

/**
 * R3-1 (safety review round 3 follow-up, issue #252): the resolution rule
 * itself, pure and React-free — like queueEntries.ts's own reducer, kept
 * this way specifically so it's directly exercisable without a DOM (web
 * has no test runner yet, #294). `useResolveUnverifiedSends` below is a
 * thin `useEffect` wrapper that calls this and fires
 * `resolveUnverifiedSend` for whatever it returns; nothing else in this
 * file duplicates the rule.
 *
 * Resolves any flagged `unverifiedSendIds` against a queue read that
 * completed AFTER the failure that raised them — still listed as a card
 * means the edit never applied (re-enable Send); gone means it did.
 *
 * F1 (safety re-verify, #252): resolve ONLY against a read that completed
 * after the failure. Without the `dataUpdatedAt` comparison this fired on
 * the very next commit — when the query's `data` is still the last
 * successful payload from BEFORE the send, which of course still lists
 * the draft — so it resolved "still pending", re-enabled Send about one
 * frame later, and the guard did nothing at all. `isFetching` alone isn't
 * sufficient; the generation is.
 *
 * F11 (safety re-verify round 2, #252): the two directions need DIFFERENT
 * evidence, because the server request outlives the client's error.
 * `POST /v1/drafts/{id}/edit-and-send` synchronously resumes the
 * LangGraph thread under a per-case lock — hundreds of ms to seconds —
 * and the ambiguous triggers (edge 504, client timeout, dropped
 * connection) all leave the origin still working. So a read completing
 * 200ms after the failure can honestly report the draft still `pending`
 * while the origin commits a second later. Resolving "still pending" on
 * that read re-enables Send permanently, and the retype-and-resend lands
 * on the idempotent 200 that discards the new body.
 *   gone          -> definitive on the FIRST post-failure read (the
 *                    editor closes either way; a resend can only 409 or
 *                    hit the idempotent 200).
 *   still pending -> only trustworthy once a full poll interval has
 *                    elapsed past the failure, by which time an in-flight
 *                    commit has long landed. Costs one extra poll of dead
 *                    Send under the explanatory line, the safe direction
 *                    by construction.
 */
export function dueUnverifiedResolutions(
  items: QueueResponse["items"] | undefined,
  dataUpdatedAt: number,
  unverifiedSendIds: ReadonlyMap<string, number>,
): DueUnverifiedResolution[] {
  if (!items) return [];
  const freshIds = new Set(items.map((item) => item.draft_id));
  const due: DueUnverifiedResolution[] = [];
  for (const [draftId, failedAt] of unverifiedSendIds) {
    if (dataUpdatedAt <= failedAt) continue;
    const stillPending = freshIds.has(draftId);
    if (stillPending && dataUpdatedAt <= failedAt + UNVERIFIED_SETTLE_MS) continue;
    due.push({ draftId, stillPending });
  }
  return due;
}

export function useResolveUnverifiedSends({
  data,
  dataUpdatedAt,
  unverifiedSendIds,
  resolveUnverifiedSend,
}: UseResolveUnverifiedSendsOptions): void {
  useEffect(() => {
    for (const { draftId, stillPending } of dueUnverifiedResolutions(
      data?.items,
      dataUpdatedAt,
      unverifiedSendIds,
    )) {
      resolveUnverifiedSend(draftId, stillPending);
    }
  }, [data, dataUpdatedAt, unverifiedSendIds, resolveUnverifiedSend]);
}
