import {
  buildQueueView,
  pruneQueueSnapshots,
  queueEntriesReducer,
  type QueueEntriesState,
} from "../src/features/queue/queueEntries";
import {
  dueUnverifiedResolutions,
  UNVERIFIED_SETTLE_MS,
} from "../src/features/queue/useResolveUnverifiedSends";

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

// ---------------------------------------------------------------------
// ROUND 5: the stamp must survive the countdown winning the race.
//
// An ambiguous undo failure is BY CONSTRUCTION the slow kind, so it
// normally surfaces AFTER the independent 1s countdown has already
// flipped the entry to "sent". Round 4 gated `undoAmbiguous` on
// "sending" alone, so that dominant ordering threw the evidence away and
// the card settled on a permanent controls-less "Sent." with the draft
// still pending on the server.
// ---------------------------------------------------------------------
console.log("\n--- ROUND 5: undoAmbiguous after the countdown already flipped to sent ---");
{
  let st: QueueEntriesState = {};
  st = queueEntriesReducer(st, { type: "approved", draftId: "d1", approvedAtClient: 1_000, undoExpiresAtClient: 6_000 } as never);
  st = queueEntriesReducer(st, { type: "expired", draftId: "d1" } as never);
  console.log("after countdown, status:", (st.d1 as { status: string }).status);
  st = queueEntriesReducer(st, { type: "undoAmbiguous", draftId: "d1", at: 7_500, approvedAtClient: 1_000 } as never);
  const e = st.d1 as { status: string; undoAmbiguousAt?: number };
  console.log("status:", e.status, "| undoAmbiguousAt:", e.undoAmbiguousAt);
  console.log(
    e.undoAmbiguousAt === 7_500
      ? "PASS: the late ambiguous failure is still recorded, so the card can recover"
      : "FAIL: evidence dropped, this is the permanent stuck-Sent card",
  );
}

console.log("\n--- ROUND 5: the arms that must NOT stamp still do not ---");
{
  let st: QueueEntriesState = {};
  st = queueEntriesReducer(st, { type: "approved", draftId: "d2", approvedAtClient: 1_000, undoExpiresAtClient: 6_000 } as never);
  st = queueEntriesReducer(st, { type: "expired", draftId: "d2" } as never);
  const plain = st.d2 as { undoAmbiguousAt?: number };
  console.log("plain approve then expire  -> undoAmbiguousAt:", plain.undoAmbiguousAt);

  let cleared: QueueEntriesState = queueEntriesReducer(st, { type: "cleared", draftId: "d2" } as never);
  cleared = queueEntriesReducer(cleared, { type: "undoAmbiguous", draftId: "d2", at: 9_000, approvedAtClient: 1_000 } as never);
  console.log("dispatch against a CLEARED entry -> entry:", cleared.d2 ?? "(absent, correctly dropped)");

  // F4 (round 6): the two checks above do NOT exercise the status gate.
  // One covers "no dispatch was made" and the other "the entry is
  // absent", neither of which the gate controls. Mutation-proved:
  // replacing the gate with `if (!current) return state;` left both
  // printing byte-identical output while a skipped entry happily
  // accepted the stamp. A skipped arm is what makes the gate
  // load-bearing.
  let skipped: QueueEntriesState = queueEntriesReducer({}, { type: "skipped", draftId: "d3" } as never);
  skipped = queueEntriesReducer(skipped, { type: "undoAmbiguous", draftId: "d3", at: 42, approvedAtClient: 1_000 } as never);
  console.log("dispatch against a SKIPPED entry -> entry:", JSON.stringify(skipped.d3));
  console.log(
    JSON.stringify(skipped.d3) === '{"status":"skipped"}'
      ? "PASS: a skipped entry never takes the stamp"
      : "FAIL: a skipped entry accepted the stamp",
  );
  // Honest about what this does and does not prove. It does NOT isolate
  // the status gate: mutating that gate to `if (!current) return state;`
  // leaves this passing, because F2's cycle check catches a skipped entry
  // anyway (it carries no `approvedAtClient`, so `undefined !== 1_000`).
  // After F2 the two guards overlap for every entry with no cycle stamp.
  // The status gate still earns its place for readability and for the
  // `sending`/`sent` allowlist itself, which the ROUND 5 scenario above
  // does isolate. Saying so beats a label claiming a mutation proof this
  // does not have.

  // F2 (round 6): the stamp is scoped to its own approve cycle.
  let cyc: QueueEntriesState = queueEntriesReducer({}, { type: "approved", draftId: "d4", approvedAtClient: 20_000, undoExpiresAtClient: 25_000 } as never);
  cyc = queueEntriesReducer(cyc, { type: "expired", draftId: "d4" } as never);
  cyc = queueEntriesReducer(cyc, { type: "undoAmbiguous", draftId: "d4", at: 65_000, approvedAtClient: 1_000 } as never);
  const stale = (cyc.d4 as { undoAmbiguousAt?: number }).undoAmbiguousAt;
  console.log("cycle-1's late failure against cycle 2 -> undoAmbiguousAt:", stale);
  console.log(stale === undefined ? "PASS: cross-cycle stamp rejected" : "FAIL: cycle 1 stamped cycle 2");
}
