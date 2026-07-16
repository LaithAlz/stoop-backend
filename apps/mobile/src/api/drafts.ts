/**
 * The approve loop — docs/03-engineering/api-contracts.md "Drafts (the
 * approve loop)" section. Plain typed functions (not hooks): the Home
 * screen's approve/undo/skip state machine (src/features/queue/
 * queueEntries.ts) wraps these in its own `useMutation` calls so the
 * optimistic local state and the network call are decided in one place.
 */
import { apiRequest } from "./client";
import type { ApproveDraftResponse, RejectDraftResponse, UndoDraftResponse } from "./types";

export function approveDraft(draftId: string): Promise<ApproveDraftResponse> {
  return apiRequest<ApproveDraftResponse>(`/v1/drafts/${draftId}/approve`, { method: "POST" });
}

/** DELETE .../approve — cancels within the undo window (api-contracts.md:
 *  "the undo window is data"; this call, not a client timer, is what a tap
 *  on Undo actually does). */
export function undoDraftApprove(draftId: string): Promise<UndoDraftResponse> {
  return apiRequest<UndoDraftResponse>(`/v1/drafts/${draftId}/approve`, { method: "DELETE" });
}

export function rejectDraft(draftId: string, note?: string): Promise<RejectDraftResponse> {
  return apiRequest<RejectDraftResponse>(`/v1/drafts/${draftId}/reject`, {
    method: "POST",
    body: note ? { note } : {},
  });
}

export function editAndSendDraft(draftId: string, body: string): Promise<ApproveDraftResponse> {
  return apiRequest<ApproveDraftResponse>(`/v1/drafts/${draftId}/edit-and-send`, {
    method: "POST",
    body: { body },
  });
}
