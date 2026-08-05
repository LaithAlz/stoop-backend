/**
 * The approval queue's local state machine — pure, no React import, so
 * it's unit-testable exactly like src/auth/resolveAuthRoute.ts (web has no
 * test runner configured yet — see the PR report — but this stays pure
 * for whenever one lands). Ported near-verbatim from
 * apps/mobile/src/features/queue/queueEntries.ts (campaign issue #234
 * PR 2). src/routes/app.index.tsx layers this over the server's
 * `GET /v1/queue` data with a `useReducer`; nothing here talks to the
 * network.
 *
 * Why a local overlay at all, when the server is the source of truth: the
 * queue only ever lists cases still needing action, so the moment a draft
 * is approved or skipped, a fresh server fetch would just drop the card —
 * but the founder ruling (src/components/clarity/SkippedCard.tsx) is that
 * Skip keeps the card visible, muted ("No reply sent — case still open"),
 * and Approve needs to show a live undo countdown before the card can
 * honestly disappear. Both are client-side presentation states on top of
 * a server that has already moved on.
 */
import type { QueueItem } from "@/api/types";

/**
 * `undoExpiresAtClient` / `approvedAtClient` are both CLIENT `Date.now()`
 * epoch milliseconds — never a raw server timestamp string. B2 (safety
 * review, #234 PR 2): the previous shape stored the server's `undo_until`
 * string here and compared it directly against `new Date()` at render
 * time, silently mixing the server's clock with the client's — a client
 * clock a couple of minutes fast would swallow the whole undo window with
 * no error. `computeUndoExpiresAt` below is the ONE place that crosses
 * clocks (using the approve response's own `Date` header as the anchor);
 * everything downstream of it — this reducer, `secondsRemaining`,
 * `totalUndoSeconds` — works purely in client-clock numbers.
 */
/**
 * `undoAmbiguousAt` (BLOCKER 1, safety review ROUND 4, #291/#279): the
 * CLIENT `Date.now()` moment an Undo tap on THIS entry hit an ambiguous
 * failure (useDraftActions.ts's `undoMutation` onError, the network-error/
 * 5xx branch), undefined on every entry that arm never fired for,
 * including a plain, uneventful Approve. Note what it is NOT: a reliable
 * "no undo was attempted" signal. A `draft_not_undoable` or an
 * `already_sent` undo also leaves it undefined, because the server
 * answered those and they need no evidence. This is deliberately
 * NOT derived from `approvedAtClient`; see the two retirement effects that
 * read it (src/routes/app.index.tsx, app.conversations.$id.tsx) for why
 * round 3's `dataUpdatedAt > approvedAtClient` check was wrong on its own
 * terms, not just under-scoped.
 */
export type QueueEntry =
  | { status: "idle" }
  | {
      status: "sending";
      undoExpiresAtClient: number;
      approvedAtClient: number;
      undoAmbiguousAt?: number;
    }
  | { status: "sent"; approvedAtClient: number; undoAmbiguousAt?: number }
  | { status: "skipped" };

/** Keyed by `draft_id` — the id that drives approve/undo/reject per the
 *  queue contract (api-contracts.md: "Which id drives which action"). */
export type QueueEntriesState = Record<string, QueueEntry>;

export type QueueEntriesAction =
  | { type: "approved"; draftId: string; undoExpiresAtClient: number; approvedAtClient: number }
  | { type: "undone"; draftId: string }
  | { type: "expired"; draftId: string }
  | { type: "skipped"; draftId: string }
  | { type: "cleared"; draftId: string }
  // BLOCKER 1 (safety review round 4, #291/#279): dispatched ONLY from
  // useDraftActions.ts's `undoMutation` onError's ambiguous branch, the
  // one place an undo was genuinely attempted and the server's answer is
  // genuinely unknown. Never dispatched by a plain Approve, so no stale
  // read can ever satisfy `undoAmbiguousAt !== undefined` on an entry
  // nothing was ever attempted against.
  | { type: "undoAmbiguous"; draftId: string; at: number; approvedAtClient: number };

const IDLE: QueueEntry = { status: "idle" };

