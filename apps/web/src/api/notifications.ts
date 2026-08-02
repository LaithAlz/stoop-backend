/**
 * docs/03-engineering/api-contracts.md "Notifications / emergencies"
 * section, `POST /v1/notifications/{id}/ack`. Ported near-verbatim from
 * apps/mobile/src/api/notifications.ts (campaign issue #234 PR 2) — the
 * shipped path for the emergency banner's acknowledge action (Queue
 * section's v1.15 amendment: `GET /v1/notifications?...` for a list read
 * was never implemented, so `QueueItem.notification_id` plus this ack
 * call is the real mechanism). Wired from
 * src/features/emergency/useAcknowledge.ts. Idempotent 200
 * `{ acknowledged_at }` server-side.
 */
import { apiRequest } from "./client";
import type { AckNotificationResponse } from "./types";

export function ackNotification(id: string): Promise<AckNotificationResponse> {
  return apiRequest<AckNotificationResponse>(`/v1/notifications/${id}/ack`, { method: "POST" });
}
