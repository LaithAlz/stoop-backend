/**
 * The approve loop — docs/03-engineering/api-contracts.md "Drafts (the
 * approve loop)" section. Ported near-verbatim from
 * apps/mobile/src/api/drafts.ts (campaign issue #234 PR 2). Plain typed
 * functions, not hooks — src/features/queue/useDraftActions.ts wraps
 * these in its own `useMutation` calls so the local approve/undo/skip
 * overlay state and the network call are decided in one place.
 *
 * B2 (safety review, #234 PR 2): approve and edit-and-send both return an
 * `undo_until` that useDraftActions.ts needs to anchor to the SERVER's own
 * clock (never the client's), so both go through `apiRequestWithDate`
 * instead of the plain `apiRequest` — see src/api/client.ts's
 * `ApiResponseEnvelope` and useDraftActions.ts's `computeUndoExpiresAt`
 * call site. Undo and reject carry no such countdown and stay on the
 * plain client.
 */
import { apiRequest, apiRequestWithDate, type ApiResponseEnvelope } from "./client";
import type { ApproveDraftResponse, RejectDraftResponse, UndoDraftResponse } from "./types";

export function approveDraft(draftId: string): Promise<ApiResponseEnvelope<ApproveDraftResponse>> {
  return apiRequestWithDate<ApproveDraftResponse>(`/v1/drafts/${draftId}/approve`, {
    method: "POST",
  });
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

export function editAndSendDraft(
  draftId: string,
  body: string,
): Promise<ApiResponseEnvelope<ApproveDraftResponse>> {
  return apiRequestWithDate<ApproveDraftResponse>(`/v1/drafts/${draftId}/edit-and-send`, {
    method: "POST",
    body: { body },
  });
}