export function queueEntriesReducer(
  state: QueueEntriesState,
  action: QueueEntriesAction,
): QueueEntriesState {
  switch (action.type) {
    case "approved":
      return {
        ...state,
        [action.draftId]: {
          status: "sending",
          undoExpiresAtClient: action.undoExpiresAtClient,
          approvedAtClient: action.approvedAtClient,
        },
      };
    case "expired": {
      const current = state[action.draftId];
      if (current?.status !== "sending") return state;
      // BLOCKER 1 (safety review round 3, #291/#279): `approvedAtClient`
      // used to be dropped here. Kept on "sent" now for its own sake (see
      // that field's own comment on the type above); ROUND 4 also carries
      // `undoAmbiguousAt` through this same transition unchanged, for the
      // identical reason, the retirement effects need it to survive
      // "sending" -> "sent", not just "approved" -> "sending".
      return {
        ...state,
        [action.draftId]: {
          status: "sent",
          approvedAtClient: current.approvedAtClient,
          undoAmbiguousAt: current.undoAmbiguousAt,
        },
      };
    }
    // BLOCKER 1 (safety review round 4, #291/#279): stamps the ONE piece
    // of positive evidence the two retirement effects can trust, see the
    // action's own comment above and `undoAmbiguousAt`'s comment on the
    // type.
    //
    // Round 5 found round 4 gated this on "sending" alone, which threw
    // the evidence away in the DOMINANT case. The countdown that flips
    // sending to sent is an independent 1s timer that does not know an
    // undo is in flight, and an ambiguous failure is BY CONSTRUCTION the
    // slow kind (dropped TCP, client timeout, edge 504). `already_sent`
    // and `draft_not_undoable` are fast because the server answered;
    // this arm is the one that does not. So the stamp landed only on
    // ambiguous failures quick enough to beat a 5 second timer, which is
    // the least likely ambiguous failure there is, and everything slower
    // settled on a permanent controls-less "Sent." with the draft still
    // pending on the server and the tenant unanswered.
    //
    // Accepting "sent" keeps this positive evidence: still written from
    // exactly one arm (the ambiguous branch of undoMutation.onError), and
    // never by an approve, an edit-and-send, a successful undo,
    // `already_sent`, or `draft_not_undoable`. Both retirement effects
    // re-run on `entries`, and that arm calls `onSettled()` right after
    // this dispatch, so the qualifying read lands immediately behind it.
    // A dispatch against a cleared, undone or skipped entry is still
    // dropped: those no longer exist, or no longer describe something
    // that could be in flight.
    case "undoAmbiguous": {
      const current = state[action.draftId];
      if (current?.status !== "sending" && current?.status !== "sent") return state;
      // F2 (safety review round 6): scoped to the approve cycle the undo
      // was fired from. `approved` builds a fresh entry with no stamp, so
      // a re-approve resets it, but a cycle-1 DELETE still hung when
      // cycle 2 begins could otherwise stamp cycle 2's entry with cycle
      // 1's failure. The reviewer could not turn that into user harm (the
      // retirement effects also need a read resolving 45+ seconds late
      // while still listing the draft, and the `onSettled()` refetch one
      // line after the dispatch wins that race in every ordering they
      // built), but `apiRequest` passes no AbortSignal for undo, so the
      // hung-fetch half is genuinely unbounded. Three lines kills the
      // class rather than relying on a race staying won.
      if (current.approvedAtClient !== action.approvedAtClient) return state;
      return {
        ...state,
        [action.draftId]: { ...current, undoAmbiguousAt: action.at },
      };
    }
    case "skipped":
      return { ...state, [action.draftId]: { status: "skipped" } };
    case "undone":
    case "cleared": {
      if (!(action.draftId in state)) return state;
      const next = { ...state };
      delete next[action.draftId];
      return next;
    }
    default:
      return state;
  }
}

export function entryFor(state: QueueEntriesState, draftId: string): QueueEntry {
  return state[draftId] ?? IDLE;
}

/**
 * Seconds left in the undo window, clamped to >= 0 — a pure client-clock
 * delta against `undoExpiresAtClient` (itself already anchored to the
 * server's clock once, at receipt time, by `computeUndoExpiresAt` below).
 * Never re-parses a server timestamp here (B2).
 *
 * A3 (safety review, #234 PR 2): guarded against a non-finite
 * `undoExpiresAtClient` so a bad value renders as "no time left" (the
 * undo ticket's `00:00`, never `00:NaN`). As of round 3
 * `computeUndoExpiresAt` always returns a finite number, so this is
 * belt-and-suspenders against any future caller that doesn't.
 */
export function secondsRemaining(undoExpiresAtClient: number, now: number = Date.now()): number {
  if (!Number.isFinite(undoExpiresAtClient)) return 0;
  const diffMs = undoExpiresAtClient - now;
  return Math.max(0, Math.round(diffMs / 1000));
}

/** For the undo ticket's progress bar only (a visual nicety) — the actual
 *  gate on whether Undo still works is the server's `undo_until`, checked
 *  by the DELETE call itself, not this number. A3: guarded the same way as
 *  `secondsRemaining` — an unparsable window falls back to `1` (a full,
 *  already-elapsed bar) rather than a NaN-driven width. */
