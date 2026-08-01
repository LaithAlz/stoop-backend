/**
 * docs/03-engineering/api-contracts.md "Notifications / emergencies"
 * section, `POST /v1/notifications/{id}/ack`. This is the shipped path for
 * the mobile emergency banner's acknowledge action (v1.15 amendment —
 * `GET /v1/notifications?...` for a list read was never implemented, so
 * `notification_id` on the queue card plus this ack call is the real
 * mechanism): wired from src/features/emergency/useAcknowledge.ts against
 * `QueueItem.notification_id` (src/api/types.ts). Idempotent 200
 * `{ acknowledged_at }` server-side.
 */
import { apiRequest } from "./client";
import type { AckNotificationResponse } from "./types";

export function ackNotification(id: string): Promise<AckNotificationResponse> {
  return apiRequest<AckNotificationResponse>(`/v1/notifications/${id}/ack`, { method: "POST" });
}
