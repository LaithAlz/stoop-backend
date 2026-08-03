/**
 * The approve/undo/skip/edit-and-send mutations for Home
 * (src/routes/app.index.tsx). Ported near-verbatim from
 * apps/mobile/src/features/queue/useDraftActions.ts (campaign issue #234
 * PR 2) — same undo-countdown/draft_stale/already_sent handling, only the
 * import paths changed. Web has one caller today (mobile shares this
 * between Home and case-detail); a later PR wiring the conversation
 * thread's own approve/undo/reject can reuse this unchanged.
 *
 * Network calls go through src/api/drafts.ts; the local "sending"/"sent"/
 * "skipped" overlay is src/features/queue/queueEntries.ts's pure reducer.
 * This hook is the React glue between the two — not pure itself (it owns
 * mutations/timers), so it's exercised by hand (see the PR report's manual
 * QA list) rather than unit-tested; the reducer/helpers it wraps stay pure
 * and unit-testable once a test runner exists.
 */
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { approveDraft, editAndSendDraft, rejectDraft, undoDraftApprove } from "@/api/drafts";
import { ApiError, toHouseApiError } from "@/api/errors";
import { firstName } from "@/lib/tenantName";
import {
  computeUndoExpiresAt,
  draftStaleNotice,
  queueEntriesReducer,
  secondsRemaining,
} from "./queueEntries";

export interface DraftContext {
  draftId: string;
  caseId: string;
  tenantName: string;
}

interface UseDraftActionsOptions {
  /** House-voice message surfaced for a failure the landlord should see
   *  (network errors, `already_sent`, `draft_not_undoable`, ...) — the
   *  screen decides how to present it (a toast today, src/routes/
   *  app.index.tsx). */
  onNotice: (message: string) => void;
  /** Called after any server-confirmed state change that the screen's own
   *  query doesn't already own refetching for (draft_stale, skip, an
   *  already-resolved undo/approve) — Home refetches `useQueue` so the
   *  screen stays honest about what the server actually thinks happened. */
  onSettled: () => void;
}

