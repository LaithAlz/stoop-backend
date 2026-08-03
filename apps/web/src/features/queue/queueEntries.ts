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
export type QueueEntry =
  | { status: "idle" }
  | { status: "sending"; undoExpiresAtClient: number; approvedAtClient: number }
  | { status: "sent" }
  | { status: "skipped" };

/** Keyed by `draft_id` — the id that drives approve/undo/reject per the
 *  queue contract (api-contracts.md: "Which id drives which action"). */
export type QueueEntriesState = Record<string, QueueEntry>;

export type QueueEntriesAction =
  | { type: "approved"; draftId: string; undoExpiresAtClient: number; approvedAtClient: number }
  | { type: "undone"; draftId: string }
  | { type: "expired"; draftId: string }
  | { type: "skipped"; draftId: string }
  | { type: "cleared"; draftId: string };

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
      return { ...state, [action.draftId]: { status: "sent" } };
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
 * Merges fresh `GET /v1/queue` items with the local overlay. A skipped
 * item that has fallen out of the server's `items` (the common case — the
 * queue only lists cases still needing action) is kept visible from its
 * last-known snapshot, muted, per the founder ruling; nothing else
 * persists past its server row disappearing — EXCEPT the item currently
 * open in the edit-and-send panel (`pinnedEditingItem`, A7 below).
 */
export function buildQueueView(
  items: QueueItem[],
  entries: QueueEntriesState,
  skippedSnapshots: Record<string, QueueItem>,
  pinnedEditingItem?: QueueItem | null,
): QueueViewRow[] {
  const seen = new Set(items.map((item) => item.draft_id));
  const rows: QueueViewRow[] = items.map((item) => ({
    item,
    entry: entryFor(entries, item.draft_id),
  }));

  for (const [draftId, entry] of Object.entries(entries)) {
    if (entry.status === "skipped" && !seen.has(draftId)) {
      const snapshot = skippedSnapshots[draftId];
      if (snapshot) rows.push({ item: snapshot, entry });
    }
  }

  // A7 (safety review, #234 PR 2): a routine 20s background poll must
  // never unmount an open editor out from under the landlord mid-type. If
  // the item they're editing has fallen out of the server's fresh
  // `items` (e.g. it briefly drops off the list on an unrelated field
  // update), keep rendering it from its last-known snapshot — the caller
  // (src/routes/app.index.tsx) only ever passes one in while that draft's
  // editor is actually open, and stops as soon as the landlord closes or
  // submits it.
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
  return `${tenantFirstName} replied — this draft just updated.`;
}

/**
 * M1 senior advisory (mobile, ported here verbatim): drop any snapshot
 * whose entry is no longer "skipped" — a skip that failed (its entry was
 * `cleared` by the error handler) or otherwise resolved would leave its
 * snapshot in Home's map forever, keeping a stale card resurrectable and
 * tenant text pinned in memory past its purpose. Returns the SAME object
 * when nothing needs pruning so a `setState` caller can bail without
 * re-rendering.
 */
export function pruneSkippedSnapshots(
  snapshots: Record<string, QueueItem>,
  entries: QueueEntriesState,
): Record<string, QueueItem> {
  const staleIds = Object.keys(snapshots).filter(
    (draftId) => entries[draftId]?.status !== "skipped",
  );
  if (staleIds.length === 0) return snapshots;
  const next = { ...snapshots };
  for (const draftId of staleIds) delete next[draftId];
  return next;
}
