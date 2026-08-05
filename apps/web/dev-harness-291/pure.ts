import {
  buildQueueView,
  pruneQueueSnapshots,
  queueEntriesReducer,
  type QueueEntriesState,
} from "/Users/laith/Businesses/LandlordAI-queue291/apps/web/src/features/queue/queueEntries";
import {
  dueUnverifiedResolutions,
  UNVERIFIED_SETTLE_MS,
} from "/Users/laith/Businesses/LandlordAI-queue291/apps/web/src/features/queue/useResolveUnverifiedSends";

function item(id: string, body = "body-" + id) {
  return {
    case_id: "case-" + id,
    draft_id: id,
    severity: "routine",
    title: null,
    tenant_name: "Maria Ortiz",
    property_label: "12 Ossington",
    unit: "2",
    tenant_message: "leak",
    received_at: "2026-01-01T00:00:00Z",
    has_media: false,
    media_note: null,
    draft_body: body,
    why: "why",
    reasoning: [],
    refusal_flags: [],
    recipient: "tenant",
    notification_id: null,
  } as any;
}

const log = (...a: unknown[]) => console.log(...a);

log("=== A. pin holds when row leaves items ===");
let entries: QueueEntriesState = {};
entries = queueEntriesReducer(entries, {
  type: "approved",
  draftId: "d1",
  undoExpiresAtClient: Date.now() + 5000,
  approvedAtClient: Date.now(),
});
// NOTE (round 4, preservation fix): `queueSnapshots` has been
// `Record<string, QueueSnapshot>` (`{ item, index }`), not
// `Record<string, QueueItem>`, since item 6 (round 3, #291/#279) added
// per-snapshot position pinning. This file predates that change; wrapped
// here so it still runs against the current `buildQueueView` signature.
const snaps = { d1: { item: item("d1"), index: 0 } };
log("with row still in items:", buildQueueView([item("d1"), item("d2")], entries, snaps).map((r) => [r.item.draft_id, r.entry.status]));
log("after refetch drops d1  :", buildQueueView([item("d2")], entries, snaps).map((r) => [r.item.draft_id, r.entry.status]));
log("ORDER CHECK (d1 was index 0, now?):", buildQueueView([item("d2"), item("d3")], entries, snaps).map((r) => r.item.draft_id));

log("");
log("=== B. release paths ===");
const expired = queueEntriesReducer(entries, { type: "expired", draftId: "d1" });
log("expired -> rows:", buildQueueView([], expired, snaps).map((r) => [r.item.draft_id, r.entry.status]));
const undone = queueEntriesReducer(entries, { type: "undone", draftId: "d1" });
log("undone  -> rows:", buildQueueView([], undone, snaps).map((r) => [r.item.draft_id, r.entry.status]));
const cleared = queueEntriesReducer(entries, { type: "cleared", draftId: "d1" });
log("cleared -> rows:", buildQueueView([], cleared, snaps).map((r) => [r.item.draft_id, r.entry.status]));

log("");
log("=== C. snapshot lifetime after release (prune only runs on write) ===");
log("snapshot still in map after expiry?", Object.keys(pruneQueueSnapshots(snaps, expired)).length === 0 ? "pruned (only if prune called)" : "kept");
log("prune(expired) ->", Object.keys(pruneQueueSnapshots(snaps, expired)));
log("prune(sending) ->", Object.keys(pruneQueueSnapshots(snaps, entries)));

log("");
log("=== D. resurrect check: approve fails outright ===");
// handleApprove writes the snapshot BEFORE mutate; mutation 500s -> handleError -> cleared
let e2: QueueEntriesState = {};
const snaps2 = { d9: item("d9") };
e2 = queueEntriesReducer(e2, { type: "cleared", draftId: "d9" });
log("entries after failed approve:", JSON.stringify(e2));
log("rows with row gone from items:", buildQueueView([], e2, snaps2).map((r) => r.item.draft_id));

log("");
log("=== E. skipped-on-another-device: server drops row, local entry sending ===");
log("rows:", buildQueueView([], entries, snaps).map((r) => [r.item.draft_id, r.entry.status]));

log("");
log("=== F. dueUnverifiedResolutions asymmetry ===");
log("UNVERIFIED_SETTLE_MS =", UNVERIFIED_SETTLE_MS);
const failedAt = 1_000_000;
const flags = new Map([["d1", failedAt]]);
const cases: Array<[string, number, any[]]> = [
  ["stale read (predates failure), draft present", failedAt - 1, [item("d1")]],
  ["read exactly at failedAt, draft present", failedAt, [item("d1")]],
  ["read 1ms after, draft GONE", failedAt + 1, []],
  ["read 1ms after, draft present", failedAt + 1, [item("d1")]],
  ["read at failedAt+SETTLE, draft present", failedAt + UNVERIFIED_SETTLE_MS, [item("d1")]],
  ["read at failedAt+SETTLE+1, draft present", failedAt + UNVERIFIED_SETTLE_MS + 1, [item("d1")]],
  ["read stale (predates), draft GONE", failedAt - 1, []],
];
for (const [label, updatedAt, items] of cases) {
  log(label.padEnd(46), "->", JSON.stringify(dueUnverifiedResolutions(items, updatedAt, flags)));
}
log("data undefined ->", JSON.stringify(dueUnverifiedResolutions(undefined, failedAt + 999999, flags)));
log("empty items, fresh read ->", JSON.stringify(dueUnverifiedResolutions([], failedAt + 1, flags)));