export function useDraftActions({ onNotice, onSettled }: UseDraftActionsOptions) {
  const [entries, dispatch] = useReducer(queueEntriesReducer, {});
  const [staleNotices, setStaleNotices] = useState<Record<string, string>>({});
  const [editingContext, setEditingContext] = useState<(DraftContext & { body: string }) | null>(
    null,
  );
  const [tick, forceTick] = useReducer((count: number) => count + 1, 0);

  // A2 (safety review, #234 PR 2): which draft ids currently have a
  // mutation in flight — approve, undo, skip, or edit-and-send. A single
  // `useMutation()` instance's own `isPending`/`variables` only reflect
  // the MOST RECENT call (react-query replaces the tracked mutation per
  // `.mutate()`), which is wrong here since one hook instance serves every
  // card on the queue at once — this is a plain per-draft-id set instead,
  // set synchronously before each `.mutate()` call and cleared in that
  // specific call's own `onSettled` (react-query calls per-invocation
  // callbacks for every concurrent call, unlike the observer's aggregate
  // state). Exposed as `isBusy` so the screen can disable a card's
  // Edit/Skip/Approve/Undo controls for exactly the draft with something
  // in flight — the concrete case this guards is an approve tap racing a
  // skip tap on the same card.
  const [busyDraftIds, setBusyDraftIds] = useState<ReadonlySet<string>>(() => new Set());

  const markBusy = useCallback((draftId: string) => {
    setBusyDraftIds((prev) => {
      if (prev.has(draftId)) return prev;
      const next = new Set(prev);
      next.add(draftId);
      return next;
    });
  }, []);

  const clearBusy = useCallback((draftId: string) => {
    setBusyDraftIds((prev) => {
      if (!prev.has(draftId)) return prev;
      const next = new Set(prev);
      next.delete(draftId);
      return next;
    });
  }, []);

  // A8 (safety review, #234 PR 2): the stale-draft notice used to fire a
  // bare `setTimeout` with no stored id — never cleared on unmount (a
  // pending timer would still try to update state after the screen was
  // gone) and never cleared on a REPEAT call for the same case (a second
  // `draft_stale` within the 6s window would let the first timer delete
  // the second notice early). Timers are tracked per `caseId` here so a
  // repeat call resets its own timer instead of racing it, and the
  // cleanup effect below clears every outstanding one on unmount.
  const staleNoticeTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  useEffect(() => {
    const timers = staleNoticeTimers.current;
    return () => {
      for (const timerId of Object.values(timers)) clearTimeout(timerId);
    };
  }, []);

  const showStaleNotice = useCallback((caseId: string, tenantName: string) => {
    setStaleNotices((prev) => ({ ...prev, [caseId]: draftStaleNotice(firstName(tenantName)) }));
    const existing = staleNoticeTimers.current[caseId];
    if (existing) clearTimeout(existing);
    staleNoticeTimers.current[caseId] = setTimeout(() => {
      delete staleNoticeTimers.current[caseId];
      setStaleNotices((prev) => {
        if (!(caseId in prev)) return prev;
        const next = { ...prev };
        delete next[caseId];
        return next;
      });
    }, 6000);
  }, []);

  // A1 (safety review, #234 PR 2): this only ever runs from a genuine
  // mutationFn rejection now — every `onSuccess` below catches its own
  // bookkeeping instead of letting it throw back into react-query's
  // pipeline, where a throwing `onSuccess` is otherwise indistinguishable
  // from the request itself failing (react-query's `execute()` wraps
  // mutationFn + onSuccess + onSettled in ONE try/catch and calls
  // `onError` for a throw from ANY of them). So "clear the optimistic
  // state" here is now only ever a response to the server actually
  // rejecting the call, never to our own post-success code slipping.
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
    onSuccess: (result, ctx) => {
      try {
        const approvedAtClient = Date.now();
        dispatch({
          type: "approved",
          draftId: ctx.draftId,
          undoExpiresAtClient: computeUndoExpiresAt(
            result.data.undo_until,
            result.dateHeader,
            approvedAtClient,
          ),
          approvedAtClient,
        });
      } catch {
        // A1: the approve REQUEST already succeeded server-side — a throw
        // in this block is our own bookkeeping breaking, not a rejected
        // approve. Never dispatch "cleared" here (that would wrongly
        // revert the card to an approvable-looking state on a reply
        // that's already sending); refetch instead so the next server
        // read is the honest source of truth for this card.
        onNotice("That went through, but the on-screen countdown didn't update.");
        onSettled();
      }
    },
    onError: handleError,
    onSettled: (_data, _error, ctx) => clearBusy(ctx.draftId),
  });

  const undoMutation = useMutation({
    mutationFn: (ctx: DraftContext) => undoDraftApprove(ctx.draftId),
    onSuccess: (_data, ctx) => dispatch({ type: "undone", draftId: ctx.draftId }),
    onError: (error, ctx) => {
      // M1 senior advisory (mobile, ported here): a 409 `already_sent` on
      // undo means the reply genuinely went out — the honest card state is
      // "sent", not a flash of the idle decision card (which would invite
      // a second approve tap on a reply that already left) while the
      // refetch catches up. `expired` only fires from "sending", which is
      // the only state Undo is ever offered from.
      if (error instanceof ApiError && error.code === "already_sent") {
        dispatch({ type: "expired", draftId: ctx.draftId });
        onNotice(toHouseApiError(error));
        onSettled();
        return;
      }
      handleError(error, ctx);
    },
    onSettled: (_data, _error, ctx) => clearBusy(ctx.draftId),
  });

  const skipMutation = useMutation({
    mutationFn: (ctx: DraftContext) => rejectDraft(ctx.draftId),
    // A6 (safety review, #234 PR 2): the previous cut never invalidated
    // the queue/counts after a successful skip — the header's counts
    // strip (and anything else reading `useQueue`) only caught up on the
    // next 20s poll or window-focus refetch. Approve/undo/edit don't need
    // this (their local "sending"/"sent" overlay IS the honest UI state
    // for those transitions, per queueEntries.ts's own docstring); skip
    // has no such overlay for the queue's own counts, so it refetches
    // immediately on success — same A1 split as every mutation above,
    // wrapped so a bookkeeping throw here can't masquerade as the reject
    // itself failing.
    onSuccess: () => {
      try {
        onSettled();
      } catch {
        onNotice("That went through, but the queue didn't refresh.");
      }
    },
    onError: handleError,
    onSettled: (_data, _error, ctx) => clearBusy(ctx.draftId),
  });

  const editMutation = useMutation({
    mutationFn: (ctx: DraftContext & { body: string }) => editAndSendDraft(ctx.draftId, ctx.body),
    onSuccess: (result, ctx) => {
      try {
        const approvedAtClient = Date.now();
        dispatch({
          type: "approved",
          draftId: ctx.draftId,
          undoExpiresAtClient: computeUndoExpiresAt(
            result.data.undo_until,
            result.dateHeader,
            approvedAtClient,
          ),
          approvedAtClient,
        });
        setEditingContext(null);
      } catch {
        // A1, same reasoning as approve's onSuccess above — the
        // edit-and-send request already succeeded.
        onNotice("That went through, but the on-screen countdown didn't update.");
        onSettled();
        setEditingContext(null);
      }
    },
    onError: (error, ctx) => {
      // A7 (safety review, #234 PR 2): this used to unconditionally close
      // the editor here (`setEditingContext(null)`) on ANY failure — from
      // a dropped connection to `draft_stale` — throwing away whatever
      // the landlord had just typed. Leave it open with the text intact;
      // the landlord decides whether to retry Send or tap Cancel
      // themselves (Cancel already closes it deliberately, as a
      // landlord-initiated action).
      handleError(error, ctx);
    },
    onSettled: (_data, _error, ctx) => clearBusy(ctx.draftId),
  });

  // Ticks any live undo countdown once a second; the effect below then
  // flips a countdown that's genuinely hit zero from "sending" to "sent".
  useEffect(() => {
    const hasSending = Object.values(entries).some((entry) => entry.status === "sending");
    if (!hasSending) return;
    const timer = setInterval(forceTick, 1000);
    return () => clearInterval(timer);
  }, [entries]);

  // `tick` is a real dependency — the reducer above increments it once a
  // second while anything is "sending", and wall-clock time (what
  // `secondsRemaining` reads) only moves the outcome between ticks.
  useEffect(() => {
    for (const [draftId, entry] of Object.entries(entries)) {
      if (entry.status === "sending" && secondsRemaining(entry.undoExpiresAtClient) <= 0) {
        dispatch({ type: "expired", draftId });
      }
    }
  }, [entries, tick]);

  return {
    entries,
    dispatch,
    staleNotices,
    editingContext,
    /** A2: true while an approve/undo/skip/edit-and-send is in flight for
     *  THIS draft id specifically — never a global "something is
     *  happening" flag. */
    isBusy: (draftId: string) => busyDraftIds.has(draftId),
    approve: (ctx: DraftContext) => {
      markBusy(ctx.draftId);
      approveMutation.mutate(ctx);
    },
    undo: (ctx: DraftContext) => {
      markBusy(ctx.draftId);
      undoMutation.mutate(ctx);
    },
    skip: (ctx: DraftContext) => {
      markBusy(ctx.draftId);
      dispatch({ type: "skipped", draftId: ctx.draftId });
      skipMutation.mutate(ctx);
    },
    openEditor: (ctx: DraftContext, body: string) => setEditingContext({ ...ctx, body }),
    cancelEditor: () => setEditingContext(null),
    submitEdit: (body: string) => {
      if (!editingContext) return;
      markBusy(editingContext.draftId);
      editMutation.mutate({ ...editingContext, body });
    },
    isEditSubmitting: editMutation.isPending,
  };
}