export function totalUndoSeconds(entry: {
  undoExpiresAtClient: number;
  approvedAtClient: number;
}): number {
  const totalMs = entry.undoExpiresAtClient - entry.approvedAtClient;
  if (!Number.isFinite(totalMs)) return 1;
  return Math.max(1, Math.round(totalMs / 1000));
}

/** The contract's own undo window (api-contracts.md: `scheduled_send_at =
 *  now() + 5s`) — used ONLY as the no-anchor fallback below, never to
 *  shorten a window the server actually reported. */
const UNDO_WINDOW_FALLBACK_MS = 5_000;

/**
 * B2 (safety review, #234 PR 2): the one place a server timestamp
 * (`undo_until`) and the server's OWN clock (the approve response's `Date`
 * header) meet — everything after this returns is pure client-clock math.
 * `windowMs` is how long the server itself thinks the undo window lasts;
 * adding that to the CLIENT's `Date.now()` at receipt time gives an
 * expiry that's honest even when the client's wall clock is skewed from
 * the server's.
 *
 * The no-anchor fallback (round 3): `Date` is NOT a CORS-safelisted
 * response header, so a browser hands us `null` here unless the API
 * exposes it (`Access-Control-Expose-Headers: Date` — #251). When the
 * anchor is missing OR either timestamp is unparsable, the round-2
 * fallback re-parsed `undo_until` against the client clock — the exact
 * pre-B2 bug: a client clock 6s fast silently deletes the whole 5s
 * window. Now it degrades to the contract's full window from receipt
 * time instead — the landlord always gets their 5 seconds to tap Undo.
 * Fail-open is correct here because this number only ever gates the
 * OFFER of undo; the server's DELETE call is the real gate, and a
 * too-late tap comes back `already_sent`, which flips the card to an
 * honest "sent" state (useDraftActions.ts).
 */
export function computeUndoExpiresAt(
  undoUntil: string,
  serverDateHeader: string | null | undefined,
  receivedAtClient: number = Date.now(),
): number {
  const undoUntilMs = Date.parse(undoUntil);
  const serverNowMs = serverDateHeader ? Date.parse(serverDateHeader) : NaN;
  if (Number.isFinite(undoUntilMs) && Number.isFinite(serverNowMs)) {
    return receivedAtClient + (undoUntilMs - serverNowMs);
  }
  return receivedAtClient + UNDO_WINDOW_FALLBACK_MS;
}

export interface QueueViewRow {
  item: QueueItem;
  entry: QueueEntry;
}

/**
 * A last-known `QueueItem` plus WHERE it sat in the fresh `items` array
 * the moment it needed pinning. Item 6 (safety review, #291/#279):
 * without the index, a pinned row could only ever be appended to the
 * end of the list, so approving the FIRST of several cards and then
 * losing its server row to a mid-window refetch would visibly walk the
 * card to the bottom of the queue while its 5 second Undo was still
 * live: the landlord's thumb stays where it was, the card does not.
 * Captured once, by this file's callers (src/routes/app.index.tsx), at
 * the moment the local overlay first pins the draft; not recomputed on
 * every render.
 */
export interface QueueSnapshot {
  item: QueueItem;
  index: number;
}

