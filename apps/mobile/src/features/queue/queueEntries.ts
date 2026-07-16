/**
 * The approval queue's local state machine — pure, no React/RN import, so
 * it's unit-testable exactly like src/auth/resolveAuthRoute.ts. Home
 * (src/app/(tabs)/index.tsx) layers this over the server's `GET /v1/queue`
 * data with a `useReducer`; nothing here talks to the network.
 *
 * Why a local overlay at all, when the server is the source of truth: the
 * queue only ever lists cases still needing action, so the moment a draft
 * is approved or skipped, a fresh server fetch would just drop the card —
 * but the founder ruling (apps/web/src/components/clarity/SkippedCard.tsx)
 * is that Skip keeps the card visible, muted ("No reply sent — case still
 * open"), and Approve needs to show a live undo countdown before the card
 * can honestly disappear. Both are client-side presentation states on top
 * of a server that has already moved on.
 */
import type { QueueItem } from "@/api/types";

export type QueueEntry =
  | { status: "idle" }
  | { status: "sending"; undoUntil: string; approvedAt: string }
  | { status: "sent" }
  | { status: "skipped" };

/** Keyed by `draft_id` — the id that drives approve/undo/reject per the
 *  queue contract (api-contracts.md: "Which id drives which action"). */
export type QueueEntriesState = Record<string, QueueEntry>;

export type QueueEntriesAction =
  | { type: "approved"; draftId: string; undoUntil: string; approvedAt: string }
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
          undoUntil: action.undoUntil,
          approvedAt: action.approvedAt,
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

/** Seconds left in the undo window, clamped to >= 0 — derived from the
 *  server's `undo_until` (api-contracts.md: "the undo window is data"),
 *  never a client-local constant. */
export function secondsRemaining(undoUntil: string, now: Date = new Date()): number {
  const diffMs = new Date(undoUntil).getTime() - now.getTime();
  return Math.max(0, Math.round(diffMs / 1000));
}

/** For the undo ticket's progress bar only (a visual nicety) — the actual
 *  gate on whether Undo still works is the server's `undo_until`, checked
 *  by the DELETE call itself, not this number. */
export function totalUndoSeconds(entry: { undoUntil: string; approvedAt: string }): number {
  const totalMs = new Date(entry.undoUntil).getTime() - new Date(entry.approvedAt).getTime();
  return Math.max(1, Math.round(totalMs / 1000));
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
 * persists past its server row disappearing.
 */
export function buildQueueView(
  items: QueueItem[],
  entries: QueueEntriesState,
  skippedSnapshots: Record<string, QueueItem>,
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

  return rows;
}

/** The `draft_stale` one-line note (409, `fresh_draft_id` in the error body
 *  — api-contracts.md's Drafts section + conversation-model.md's own
 *  example "Maria replied — draft updated"). Kept as a named export so the
 *  exact wording is covered by a test rather than inlined in the screen. */
export function draftStaleNotice(tenantFirstName: string): string {
  return `${tenantFirstName} replied — this draft just updated.`;
}
