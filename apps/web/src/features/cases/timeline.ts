/**
 * Turns `GET /v1/cases/{id}`'s flat, oldest-first `timeline` array
 * (docs/03-engineering/api-contracts.md "Cases" section) into the rows the
 * case-detail screen actually renders: day dividers inserted between
 * entries that cross a calendar day, audit entries mapped to a
 * plain-English label (or dropped — see auditLabels.ts), and draft entries
 * kept only while they're still "live" (pending/stale) — an
 * approved/sent draft is represented by the real outbound `message` entry
 * instead, and a rejected/cancelled one by its own audit line, so keeping
 * the draft row too would show the same event twice.
 *
 * Ported near-verbatim from apps/mobile/src/features/cases/timeline.ts
 * (campaign issue #234 PR 3). Pure — no React import — so it's
 * unit-testable exactly like src/features/queue/queueEntries.ts once a web
 * test runner exists.
 */
import type {
  TimelineAuditEntry,
  TimelineDraftEntry,
  TimelineEntry,
  TimelineMessageEntry,
} from "@/api/types";
import { formatDayLabel } from "@/lib/relativeTime";
import { auditActionLabel } from "./auditLabels";

export type TimelineRow =
  | { kind: "day-divider"; key: string; label: string }
  | { kind: "message"; key: string; entry: TimelineMessageEntry }
  | { kind: "audit"; key: string; entry: TimelineAuditEntry; label: string }
  | { kind: "draft"; key: string; entry: TimelineDraftEntry };

const LIVE_DRAFT_STATUSES: TimelineDraftEntry["status"][] = ["pending", "stale"];

export function buildTimelineRows(entries: TimelineEntry[], now: Date = new Date()): TimelineRow[] {
  const rows: TimelineRow[] = [];
  let lastDayKey: string | null = null;

  // Safety review (#234 PR 3 fix round, LOW): a day-divider must only be
  // inserted right before the first entry of a new day that ACTUALLY
  // renders a row — inserting it unconditionally (the old order) could
  // leave a bare date stamp with nothing under it when every entry from
  // that day turns out to be a suppressed audit action (auditLabels.ts)
  // or a non-live draft. Called only once a caller already knows its
  // entry will push a row.
  const pushDayDividerIfNeeded = (at: string) => {
    const dayKey = new Date(at).toDateString();
    if (dayKey !== lastDayKey) {
      rows.push({ kind: "day-divider", key: `day-${dayKey}`, label: formatDayLabel(at, now) });
      lastDayKey = dayKey;
    }
  };

  entries.forEach((entry, index) => {
    if (entry.kind === "message") {
      pushDayDividerIfNeeded(entry.at);
      rows.push({ kind: "message", key: `message-${index}`, entry });
      return;
    }

    if (entry.kind === "audit") {
      const label = auditActionLabel(entry.action);
      if (!label) return;
      pushDayDividerIfNeeded(entry.at);
      rows.push({ kind: "audit", key: `audit-${index}`, entry, label });
      return;
    }

    if (LIVE_DRAFT_STATUSES.includes(entry.status)) {
      pushDayDividerIfNeeded(entry.at);
      rows.push({ kind: "draft", key: `draft-${entry.id}`, entry });
    }
  });

  return rows;
}