/**
 * Merges fresh `GET /v1/queue` items with the local overlay. THREE entry
 * statuses persist past their server row disappearing from `items`, from
 * their own last-known snapshot in `queueSnapshots`: `skipped` (the
 * founder ruling, the card stays visible, muted), `sending` (issue #291),
 * and `sent` (item 5, safety review #291/#279, see below).
 *
 * #291: `GET /v1/drafts/{id}/approve` moves the row to `status='approved'`
 * server-side immediately, and `GET /v1/queue` INNER JOINs the pending
 * draft, so an approved draft (or a successful edit-and-send, which
 * dispatches the identical "approved" action) leaves `items` on the VERY
 * NEXT read, well before the undo window (still running server-side) has
 * elapsed. Without a pin here, any of Home's several refetch triggers
 * landing mid-window (the 20s poll, the window-focus refetch, another
 * card's skip/error `onSettled`, or `useAcknowledge`'s unconditional queue
 * invalidation) deletes the card and its Undo control while the undo is
 * still live and the landlord has no way to press it.
 *
 * Item 8 (safety review, #291/#279, this file's own house rule: every
 * defect found here so far has been a comment asserting a guarantee the
 * code no longer had): a prior revision of this paragraph claimed
 * `sending` "is released from the pin the moment it stops being true: the
 * countdown hitting zero or a successful undo, never before." That was
 * already wrong the day it was written. BLOCKER 1 (useDraftActions.ts's
 * `undoMutation`) is a THIRD release path: an undo call that fails
 * ambiguously (a dropped connection, a 5xx) must NOT clear the entry,
 * because the reply may genuinely still be sending, and this function has
 * no way to tell the difference from here. This docstring makes no claim
 * about the exhaustive list of ways a `sending`/`sent` pin ever gets
 * released, only about what it does while one of those three statuses is
 * still the honest state, which is the one thing this function can
 * actually promise.
 *
 * Item 5 (safety review, #291/#279; corrected in round 3's re-verify,
 * see Finding 3; QUALIFIED AGAIN in round 4, see the correction below): a
 * prior revision of this paragraph claimed `sending`'s transition to
 * `"sent"` needed to stay pinned for one more commit so `QueueRow`'s
 * focus-return effect and the "Sent." confirmation text both had a chance
 * to react to it before the row could unmount out from under them.
 * Measured (a MutationObserver plus a `requestAnimationFrame` poll,
 * against the exact mid-window-refetch case this was written for, i.e. a
 * qualifying read had already landed by the time the countdown expired):
 * that benefit is a no-op there, Home's own parent-level retirement
 * effect (src/routes/app.index.tsx) commits in the SAME pass as this
 * pin's own consumer, so "sent" is dispatched-and-cleared before the
 * browser ever paints a frame with "Sent." on screen in THAT case: zero
 * of several hundred sampled painted frames showed it.
 *
 * Round 4 correction: that measurement is true only when a qualifying
 * read has already landed before expiry, it is not the ordinary case.
 * A plain Approve triggers no refetch of its own (this hook's own
 * docstring, "the local overlay IS the honest UI state"), and the queue's
 * background poll is 20 seconds, so on an ordinary approve with no
 * concurrent refetch in flight, nothing satisfies either retirement
 * effect's gate (round 4: `undoAmbiguousAt !== undefined`) and "Sent."
 * paints and HOLDS, same session, until something else refetches. Whether
 * this pin has any real benefit in that ordinary case was never
 * measured; take Item 8 above at its word instead: this docstring
 * makes no claim about why a `sending`/`sent` pin is released when it is,
 * only about what `buildQueueView` does while one of those three statuses
 * is still the honest state. `sent` stays pinned here regardless, because
 * BLOCKER 1 (useDraftActions.ts's `undoMutation` onError, src/routes/
 * app.index.tsx's retirement effect) needs the snapshot's `approvedAtClient`
 * AND (round 4) `undoAmbiguousAt` to survive the sending -> sent
 * transition to tell an honest "sent" apart from a stuck one, a real
 * reason, just not the one originally written here. src/routes/
 * app.index.tsx's own effect is what retires the pin for good, one read
 * later, by dispatching `cleared` once the draft has genuinely left a
 * fresh `items` read OR a fresh read lands after a genuinely ambiguous
 * undo attempt on this same entry. This function has no opinion on when
 * that happens, only on not unmounting the row out from under a status
 * change it didn't cause.
 *
 * A `sending`/`sent` row already present in `items` uses the SNAPSHOT's
 * `draft_body`, not the live item's, whenever a
 * snapshot exists (item 3 / BLOCKER 3, safety review #291/#279): a
 * successful edit-and-send does not itself trigger a refetch, so the very
 * next commit after Approve can still find this draft in a STALE `items`
 * read (any of the refetch triggers listed above, racing the edit) with
 * its OLD, pre-edit body, while the snapshot captured at submit time
 * already carries the body that actually went out. Without this, the
 * pinned bubble shows the wrong text first and the right text moments
 * later, for exactly the seconds the landlord is deciding whether to tap
 * Undo. One body for the whole window, sourced from the same snapshot
 * either way the row is built.
 *
 * EXCEPT the item currently open in the edit-and-send panel
 * (`pinnedEditingItem`, A7 below), a separate single-slot pin because at
 * most one editor is ever open at a time.
 */
