/**
 * docs/03-engineering/api-contracts.md "Notifications / emergencies"
 * section. Typed for contract completeness (issue #210 M1 scope item 1:
 * "types mirroring the contract shapes... do NOT invent fields"), but NOT
 * currently called from the emergency banner — `GET /v1/queue`'s item
 * shape carries no notification id to ack against (see src/api/types.ts's
 * `QueueItem` comment and the mobile M1 report's "emergency banner
 * notification-id finding"). Wiring this in is a real api-contracts.md
 * follow-up, not something this file fakes a correlation for.
 */
import { apiRequest } from "./client";
import type { AckNotificationResponse } from "./types";

export function ackNotification(id: string): Promise<AckNotificationResponse> {
  return apiRequest<AckNotificationResponse>(`/v1/notifications/${id}/ack`, { method: "POST" });
}
