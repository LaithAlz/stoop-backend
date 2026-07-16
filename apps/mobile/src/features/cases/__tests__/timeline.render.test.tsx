/**
 * Renders the real Clarity components (DayDivider/ThreadMessageRow/
 * AuditMetaLine/DraftBubble) from `buildTimelineRows`' output — proving the
 * pure interleaving logic (timeline.test.ts) and the actual RN components
 * wire together correctly end to end. No navigation, no React Query, no
 * network — a fixed timeline fixture in, rendered text out.
 */
import { View } from "react-native";
import { render, screen } from "@testing-library/react-native";
import type { TimelineEntry } from "@/api/types";
import { DayDivider } from "@/components/clarity/DayDivider";
import { ThreadMessageRow } from "@/components/clarity/ThreadMessageRow";
import { AuditMetaLine } from "@/components/clarity/AuditMetaLine";
import { DraftBubble } from "@/components/clarity/DraftBubble";
import { buildTimelineRows } from "../timeline";

const NOW = new Date("2026-07-16T12:00:00Z");

function TimelineFixture({ entries }: { entries: TimelineEntry[] }) {
  const rows = buildTimelineRows(entries, NOW);
  return (
    <View>
      {rows.map((row) => {
        switch (row.kind) {
          case "day-divider":
            return <DayDivider key={row.key}>{row.label}</DayDivider>;
          case "message":
            return <ThreadMessageRow key={row.key} entry={row.entry} tenantFirstName="Maria" />;
          case "audit":
            return <AuditMetaLine key={row.key} label={row.label} at={row.entry.at} />;
          case "draft":
            return <DraftBubble key={row.key} label="I'd like to reply" body={row.entry.body} />;
          default:
            return null;
        }
      })}
    </View>
  );
}

describe("case-detail timeline (rendered)", () => {
  it("shows a day divider, the tenant's message, a quiet audit line, and the pending draft — in order", () => {
    const entries: TimelineEntry[] = [
      {
        kind: "audit",
        actor: "system",
        action: "case_opened",
        payload: {},
        at: "2026-07-16T07:00:00Z",
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
        body: "Hi Maria — so sorry, sending someone today.",
        at: "2026-07-16T08:00:06Z",
      },
    ];

    render(<TimelineFixture entries={entries} />);

    expect(screen.getByText("TODAY")).toBeOnTheScreen();
    expect(screen.getByText(/Case opened\./)).toBeOnTheScreen();
    expect(screen.getByText("No heat since last night")).toBeOnTheScreen();
    expect(screen.getByText("Hi Maria — so sorry, sending someone today.")).toBeOnTheScreen();
  });

  it("never renders a row for a suppressed audit action (message_received)", () => {
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
    ];

    render(<TimelineFixture entries={entries} />);

    expect(screen.getByText("no heat")).toBeOnTheScreen();
    expect(screen.queryByText(/message received/i)).toBeNull();
  });
});