export function buildQueueView(
  items: QueueItem[],
  entries: QueueEntriesState,
  queueSnapshots: Record<string, QueueSnapshot>,
  pinnedEditingItem?: QueueItem | null,
): QueueViewRow[] {
  const seen = new Set(items.map((item) => item.draft_id));
  const rows: QueueViewRow[] = items.map((item) => {
    const entry = entryFor(entries, item.draft_id);
    const snapshot = queueSnapshots[item.draft_id];
    // BLOCKER 3: prefer the snapshot's body for the row this live item
    // would otherwise build, for as long as it's pinned-worthy. A plain
    // Approve's snapshot body always matches the live item's anyway (the
    // body never changed), so this is a no-op in that case.
    if ((entry.status === "sending" || entry.status === "sent") && snapshot) {
      return { item: { ...item, draft_body: snapshot.item.draft_body }, entry };
    }
    return { item, entry };
  });

  // Item 6: collect every row whose server item has already dropped out
  // of `items`, sorted by where it was LAST seen, then splice each back
  // into that position (adjusting for the ones already spliced ahead of
  // it) instead of appending. An approved-and-dropped first card stays
  // the first card while its Undo ticket is live.
  const pinned: Array<{ index: number; row: QueueViewRow }> = [];
  for (const [draftId, entry] of Object.entries(entries)) {
    if (
      (entry.status === "skipped" || entry.status === "sending" || entry.status === "sent") &&
      !seen.has(draftId)
    ) {
      const snapshot = queueSnapshots[draftId];
      if (snapshot) pinned.push({ index: snapshot.index, row: { item: snapshot.item, entry } });
    }
  }
  pinned.sort((a, b) => a.index - b.index);
  pinned.forEach(({ index, row }, alreadySpliced) => {
    const insertAt = Math.min(Math.max(index + alreadySpliced, 0), rows.length);
    rows.splice(insertAt, 0, row);
  });

  // A7 (safety review, #234 PR 2): a routine 20s background poll must
  // never unmount an open editor out from under the landlord mid-type. If
  // the item they're editing has fallen out of the server's fresh
  // `items` (e.g. it briefly drops off the list on an unrelated field
  // update), keep rendering it from its last-known snapshot — the caller
  // (src/routes/app.index.tsx) only ever passes one in while that draft's
  // editor is actually open, and stops as soon as the landlord closes or
  // submits it.
  // Finding 6 (safety review round 3, #291/#279, a trap noted, not fixed
  // here): this push has no "is `pinnedEditingItem.draft_id` already one
  // of the `pinned` rows spliced in above" check. Not reachable today.
  // `entryFor` returns exactly one status per draft id, and the caller
  // (src/routes/app.index.tsx) only ever passes a `pinnedEditingItem`
  // while `editingContext` is open for that same id, a state Approve/Skip
  // can't also be mid-flight on. But `sent` widened the set of statuses
  // this file pins past their server row, and the widening is what makes
  // the trap worth naming: a draft that were EVER both `sent`-pinned (via
  // `queueSnapshots`) and the open-editor pin at once would render twice
  // under the identical React key (`row.item.draft_id`, src/routes/
  // app.index.tsx's `<QueueRow key={row.item.draft_id}>`). If a future
  // change ever lets editing reopen on a draft this file still considers
  // `sent`, this is where that breaks.
  if (pinnedEditingItem && !seen.has(pinnedEditingItem.draft_id)) {
    rows.push({ item: pinnedEditingItem, entry: entryFor(entries, pinnedEditingItem.draft_id) });
  }

  return rows;
}

/** The `draft_stale` one-line note (409, `fresh_draft_id` in the error body
 *  — api-contracts.md's Drafts section + conversation-model.md's own
 *  example "Maria replied — draft updated"). Kept as a named export so the
 *  exact wording lives in one place rather than inlined in the screen. */
export function draftStaleNotice(tenantFirstName: string): string {
  return `${tenantFirstName} replied. This draft just updated.`;
}

/**
 * M1 senior advisory (mobile, ported here verbatim): drop any snapshot
 * whose entry is no longer one of the statuses `buildQueueView` above
 * ever pins (`skipped`, `sending`, or, item 5, safety review #291/#279,
 * `sent`). A skip or approve that failed (its entry was `cleared` by the
 * error handler), or an undo/expiry that resolved it, would otherwise
 * leave its snapshot in Home's map forever, keeping a stale card
 * resurrectable and tenant text pinned in memory past its purpose.
 * Returns the SAME object when nothing needs pruning so a `setState`
 * caller can bail without re-rendering.
 */
export function pruneQueueSnapshots(
  snapshots: Record<string, QueueSnapshot>,
  entries: QueueEntriesState,
): Record<string, QueueSnapshot> {
  const staleIds = Object.keys(snapshots).filter((draftId) => {
    const status = entries[draftId]?.status;
    return status !== "skipped" && status !== "sending" && status !== "sent";
  });
  if (staleIds.length === 0) return snapshots;
  const next = { ...snapshots };
  for (const draftId of staleIds) delete next[draftId];
  return next;
}
