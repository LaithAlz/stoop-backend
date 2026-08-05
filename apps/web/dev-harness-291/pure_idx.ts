import {
  buildQueueView, pruneQueueSnapshots, queueEntriesReducer,
  type QueueEntriesState,
} from "/Users/laith/Businesses/LandlordAI-queue291/apps/web/src/features/queue/queueEntries";

const it = (id: string) => ({
  case_id: "c-" + id, draft_id: id, severity: "routine", title: null,
  tenant_name: "Maria", property_label: "12 Oss", unit: "2",
  tenant_message: "leak " + id, received_at: "2026-01-01T00:00:00Z",
  has_media: false, media_note: null, draft_body: "b-" + id, why: "w",
  reasoning: [], refusal_flags: [], recipient: "tenant", notification_id: null,
}) as any;

const approved = (s: QueueEntriesState, id: string) =>
  queueEntriesReducer(s, { type: "approved", draftId: id, undoExpiresAtClient: Date.now() + 5000, approvedAtClient: Date.now() });
const skipped = (s: QueueEntriesState, id: string) => queueEntriesReducer(s, { type: "skipped", draftId: id });
const expired = (s: QueueEntriesState, id: string) => queueEntriesReducer(s, { type: "expired", draftId: id });

console.log("--- A: stale index far past the end of a shrunken queue ---");
let e = approved({}, "x");
console.log(buildQueueView([it("a"), it("b")], e, { x: { item: it("x"), index: 9 } }).map(r => r.item.draft_id));

console.log("--- B: two pins captured at the SAME index ---");
e = approved(approved({}, "x"), "y");
console.log(buildQueueView([it("a")], e, { x: { item: it("x"), index: 0 }, y: { item: it("y"), index: 0 } }).map(r => r.item.draft_id));

console.log("--- C: skipped pin at index 0 while the queue has since reordered ---");
e = skipped({}, "s");
console.log(buildQueueView([it("n1"), it("n2"), it("n3")], e, { s: { item: it("s"), index: 0 } }).map(r => r.item.draft_id));

console.log("--- D: negative / NaN index (nothing produces these today, but) ---");
e = approved({}, "x");
console.log("neg:", buildQueueView([it("a")], e, { x: { item: it("x"), index: -5 } }).map(r => r.item.draft_id));
console.log("NaN:", buildQueueView([it("a")], e, { x: { item: it("x"), index: NaN as any } }).map(r => r.item.draft_id));

console.log("--- E: can a pin resurrect a row after a GENUINE removal (cleared)? ---");
e = queueEntriesReducer(expired(approved({}, "x"), "x"), { type: "cleared", draftId: "x" });
console.log("rows:", buildQueueView([], e, { x: { item: it("x"), index: 0 } }).map(r => r.item.draft_id), "(expect [])");
console.log("snapshot pruned once cleared:", Object.keys(pruneQueueSnapshots({ x: { item: it("x"), index: 0 } }, e)));

console.log("--- F: a STUCK 'sent' entry keeps its snapshot (tenant text) forever ---");
e = expired(approved({}, "x"), "x");
const kept = pruneQueueSnapshots({ x: { item: it("x"), index: 0 } }, e);
console.log("prune keeps the sent snapshot:", Object.keys(kept), "-> tenant_message retained:", JSON.stringify((kept as any).x.item.tenant_message));

console.log("--- G: duplicate draft_id rows (React key collision) via pinnedEditingItem + a sent pin ---");
e = expired(approved({}, "x"), "x");
const rows = buildQueueView([], e, { x: { item: it("x"), index: 0 } }, it("x"));
console.log("draft ids:", rows.map(r => r.item.draft_id), "(two 'x' == duplicate React keys)");
