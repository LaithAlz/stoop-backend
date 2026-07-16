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
 * Pure — no React/RN import — so it's unit-testable like
 * src/features/queue/queueEntries.ts.
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

  entries.forEach((entry, index) => {
    const dayKey = new Date(entry.at).toDateString();
    if (dayKey !== lastDayKey) {
      rows.push({
        kind: "day-divider",
        key: `day-${dayKey}`,
        label: formatDayLabel(entry.at, now),
      });
      lastDayKey = dayKey;
    }

    if (entry.kind === "message") {
      rows.push({ kind: "message", key: `message-${index}`, entry });
      return;
    }

    if (entry.kind === "audit") {
      const label = auditActionLabel(entry.action);
      if (label) rows.push({ kind: "audit", key: `audit-${index}`, entry, label });
      return;
    }

    if (LIVE_DRAFT_STATUSES.includes(entry.status)) {
      rows.push({ kind: "draft", key: `draft-${entry.id}`, entry });
    }
  });

  return rows;
}
