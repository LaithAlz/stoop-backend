/**
 * Pure logic tests for the Home queue's local state machine — no
 * React/RN/network involved (see queueEntries.ts's docstring). Covers the
 * issue #210 M1 brief's explicit ask: the approve→undo countdown state
 * machine, and the skip-muted state persisting past a server refetch; and
 * issue #250's `computeUndoExpiresAt` — the one place the server's clock
 * and the device's clock meet, plus its no-anchor fallback.
 */
import type { QueueItem } from "@/api/types";
import {
  buildQueueView,
  computeUndoExpiresAt,
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
    notification_id: null,
    ...overrides,
  };
}

describe("queueEntriesReducer — approve/undo state machine", () => {
  it("moves a draft into 'sending' with the device-clock-anchored expiry on approve", () => {
    const state = queueEntriesReducer(
      {},
      {
        type: "approved",
        draftId: "draft-1",
        undoExpiresAtClient: 1_000_005_000,
        approvedAtClient: 1_000_000_000,
      },
    );
    expect(state["draft-1"]).toEqual({
      status: "sending",
      undoExpiresAtClient: 1_000_005_000,
      approvedAtClient: 1_000_000_000,
    });
  });

  it("undo clears the local entry back to no override (idle)", () => {
    const sending = queueEntriesReducer(
      {},
      { type: "approved", draftId: "draft-1", undoExpiresAtClient: 5, approvedAtClient: 0 },
    );
    const undone = queueEntriesReducer(sending, { type: "undone", draftId: "draft-1" });
    expect(undone["draft-1"]).toBeUndefined();
  });

  it("expired only fires from 'sending' — never overwrites idle/skipped by accident", () => {
    const idle = queueEntriesReducer({}, { type: "expired", draftId: "draft-1" });
    expect(idle).toEqual({});

    const sending = queueEntriesReducer(
      {},
      { type: "approved", draftId: "draft-1", undoExpiresAtClient: 5, approvedAtClient: 0 },
    );
    const sent = queueEntriesReducer(sending, { type: "expired", draftId: "draft-1" });
    expect(sent["draft-1"]).toEqual({ status: "sent" });
  });

  it("secondsRemaining clamps at zero and never goes negative", () => {
    const now = new Date("2026-07-16T08:00:10Z").getTime();
    expect(secondsRemaining(new Date("2026-07-16T08:00:05Z").getTime(), now)).toBe(0);
    expect(secondsRemaining(new Date("2026-07-16T08:00:15Z").getTime(), now)).toBe(5);
  });

  it("secondsRemaining renders '00:00', never NaN, for a non-finite expiry", () => {
    expect(secondsRemaining(NaN)).toBe(0);
  });

  it("totalUndoSeconds derives the ticket's progress-bar denominator from the two device-clock numbers", () => {
    expect(
      totalUndoSeconds({
        approvedAtClient: new Date("2026-07-16T08:00:00Z").getTime(),
        undoExpiresAtClient: new Date("2026-07-16T08:00:05Z").getTime(),
      }),
    ).toBe(5);
  });

  it("totalUndoSeconds falls back to a full, already-elapsed bar (1) rather than NaN", () => {
    expect(totalUndoSeconds({ approvedAtClient: NaN, undoExpiresAtClient: 5 })).toBe(1);
  });
});

describe("computeUndoExpiresAt — issue #250 (the one place the two clocks meet)", () => {
  it("anchors to the server's own clock, not the device's — a fast device clock no longer swallows the window", () => {
    // The device thinks it's 08:02:00 (2 minutes fast); the server's own
    // Date header says the real time is 08:00:00, and it granted a 5s
    // window (undo_until 08:00:05). The old bug did
    // `new Date(undo_until) - Date.now()`, which here would already be
    // negative — an instant, silent zero-second window. The fix instead
    // reads the 5s the SERVER granted and applies it to the device's own
    // receipt time, so the landlord still gets a real 5 seconds.
    const deviceNow = new Date("2026-07-16T08:02:00Z").getTime();
    const expiresAt = computeUndoExpiresAt(
      "2026-07-16T08:00:05Z",
      "2026-07-16T08:00:00Z",
      deviceNow,
    );
    expect(expiresAt).toBe(deviceNow + 5_000);
    expect(secondsRemaining(expiresAt, deviceNow)).toBe(5);
  });

  it("a slow device clock still gets exactly the server's granted window, not an inflated one", () => {
    // Device clock is 3 minutes slow relative to the server.
    const deviceNow = new Date("2026-07-16T07:57:00Z").getTime();
    const expiresAt = computeUndoExpiresAt(
      "2026-07-16T08:00:05Z",
      "2026-07-16T08:00:00Z",
      deviceNow,
    );
    expect(expiresAt).toBe(deviceNow + 5_000);
  });

  it("falls back to the contract's full 5s window from receipt when the Date header is missing", () => {
    const deviceNow = new Date("2026-07-16T08:02:00Z").getTime();
    const expiresAt = computeUndoExpiresAt("2026-07-16T08:00:05Z", null, deviceNow);
    expect(expiresAt).toBe(deviceNow + 5_000);
  });

  it("falls back the same way when the Date header is present but unparsable", () => {
    const deviceNow = new Date("2026-07-16T08:02:00Z").getTime();
    const expiresAt = computeUndoExpiresAt("2026-07-16T08:00:05Z", "not-a-date", deviceNow);
    expect(expiresAt).toBe(deviceNow + 5_000);
  });

  it("falls back the same way when undo_until itself is unparsable", () => {
    const deviceNow = new Date("2026-07-16T08:02:00Z").getTime();
    const expiresAt = computeUndoExpiresAt("not-a-date", "2026-07-16T08:00:00Z", deviceNow);
    expect(expiresAt).toBe(deviceNow + 5_000);
  });

  it("never re-parses undo_until against the device clock on fallback (the original #250 bug)", () => {
    // If the fallback wrongly fell back to `new Date(undo_until) -
    // Date.now()`, a device clock far in the future would make this
    // negative/zero. It must instead be the full 5s window from receipt.
    const deviceNow = new Date("2030-01-01T00:00:00Z").getTime();
    const expiresAt = computeUndoExpiresAt("2026-07-16T08:00:05Z", null, deviceNow);
    expect(expiresAt).toBeGreaterThan(deviceNow);
    expect(secondsRemaining(expiresAt, deviceNow)).toBe(5);
  });

  it("defaults receivedAtClient to Date.now() when not passed", () => {
    const before = Date.now();
    const expiresAt = computeUndoExpiresAt("2026-07-16T08:00:05Z", "2026-07-16T08:00:00Z");
    const after = Date.now();
    expect(expiresAt).toBeGreaterThanOrEqual(before + 5_000);
    expect(expiresAt).toBeLessThanOrEqual(after + 5_000);
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
