/**
 * Pure logic tests for the case-detail timeline builder — day-divider
 * insertion and the interleaving/suppression rules (see timeline.ts and
 * auditLabels.ts's docstrings for the reasoning). No React/RN involved;
 * src/features/cases/__tests__/timeline.render.test.tsx covers the actual
 * component rendering.
 */
import type { TimelineEntry } from "@/api/types";
import { buildTimelineRows } from "../timeline";

const NOW = new Date("2026-07-16T12:00:00Z");

describe("buildTimelineRows", () => {
  it("interleaves messages, audit lines, and the live draft in order, with a day divider per calendar day", () => {
    const entries: TimelineEntry[] = [
      {
        kind: "audit",
        actor: "system",
        action: "case_opened",
        payload: {},
        at: "2026-07-15T09:00:00Z",
      },
      {
        kind: "message",
        direction: "inbound",
        party: "tenant",
        body: "No heat since last night",
        media: [],
        at: "2026-07-16T08:00:00Z",
      },
      {
        kind: "draft",
        id: "draft-1",
        status: "pending",
        body: "Hi Maria — sending someone today.",
        at: "2026-07-16T08:00:06Z",
      },
    ];

    const rows = buildTimelineRows(entries, NOW);

    expect(rows.map((row) => row.kind)).toEqual([
      "day-divider",
      "audit",
      "day-divider",
      "message",
      "draft",
    ]);
    expect(rows[0]).toMatchObject({ kind: "day-divider", label: "YESTERDAY" });
    expect(rows[2]).toMatchObject({ kind: "day-divider", label: "TODAY" });
  });

  it("suppresses message_received and drafted (redundant with the adjacent message/draft bubble)", () => {
    const entries: TimelineEntry[] = [
      {
        kind: "audit",
        actor: "prefilter",
        action: "message_received",
        payload: {},
        at: "2026-07-16T08:00:00Z",
      },
      {
        kind: "message",
        direction: "inbound",
        party: "tenant",
        body: "no heat",
        media: [],
        at: "2026-07-16T08:00:00Z",
      },
      { kind: "audit", actor: "agent", action: "drafted", payload: {}, at: "2026-07-16T08:00:05Z" },
      {
        kind: "draft",
        id: "draft-1",
        status: "pending",
        body: "reply",
        at: "2026-07-16T08:00:05Z",
      },
    ];

    const rows = buildTimelineRows(entries, NOW);

    expect(rows.map((row) => row.kind)).toEqual(["day-divider", "message", "draft"]);
  });

  it("drops a resolved (approved/sent) draft row — the real outbound message represents it instead", () => {
    const entries: TimelineEntry[] = [
      {
        kind: "draft",
        id: "draft-1",
        status: "sent",
        body: "already sent",
        at: "2026-07-16T08:00:05Z",
      },
      {
        kind: "message",
        direction: "outbound",
        party: "tenant",
        body: "already sent",
        media: [],
        at: "2026-07-16T08:00:06Z",
      },
    ];

    const rows = buildTimelineRows(entries, NOW);

    expect(rows.map((row) => row.kind)).toEqual(["day-divider", "message"]);
  });

  it("keeps a stale draft row (still worth a look, per the stale-draft rule) but drops rejected/cancelled", () => {
    const entries: TimelineEntry[] = [
      {
        kind: "draft",
        id: "draft-1",
        status: "stale",
        body: "superseded",
        at: "2026-07-16T08:00:00Z",
      },
      {
        kind: "draft",
        id: "draft-2",
        status: "rejected",
        body: "skipped",
        at: "2026-07-16T08:00:01Z",
      },
    ];

    const rows = buildTimelineRows(entries, NOW);

    expect(rows.map((row) => row.kind)).toEqual(["day-divider", "draft"]);
    expect(rows[1]).toMatchObject({ entry: { id: "draft-1" } });
  });
});
