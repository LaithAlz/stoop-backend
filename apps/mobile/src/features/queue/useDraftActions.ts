/**
 * The approve/undo/skip/edit-and-send mutations, shared by Home
 * (src/app/(tabs)/index.tsx) and the case-detail screen
 * (src/app/(tabs)/conversations/[id].tsx) — both surfaces can act on a
 * pending draft, and both need the exact same undo-countdown/draft_stale/
 * already_sent handling, so it lives here once instead of twice.
 *
 * Network calls go through src/api/drafts.ts; the local "sending"/"sent"/
 * "skipped" overlay is src/features/queue/queueEntries.ts's pure reducer.
 * This hook is the React glue between the two — RN-agnostic itself (no
 * react-native import), but not pure (it owns mutations/timers), so it's
 * exercised via the render-level tests on the two screens rather than
 * unit-tested directly; the reducer/helpers it wraps ARE unit-tested
 * (src/features/queue/__tests__/queueEntries.test.ts).
 */
import { useCallback, useEffect, useReducer, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { approveDraft, editAndSendDraft, rejectDraft, undoDraftApprove } from "@/api/drafts";
import { ApiError, toHouseApiError } from "@/api/errors";
import { firstName } from "@/lib/tenantName";
import { draftStaleNotice, queueEntriesReducer, secondsRemaining } from "./queueEntries";

export interface DraftContext {
  draftId: string;
  caseId: string;
  tenantName: string;
}

interface UseDraftActionsOptions {
  /** House-voice message surfaced for a failure the landlord should see
   *  (network errors, `already_sent`, `draft_not_undoable`, ...) — the
   *  screen decides how to present it (`Alert.alert` today). */
  onNotice: (message: string) => void;
  /** Called after any server-confirmed state change that the screen's own
   *  query doesn't already own refetching for (draft_stale, skip, an
   *  already-resolved undo/approve) — Home refetches `useQueue`,
   *  case-detail refetches `useCase` + invalidates the queue so the two
   *  surfaces stay honest about each other. */
  onSettled: () => void;
}

export function useDraftActions({ onNotice, onSettled }: UseDraftActionsOptions) {
  const [entries, dispatch] = useReducer(queueEntriesReducer, {});
  const [staleNotices, setStaleNotices] = useState<Record<string, string>>({});
  const [editingContext, setEditingContext] = useState<(DraftContext & { body: string }) | null>(
    null,
  );
  const [, forceTick] = useReducer((count: number) => count + 1, 0);

  const showStaleNotice = useCallback((caseId: string, tenantName: string) => {
    setStaleNotices((prev) => ({ ...prev, [caseId]: draftStaleNotice(firstName(tenantName)) }));
    setTimeout(() => {
      setStaleNotices((prev) => {
        if (!(caseId in prev)) return prev;
        const next = { ...prev };
        delete next[caseId];
        return next;
      });
    }, 6000);
  }, []);

  const handleError = useCallback(
    (error: unknown, ctx: DraftContext) => {
      dispatch({ type: "cleared", draftId: ctx.draftId });
      if (error instanceof ApiError && error.code === "draft_stale") {
        showStaleNotice(ctx.caseId, ctx.tenantName);
        onSettled();
        return;
      }
      onNotice(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      );
      if (
        error instanceof ApiError &&
        (error.code === "already_sent" || error.code === "draft_not_undoable")
      ) {
        onSettled();
      }
    },
    [onNotice, onSettled, showStaleNotice],
  );

  const approveMutation = useMutation({
    mutationFn: (ctx: DraftContext) => approveDraft(ctx.draftId),
    onSuccess: (data, ctx) =>
      dispatch({
        type: "approved",
        draftId: ctx.draftId,
        undoUntil: data.undo_until,
        approvedAt: new Date().toISOString(),
      }),
    onError: handleError,
  });

  const undoMutation = useMutation({
    mutationFn: (ctx: DraftContext) => undoDraftApprove(ctx.draftId),
    onSuccess: (_data, ctx) => dispatch({ type: "undone", draftId: ctx.draftId }),
    onError: handleError,
  });

  const skipMutation = useMutation({
    mutationFn: (ctx: DraftContext) => rejectDraft(ctx.draftId),
    onError: handleError,
  });

  const editMutation = useMutation({
    mutationFn: (ctx: DraftContext & { body: string }) => editAndSendDraft(ctx.draftId, ctx.body),
    onSuccess: (data, ctx) => {
      dispatch({
        type: "approved",
        draftId: ctx.draftId,
        undoUntil: data.undo_until,
        approvedAt: new Date().toISOString(),
      });
      setEditingContext(null);
    },
    onError: (error, ctx) => {
      handleError(error, ctx);
      setEditingContext(null);
    },
  });

  // Ticks any live undo countdown once a second; the effect below then
  // flips a countdown that's genuinely hit zero from "sending" to "sent".
  useEffect(() => {
    const hasSending = Object.values(entries).some((entry) => entry.status === "sending");
    if (!hasSending) return;
    const timer = setInterval(forceTick, 1000);
    return () => clearInterval(timer);
  }, [entries]);

  useEffect(() => {
    for (const [draftId, entry] of Object.entries(entries)) {
      if (entry.status === "sending" && secondsRemaining(entry.undoUntil) <= 0) {
        dispatch({ type: "expired", draftId });
      }
    }
  });

  return {
    entries,
    dispatch,
    staleNotices,
    editingContext,
    approve: (ctx: DraftContext) => approveMutation.mutate(ctx),
    undo: (ctx: DraftContext) => undoMutation.mutate(ctx),
    skip: (ctx: DraftContext) => {
      dispatch({ type: "skipped", draftId: ctx.draftId });
      skipMutation.mutate(ctx);
    },
    openEditor: (ctx: DraftContext, body: string) => setEditingContext({ ...ctx, body }),
    cancelEditor: () => setEditingContext(null),
    submitEdit: (body: string) => {
      if (!editingContext) return;
      editMutation.mutate({ ...editingContext, body });
    },
    isEditSubmitting: editMutation.isPending,
  };
}
