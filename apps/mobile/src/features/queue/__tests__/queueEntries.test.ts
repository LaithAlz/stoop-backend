/**
 * Pure logic tests for the Home queue's local state machine — no
 * React/RN/network involved (see queueEntries.ts's docstring). Covers the
 * issue #210 M1 brief's explicit ask: the approve→undo countdown state
 * machine, and the skip-muted state persisting past a server refetch.
 */
import type { QueueItem } from "@/api/types";
import {
  buildQueueView,
  draftStaleNotice,
  pruneSkippedSnapshots,
  queueEntriesReducer,
  secondsRemaining,
  totalUndoSeconds,
} from "../queueEntries";

function makeItem(overrides: Partial<QueueItem> = {}): QueueItem {
  return {
    case_id: "case-1",
    draft_id: "draft-1",
    severity: "urgent",
    title: null,
    property_label: "41 Palmerston",
    tenant_name: "Maria",
    unit: "2",
    received_at: "2026-07-16T08:00:00Z",
    tenant_message: "no heat since last night",
    draft_body: "Hi Maria — so sorry, sending someone today.",
    draft_recipient: "tenant",
    why: "No heat overnight can't wait.",
    reasoning: ["no heat + overnight"],
    refusal_flags: [],
    has_media: false,
    media_note: null,
    ...overrides,
  };
}

describe("queueEntriesReducer — approve/undo state machine", () => {
  it("moves a draft into 'sending' with the server's own undo_until on approve", () => {
    const state = queueEntriesReducer(
      {},
      {
        type: "approved",
        draftId: "draft-1",
        undoUntil: "2026-07-16T08:00:05Z",
        approvedAt: "2026-07-16T08:00:00Z",
      },
    );
    expect(state["draft-1"]).toEqual({
      status: "sending",
      undoUntil: "2026-07-16T08:00:05Z",
      approvedAt: "2026-07-16T08:00:00Z",
    });
  });

  it("undo clears the local entry back to no override (idle)", () => {
    const sending = queueEntriesReducer(
      {},
      { type: "approved", draftId: "draft-1", undoUntil: "x", approvedAt: "y" },
    );
    const undone = queueEntriesReducer(sending, { type: "undone", draftId: "draft-1" });
    expect(undone["draft-1"]).toBeUndefined();
  });

  it("expired only fires from 'sending' — never overwrites idle/skipped by accident", () => {
    const idle = queueEntriesReducer({}, { type: "expired", draftId: "draft-1" });
    expect(idle).toEqual({});

    const sending = queueEntriesReducer(
      {},
      { type: "approved", draftId: "draft-1", undoUntil: "x", approvedAt: "y" },
    );
    const sent = queueEntriesReducer(sending, { type: "expired", draftId: "draft-1" });
    expect(sent["draft-1"]).toEqual({ status: "sent" });
  });

  it("secondsRemaining clamps at zero and never goes negative", () => {
    const now = new Date("2026-07-16T08:00:10Z");
    expect(secondsRemaining("2026-07-16T08:00:05Z", now)).toBe(0);
    expect(secondsRemaining("2026-07-16T08:00:15Z", now)).toBe(5);
  });

  it("totalUndoSeconds derives the ticket's progress-bar denominator from the two server timestamps", () => {
    expect(
      totalUndoSeconds({ approvedAt: "2026-07-16T08:00:00Z", undoUntil: "2026-07-16T08:00:05Z" }),
    ).toBe(5);
  });
});

describe("buildQueueView — skip keeps the card visible and muted", () => {
  it("renders a fresh item as idle when it has no local override", () => {
    const item = makeItem();
    const rows = buildQueueView([item], {}, {});
    expect(rows).toEqual([{ item, entry: { status: "idle" } }]);
  });

  it("keeps a skipped card visible from its snapshot after the server drops it from `items`", () => {
    const item = makeItem();
    const entries = queueEntriesReducer({}, { type: "skipped", draftId: item.draft_id });
    const snapshots = { [item.draft_id]: item };

    // The server's next `items` no longer includes this case (skip doesn't
    // re-queue it) — the row must still render, muted, per the founder
    // ruling ("No reply sent — case still open").
    const rows = buildQueueView([], entries, snapshots);

    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({ item, entry: { status: "skipped" } });
  });

  it("does not resurrect a skipped card with no snapshot on file", () => {
    const entries = queueEntriesReducer({}, { type: "skipped", draftId: "draft-1" });
    const rows = buildQueueView([], entries, {});
    expect(rows).toEqual([]);
  });
});

describe("pruneSkippedSnapshots — M1 senior advisory (snapshots die with their skip)", () => {
  it("drops a snapshot whose entry was cleared (e.g. the reject call failed)", () => {
    const item = makeItem();
    const skipped = queueEntriesReducer({}, { type: "skipped", draftId: item.draft_id });
    const cleared = queueEntriesReducer(skipped, { type: "cleared", draftId: item.draft_id });

    const pruned = pruneSkippedSnapshots({ [item.draft_id]: item }, cleared);

    expect(pruned).toEqual({});
  });

  it("keeps the snapshot while the skip is still live", () => {
    const item = makeItem();
    const entries = queueEntriesReducer({}, { type: "skipped", draftId: item.draft_id });

    const pruned = pruneSkippedSnapshots({ [item.draft_id]: item }, entries);

    expect(pruned).toEqual({ [item.draft_id]: item });
  });

  it("returns the SAME object when nothing needs pruning — a setState caller must not re-render", () => {
    const item = makeItem();
    const entries = queueEntriesReducer({}, { type: "skipped", draftId: item.draft_id });
    const snapshots = { [item.draft_id]: item };

    expect(pruneSkippedSnapshots(snapshots, entries)).toBe(snapshots);
    expect(pruneSkippedSnapshots({}, {})).toEqual({});
  });
});

describe("draftStaleNotice", () => {
  it("names the tenant in the honest one-line note (conversation-model.md's own example wording)", () => {
    expect(draftStaleNotice("Maria")).toBe("Maria replied — this draft just updated.");
  });
});
